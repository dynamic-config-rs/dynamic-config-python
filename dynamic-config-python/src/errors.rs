//! The exception hierarchy, mirroring `ErrorKind` one for one.
//!
//! Python people read exception *types*, not error strings, so every kind
//! the Rust crate distinguishes gets a class here — under one base, so
//! `except DynamicConfigError` still catches the lot. Each instance also
//! carries `kind`, `path` and `origin` attributes, because a diagnostic
//! nobody can branch on is only half a diagnostic.
//!
//! Pydantic's own `ValidationError` is deliberately *not* translated: it
//! is the error Python users already know how to read. It only passes
//! through a scrubber (see `config.rs`), because Pydantic echoes the
//! offending input by default and this crate's rule — values stay out of
//! diagnostics — wins at the boundary.

use dynamic_config::{Error, ErrorKind, Origin};
use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyType;

create_exception!(
    _core,
    DynamicConfigError,
    PyException,
    "Base class for every error this library raises."
);
create_exception!(
    _core,
    IoError,
    DynamicConfigError,
    "A source could not be read."
);
create_exception!(
    _core,
    ParseError,
    DynamicConfigError,
    "A source was read but is not valid in its format."
);
create_exception!(
    _core,
    MissingError,
    DynamicConfigError,
    "A required value was not supplied by any source."
);
create_exception!(
    _core,
    TypeMismatchError,
    DynamicConfigError,
    "A value was supplied but cannot become the requested type."
);
create_exception!(
    _core,
    EnvError,
    DynamicConfigError,
    "An environment variable could not be interpreted."
);
create_exception!(
    _core,
    InvalidError,
    DynamicConfigError,
    "Everything parsed, but the configuration as a whole was rejected."
);
create_exception!(
    _core,
    RemoteError,
    DynamicConfigError,
    "A remote store could not be read."
);
create_exception!(
    _core,
    AuthError,
    DynamicConfigError,
    "A credential was rejected, or could not be obtained.\n\n\
     Distinct from `RemoteError`, which is the store being unreachable: \
     waiting will not fix this one."
);
create_exception!(
    _core,
    DecryptError,
    DynamicConfigError,
    "An encrypted source could not be decrypted."
);
create_exception!(
    _core,
    BackendError,
    DynamicConfigError,
    "The active backend failed for a reason of its own."
);

/// The class one `ErrorKind` maps to.
fn class_for(py: Python<'_>, kind: ErrorKind) -> Bound<'_, PyType> {
    match kind {
        ErrorKind::Io => py.get_type::<IoError>(),
        ErrorKind::Parse => py.get_type::<ParseError>(),
        ErrorKind::Missing => py.get_type::<MissingError>(),
        ErrorKind::Type => py.get_type::<TypeMismatchError>(),
        ErrorKind::Env => py.get_type::<EnvError>(),
        ErrorKind::Invalid => py.get_type::<InvalidError>(),
        ErrorKind::Remote => py.get_type::<RemoteError>(),
        ErrorKind::Auth => py.get_type::<AuthError>(),
        ErrorKind::Decrypt => py.get_type::<DecryptError>(),
        ErrorKind::Backend => py.get_type::<BackendError>(),
        // `ErrorKind` is `#[non_exhaustive]`: a kind this binding predates
        // still reaches Python as the base class rather than as a panic or
        // a wrong one. `except DynamicConfigError` keeps catching it.
        _ => py.get_type::<DynamicConfigError>(),
    }
}

/// How an [`Origin`] reads from Python: a `(kind, detail)` pair.
///
/// Structured rather than only rendered, so a caller can branch on "this
/// came from the environment" without parsing English.
pub(crate) fn origin_parts(origin: &Origin) -> (&'static str, Option<String>) {
    match origin {
        Origin::File(path) => ("file", Some(path.display().to_string())),
        Origin::Env(name) => ("env", Some(name.clone())),
        Origin::Inline => ("inline", None),
        Origin::Remote(store) => ("remote", Some(store.clone())),
        Origin::Runtime(layer) => ("runtime", Some((*layer).to_owned())),
        Origin::Unknown => ("unknown", None),
        // Also `#[non_exhaustive]`; an origin this binding predates reads
        // as unknown, which is exactly what it is from here.
        _ => ("unknown", None),
    }
}

/// Turns a crate error into the Python exception that mirrors it.
///
/// The message is the error's own `Display` — which already names the key
/// path and the source, and already excludes the offending *value*.
pub(crate) fn to_py_err(py: Python<'_>, error: &Error) -> PyErr {
    let class = class_for(py, error.kind());

    let instance = match class.call1((error.to_string(),)) {
        Ok(instance) => instance,
        // Constructing the exception is not a failure worth swallowing,
        // but it must not lose the original either.
        Err(failure) => return failure,
    };

    let (origin_kind, origin_detail) = origin_parts(error.origin());
    let attributes: [(&str, Py<PyAny>); 4] = [
        (
            "kind",
            error.kind().as_str().into_pyobject(py).unwrap().into(),
        ),
        ("path", error.path().into_pyobject(py).unwrap().into()),
        ("origin_kind", origin_kind.into_pyobject(py).unwrap().into()),
        (
            "origin",
            match origin_detail {
                Some(detail) => detail.into_pyobject(py).unwrap().into(),
                None => py.None(),
            },
        ),
    ];

    for (name, value) in attributes {
        if let Err(failure) = instance.setattr(name, value) {
            return failure;
        }
    }

    PyErr::from_value(instance)
}

/// Registers every exception class on the module.
pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add(
        "DynamicConfigError",
        module.py().get_type::<DynamicConfigError>(),
    )?;
    module.add("IoError", module.py().get_type::<IoError>())?;
    module.add("ParseError", module.py().get_type::<ParseError>())?;
    module.add("MissingError", module.py().get_type::<MissingError>())?;
    module.add(
        "TypeMismatchError",
        module.py().get_type::<TypeMismatchError>(),
    )?;
    module.add("EnvError", module.py().get_type::<EnvError>())?;
    module.add("InvalidError", module.py().get_type::<InvalidError>())?;
    module.add("RemoteError", module.py().get_type::<RemoteError>())?;
    module.add("AuthError", module.py().get_type::<AuthError>())?;
    module.add("DecryptError", module.py().get_type::<DecryptError>())?;
    module.add("BackendError", module.py().get_type::<BackendError>())?;

    Ok(())
}
