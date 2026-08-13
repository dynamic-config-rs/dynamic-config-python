//! HashiCorp Vault, as a compiled object a Python `RemoteSource` wraps.
//!
//! The counterpart to [`crate::etcd`], and deliberately the *other* kind of
//! store: `ureq` rather than gRPC, blocking rather than async, and a login
//! that produces a token with a lifetime rather than a connection that
//! carries a password. Nothing in this file touches the tokio runtime, which
//! is the point — a program using only Vault starts no runtime at all, and
//! `tests/test_runtime.py` asserts exactly that.
//!
//! ## Two layers of credential renewal, and they do not fight
//!
//! The Rust crate already renews: `Session` caches the Vault token, refreshes
//! it within a minute of expiry, and invalidates it when Vault answers 403.
//! That is the *token* rotating under a fixed login.
//!
//! What a Python callable adds is the layer above — the **login credential**
//! itself rotating. An AppRole secret id a sidecar rewrites every hour, a JWT
//! a workload-identity daemon refreshes: the Rust crate has no way to notice
//! those, because it was handed a `String` at construction.
//!
//! So the facade resolves the login credential on every fetch, and when it
//! has moved, the `Vault` is rebuilt. Rebuilding invalidates the session,
//! which is correct rather than wasteful: a token obtained with a credential
//! that has since been replaced is a token nobody should keep. When the
//! credential has *not* moved — the overwhelmingly common case — the same
//! `Vault` is reused and its session cache is untouched.

use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use dynamic_config::{Error, RemoteSource};
use dynamic_config_store_core::tls::TlsConfig;
use dynamic_config_store_core::LoneAuthority;
use dynamic_config_vault::{Auth, Vault};
use pyo3::prelude::*;

use crate::errors::raised;
use crate::redact::{safe, Redactor};
use crate::tls::Tls;

/// A login credential, as the facade resolved it for one call.
///
/// Compared, not hashed: the whole purpose is to notice that a token is not
/// the token it was last time.
#[derive(Clone, PartialEq, Eq)]
struct Resolved {
    kind: String,
    mount: String,
    first: String,
    second: String,
}

impl Resolved {
    /// The halves of this credential that must never appear in a message.
    ///
    /// Which half is secret is a property of the auth method, and the store
    /// crate's own `Debug` agrees with this list: an AppRole role id and a
    /// userpass user name stay printable, because redaction that hides the
    /// half worth seeing is redaction nobody can debug through.
    fn secrets(&self) -> Vec<&str> {
        match self.kind.as_str() {
            "token" | "jwt" => vec![self.first.as_str()],
            "app_role" | "userpass" | "ldap" => vec![self.second.as_str()],
            _ => Vec::new(),
        }
    }

    /// The engine's `Auth` for this credential.
    fn auth(&self) -> Result<Auth, Error> {
        let mount = self.mount.clone();
        let first = self.first.clone();
        let second = self.second.clone();

        Ok(match self.kind.as_str() {
            // `Auth::Token` has no mount, and `at_mount` is documented to
            // ignore it rather than to be an error.
            "token" => Auth::token(first),
            "app_role" => Auth::app_role(first, second).at_mount(mount),
            "kubernetes" => Auth::kubernetes(first)
                .at_mount(mount)
                .with_token_path(second),
            "userpass" => Auth::userpass(first, second).at_mount(mount),
            "ldap" => Auth::ldap(first, second).at_mount(mount),
            "jwt" => {
                let auth = Auth::jwt(first).at_mount(mount);

                // An empty role is *no role*, which is a different thing from
                // a role named "": the mount's own default is then used.
                if second.is_empty() {
                    auth
                } else {
                    auth.with_role(second)
                }
            }
            "certificate" => {
                let auth = Auth::certificate().at_mount(mount);

                if second.is_empty() {
                    auth
                } else {
                    auth.with_role(second)
                }
            }
            other => {
                return Err(Error::remote(format!(
                    "{other:?} is not a Vault auth method"
                )))
            }
        })
    }
}

