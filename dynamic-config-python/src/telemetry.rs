//! What is true of a configuration right now, and how to expose it.
//!
//! Two halves, mirroring the Rust crate's:
//!
//! - a **status** — [`ConfigStatus`] and [`RemoteStatus`] — handed over as a
//!   plain dict the facade turns into a frozen dataclass, the same shape
//!   `explain()` and `check()` already use. Nothing here is a live view: a
//!   status is a snapshot of a handful of atomic loads, which is what makes
//!   it free enough to call per scrape.
//! - an **exposition**, which is the engine's own
//!   [`telemetry::Exposition`](dynamic_config::telemetry::Exposition) with a
//!   Python door on it. The metric names, the families, the absent-rather-
//!   than-zero rule and the cardinality bound are all the Rust crate's, so
//!   the two surfaces cannot drift into disagreeing about what
//!   `dynamic_config_last_success_seconds` means.
//!
//! ## `Instant` does not cross, and nothing invents a timestamp
//!
//! `ConfigStatus::loaded_at` and `RemoteStatus::last_fetch` are
//! [`std::time::Instant`]s **deliberately**: they are read as *how long
//! ago*, and a wall clock going backwards under NTP would make a fresh
//! configuration look stale. An `Instant` has no epoch — not Unix's, and
//! not `time.monotonic()`'s either, which is a different clock read from a
//! different origin in the same process — so there is no honest absolute
//! number to hand Python.
//!
//! What crosses is therefore the *elapsed* seconds, as a float, measured at
//! the moment the status was taken: `stale_for`, and `seconds_ago` on a
//! failure. That is the only reading of a monotonic instant that survives
//! the boundary, and it is the number an alert is written against anyway.
//! A `datetime` here would be a fabrication — subtracting the elapsed time
//! from `time.time()` and calling the result "when it loaded" is precisely
//! the lie the `Instant` was chosen to avoid.
//!
//! ## What may not cross
//!
//! No configured **value**, ever — a status is counts, durations and two
//! fixed enums, so there is nothing here for one to hide in. No **store
//! address**: the only string a remote source can produce for itself is
//! `describe()`, which is a URL and routinely embeds `user:password@host`,
//! and nothing in this module asks for one. And the **key path** a
//! `last_failure` carries stops at the status object: the exposition
//! renders the failure's *category* and never its path, because a path is
//! unbounded label cardinality as well as a detail an operator did not ask
//! to publish. Every metric label is the caller's own string.
//!
//! `tests/test_telemetry.py` and `tests/test_secrets.py` are where that is
//! enforced rather than merely intended.

use std::sync::{Mutex, PoisonError};

use dynamic_config::telemetry::Exposition as Rendered;
use dynamic_config::{ConfigStatus, FailureStatus, RemoteStatus};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::config::Config;

/// A [`ConfigStatus`] as the dict the facade's dataclass is built from.
pub(crate) fn config_status<'py>(
    py: Python<'py>,
    status: &ConfigStatus,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);

    dict.set_item("generation", status.generation)?;
    // `loaded_at` itself does not cross; see the module docs.
    dict.set_item(
        "stale_for",
        status.stale_for().map(|elapsed| elapsed.as_secs_f64()),
    )?;
    dict.set_item(
        "last_reason",
        status.last_reason.as_ref().map(|reason| reason.as_str()),
    )?;
    dict.set_item("last_failure", failure(py, status.last_failure.as_ref())?)?;
    dict.set_item("consecutive_failures", status.consecutive_failures)?;
    // Sent rather than recomputed in Python. It is one comparison, but a
    // second spelling of a rule is how two surfaces come to disagree about
    // it — the same argument the Rust side makes for `RemoteStatus`
    // reusing `FailureStatus` instead of growing a vocabulary of its own.
    dict.set_item("is_healthy", status.is_healthy())?;

    Ok(dict)
}

/// A [`RemoteStatus`] as the dict the facade's dataclass is built from.
pub(crate) fn remote_status<'py>(
    py: Python<'py>,
    status: &RemoteStatus,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);

    dict.set_item("fetches", status.fetches)?;
    dict.set_item(
        "stale_for",
        status.stale_for().map(|elapsed| elapsed.as_secs_f64()),
    )?;
    dict.set_item(
        "last_fetch_duration",
        status
            .last_fetch_duration
            .map(|elapsed| elapsed.as_secs_f64()),
    )?;
    dict.set_item("last_failure", failure(py, status.last_failure.as_ref())?)?;
    dict.set_item("consecutive_failures", status.consecutive_failures)?;
    // Three states, and the third is the point: `None` before the store has
    // been asked anything at all. Computed by the engine for the reason
    // `is_healthy` is — and this one is not obvious, which makes a second
    // implementation of it a real risk rather than a theoretical one.
    dict.set_item("reachable", status.reachable())?;

    Ok(dict)
}

