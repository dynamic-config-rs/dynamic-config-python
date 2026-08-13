//! Consul's key/value store, as a compiled object a Python `RemoteSource`
//! wraps.
//!
//! [`crate::vault`]'s shape, with a shorter auth story. Consul either presents
//! a token somebody handed the process — the usual case,
//! `CONSUL_HTTP_TOKEN` — or exchanges a bearer token for one at an auth
//! method, which is how a workload in Kubernetes proves who it is with no
//! secret to distribute. `ureq`, blocking, no runtime.
//!
//! ## The two credentials, and which one a callable moves
//!
//! There are two secrets in the login path and they rotate on different
//! clocks. The **ACL token** is Consul's to expire and the Rust crate's to
//! replace: `Cached` logs in again as expiry approaches and after a 403, and
//! none of that is visible from here. The **bearer token** is the deployment's
//! — a projected service-account JWT the kubelet rewrites, an OIDC id token a
//! daemon refreshes — and the Rust crate cannot notice that one moving,
//! because it was handed a `String`.
//!
//! So the bearer is what a callable is for, and a bearer that has moved
//! rebuilds the `Consul` — which throws away the ACL token bought with the
//! old one, correctly: a token obtained with a credential that has since been
//! replaced is a token nobody should keep. A bearer that has *not* moved
//! reuses the same `Consul` and its session cache, which is the difference
//! between one login and one login per refresh.
//!
//! A bearer read from a **file** is the exception that needs none of this:
//! `Bearer::File` is re-read by the Rust crate at every login already, so the
//! path is what crosses and the file's contents never do.

use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use dynamic_config::{Error, RemoteSource};
use dynamic_config_consul::{Auth, Consul};
use dynamic_config_store_core::tls::TlsConfig;
use dynamic_config_store_core::LoneAuthority;
use pyo3::prelude::*;

use crate::errors::raised;
use crate::format;
use crate::redact::{safe, Redactor};
use crate::tls::Tls;

/// A way of getting an ACL token, as the facade resolved it for one call.
///
/// Compared, not hashed: the whole purpose is to notice that a bearer token
/// is not the bearer token it was last time.
#[derive(Clone, PartialEq, Eq)]
struct Resolved {
    kind: String,
    method: String,
    bearer: String,
    meta: Vec<(String, String)>,
}

impl Resolved {
    /// The parts of this credential that must never appear in a message.
    ///
    /// A `login_file` has none: what it carries is a path, and the token
    /// behind it is read by the Rust crate and never held here.
    fn secrets(&self) -> Vec<&str> {
        match self.kind.as_str() {
            "token" | "login" => vec![self.bearer.as_str()],
            _ => Vec::new(),
        }
    }

    /// The engine's `Auth` for this credential.
    fn auth(&self) -> Result<Auth, Error> {
        let auth = match self.kind.as_str() {
            "anonymous" => return Ok(Auth::Anonymous),
            "token" => return Ok(Auth::token(self.bearer.clone())),
            "login" => Auth::jwt(self.method.clone(), self.bearer.clone()),
            // The path, not the token: `Bearer::File` is re-read at every
            // login, which is what keeps a rotated projected token working.
            "login_file" => {
                Auth::kubernetes(self.method.clone()).with_bearer_file(self.bearer.clone())
            }
            other => {
                return Err(Error::remote(format!(
                    "{other:?} is not a Consul auth method"
                )))
            }
        };

        Ok(self
            .meta
            .iter()
            .fold(auth, |auth, (name, value)| auth.with_meta(name, value)))
    }
}

/// The client, and the credential it was built with.
struct Built {
    credential: Resolved,
    /// `Arc` so the fetch can drop the lock before the network read.
    consul: Arc<Consul>,
}

/// A key in Consul's KV store, read through the Rust client.
#[pyclass(module = "dynamic_config_remote._core", name = "ConsulStore", frozen)]
pub(crate) struct ConsulStore {
    address: String,
    key: String,
    /// `None` when the key names no format and none was given. Refused by the
    /// facade at construction, so it means only "the key had an extension".
    format: Option<dynamic_config::Format>,
    datacenter: Option<String>,
    timeout: Duration,
    /// `None` for the platform's trust store, which is what an empty
    /// `TlsConfig` means and what this store already did.
    tls: Option<TlsConfig>,
    described: String,
    redactor: Redactor,
    built: Mutex<Option<Built>>,
}

#[pymethods]
impl ConsulStore {
    /// Builds the store. Reaching the agent happens on the first fetch.
    #[new]
    #[pyo3(signature = (address, key, format, datacenter, timeout, tls))]
    fn new(
        address: String,
        key: String,
        format: Option<&str>,
        datacenter: Option<String>,
        timeout: f64,
        tls: Option<&Tls>,
    ) -> PyResult<Self> {
        // An agent address is `http://user:password@host` in nobody's
        // deployment, but the rule is that every URL this wheel prints has
        // been through the shared redaction, and an agent behind a
        // credentialed proxy is exactly the deployment nobody thought of.
        let redactor = Redactor::new([&address], LoneAuthority::Username);
        let described = format!(
            "consul {} kv/{key}",
            safe(&address, LoneAuthority::Username)
        );

        Ok(Self {
            address,
            key,
            format: format::maybe(format)?,
            datacenter,
            timeout: Duration::from_secs_f64(timeout),
            tls: crate::tls::wanted(tls),
            described,
            redactor,
            built: Mutex::new(None),
        })
    }

    /// Reads the key, and answers `(document, format)`.
    ///
    /// The four credential arguments are the auth method as the facade
    /// resolved it for *this* call — every callable already called.
    #[pyo3(signature = (kind, method, bearer, meta))]
    fn fetch(
        &self,
        py: Python<'_>,
        kind: String,
        method: String,
        bearer: String,
        meta: Vec<(String, String)>,
    ) -> PyResult<(String, String)> {
        let credential = Resolved {
            kind,
            method,
            bearer,
            meta,
        };
        let secrets = credential.secrets();

        py.detach(|| self.read(&credential))
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

impl ConsulStore {
    /// One read, logging in again first if the bearer token moved.
    fn read(&self, credential: &Resolved) -> Result<(String, String), Error> {
        let fetched = self.client(credential)?.fetch()?;

        Ok((fetched.text, format::named(fetched.format).to_owned()))
    }

    /// The client for this credential, building or rebuilding as needed.
    fn client(&self, credential: &Resolved) -> Result<Arc<Consul>, Error> {
        let mut built = self.built.lock().unwrap_or_else(PoisonError::into_inner);

        if let Some(current) = built.as_ref() {
            if &current.credential == credential {
                return Ok(Arc::clone(&current.consul));
            }
        }

        let mut consul = Consul::new(&self.address, self.key.clone())
            .with_timeout(self.timeout)
            .with_auth(credential.auth()?);

        if let Some(format) = self.format {
            consul = consul.with_format(format);
        }

        if let Some(datacenter) = &self.datacenter {
            consul = consul.with_datacenter(datacenter);
        }

        // Consul expresses all of it: `ureq` takes the material as bytes, and
        // the store crate reads a file where one was named.
        if let Some(tls) = &self.tls {
            consul = consul.with_tls(tls.clone());
        }

        let consul = Arc::new(consul);

        *built = Some(Built {
            credential: credential.clone(),
            consul: Arc::clone(&consul),
        });

        Ok(consul)
    }
}
