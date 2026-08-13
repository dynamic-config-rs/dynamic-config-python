//! A remote store implemented in Python.
//!
//! The engine's [`RemoteSource`] is a Rust trait, and the whole of this
//! module is the shim that lets a plain Python object satisfy it:
//!
//! ```text
//! refresh_remote()   Python thread, GIL released (py.detach)
//!     → Remote::refresh()                          no GIL, no lock held
//!     → PyRemoteSource::fetch()                    re-attaches
//!     → the Python object's fetch()                GIL, for its own duration
//!     → (text, format) → Fetched                   GIL dropped again
//! ```
//!
//! ## The GIL is not held across the network call
//!
//! The design note this implements proposed a worker thread and a channel,
//! on the reasoning that calling Python from the fetch path holds the GIL
//! for the length of an HTTP request. Measured, that is not what happens:
//! a Python `fetch()` doing I/O releases the GIL itself — `socket.recv`,
//! `time.sleep` and every stdlib blocking call do — and a CPU-bound one is
//! pre-empted at the switch interval like any other Python thread.
//! `tests/test_remote.py::test_a_python_fetch_does_not_stop_other_threads`
//! is that measurement, and it is why there is no worker thread here.
//!
//! What the worker would have bought is a *timeout*: a channel `recv`
//! could give up on a Python `fetch()` that never returns. It would not
//! have bought a cure — the abandoned thread is still running Python, and
//! a second refresh queues behind it or spawns another — so the honest
//! contract is the one Python already has: **the timeout belongs to the
//! `fetch()` implementation**, where the client that can honour it lives.
//!
//! What it *would* have cost is a deadlock: a `fetch()` that calls back
//! into the extension would be waiting on the worker thread it is running
//! on. Calling straight through has no such edge, so a `fetch()` may read
//! `current()`, `snapshot()` or `explain()` freely. The one thing it may
//! not do is drive the refresh it is answering, and that is refused by
//! name rather than left to recurse until the stack ends.

use std::cell::Cell;
use std::sync::{Mutex, PoisonError, Weak};

use dynamic_config::{Error, Fetched, Format, RemoteSource};
use pyo3::prelude::*;
use pyo3::{PyTraverseError, PyVisit};

use crate::errors;

thread_local! {
    /// Whether this thread is inside a Python `fetch()`.
    ///
    /// The engine calls `fetch` on whichever thread asked for the refresh,
    /// so *inside a fetch* is a per-thread fact — and a thread-local stays
    /// a per-thread fact on a free-threaded interpreter too, where a
    /// `static` would not.
    static FETCHING: Cell<bool> = const { Cell::new(false) };
}

/// Whether this thread is inside a Python `fetch()`.
pub(crate) fn fetching() -> bool {
    FETCHING.with(Cell::get)
}

/// Sets the in-fetch flag for as long as it lives.
///
/// A guard rather than a pair of calls: `fetch()` returns through an error
/// path as often as through the happy one, and a flag left standing would
/// refuse every later refresh on that thread.
struct Fetching;

impl Fetching {
    fn enter() -> Self {
        FETCHING.with(|flag| flag.set(true));

        Self
    }
}

impl Drop for Fetching {
    fn drop(&mut self) {
        FETCHING.with(|flag| flag.set(false));
    }
}

/// The Python object a configuration fetches from, owned by the
/// configuration rather than by the shim the engine holds.
///
/// That placement is the whole reason this type exists. The engine's
/// `Remote` is leaked `&'static` (see `Layers`), so a `Py<PyAny>` stored
/// *inside* the shim would be immortal — and the ordinary shape of a
/// Python source is one that holds the configuration it feeds, which
/// would close a cycle through an immortal `static` that no `tp_traverse`
/// could ever reach. Here the object is an edge `Config` reports from
/// `__traverse__` and drops from `__clear__`, exactly like a hook.
#[derive(Default)]
pub(crate) struct PySource {
    object: Mutex<Option<Py<PyAny>>>,
    /// The exception the most recent `fetch` raised, held only until the
    /// boundary attaches it to the error it raises.
    ///
    /// The exception's *message* never travels — a store's exception
    /// routinely carries the URL it called, and this crate's rule is that
    /// values stay out of diagnostics. The exception object does, as
    /// `__cause__`, which is where Python already teaches people to look
    /// for the traceback.
    failure: Mutex<Option<Py<PyAny>>>,
}

impl PySource {
    /// Installs the object every later fetch calls.
    pub(crate) fn install(&self, object: Py<PyAny>) {
        *self.object.lock().unwrap_or_else(PoisonError::into_inner) = Some(object);
        self.clear_failure();
    }

    /// Drops the object and any kept exception — what `release()` and
    /// `__clear__` call.
    pub(crate) fn clear(&self) {
        let _ = self
            .object
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        self.clear_failure();
    }

    fn clear_failure(&self) {
        let _ = self
            .failure
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
    }

    /// The exception the last fetch raised, taken.
    pub(crate) fn take_failure(&self) -> Option<Py<PyAny>> {
        self.failure
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take()
    }

    /// The edge `Config::__traverse__` reports.
    ///
    /// The kept exception is deliberately not visited: it lives for the
    /// few microseconds between a failed fetch and the raise that attaches
    /// it, and a traverse that reported an edge which had since been taken
    /// would be reporting an edge that does not exist.
    pub(crate) fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        // A fetch may be running on another thread, and a traverse that
        // blocked on the lock would block the collector — the same reason
        // the hook list is visited under `try_lock`.
        let Ok(object) = self.object.try_lock() else {
            return Ok(());
        };

