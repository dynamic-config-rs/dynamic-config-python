//! etcd, as a compiled object a Python `RemoteSource` wraps.
//!
//! ## The shape, and why it is this one
//!
//! Nothing in this file hands a Rust value to the base wheel. The two wheels
//! are two extension modules with two static copies of the engine, and a
//! `RemoteSource` trait object built here is not the trait the base wheel's
//! `Config::remote()` knows — the type identity would not match, and `pyo3`
//! could not downcast across the boundary if it did.
//!
//! So this store is a Python remote source like any other: [`EtcdStore`]
//! answers `fetch()` with a `(text, format)` pair, and the base wheel merges
//! it through exactly the path item 05 built for a store written in Python.
//! The Rust in here is the client, not the crossing. What that buys is that
//! the base wheel needs no change at all to support this one, and the
//! established GIL discipline, error translation and cycle handling apply
//! unaltered because it is the same path.
//!
//! ## Credentials are resolved in Python, above this
//!
//! `user` and `password` arrive as already-resolved strings on every call.
//! The callable that produced them — a token that expires, a secret a
//! sidecar rotates — is called by the Python facade, on the caller's thread,
//! before the GIL is released. That keeps this file free of Rust-to-Python
//! callbacks on the fetch path, which is the thing that would otherwise have
//! to hold the GIL across a network read.
//!
//! When the resolved pair differs from the one the current client was built
//! with, the client is rebuilt. `etcd-client` puts credentials in the
//! connection, so there is nothing else to change them with — and rebuilding
//! is cheap next to the thing it protects, which is a watcher outliving the
//! password it started with.

use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use dynamic_config::{AsyncRemoteSource, Error, Format};
use dynamic_config_etcd::{ConnectOptions, Etcd};
use dynamic_config_store_core::tls::TlsConfig;
use dynamic_config_store_core::LoneAuthority;
use pyo3::prelude::*;

use crate::errors::raised;
use crate::format;
use crate::redact::{safe, Redactor};
use crate::runtime;
use crate::tls::Tls;

/// The client, and the credentials it was built with.
struct Built {
    /// `None` for an unauthenticated connection.
    credentials: Option<(String, String)>,
    /// `Arc`, because the future handed to the runtime has to own it: the
    /// engine's `fetch` borrows `&self`, and a borrow cannot be `'static`.
    etcd: Arc<Etcd>,
}

/// A key in etcd, read through the Rust client.
///
/// `frozen`, so the only mutable state is behind the lock below. A `#[pyclass]`
/// that Python can mutate would need `&mut self`, which would put a `RefCell`
/// borrow across a network read.
#[pyclass(module = "dynamic_config_remote._core", name = "EtcdStore", frozen)]
pub(crate) struct EtcdStore {
    endpoints: Vec<String>,
    key: String,
    /// `None` when the key names no format and none was given — the failure
    /// is raised at construction by the facade, so this being `None` here
    /// means only that the key had no extension.
    format: Option<Format>,
    timeout: Duration,
    /// `None` for the platform's trust store — which for this store means
    /// `ConnectOptions` alone, exactly as before TLS had a spelling here.
    tls: Option<TlsConfig>,
    /// The description, redacted, computed once. Handed to the base wheel's
    /// `remote()` where it becomes provenance and error text.
    described: String,
    redactor: Redactor,
    built: Mutex<Option<Built>>,
}

#[pymethods]
impl EtcdStore {
    /// Builds the store. Connecting happens on the first fetch.
    ///
    /// The runtime starts here rather than at the first fetch, because a
    /// store is the thing that needs one and construction is the moment a
    /// user can see it happen.
    #[new]
    #[pyo3(signature = (endpoints, key, format, timeout, tls))]
    fn new(
        endpoints: Vec<String>,
        key: String,
        format: Option<&str>,
        timeout: f64,
        tls: Option<&Tls>,
    ) -> PyResult<Self> {
        let format = format::maybe(format)?;

        // etcd endpoints are `scheme://host:port`; a credential in one is
        // `user:password@host`, so a lone authority is a user name.
        let redactor = Redactor::new(&endpoints, LoneAuthority::Username);
        let described = format!(
            "etcd {} key {key}",
            endpoints
                .iter()
                .map(|endpoint| safe(endpoint, LoneAuthority::Username))
                .collect::<Vec<_>>()
                .join(", ")
        );

        runtime::runtime().map_err(|error| raised(&error, &redactor, &[]))?;

        Ok(Self {
            endpoints,
            key,
            format,
            timeout: Duration::from_secs_f64(timeout),
            tls: crate::tls::wanted(tls),
            described,
            redactor,
            built: Mutex::new(None),
        })
    }