/// A [`FailureStatus`] as a dict, or `None` where there has not been one.
///
/// The category and the key path, and no message: an `Error`'s `Display` is
/// value-free by policy, but a struct that carried free text would be one
/// careless construction away from putting a value in every log line that
/// printed a status. This is the same three fields the Rust struct has.
fn failure<'py>(
    py: Python<'py>,
    failure: Option<&FailureStatus>,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    let Some(failure) = failure else {
        return Ok(None);
    };

    let dict = PyDict::new(py);

    dict.set_item("kind", failure.kind.as_str())?;
    dict.set_item("path", &failure.path)?;
    dict.set_item("seconds_ago", failure.at.elapsed().as_secs_f64())?;

    Ok(Some(dict))
}

/// One or more configurations' status, as a Prometheus text body.
///
/// A thin door on the engine's own `Exposition`: every family, every name
/// and every absent-rather-than-zero decision is the Rust crate's, and this
/// crate adds no metrics ecosystem of its own — the text format is a wire
/// encoding, not a dependency, which is exactly the discipline that let the
/// Rust `telemetry` feature ship with no crates behind it.
///
/// Statuses are taken at `add` time and the durations are measured at
/// `render`, so the seconds in the body are as fresh as the response is.
#[pyclass(module = "dynamic_config._core", name = "Exposition", frozen)]
pub(crate) struct Exposition {
    /// `frozen` with the state behind a lock rather than a `#[pyclass]`
    /// with interior mutability: this module declares
    /// `Py_MOD_GIL_NOT_USED`, so every class in it has to be `Sync` on its
    /// own terms rather than on the GIL's.
    inner: Mutex<Rendered>,
}

impl Exposition {
    fn with<R>(&self, apply: impl FnOnce(&mut Rendered) -> R) -> R {
        apply(&mut self.inner.lock().unwrap_or_else(PoisonError::into_inner))
    }
}

#[pymethods]
impl Exposition {
    /// An empty exposition: `Exposition()`. No parameters.
    #[new]
    fn new() -> Self {
        Self {
            inner: Mutex::new(Rendered::new()),
        }
    }

    /// Adds one configuration's series: `add(labels, config)`.
    ///
    /// The configuration is asked for its status here, not held: what the
    /// exposition keeps is the snapshot, so a scrape cannot be affected by
    /// a reload landing between two `add` calls.
    ///
    /// Parameters:
    ///     labels: `(name, value)` pairs put on every series this call
    ///         adds. The caller names the configuration — nothing here
    ///         does it for them, because the only string a configuration
    ///         has for itself is a section key that means nothing outside
    ///         the process.
    ///     config: the configuration to read the status of.
    fn add(&self, py: Python<'_>, labels: Vec<(String, String)>, config: &Config) -> PyResult<()> {
        let status = config.core_status(py)?;

        self.with(|exposition| exposition.add_with(&borrowed(&labels), &status));

        Ok(())
    }

    /// Adds one configuration's remote series: `add_remote(labels, config)`.
    ///
    /// Parameters:
    ///     labels: as `add` — and usually the *same* labels that call was
    ///         given, so the two halves join in a query: *the store
    ///         answered* beside *the document installed* is the pair an
    ///         operator is comparing.
    ///     config: the configuration whose store to report on.
    fn add_remote(&self, labels: Vec<(String, String)>, config: &Config) {
        let status = config.core_remote_status();

        self.with(|exposition| exposition.add_remote_with(&borrowed(&labels), &status));
    }

    /// The exposition, as a Prometheus text body. No parameters.
    fn render(&self) -> String {
        self.with(|exposition| exposition.render())
    }

    fn __repr__(&self) -> String {
        // No labels and no numbers: a `repr` lands in a log line by
        // accident, and the labels are a caller's strings this crate has
        // made no promises about.
        "<dynamic_config.Exposition>".to_owned()
    }
}

/// The label pairs as the engine takes them.
fn borrowed(labels: &[(String, String)]) -> Vec<(&str, &str)> {
    labels
        .iter()
        .map(|(name, value)| (name.as_str(), value.as_str()))
        .collect()
}

/// Registers the classes on the module.
pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Exposition>()?;

    Ok(())
}
