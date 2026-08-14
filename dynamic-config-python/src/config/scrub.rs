//! Turning a Python failure into a message that carries no values.
//!
//! Pydantic's `ValidationError` echoes the offending input by default, and
//! this boundary is the last place that can decide it does not: what
//! crosses is locations, messages and error types, with the input and the
//! context dropped.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

/// A short, value-free rendering of a Python failure.
pub(super) fn describe(py: Python<'_>, failure: &PyErr) -> String {
    let _ = py;

    failure.to_string()
}

/// Pydantic's report, minus the offending values.
///
/// `ValidationError`'s own `str()` embeds `input_value=...`, which is the
/// one place its defaults and this crate's rules disagree — and this
/// crate's rule wins at the boundary. The location, the message and the
/// error type are kept, because those are what a person fixes.
pub(super) fn scrub_validation(py: Python<'_>, failure: &PyErr) -> String {
    let value = failure.value(py);

    let Ok(reports) = value.call_method0(pyo3::intern!(py, "errors")) else {
        // Not a `ValidationError` — something the model itself raised.
        return failure.to_string();
    };

    let Ok(iterator) = reports.try_iter() else {
        return "the configuration did not validate".to_owned();
    };

    let mut lines = Vec::new();

    for report in iterator {
        let Ok(report) = report else { continue };

        let path = report
            .get_item("loc")
            .ok()
            .and_then(|location| location.try_iter().ok())
            .map(|parts| {
                parts
                    .filter_map(|part| part.ok())
                    .map(|part| part.str().map(|text| text.to_string()).unwrap_or_default())
                    .collect::<Vec<_>>()
                    .join(".")
            })
            .filter(|path| !path.is_empty())
            .unwrap_or_else(|| "(the configuration)".to_owned());

        let kind = report
            .get_item("type")
            .ok()
            .and_then(|kind| kind.extract::<String>().ok())
            .unwrap_or_default();

        let message = report
            .get_item("msg")
            .ok()
            .and_then(|message| message.extract::<String>().ok())
            .map(|message| scrub_message(&kind, message))
            .unwrap_or_else(|| "did not validate".to_owned());

        lines.push(if kind.is_empty() {
            format!("{path}: {message}")
        } else {
            format!("{path}: {message} [{kind}]")
        });
    }

    if lines.is_empty() {
        return "the configuration did not validate".to_owned();
    }

    lines.join("; ")
}

/// One report's message, minus anything a validator wrote itself.
///
/// Pydantic's own messages are value-free by construction — "Input should
/// be a valid integer" names the expectation, never the input. A message
/// under `value_error` or `assertion_error` is different in kind: it is
/// whatever the model author passed to `raise ValueError(...)`, and
/// `raise ValueError(f"invalid token {value}")` is the ordinary way to
/// write one. That text reaches `str(InvalidError)` and `.errors`, so it
/// is replaced here rather than trusted; the path and the type are kept,
/// which is what a person needs to find the field.
fn scrub_message(kind: &str, message: String) -> String {
    if matches!(kind, "value_error" | "assertion_error") {
        return "rejected by a validator (its message is not repeated here, \
                because a validator's own text can carry the value)"
            .to_owned();
    }

    message
}

/// The scrubbed error reports, as Python data.
pub(super) fn scrubbed_reports<'py>(
    py: Python<'py>,
    failure: &PyErr,
) -> Option<Bound<'py, PyList>> {
    let value = failure.value(py);
    let reports = value.call_method0(pyo3::intern!(py, "errors")).ok()?;
    let list = PyList::empty(py);

    for report in reports.try_iter().ok()? {
        let Ok(report) = report else { continue };
        let entry = PyDict::new(py);

        let kind = report
            .get_item("type")
            .ok()
            .and_then(|value| value.extract::<String>().ok())
            .unwrap_or_default();

        // `msg` is rebuilt rather than copied, for the same reason the
        // rendered message is: a custom validator writes it.
        if let Ok(message) = report.get_item("msg") {
            let scrubbed = message
                .extract::<String>()
                .map(|text| scrub_message(&kind, text))
                .unwrap_or_else(|_| "did not validate".to_owned());
            let _ = entry.set_item("msg", scrubbed);
        }

        for field in ["loc", "type", "url"] {
            if let Ok(item) = report.get_item(field) {
                // Everything except `input` and `ctx`, which carry the
                // value that must not travel.
                let _ = entry.set_item(field, item);
            }
        }

        list.append(entry).ok()?;
    }

    Some(list)
}
