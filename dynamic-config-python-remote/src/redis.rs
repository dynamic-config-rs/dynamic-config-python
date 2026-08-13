//! Redis, as a compiled object a Python `RemoteSource` wraps.
//!
//! The simplest of the seven: one URL, one key, one `GET`. Blocking, so
//! nothing here touches the tokio runtime — a program reading only Redis
//! starts no threads at all.
//!
//! ## The credential is *in* the URL, which is what makes this different
//!
//! Redis has no login and no token: a client authenticates by sending `AUTH`
//! from what its connection string carried, and `redis::Client` is built from
//! that string. So a rotated password is not a header this store can change —
//! it is a different URL, and therefore a different client.
//!
//! That is why `user` and `password` are separate Python arguments rather
//! than something the caller writes into the URL themselves. A callable
//! cannot rotate a substring of a string somebody passed at construction; it
//! can rotate an argument. The two are spliced into the authority here, on
//! every fetch, and a pair that has not moved reuses the client and its open
//! connection — which is the difference between one connection and one per
//! refresh.
//!
//! ## Percent-encoding, because a password is not a URL
//!
//! `redis-rs` percent-*decodes* the authority it parses, so a password
//! containing `@`, `:`, `/` or a space has to be encoded on the way in or it
//! would arrive truncated — or, worse, would move the host. Everything
//! outside the unreserved set goes through [`encoded`], which is the one
//! place this wheel builds a URL rather than quoting one.
//!
//! `redis://user@host` reads the lone authority as a **user name**, which is
//! Redis' rule and not NATS'; the difference is one argument to the shared
//! redactor and is named there.

use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use dynamic_config::{Error, RemoteSource};
use dynamic_config_redis::Redis;
use dynamic_config_store_core::tls::TlsConfig;
use dynamic_config_store_core::LoneAuthority;
use pyo3::prelude::*;

use crate::errors::raised;
use crate::format;
use crate::redact::{safe, Redactor};
use crate::tls::Tls;

/// The client, and the credentials it was built with.
struct Built {
    /// `None` for a URL with no credentials spliced into it.
    credentials: Option<(String, String)>,
    /// `Arc` so the fetch can drop the lock before the network read.
    redis: Arc<Redis>,
}

/// A key in Redis, read through the Rust client.
#[pyclass(module = "dynamic_config_remote._core", name = "RedisStore", frozen)]
pub(crate) struct RedisStore {
    url: String,
    key: String,
    /// `None` when the key names no format and none was given — refused by
    /// the facade at construction.
    format: Option<dynamic_config::Format>,
    timeout: Duration,
    /// `None` for the platform's trust store. A `rediss://` URL works with
    /// no `TlsConfig` at all — the wheel is built with the crate's `tls`
    /// feature on — and this is what a *private* authority needs.
    tls: Option<TlsConfig>,
    described: String,
    redactor: Redactor,
    built: Mutex<Option<Built>>,
}

#[pymethods]
impl RedisStore {
    /// Builds the store. Connecting happens on the first fetch.
    #[new]
    #[pyo3(signature = (url, key, format, timeout, tls))]
    fn new(
        url: String,
        key: String,
        format: Option<&str>,
        timeout: f64,
        tls: Option<&Tls>,
    ) -> PyResult<Self> {
        // `redis://user:password@host` is an ordinary way to write this one,
        // so the redactor is not a formality here.
        let redactor = Redactor::new([&url], LoneAuthority::Username);
        let described = format!("redis {} key {key}", safe(&url, LoneAuthority::Username));

        Ok(Self {
            url,
            key,
            format: format::maybe(format)?,
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
        // A password with no user name is Redis' own shape — `requirepass`
        // predates ACLs, and the default user is implied — so the pair is
        // built from whichever halves are there.
        let credentials = match (user, password) {
            (None, None) => None,
            (user, password) => Some((user.unwrap_or_default(), password.unwrap_or_default())),
        };

        let secret = credentials
            .as_ref()
            .map(|(_, password)| password.clone())
            .unwrap_or_default();

        py.detach(|| self.read(credentials))
            .map_err(|error| raised(&error, &self.redactor, &[&secret]))
    }

    /// Names the store, redacted. What the engine records as provenance.
    fn describe(&self) -> String {
        self.described.clone()
    }

    fn __repr__(&self) -> String {
        format!("<{}>", self.described)
    }
}

impl RedisStore {
    /// One read, with the client rebuilt first if the credentials moved.
    fn read(&self, credentials: Option<(String, String)>) -> Result<(String, String), Error> {
        let fetched = self.client(credentials)?.fetch()?;

        Ok((fetched.text, format::named(fetched.format).to_owned()))
    }

    /// The client for these credentials, building or rebuilding as needed.
    ///
    /// The lock is held across the build so that two threads arriving
    /// together open one connection rather than two.
    fn client(&self, credentials: Option<(String, String)>) -> Result<Arc<Redis>, Error> {
        let mut built = self.built.lock().unwrap_or_else(PoisonError::into_inner);

        if let Some(current) = built.as_ref() {
            if current.credentials == credentials {
                return Ok(Arc::clone(&current.redis));
            }
        }

        let url = match &credentials {
            Some((user, password)) => authenticated(&self.url, user, password),
            None => self.url.clone(),
        };

        // Two constructors rather than a builder method, because the TLS
        // material has to be in hand when `redis::Client` is built. The store
        // crate refuses TLS material on a URL that is not `rediss://`, which
        // is the deployment that believes it is encrypted and is not.
        let mut redis = match &self.tls {
            Some(tls) => Redis::with_tls(&url, self.key.clone(), tls)?,
            None => Redis::new(&url, self.key.clone())?,
        }
        .with_timeout(self.timeout);

        if let Some(format) = self.format {
            redis = redis.with_format(format);
        }

        let redis = Arc::new(redis);

        *built = Some(Built {
            credentials,
            redis: Arc::clone(&redis),
        });

        Ok(redis)
    }
}

/// `url` with `user` and `password` in its authority, replacing any that were
/// already there.
///
/// The arguments win over the URL rather than being refused as a conflict:
/// the URL is where a deployment writes the *address*, and one that also
/// carries a credential is usually a development default somebody forgot —
/// which an explicit, rotatable argument is meant to replace.
///
/// A URL this cannot parse is returned unchanged, and the client says what is
/// wrong with it. Guessing at the shape of a malformed URL would only move
/// the error somewhere less useful.
fn authenticated(url: &str, user: &str, password: &str) -> String {
    let Some((scheme, rest)) = url.split_once("://") else {
        return url.to_owned();
    };

    // `rsplit_once`, matching the redactor: a password already in the URL may
    // itself contain `@`, and splitting on the first one would leave its tail
    // in front of the host.
    let host = rest.rsplit_once('@').map_or(rest, |(_, tail)| tail);

    format!("{scheme}://{}:{}@{host}", encoded(user), encoded(password))
}

/// One authority component, percent-encoded.
///
/// Everything outside RFC 3986's unreserved set, because `redis-rs`
/// percent-decodes what it parses: a password containing `@` or `:` that went
/// in raw would come out as a different password, or as a different host.
fn encoded(component: &str) -> String {
    let mut encoded = String::with_capacity(component.len());

    for byte in component.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                encoded.push(char::from(byte));
            }
            other => encoded.push_str(&format!("%{other:02X}")),
        }
    }

    encoded
}