    /// Reads the key, and answers `(document, format)`.
    ///
    /// `user` and `password` are the credentials as the facade resolved them
    /// for *this* call.
    #[pyo3(signature = (user, password))]
    fn fetch(
        &self,
        py: Python<'_>,
        user: Option<String>,
        password: Option<String>,
    ) -> PyResult<(String, String)> {
        let credentials = match (user, password) {
            (Some(user), Some(password)) => Some((user, password)),
            _ => None,
        };

        // Copied before the move, because it is needed *after* the read: a
        // failure's message is scrubbed of the credential this call used.
        let secret = credentials
            .as_ref()
            .map(|(_, password)| password.clone())
            .unwrap_or_default();

        // The GIL is released for the whole of it — the connect, the request
        // and the wait — so a slow etcd does not stop the process. Nothing
        // below re-enters Python, which is what makes that safe rather than
        // merely fast.
        py.detach(|| self.read(credentials))
            .map_err(|error| raised(&error, &self.redactor, &[&secret]))
    }

    /// Names the store, redacted. What the engine records as provenance.
    fn describe(&self) -> String {
        self.described.clone()
    }

    fn __repr__(&self) -> String {
        // The description is already redacted; a `repr` that rebuilt it from
        // the raw endpoints would be the one surface that leaked.
        format!("<{}>", self.described)
    }
}

impl EtcdStore {
    /// One read, with the client rebuilt first if the credentials moved.
    fn read(&self, credentials: Option<(String, String)>) -> Result<(String, String), Error> {
        // No guard for a missing format here, deliberately. `format` is the
        // *explicit* one; `None` means only that the caller did not pass one,
        // and `Etcd::with_options` reads it from the key's extension — which
        // is the ordinary case (`myapp/db.json`). A key that names no format
        // either is refused by the facade at construction, where the message
        // can name the Python argument; a store built through this compiled
        // class directly gets the engine's own wording instead.
        let etcd = self.client(credentials)?;
        let fetched = runtime::block_on(async move { etcd.fetch().await })??;

        Ok((fetched.text, format::named(fetched.format).to_owned()))
    }

    /// The client for these credentials, building or rebuilding as needed.
    ///
    /// The lock is held across the build so that two threads arriving
    /// together produce one connection rather than two — the same bargain
    /// the store crates' credential cache makes, and for the same reason.
    fn client(&self, credentials: Option<(String, String)>) -> Result<Arc<Etcd>, Error> {
        let mut built = self.built.lock().unwrap_or_else(PoisonError::into_inner);

        if let Some(current) = built.as_ref() {
            if current.credentials == credentials {
                return Ok(Arc::clone(&current.etcd));
            }
        }

        let mut options = ConnectOptions::new();

        if let Some((user, password)) = &credentials {
            options = options.with_user(user.clone(), password.clone());
        }

        let endpoints = self.endpoints.clone();
        let key = self.key.clone();
        let format = self.format;
        let timeout = self.timeout;
        let tls = self.tls.clone();

        let etcd = runtime::block_on(async move {
            // `with_options` and `with_tls` are the same constructor with and
            // without the TLS slot filled, which is why the choice is here
            // rather than a `ConnectOptions` this file assembled: the
            // translation from data to a `tonic` configuration belongs to the
            // store crate, and is the whole reason nothing here names one.
            let mut etcd = match &tls {
                Some(tls) => Etcd::with_tls(endpoints, key, options, tls).await?,
                None => Etcd::with_options(endpoints, key, options).await?,
            }
            .with_timeout(timeout);

            if let Some(format) = format {
                etcd = etcd.with_format(format);
            }

            Ok::<_, Error>(Arc::new(etcd))
        })??;

        *built = Some(Built {
            credentials,
            etcd: Arc::clone(&etcd),
        });

        Ok(etcd)
    }
}
