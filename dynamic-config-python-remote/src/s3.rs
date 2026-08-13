//! An S3 object, as a compiled object a Python `RemoteSource` wraps.
//!
//! The one store whose credential is not a string, and the reason this file
//! is longer than the other six.
//!
//! ## The credential surface is a trait, not a value
//!
//! Every other client here takes what it needs as a `String` — a token, a
//! password, a bearer — so a Python callable resolves to a string and the
//! string is handed over. The AWS SDK takes a
//! [`ProvideCredentials`] implementation instead, and asks it *per request*,
//! from inside the SDK's own async machinery. That is not a shape a `String`
//! fits: the chain is how the SDK finds credentials in the environment, the
//! profile, the instance role and IRSA, and replacing it with a fixed pair
//! would replace the thing most deployments actually use.
//!
//! So there are two modes here and both are first class:
//!
//! - **No credential arguments** — the SDK's own chain, unaltered. This is the
//!   right answer on EC2, ECS, EKS and anywhere `AWS_ACCESS_KEY_ID` is set,
//!   and it is what a Python caller gets by writing nothing.
//! - **Credential arguments, each of which may be a callable** — a
//!   [`FromPython`] provider is installed in the chain's place, and it answers
//!   every request from a slot the facade refreshes before each fetch.
//!
//! ## Why a slot rather than a callback
//!
//! `provide_credentials` runs on this wheel's tokio runtime, and **nothing on
//! that runtime may touch Python**. That is not a preference: the runtime is
//! never shut down, which is safe precisely because a worker still running
//! while CPython finalises holds nothing but Rust. A provider that called a
//! Python callable would put a GIL acquisition on a thread that may outlive
//! the interpreter, and would turn the one safe immortal runtime in this
//! wheel into the dangerous kind.
//!
//! The resolution therefore stays where it is for every other store — in the
//! facade, on the caller's thread, before the GIL is released — and what
//! crosses is the resolved value, written into a mutex the provider reads.
//! The provider is a shim over that slot and does no work at all.
//!
//! ## What that buys, beyond correctness
//!
//! The SDK client is built **once** and never rebuilt for a credential
//! change, because the credential is not baked into it: rotating a key
//! updates a `String` behind a mutex, and the next request signs with it. The
//! other six stores rebuild a client when their credential moves; this one
//! has nothing to rebuild, which is the better end of the same bargain.
//!
//! The one thing that has to be switched off for this to work is the SDK's
//! **identity cache**. It exists to keep a provider that calls IMDS from
//! being called per request, and it caches credentials with no expiry
//! indefinitely — which for a slot that is deliberately mutable would mean
//! the first key signing every request forever. `IdentityCache::no_cache()`
//! costs nothing here, because the provider it defeats is a mutex read.
//!
//! ## TLS: a private authority yes, a client certificate no
//!
//! The SDK reaches TLS through `aws-smithy-http-client`, whose TLS context is
//! a trust store and nothing else — there is no client-certificate slot to
//! fill. So a `TlsConfig` naming one is **refused** at construction rather
//! than applied in part: a caller who asked to present a certificate and did
//! not would meet it as an authentication failure a long way from the cause.
//! A private authority is the setting that matters here anyway, because it is
//! the S3-compatible servers — MinIO, Ceph, a company's own gateway — that
//! present certificates AWS' public chain has never heard of.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use aws_config::identity::IdentityCache;
use aws_config::{BehaviorVersion, Region};
use aws_credential_types::provider::{future, ProvideCredentials, SharedCredentialsProvider};
use aws_credential_types::Credentials;
use dynamic_config::{AsyncRemoteSource, Error};
use dynamic_config_s3::S3;
use dynamic_config_store_core::tls::TlsConfig;
use dynamic_config_store_core::LoneAuthority;
use pyo3::prelude::*;

use crate::errors::raised;
use crate::format;
use crate::redact::{safe, Redactor};
use crate::runtime;
use crate::tls::Tls;

/// What the provider hands the SDK: an access key pair and, for a session,
/// the token that goes with it.
#[derive(Clone, Default, PartialEq, Eq)]
struct Keys {
    access_key_id: String,
    secret_access_key: String,
    /// `None` for a long-lived key pair; `Some` for anything assumed —
    /// `sts:AssumeRole`, IRSA, an SSO session.
    session_token: Option<String>,
}

// Hand-written, never derived: `ProvideCredentials` requires `Debug` on the
// provider, and a derive here would print a secret access key the first time
// somebody put a `{:?}` on an SDK config. The access key id stays, because it
// is the half an operator matches against an IAM console.
impl std::fmt::Debug for Keys {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Keys")
            .field("access_key_id", &self.access_key_id)
            .field("secret_access_key", &"***")
            .field("session_token", &self.session_token.as_ref().map(|_| "***"))
            .finish()
    }
}

