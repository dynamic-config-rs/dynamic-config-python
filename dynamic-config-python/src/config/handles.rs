//! The two smaller classes: a running watcher, and a resolved section.
//!
//! Both are `frozen` and both answer with shape rather than values — a
//! `repr` lands in a log line, and neither of these has a field a value
//! could occupy.

use std::sync::Mutex;

use dynamic_config::watch::WatchHandle;
use pyo3::prelude::*;
use serde_json::Value;

use crate::convert;
use crate::errors;

/// A running watcher. Dropping or stopping it ends the watch.
#[pyclass(module = "dynamic_config._core", name = "Watch", frozen)]
pub(crate) struct Watch {
    pub(super) handle: Mutex<Option<WatchHandle>>,
}

#[pymethods]
impl Watch {
    /// Stops watching. No parameters, and idempotent.
    fn stop(&self) {
        let _ = self
            .handle
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
    }

    /// Watches for the rest of the process. No parameters.
    ///
    /// Whatever happens to this handle afterwards: the watcher outlives
    /// it deliberately, which is the shape a program that watches until
    /// it exits wants.
    fn detach(&self) {
        if let Some(handle) = self
            .handle
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take()
        {
            handle.detach();
        }
    }

    #[getter]
    /// Whether this watch is still running. No parameters.
    fn running(&self) -> bool {
        self.handle
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .is_some()
    }

    fn __repr__(&self) -> String {
        format!("<dynamic_config.Watch running={}>", self.running())
    }
}

/// The resolved section, values and provenance, without a model.

#[pyclass(module = "dynamic_config._core", name = "Snapshot", frozen)]
pub(crate) struct Snapshot {
    pub(super) inner: dynamic_config::Snapshot,
}

#[pymethods]
impl Snapshot {
    /// The resolved values as plain Python data. No parameters.
    fn to_dict(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let value = self.inner.to_value();
        let tree = value_to_json(&value);

        Ok(convert::to_py(py, &tree)?.unbind())
    }

    /// Where a value came from, in this snapshot: `source_of(path)`.
    ///
    /// Parameters:
    ///     path: the dotted path to trace.
    fn source_of(&self, path: &str) -> Option<(String, Option<String>)> {
        self.inner.source_of(path).map(|origin| {
            let (kind, detail) = errors::origin_parts(origin);

            (kind.to_owned(), detail)
        })
    }

    /// Whether this snapshot holds a value at `path`: `contains(path)`.
    ///
    /// Parameters:
    ///     path: the dotted path to look for.
    fn contains(&self, path: &str) -> bool {
        self.inner.contains(path)
    }

    /// Every dotted path that holds a value rather than a table. No
    /// parameters.
    fn leaf_paths(&self) -> Vec<String> {
        self.inner.leaf_paths()
    }

    /// The first segment of every path, deduplicated. No parameters.
    fn top_level_keys(&self) -> Vec<String> {
        self.inner.top_level_keys()
    }

    /// Whether the section resolved to nothing at all. No parameters.
    fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }

    /// Which paths differ between two snapshots: `diff(other)`.
    ///
    /// Paths, never values. `(path, kind)`, where the kind is `added`,
    /// `removed` or `changed`: structured rather than rendered, so the
    /// caller branches on it instead of parsing English.
    ///
    /// Parameters:
    ///     other: the snapshot to compare against. This one is the
    ///         *previous* state and `other` the newer, so a key only
    ///         `other` has reads as `added`.
    fn diff(&self, other: &Snapshot) -> Vec<(String, String)> {
        changes_as_pairs(self.inner.diff(&other.inner))
    }

    /// Shape, never values — the same line every diagnostic here holds.
    fn __repr__(&self) -> String {
        format!(
            "<dynamic_config.Snapshot keys={}>",
            self.inner.leaf_paths().len()
        )
    }
}

/// The crate's `Change` list, as pairs Python can branch on.
pub(super) fn changes_as_pairs(changes: Vec<dynamic_config::Change>) -> Vec<(String, String)> {
    changes
        .into_iter()
        .map(|change| (change.path, change.kind.to_string()))
        .collect()
}

/// The crate's owned value mirror as the tree the converter walks.
fn value_to_json(value: &dynamic_config::Value) -> Value {
    use dynamic_config::Value as Owned;

    match value {
        Owned::Null => Value::Null,
        Owned::Bool(boolean) => Value::Bool(*boolean),
        // `u64` before the float: the crate's integer is an `i128`, and a
        // perfectly ordinary `u64` identifier above `i64::MAX` used to
        // fall straight through to `as f64` — so `snapshot().to_dict()`
        // rounded a value the installed model kept exactly, and the two
        // public views of one snapshot disagreed. The book promises the
        // digits survive; this is where that promise is kept.
        Owned::Integer(number) => i64::try_from(*number)
            .map(Value::from)
            .or_else(|_| u64::try_from(*number).map(Value::from))
            .unwrap_or_else(|_| Value::from(*number as f64)),
        Owned::Float(number) => serde_json::Number::from_f64(*number)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        Owned::String(text) => Value::String(text.clone()),
        Owned::Array(items) => Value::Array(items.iter().map(value_to_json).collect()),
        Owned::Table(entries) => Value::Object(
            entries
                .iter()
                .map(|(key, item)| (key.clone(), value_to_json(item)))
                .collect(),
        ),
    }
}
