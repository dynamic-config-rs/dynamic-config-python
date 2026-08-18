//! The one place a format name becomes the engine's [`Format`], and back.
//!
//! Seven stores cross this boundary and every one of them crosses it the same
//! way: Python has a string, the engine has an enum, and the pair has to
//! agree in both directions or a document parses as the wrong thing. One
//! copy, so that adding a format is one edit rather than seven.

use dynamic_config::Format;
use pyo3::prelude::*;

/// The engine's `Format` for a name the facade validated.
///
/// The facade checks first, so that the error names the Python argument; this
/// arm is what a caller who reached a `#[pyclass]` directly gets instead.
pub(crate) fn parsed(named: &str) -> PyResult<Format> {
    match named.to_ascii_lowercase().as_str() {
        "json" => Ok(Format::Json),
        "toml" => Ok(Format::Toml),
        "yaml" | "yml" => Ok(Format::Yaml),
        "ini" => Ok(Format::Ini),
        "properties" => Ok(Format::Properties),
        other => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "{other:?} is not a document format — expected \"json\", \"toml\", \
             \"yaml\", \"ini\" or \"properties\""
        ))),
    }
}

/// The optional form, for the stores whose format may come from the key.
///
/// # Errors
///
/// If a name was given and is not a format this engine reads.
pub(crate) fn maybe(named: Option<&str>) -> PyResult<Option<Format>> {
    match named {
        Some(named) => parsed(named).map(Some),
        None => Ok(None),
    }
}

/// The name the base wheel's `Format` answers to.
pub(crate) fn named(format: Format) -> &'static str {
    // `feature()` names every variant — the wildcard-free spelling the
    // 0.7 migration guide prescribes, so the next format is additive
    // here too.
    format.feature()
}