/// The [`ProvideCredentials`] shim a Python callable becomes.
///
/// It holds no callable and calls nothing: the facade has already resolved
/// the credential on the caller's thread and written it here. Answering is a
/// mutex read, which is why the identity cache in front of it is switched off
/// rather than tuned.
#[derive(Debug)]
struct FromPython {
    current: Arc<Mutex<Keys>>,
}

impl FromPython {
    /// The credentials as of this request.
    fn credentials(&self) -> Credentials {
        let keys = self
            .current
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone();

        Credentials::new(
            keys.access_key_id,
            keys.secret_access_key,
            keys.session_token,
            // No expiry: expiry is the SDK's cue to ask again, and this
            // provider is asked again anyway. Saying "expires in an hour"
            // would be a claim about a Python callable nobody here can make.
            None,
            "dynamic-config-remote",
        )
    }
}

impl ProvideCredentials for FromPython {
    fn provide_credentials<'a>(&'a self) -> future::ProvideCredentials<'a>
    where
        Self: 'a,
    {
        // `ready`, not `new`: there is nothing to await. The whole point of
        // resolving in the facade is that this is not an async operation.
        future::ProvideCredentials::ready(Ok(self.credentials()))
    }
}

/// The client, and which credential source it was built with.
struct Built {
    /// `true` when the SDK's own chain is in use, `false` when the shim is.
    /// The only thing that can rebuild this client: a *value* changing does
    /// not, because the value is not in the client.
    chained: bool,
    /// `Arc`, because the future handed to the runtime has to own it.
    s3: Arc<S3>,
}

/// An object in S3, read through the Rust client.
#[pyclass(module = "dynamic_config_remote._core", name = "S3Store", frozen)]
pub(crate) struct S3Store {
    bucket: String,
    key: String,
    format: Option<dynamic_config::Format>,
    region: Option<String>,
    endpoint: Option<String>,
    timeout: Duration,
    /// `None` for the platform's trust store. Never a client certificate:
    /// that is refused in `new`, so anything here is a certificate authority.
    tls: Option<TlsConfig>,
    described: String,
    redactor: Redactor,
    /// What the shim answers with. Written by the facade before each fetch,
    /// read by the SDK on each request.
    current: Arc<Mutex<Keys>>,
    built: Mutex<Option<Built>>,
    builds: AtomicUsize,
}

#[pymethods]
impl S3Store {
    /// Builds the store. Resolving credentials happens on the first fetch.
    ///
    /// The runtime starts here rather than at the first fetch, because a
    /// store is the thing that needs one and construction is the moment a
    /// user can see it happen. Nothing else reaches the network: the SDK's
    /// chain can read a file or call the instance metadata service, and that
    /// belongs to the first refresh rather than to building an object.
    #[new]
    #[pyo3(signature = (bucket, key, format, region, endpoint, timeout, tls))]
    fn new(
        bucket: String,
        key: String,
        format: Option<&str>,
        region: Option<String>,
        endpoint: Option<String>,
        timeout: f64,
        tls: Option<&Tls>,
    ) -> PyResult<Self> {
        let redactor = Redactor::new(endpoint.iter(), LoneAuthority::Username);
        let described = match &endpoint {
            Some(endpoint) => format!(
                "s3 {} {bucket}/{key}",
                safe(endpoint, LoneAuthority::Username)
            ),
            None => format!("s3 {bucket}/{key}"),
        };

        let format = format::maybe(format)?;

        // Before the runtime starts, so a configuration the SDK cannot
        // express is an error and not also two worker threads.
        let tls = crate::tls::wanted(tls);

        if let Some(tls) = &tls {
            crate::tls::s3_takes_no_client_certificate(tls, &described)?;
        }

        runtime::runtime().map_err(|error| raised(&error, &redactor, &[]))?;

        Ok(Self {
            bucket,
            key,
            format,
            region,
            endpoint,
            timeout: Duration::from_secs_f64(timeout),
            tls,
            described,
            redactor,
            current: Arc::new(Mutex::new(Keys::default())),
            built: Mutex::new(None),
            builds: AtomicUsize::new(0),
        })
    }

