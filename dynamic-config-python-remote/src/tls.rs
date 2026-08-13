//! The shared TLS vocabulary, as a Python object.
//!
//! `dynamic_config_store_core::tls::TlsConfig` was built as **data** — paths
//! and PEM bytes, no client type anywhere in a signature — precisely so that
//! it could cross into a language that has never heard of `tonic` or `ureq`.
//! This file is that crossing, and it is short for exactly that reason:
//! everything that goes over is a `str`, a `bytes` or a path, and the
//! translation into whatever each client understands stays in the store
//! crates where it belongs.
//!
//! ## What is deliberately not bound
//!
//! `Pem`, `ClientCertificate` and `CertificateAndKey` are Rust-side plumbing:
//! they exist so a store can ask *which spelling was this* and read the
//! material once. A Python caller has no use for either question — they say
//! what they have, and the store does the rest — so binding them would add
//! three names to the surface and one more place a private key could be
//! rendered.
//!
//! ## `__repr__` is Rust's `Debug`, never a rendering of the fields
//!
//! A private key is the sharpest secret in this repository, and
//! [`TlsConfig`]'s `Debug` already prints `<redacted>` where the key is bytes
//! and the path where it is a file. Building a `repr` here from the same
//! fields would be a second implementation of that rule, in the language
//! where nobody would think to look for it — so there is one implementation
//! and this delegates to it.
//!
//! ## Two stores refuse part of it, and the refusal crosses
//!
//! NATS cannot take PEM bytes (`async-nats` opens the files itself) and S3
//! cannot take a client certificate (the AWS SDK's TLS context is a trust
//! store and nothing else). Both refusals are checked **at construction**
//! here, where the mistake was written, rather than left to the store crate's
//! own refusal at the first fetch — but the wording is the store crates',
//! through [`tls_core::unsupported`], so there is one sentence rather than
//! two that can drift.
//!
//! Refusing rather than ignoring is the whole point. A caller who named a
//! certificate authority and got the platform trust store has a program that
//! believes it is pinned and is not, which is worse than a program that will
//! not start.

use std::path::PathBuf;

use dynamic_config_store_core::tls as tls_core;
use dynamic_config_store_core::tls::TlsConfig;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// A private certificate authority and a client certificate, as data.
///
/// The Python spelling of `dynamic_config_store_core::tls::TlsConfig`, method
/// for method. Immutable: every builder answers a new one, so a
/// configuration shared between two stores cannot be changed by either.
// Not `Clone`: a `#[pyclass]` that derives it also derives `FromPyObject`,
// which would make this type extractable *by value* and put a second, silent
// copy of a private key wherever pyo3 chose to make one. The stores take
// `Option<&Tls>` and clone the `TlsConfig` inside, which is one copy in one
// place.
#[pyclass(module = "dynamic_config_remote._core", name = "TlsConfig", frozen)]
#[derive(Default)]
pub(crate) struct Tls {
    inner: TlsConfig,
}

#[pymethods]
impl Tls {
    /// An empty configuration: the platform's trust store, no client
    /// certificate.
    #[new]
    fn new() -> Self {
        Self::default()
    }

    /// Trust the certificate authority in this PEM file.
    fn with_ca_certificate_file(&self, path: PathBuf) -> Self {
        Self::from(self.inner.clone().with_ca_certificate_file(path))
    }

    /// Trust the certificate authority in these PEM bytes.
    fn with_ca_certificate_pem(&self, pem: Vec<u8>) -> Self {
        Self::from(self.inner.clone().with_ca_certificate_pem(pem))
    }

    /// Present this client certificate and private key (mTLS), as files.
    fn with_client_certificate_files(&self, certificate: PathBuf, key: PathBuf) -> Self {
        Self::from(
            self.inner
                .clone()
                .with_client_certificate_files(certificate, key),
        )
    }

    /// Present this client certificate and private key (mTLS), from bytes.
    fn with_client_certificate_pem(&self, certificate: Vec<u8>, key: Vec<u8>) -> Self {
        Self::from(
            self.inner
                .clone()
                .with_client_certificate_pem(certificate, key),
        )
    }

    /// Whether this asks for nothing at all.
    fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }

    /// Shape, from the engine's own `Debug`. Never the material.
    fn __repr__(&self) -> String {
        format!("{:?}", self.inner)
    }
}

impl From<TlsConfig> for Tls {
    fn from(inner: TlsConfig) -> Self {
        Self { inner }
    }
}

/// What a store was actually asked for, or `None` for "leave the client
/// alone".
///
/// An empty configuration is not "no TLS" — it is the platform's trust store,
/// which is what every store already did — so it resolves to `None` rather
/// than to a `with_tls` call that would build a client to change nothing.
pub(crate) fn wanted(tls: Option<&Tls>) -> Option<TlsConfig> {
    tls.map(|tls| tls.inner.clone())
        .filter(|tls| !tls.is_empty())
}

/// NATS' refusal: `async-nats` opens the files itself, so bytes cannot cross.
///
/// The obvious workaround — writing the material to a temporary file — is
/// deliberately not taken, here for the same reason it is not taken in the
/// store crate: it would put a private key on a disk that never asked for
/// one.
///
/// # Errors
///
/// If the configuration names PEM bytes for either the CA or the client
/// certificate.
pub(crate) fn nats_takes_paths(tls: &TlsConfig, described: &str) -> PyResult<()> {
    if tls.ca_certificate().is_some_and(|ca| ca.path().is_none()) {
        return Err(refused(tls_core::unsupported(
            described,
            "a certificate authority from PEM bytes",
            "`async-nats` opens the file itself; name a file with \
             `with_ca_certificate_file`",
        )));
    }

    if tls.client_certificate().is_some_and(|client| {
        client.certificate().path().is_none() || client.key().path().is_none()
    }) {
        return Err(refused(tls_core::unsupported(
            described,
            "a client certificate from PEM bytes",
            "`async-nats` opens the files itself; name files with \
             `with_client_certificate_files`",
        )));
    }

    Ok(())
}

/// S3's refusal: the SDK's TLS context has a trust store and nothing else.
///
/// The way out is the Rust crate's `from_client`, which takes a connector the
/// caller built — and there is nothing in a Python process holding an
/// `aws_sdk_s3::Client` to hand over, so the honest advice names the crate
/// rather than pretending this wheel has a spelling for it.
///
/// # Errors
///
/// If the configuration names a client certificate.
pub(crate) fn s3_takes_no_client_certificate(tls: &TlsConfig, described: &str) -> PyResult<()> {
    if tls.client_certificate().is_some() {
        return Err(refused(tls_core::unsupported(
            described,
            "a client certificate",
            "the AWS SDK's TLS context has a trust store and no \
             client-certificate slot; mTLS to an S3-compatible server means \
             building the connector, which only the Rust crate can do",
        )));
    }

    Ok(())
}

/// A refusal, as the exception a constructor argument's mistake deserves.
///
/// `ValueError` rather than this wheel's `StoreError`: nothing remote has
/// happened, the mistake is in the call, and the line it is on is where a
/// reader wants to be sent — which is the same choice `_format.py` makes for
/// a key that names no format.
fn refused(error: dynamic_config::Error) -> PyErr {
    // The message and nothing else: `Error::remote` carries no path and no
    // origin, so `Display` is exactly the sentence `unsupported` wrote. It
    // names the store (already redacted), the setting, and the way out.
    PyValueError::new_err(error.to_string())
}