        match object.as_ref() {
            Some(object) => visit.call(object),
            None => Ok(()),
        }
    }

    /// Keeps `failure` and renders the categorised error that replaces it.
    fn record(&self, py: Python<'_>, failure: &PyErr, description: &str) -> Error {
        let raised = failure
            .get_type(py)
            .name()
            .map(|name| name.to_string())
            .unwrap_or_else(|_| "an exception".to_owned());

        *self.failure.lock().unwrap_or_else(PoisonError::into_inner) =
            Some(failure.value(py).clone().unbind().into_any());

        let message = format!(
            "the Python remote source `{description}` raised {raised}; its \
             message is not repeated here, because a store's exception \
             routinely carries the URL it called, a token or the document \
             itself — the exception is attached to this one as `__cause__`"
        );

        // A source that classifies its own 401 says so by raising
        // `AuthError`, and the distinction survives the crossing: `Remote`
        // may fix itself by waiting, `Auth` will not, and that is exactly
        // what a caller backing off needs to tell apart.
        if failure.is_instance_of::<errors::AuthError>(py) {
            Error::auth(message)
        } else {
            Error::remote(message)
        }
    }
}

/// The engine's view of a Python store.
pub(crate) struct PyRemoteSource {
    /// Weak, so the leaked `&'static Remote` this is installed in never
    /// keeps a Python object — or a whole configuration — alive.
    slot: Weak<PySource>,
    /// `describe()`, asked once at install.
    ///
    /// The engine calls `describe()` on the **load** path — it is where a
    /// remote value's provenance comes from — and a load runs with the GIL
    /// released, on a watcher thread as often as on a caller's. Asking
    /// Python there would put a re-entry, and a possible Python exception,
    /// on every load of every configuration that has a remote source. A
    /// store's name does not change; charging every load for the
    /// possibility that it might is not a trade worth making.
    description: String,
}

impl PyRemoteSource {
    pub(crate) fn new(slot: Weak<PySource>, description: String) -> Self {
        Self { slot, description }
    }
}

impl RemoteSource for PyRemoteSource {
    fn fetch(&self) -> Result<Fetched, Error> {
        let Some(slot) = self.slot.upgrade() else {
            return Err(Error::remote(format!(
                "`{}` was installed on a configuration that has since been \
                 dropped",
                self.description
            )));
        };

        // Nothing above this point holds a lock: `Remote::refresh` cloned
        // the source out of its own state lock before calling, and the
        // object below is cloned out of its slot before Python is entered.
        // That is what lets a `fetch()` call back into the extension.
        Python::attach(|py| {
            let source = slot
                .object
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .as_ref()
                .map(|object| object.clone_ref(py));

            let Some(source) = source else {
                return Err(Error::remote(format!(
                    "`{}` has been released; install a source with \
                     `remote(...)` before refreshing",
                    self.description
                )));
            };

            slot.clear_failure();

            let _fetching = Fetching::enter();

            match source.bind(py).call_method0(pyo3::intern!(py, "fetch")) {
                Ok(answer) => fetched(&answer, &self.description),
                Err(failure) => Err(slot.record(py, &failure, &self.description)),
            }
        })
    }

    fn describe(&self) -> String {
        self.description.clone()
    }
}

/// What `fetch()` answered, as the engine's `Fetched`.
///
/// Every refusal here names a *type*, never a value: the document is the
/// configuration, and a message quoting it would be the leak this crate
/// exists to prevent.
fn fetched(answer: &Bound<'_, PyAny>, description: &str) -> Result<Fetched, Error> {
    let (text, format) = answer
        .extract::<(Bound<'_, PyAny>, Bound<'_, PyAny>)>()
        .map_err(|_| {
            Error::remote(format!(
                "`{description}`: fetch() must return a (text, format) pair, \
                 not {}",
                type_name(answer)
            ))
        })?;

    let text = text.extract::<String>().map_err(|_| {
        Error::remote(format!(
            "`{description}`: the document fetch() returns must be a str, not \
             {}",
            type_name(&text)
        ))
    })?;

    Ok(Fetched::new(text, format_of(&format, description)?))
}

/// The `Format` a `Format` member — or a plain string — names.
fn format_of(value: &Bound<'_, PyAny>, description: &str) -> Result<Format, Error> {
    // `Format` is a `str` enum, so the first arm answers for it and for a
    // caller who wrote `"json"`; the second is for an enum that is not one.
    let named = value.extract::<String>().or_else(|_| {
        value
            .getattr(pyo3::intern!(value.py(), "value"))
            .and_then(|inner| inner.extract::<String>())
    });

    let Ok(named) = named else {
        return Err(Error::remote(format!(
            "`{description}`: the second half of fetch()'s answer must be a \
             Format, not {}",
            type_name(value)
        )));
    };

    match named.to_ascii_lowercase().as_str() {
        "json" => Ok(Format::Json),
        "toml" => Ok(Format::Toml),
        "yaml" | "yml" => Ok(Format::Yaml),
        other => Err(Error::remote(format!(
            "`{description}`: {other:?} is not a document format — expected \
             Format.JSON, Format.TOML or Format.YAML"
        ))),
    }
}

/// A type's name, for a message that must not carry the value.
fn type_name(object: &Bound<'_, PyAny>) -> String {
    object
        .get_type()
        .name()
        .map(|name| name.to_string())
        .unwrap_or_else(|_| "something else".to_owned())
}