    /// Reads the object, and answers `(document, format)`.
    ///
    /// The three credential arguments are the key pair as the facade resolved
    /// it for *this* call. All three absent means the SDK's own chain.
    #[pyo3(signature = (access_key_id, secret_access_key, session_token))]
    fn fetch(
        &self,
        py: Python<'_>,
        access_key_id: Option<String>,
        secret_access_key: Option<String>,
        session_token: Option<String>,
    ) -> PyResult<(String, String)> {
        let keys = match (access_key_id, secret_access_key) {
            (Some(access_key_id), Some(secret_access_key)) => Some(Keys {
                access_key_id,
                secret_access_key,
                session_token,
            }),
            _ => None,
        };

        let secrets: Vec<String> = keys
            .as_ref()
            .map(|keys| {
                let mut secrets = vec![keys.secret_access_key.clone()];
                secrets.extend(keys.session_token.clone());
                secrets
            })
            .unwrap_or_default();

        py.detach(|| self.read(keys)).map_err(|error| {
            let secrets: Vec<&str> = secrets.iter().map(String::as_str).collect();

            raised(&error, &self.redactor, &secrets)
        })
    }

    /// Names the store, redacted. What the engine records as provenance.
    fn describe(&self) -> String {
        self.described.clone()
    }

    /// How many SDK clients this store has built.
    ///
    /// Public for one reason, and the same one [`crate::runtime_started`] is:
    /// it is the only way to *test* the promise this store makes. Every other
    /// store proves "a credential that has not moved is not re-presented" on
    /// the wire — a second login, a second connection — and S3 has neither to
    /// count, because it signs each request from whatever the provider says.
    /// So the observable is the client, and this is it. Nothing else should
    /// read it.
    fn clients_built(&self) -> usize {
        self.builds.load(Ordering::Relaxed)
    }

    fn __repr__(&self) -> String {
        format!("<{}>", self.described)
    }
}

impl S3Store {
    /// One read, with the resolved credentials in place first.
    fn read(&self, keys: Option<Keys>) -> Result<(String, String), Error> {
        // Written before the client is asked for, so a first fetch signs with
        // the credential it resolved rather than with an empty slot. Two
        // threads fetching with different credentials would race here, and
        // that is a store shared between two identities — which is a
        // configuration mistake rather than a case to lock harder for.
        if let Some(keys) = &keys {
            *self.current.lock().unwrap_or_else(PoisonError::into_inner) = keys.clone();
        }

        let s3 = self.client(keys.is_none())?;
        let fetched = runtime::block_on(async move { s3.fetch().await })??;

        Ok((fetched.text, format::named(fetched.format).to_owned()))
    }

    /// The client, building it if this is the first fetch.
    ///
    /// `chained` says which credential source it is for. A store keeps its
    /// client across every credential *value*; only switching between the
    /// SDK's chain and an explicit key pair rebuilds one, and that is a
    /// caller who started passing credentials halfway through a process.
    fn client(&self, chained: bool) -> Result<Arc<S3>, Error> {
        let mut built = self.built.lock().unwrap_or_else(PoisonError::into_inner);

        if let Some(current) = built.as_ref() {
            if current.chained == chained {
                return Ok(Arc::clone(&current.s3));
            }
        }

        let region = self.region.clone();
        let endpoint = self.endpoint.clone();
        let provider = (!chained).then(|| FromPython {
            current: Arc::clone(&self.current),
        });

        let bucket = self.bucket.clone();
        let key = self.key.clone();
        let format = self.format;
        let timeout = self.timeout;
        let tls = self.tls.clone();

        let s3 = runtime::block_on(async move {
            let mut loader = aws_config::defaults(BehaviorVersion::latest());

            if let Some(region) = region {
                loader = loader.region(Region::new(region));
            }

            if let Some(endpoint) = endpoint {
                loader = loader.endpoint_url(endpoint);
            }

            if let Some(provider) = provider {
                loader = loader
                    .credentials_provider(SharedCredentialsProvider::new(provider))
                    // The cache in front of a mutex read is the one thing
                    // that would make a rotated key invisible.
                    .identity_cache(IdentityCache::no_cache());
            }

            let config = loader.load().await;
            // `with_tls` is `with_config` with the connector replaced; both
            // force path style, which is what makes every S3-compatible
            // server work and is exactly where a private authority turns up.
            let mut s3 = match &tls {
                Some(tls) => S3::with_tls(&config, bucket, key, tls)?,
                None => S3::with_config(&config, bucket, key),
            }
            .with_timeout(timeout);

            if let Some(format) = format {
                s3 = s3.with_format(format);
            }

            Ok::<_, Error>(Arc::new(s3))
        })??;

        self.builds.fetch_add(1, Ordering::Relaxed);

        *built = Some(Built {
            chained,
            s3: Arc::clone(&s3),
        });

        Ok(s3)
    }
}
