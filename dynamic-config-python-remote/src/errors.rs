//! The two exceptions this wheel raises, and why they are not the base
//! wheel's.
//!
//! The base wheel's `RemoteError` and `AuthError` live in
//! `dynamic_config._core`, which is a *different* extension module with its
//! own copy of the class objects. Raising them from here would mean importing
//! `dynamic_config` inside this module and holding a reference to two of its
//! attributes for the life of the process — a Python-level dependency
//! expressed in Rust, where a mistake in it is an import-time crash rather
//! than a `TypeError`.
//!
//! So this module raises its own pair, and the Python facade next door
//! translates them into the base wheel's — which is the same place item 05
//! put the translation for a store written in Python, and is where a
//! traceback reads best anyway: the base exception is raised *from* this one,
//! so `__cause__` still holds the store's own report.
//!
//! What the pair preserves is the distinction that matters to a caller
//! backing off: [`StoreAuthError`] means *this credential was refused*, which
//! waiting will not fix, and [`StoreError`] means *the store is unhappy*,
//! which waiting might.

use dynamic_config::{Error, ErrorKind};
use pyo3::prelude::*;

use crate::redact::Redactor;

pyo3::create_exception!(
    _core,
    StoreError,
    pyo3::exceptions::PyException,
    "A remote store failed. Translated to `dynamic_config.RemoteError`."
);

pyo3::create_exception!(
    _core,
    StoreAuthError,
    StoreError,
    "A remote store refused the credentials. Translated to \
     `dynamic_config.AuthError`."
);

/// The exception for an engine error, with everything secret taken out of it.
///
/// `secrets` is what was resolved for *this* call — the password, the token,
/// the secret id. It is scrubbed by value on top of the URL redaction,
/// because a client library that quotes what it sent is not something this
/// crate can rule out by reading it once.
pub(crate) fn raised(error: &Error, redactor: &Redactor, secrets: &[&str]) -> PyErr {
    let message = redactor.scrub(&error.to_string(), secrets);

    match error.kind() {
        ErrorKind::Auth => StoreAuthError::new_err(message),
        _ => StoreError::new_err(message),
    }
}

/// Adds the pair to the module.
pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("StoreError", module.py().get_type::<StoreError>())?;
    module.add("StoreAuthError", module.py().get_type::<StoreAuthError>())?;

    Ok(())
}
