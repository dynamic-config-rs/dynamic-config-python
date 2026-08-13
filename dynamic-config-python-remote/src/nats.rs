//! A NATS JetStream key/value bucket, as a compiled object a Python
//! `RemoteSource` wraps.
//!
//! [`crate::etcd`]'s shape: async, so it drives the one runtime this wheel
//! owns, and the credential lives in the connection rather than in a header —
//! which means a rotated one is a reconnection, not a different request.
//!
//! ## Connecting is deferred, and the Rust crate's constructor does not defer
//!
//! `Nats::with_options` connects, resolves the bucket and fails if either is
//! not there. That is right for Rust, where the constructor is fallible and
//! the caller is already inside a runtime. It is wrong here: every store in
//! this wheel promises that construction touches no network, so that a
//! misconfigured host is one error at the first refresh rather than an
//! exception in the middle of building a configuration. So the connection is
//! built on the first fetch, behind the same lock and the same comparison the
//! other six use.
//!
//! ## A lone authority is a *secret* here
//!
//! `nats://token@host` puts the credential in the authority with no colon,
//! which is the one line of difference between this store's redaction and
//! Redis'. It is [`LoneAuthority::Secret`] against Redis'
//! [`LoneAuthority::Username`], and the server list is redacted server by
//! server because a comma-separated list is a shape NATS accepts.
//!
//! ## TLS: paths yes, bytes no — and the refusal is the point
//!
//! `async-nats` opens the certificate files itself, so the byte spellings of
//! the shared vocabulary have nowhere to go. They are **refused**, at
//! construction, naming the call and the file spelling to use instead. The
//! alternative — quietly using the platform trust store — would leave a
//! program believing it had pinned a private authority when it had not, and
//! the other alternative — writing the bytes to a temporary file — would put
//! a private key on a disk that never asked for one.

use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use dynamic_config::{AsyncRemoteSource, Error};
use dynamic_config_nats::{ConnectOptions, Nats};
use dynamic_config_store_core::tls::TlsConfig;
use dynamic_config_store_core::LoneAuthority;
use pyo3::prelude::*;

use crate::errors::raised;
use crate::format;
use crate::redact::{safe, Redactor};
use crate::runtime;
use crate::tls::Tls;

/// A NATS credential, as the facade resolved it for one call.
#[derive(Clone, PartialEq, Eq)]
struct Resolved {
    kind: String,
    first: String,
    second: String,
}

impl Resolved {
    /// The halves of this credential that must never appear in a message.
    ///
    /// A `.creds` file's *contents* are a secret in their entirety — it holds
    /// a JWT and an NKey seed — so the whole blob is scrubbed by value.
    fn secrets(&self) -> Vec<&str> {
        match self.kind.as_str() {
            "token" | "nkey" | "credentials" => vec![self.first.as_str()],
            "user_password" => vec![self.second.as_str()],
            _ => Vec::new(),
        }
    }

    /// NATS' own `ConnectOptions` for this credential.
    fn options(&self) -> Result<ConnectOptions, Error> {
        let first = self.first.clone();
        let second = self.second.clone();

        Ok(match self.kind.as_str() {
            "anonymous" => ConnectOptions::new(),
            "token" => ConnectOptions::with_token(first),
            "user_password" => ConnectOptions::with_user_and_password(first, second),
            "nkey" => ConnectOptions::with_nkey(first),
            // The contents, not the path: the facade reads the file on every
            // fetch, so a `.creds` an operator rewrites is picked up without
            // the process being told where it lives twice.
            "credentials" => ConnectOptions::with_credentials(&first).map_err(|error| {
                // `error` is `async-nats`' parse failure, which names the
                // shape it wanted and never quotes the file.
                Error::remote(format!("nats: the credentials are not usable: {error}"))
            })?,
            other => return Err(Error::remote(format!("{other:?} is not a NATS credential"))),
        })
    }
}

/// The connection, and the credential it was made with.
struct Built {
    credential: Resolved,
    /// `Arc`, because the future handed to the runtime has to own it: the
    /// engine's `fetch` borrows `&self`, and a borrow cannot be `'static`.
    nats: Arc<Nats>,
}