/// The client, and the login credential it was built with.
struct Built {
    credential: Resolved,
    /// `Arc` so the fetch can drop the lock before the network read.
    vault: Arc<Vault>,
}

/// A secret in Vault's KV v2 store, read through the Rust client.
#[pyclass(module = "dynamic_config_remote._core", name = "VaultStore", frozen)]
pub(crate) struct VaultStore {
    address: String,
    mount: String,
    path: String,
    key: String,
    namespace: Option<String>,
    timeout: Duration,
    /// `None` for the platform's trust store, which is what an empty
    /// `TlsConfig` means and what this store already did.
    tls: Option<TlsConfig>,
    described: String,
    redactor: Redactor,
    built: Mutex<Option<Built>>,
}

#[pymethods]
impl VaultStore {
    /// Builds the store. Logging in happens on the first fetch.
    #[new]
    #[pyo3(signature = (address, mount, path, key, namespace, timeout, tls))]
    fn new(
        address: String,
        mount: String,
        path: String,
        key: String,
        namespace: Option<String>,
        timeout: f64,
        tls: Option<&Tls>,
    ) -> Self {
        // A Vault address is `https://user:password@host` in nobody's
        // deployment, but `redacted` costs nothing and the rule is that every
        // URL this wheel prints has been through it.
        let redactor = Redactor::new([&address], LoneAuthority::Username);
        let described = format!(
            "vault {} {mount}/{path}",
            safe(&address, LoneAuthority::Username)
        );

        Self {
            address,
            mount,
            path,
            key,
            namespace,
            timeout: Duration::from_secs_f64(timeout),
            tls: crate::tls::wanted(tls),
            described,
            redactor,
            built: Mutex::new(None),
        }
    }

    /// Reads the secret, and answers `(document, format)`.
    ///
    /// The four credential arguments are the auth method as the facade
    /// resolved it for *this* call — every callable already called.
    #[pyo3(signature = (kind, mount, first, second))]
    fn fetch(
        &self,
        py: Python<'_>,
        kind: String,
        mount: String,
        first: String,
        second: String,
    ) -> PyResult<(String, String)> {
        let credential = Resolved {
            kind,
            mount,
            first,
            second,
        };
        let secrets = credential.secrets();

        py.detach(|| self.read(&credential))
            .map(|text| (text, "json".to_owned()))
            .map_err(|error| raised(&error, &self.redactor, &secrets))
    }

    /// Names the store, redacted. What the engine records as provenance.
    fn describe(&self) -> String {
        self.described.clone()
    }

    fn __repr__(&self) -> String {
        format!("<{}>", self.described)
    }
}

impl VaultStore {
    /// One read, logging in again first if the login credential moved.
    fn read(&self, credential: &Resolved) -> Result<String, Error> {
        // The document, not the format: Vault's KV store holds a map, and the
        // crate wraps it under the section key as JSON. There is nothing else
        // it could be.
        Ok(self.client(credential)?.fetch()?.text)
    }

    /// The client for this credential, building or rebuilding as needed.
    fn client(&self, credential: &Resolved) -> Result<Arc<Vault>, Error> {
        let mut built = self.built.lock().unwrap_or_else(PoisonError::into_inner);

        if let Some(current) = built.as_ref() {
            if &current.credential == credential {
                return Ok(Arc::clone(&current.vault));
            }
        }

        let mut vault = Vault::new(&self.address, &self.mount, &self.path)
            .with_key(&self.key)
            .with_timeout(self.timeout)
            .with_auth(credential.auth()?);

        if let Some(namespace) = &self.namespace {
            vault = vault.with_namespace(namespace);
        }

        // Vault expresses all of it — a CA and a client certificate, each
        // from a file or from bytes — because `ureq` takes the material as
        // bytes and this crate reads the files.
        if let Some(tls) = &self.tls {
            vault = vault.with_tls(tls.clone());
        }

        let vault = Arc::new(vault);

        *built = Some(Built {
            credential: credential.clone(),
            vault: Arc::clone(&vault),
        });

        Ok(vault)
    }
}
