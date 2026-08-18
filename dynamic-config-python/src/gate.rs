//! The finalization gate: no thread attaches to a dying interpreter.
//!
//! The classic embedding crash, with its window actually closed. A
//! watcher thread that calls `Python::attach` after `Py_Finalize` has
//! begun is undefined behaviour — the GIL build used to park such a
//! thread forever, and the free-threaded build segfaults allocating the
//! new thread state. Checking a "finalizing" flag before attaching only
//! *shrinks* the window: the flag can flip between the check and the
//! attach.
//!
//! What closes it is a counter alongside the flag. Every attach from a
//! non-Python thread rides [`attach`]: increment first, then check the
//! flag — back out if it is up. The `atexit` half raises the flag and
//! then waits for the counter to reach zero. SeqCst on both sides, so
//! either the entrant sees the flag or the waiter sees the entrant;
//! there is no interleaving in which a thread slips through and attaches
//! after the wait returns. `atexit` runs while the interpreter is whole,
//! so everything the wait lets finish was safe to finish.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use pyo3::prelude::*;

static FINALIZING: AtomicBool = AtomicBool::new(false);
static IN_PYTHON: AtomicUsize = AtomicUsize::new(0);

/// How long the exit handler waits for in-flight Python work. A hook
/// wedged inside Python for longer than this was never going to finish;
/// proceeding then is the old behaviour, once, with a warning.
const DRAIN: Duration = Duration::from_secs(5);

/// [`Python::attach`], refused once finalization has begun.
///
/// `None` means the interpreter is on its way out and the caller should
/// treat the work as skipped — a reload not validated, a line not
/// delivered. Nothing is worth attaching to a dying interpreter for.
pub(crate) fn attach<R>(f: impl for<'py> FnOnce(Python<'py>) -> R) -> Option<R> {
    IN_PYTHON.fetch_add(1, Ordering::SeqCst);

    if FINALIZING.load(Ordering::SeqCst) {
        IN_PYTHON.fetch_sub(1, Ordering::SeqCst);
        return None;
    }

    let result = Python::attach(f);
    IN_PYTHON.fetch_sub(1, Ordering::SeqCst);

    Some(result)
}

/// The `atexit` half: raises the flag, then waits out whoever is inside.
#[pyfunction]
pub(crate) fn _interpreter_closing(py: Python<'_>) {
    FINALIZING.store(true, Ordering::SeqCst);

    // The threads being waited for need the interpreter: a validate is
    // *in* Python right now. Holding our attach while they finish would
    // deadlock a GIL build outright, so release it for the wait.
    py.detach(|| {
        let deadline = Instant::now() + DRAIN;

        while IN_PYTHON.load(Ordering::SeqCst) != 0 {
            if Instant::now() >= deadline {
                eprintln!(
                    "[dynamic-config] a thread is still inside Python {DRAIN:?} \
                     after exit began; proceeding with finalization"
                );
                return;
            }

            std::thread::sleep(Duration::from_millis(1));
        }
    });
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(pyo3::wrap_pyfunction!(_interpreter_closing, module)?)?;

    Ok(())
}