/// A key in a JetStream bucket, read through the Rust client.
#[pyclass(module = "dynamic_config_remote._core", name = "NatsStore", frozen)]
pub(crate) struct NatsStore {
    servers: String,
    bucket: String,
    key: String,
    format: Option<dynamic_config::Format>,
    timeout: Duration,
    /// `None` for the platform's trust store. Never a byte spelling: those
    /// are refused in `new`, so anything here is a pair of paths
    /// `async-nats` can open.
    tls: Option<TlsConfig>,
    described: String,
    redactor: Redactor,
    built: Mutex<Option<Built>>,
}

#[pymethods]
impl NatsStore {
    /// Builds the store. Connecting happens on the first fetch.
    ///
    /// The runtime starts here rather than at the first fetch, because a
    /// store is the thing that needs one and construction is the moment a
    /// user can see it happen.
    #[new]
    #[pyo3(signature = (servers, bucket, key, format, timeout, tls))]
    fn new(
        servers: Vec<String>,
        bucket: String,
        key: String,
        format: Option<&str>,
        timeout: f64,
        tls: Option<&Tls>,
    ) -> PyResult<Self> {
        let redactor = Redactor::new(&servers, LoneAuthority::Secret);
        let described = format!(
            "nats {} bucket {bucket} key {key}",
            servers
                .iter()
                .map(|server| safe(server, LoneAuthority::Secret))
                .collect::<Vec<_>>()
                .join(", ")
        );

        let format = format::maybe(format)?;

        // Before the runtime starts, so a configuration NATS cannot express
        // is an error and not also two worker threads.
        let tls = crate::tls::wanted(tls);

        if let Some(tls) = &tls {
            crate::tls::nats_takes_paths(tls, &described)?;
        }

        runtime::runtime().map_err(|error| raised(&error, &redactor, &[]))?;

        Ok(Self {
            // One string, comma separated: that is the shape `async-nats`
            // reads a server list in, and the shape the Rust crate redacts
            // server by server.
            servers: servers.join(","),
            bucket,
            key,
            format,
            timeout: Duration::from_secs_f64(timeout),
            tls,
            described,
            redactor,
            built: Mutex::new(None),
        })
    }

    /// Reads the key, and answers `(document, format)`.
    #[pyo3(signature = (kind, first, second))]
    fn fetch(
        &self,
        py: Python<'_>,
        kind: String,
        first: String,
        second: String,
    ) -> PyResult<(String, String)> {
        let credential = Resolved {
            kind,
            first,
            second,
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

impl NatsStore {
    /// One read, reconnecting first if the credential moved.
    fn read(&self, credential: &Resolved) -> Result<(String, String), Error> {
        let nats = self.client(credential)?;
        let fetched = runtime::block_on(async move { nats.fetch().await })??;

        Ok((fetched.text, format::named(fetched.format).to_owned()))
    }

    /// The connection for this credential, making or remaking it as needed.
    fn client(&self, credential: &Resolved) -> Result<Arc<Nats>, Error> {
        let mut built = self.built.lock().unwrap_or_else(PoisonError::into_inner);

        if let Some(current) = built.as_ref() {
            if &current.credential == credential {
                return Ok(Arc::clone(&current.nats));
            }
        }

        let options = credential.options()?;
        let servers = self.servers.clone();
        let bucket = self.bucket.clone();
        let key = self.key.clone();
        let format = self.format;
        let timeout = self.timeout;
        let tls = self.tls.clone();

        let nats = runtime::block_on(async move {
            // Naming a certificate authority also turns TLS on — the store
            // crate sets `require_tls`, so a `nats://` URL that would have
            // negotiated plaintext fails rather than quietly connecting
            // without the authority the caller just named.
            let mut nats = match &tls {
                Some(tls) => Nats::with_tls(servers, bucket, key, options, tls).await?,
                None => Nats::with_options(servers, bucket, key, options).await?,
            }
            .with_timeout(timeout);

            if let Some(format) = format {
                nats = nats.with_format(format);
            }

            Ok::<_, Error>(Arc::new(nats))
        })??;

        *built = Some(Built {
            credential: credential.clone(),
            nats: Arc::clone(&nats),
        });

        Ok(nats)
    }
}
