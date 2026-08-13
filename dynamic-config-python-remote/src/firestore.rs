//! A Firestore document, as a compiled object a Python `RemoteSource` wraps.
//!
//! [`crate::vault`]'s shape again — `ureq`, blocking, a token that expires and
//! a session that replaces it — with two differences that are Firestore's
//! and not this file's. The refusal is a **401** rather than a 403, and the
//! `Emulator` variant is a credential that is deliberately no credential at
//! all: the emulator wants none, and sending one would be the mistake.
//!
//! ## What a callable is for here
//!
//! `Auth::metadata_server` already renews on its own — the workload asks its
//! own metadata server and the Rust crate caches what it gets until it is
//! nearly expired — so a deployment on GKE, Cloud Run or GCE needs no
//! callable and should not write one.
//!
//! The callable is for `Auth::access_token`, which is the variant that cannot
//! renew: a token minted outside the process by `gcloud` or by a library that
//! handles Google credentials expires in an hour and the Rust crate has
//! nothing to replace it with. A callable is exactly that replacement, and it
//! is the difference between a source that dies after an hour and one that
//! does not.
//!
//! A service-account JSON key is not supported and that is the Rust crate's
//! decision, taken as a recommendation rather than a gap: signing one means
//! an RS256 stack in a configuration library, and Google's own guidance is
//! that a downloaded key is the option of last resort.

use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use dynamic_config::{Error, RemoteSource};
use dynamic_config_firestore::{Auth, Firestore};
use dynamic_config_store_core::tls::TlsConfig;
use dynamic_config_store_core::LoneAuthority;
use pyo3::prelude::*;

use crate::errors::raised;
use crate::redact::{safe, Redactor};
use crate::tls::Tls;

/// A way of getting an access token, as the facade resolved it for one call.
#[derive(Clone, PartialEq, Eq)]
struct Resolved {
    kind: String,
    value: String,
}

impl Resolved {
    /// The parts of this credential that must never appear in a message.
    ///
    /// The metadata server's *URL* is not one: it is an address, it is in
    /// `Debug` in the Rust crate for the same reason, and a sidecar's port is
    /// the first thing somebody debugging this needs to see.
    fn secrets(&self) -> Vec<&str> {
        match self.kind.as_str() {
            "access_token" => vec![self.value.as_str()],
            _ => Vec::new(),
        }
    }

    /// The engine's `Auth` for this credential.
    fn auth(&self) -> Result<Auth, Error> {
        Ok(match self.kind.as_str() {
            "emulator" => Auth::Emulator,
            "access_token" => Auth::access_token(self.value.clone()),
            "metadata_server" => Auth::metadata_server().with_url(self.value.clone()),
            other => {
                return Err(Error::remote(format!(
                    "{other:?} is not a Firestore auth method"
                )))
            }
        })
    }
}

/// The client, and the credential it was built with.
struct Built {
    credential: Resolved,
    /// `Arc` so the fetch can drop the lock before the network read.
    firestore: Arc<Firestore>,
}

/// A document in Firestore, read through the Rust client.
#[pyclass(
    module = "dynamic_config_remote._core",
    name = "FirestoreStore",
    frozen
)]
pub(crate) struct FirestoreStore {
    project: String,
    path: String,
    key: String,
    database: String,
    endpoint: Option<String>,
    timeout: Duration,
    /// `None` for the platform's trust store, which is what an empty
    /// `TlsConfig` means and what this store already did.
    tls: Option<TlsConfig>,
    described: String,
    redactor: Redactor,
    built: Mutex<Option<Built>>,
}

#[pymethods]
impl FirestoreStore {
    /// Builds the store. Getting a token happens on the first fetch.
    #[new]
    #[pyo3(signature = (project, path, key, database, endpoint, timeout, tls))]
    fn new(
        project: String,
        path: String,
        key: String,
        database: String,
        endpoint: Option<String>,
        timeout: f64,
        tls: Option<&Tls>,
    ) -> Self {
        // The endpoint is an emulator address or a private service endpoint;
        // neither ordinarily carries a credential, and both go through the
        // shared rule anyway because "ordinarily" is not a guarantee.
        let redactor = Redactor::new(endpoint.iter(), LoneAuthority::Username);
        let described = match &endpoint {
            Some(endpoint) => format!(
                "firestore {} {project}/{path}",
                safe(endpoint, LoneAuthority::Username)
            ),
            None => format!("firestore {project}/{path}"),
        };

        Self {
            project,
            path,
            key,
            database,
            endpoint,
            timeout: Duration::from_secs_f64(timeout),
            tls: crate::tls::wanted(tls),
            described,
            redactor,
            built: Mutex::new(None),
        }
    }

    /// Reads the document, and answers `(document, format)`.
    #[pyo3(signature = (kind, value))]
    fn fetch(&self, py: Python<'_>, kind: String, value: String) -> PyResult<(String, String)> {
        let credential = Resolved { kind, value };
        let secrets = credential.secrets();

        py.detach(|| self.read(&credential))
            // JSON, always: the crate turns Firestore's typed fields into a
            // JSON object wrapped under the section key, and there is nothing
            // else it could be.
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

impl FirestoreStore {
    /// One read, with a fresh client first if the credential moved.
    fn read(&self, credential: &Resolved) -> Result<String, Error> {
        Ok(self.client(credential)?.fetch()?.text)
    }

    /// The client for this credential, building or rebuilding as needed.
    fn client(&self, credential: &Resolved) -> Result<Arc<Firestore>, Error> {
        let mut built = self.built.lock().unwrap_or_else(PoisonError::into_inner);

        if let Some(current) = built.as_ref() {
            if &current.credential == credential {
                return Ok(Arc::clone(&current.firestore));
            }
        }

        let mut firestore = Firestore::new(&self.project, &self.path)
            .with_key(&self.key)
            .with_database(&self.database)
            .with_timeout(self.timeout)
            .with_auth(credential.auth()?);

        if let Some(endpoint) = &self.endpoint {
            firestore = firestore.with_endpoint(endpoint);
        }

        // Firestore expresses all of it, and the deployment it is for is a
        // private service endpoint or a TLS-inspecting proxy rather than
        // Google's own address.
        if let Some(tls) = &self.tls {
            firestore = firestore.with_tls(tls.clone());
        }

        let firestore = Arc::new(firestore);

        *built = Some(Built {
            credential: credential.clone(),
            firestore: Arc::clone(&firestore),
        });

        Ok(firestore)
    }
}
