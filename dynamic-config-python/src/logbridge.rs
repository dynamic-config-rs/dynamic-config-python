//! The engine's stderr lines, delivered to `logging` instead.
//!
//! The engine's watcher and recovery diagnostics were `eprintln!` to fd 2:
//! invisible to `logging.basicConfig`, unfilterable by `warnings`, raw
//! plain text inside anybody's structured stream, and emitted from a Rust
//! thread so they could interleave mid-line. This module installs the
//! engine's log sink and forwards every line to
//! `logging.getLogger("dynamic_config.engine")` as a real `LogRecord`.
//!
//! The shape is dictated by one hazard: **the sink runs on engine
//! threads, sometimes while a reload hook already holds the GIL on that
//! same thread.** So the sink itself does no Python at all — it pushes
//! into a bounded channel and returns — and one forwarder thread, which
//! holds no engine locks, is the only place the GIL is taken. Overflow
//! drops the line and counts it; the next delivered record says how many
//! were dropped, so silence is never silent.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError};
use std::sync::{Mutex, OnceLock};

use pyo3::prelude::*;
use pyo3::types::PyModule;

use dynamic_config::LogLevel;

/// Lines waiting for the forwarder. Bounded: a stalled interpreter must
/// not turn reload diagnostics into unbounded memory.
const CAPACITY: usize = 1024;

type Line = (LogLevel, String);

static DROPPED: AtomicUsize = AtomicUsize::new(0);
static CLOSING: AtomicBool = AtomicBool::new(false);
static SENDER: OnceLock<Mutex<Option<SyncSender<Line>>>> = OnceLock::new();

/// Installs the sink and starts the forwarder. Called from module init;
/// calling it twice is a no-op.
pub(crate) fn install() {
    let slot = SENDER.get_or_init(|| Mutex::new(None));

    let mut guard = slot
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);

    if guard.is_some() {
        return;
    }

    let (sender, receiver) = sync_channel::<Line>(CAPACITY);

    *guard = Some(sender.clone());
    drop(guard);

    std::thread::Builder::new()
        .name("dynamic-config-log-bridge".into())
        .spawn(move || forward(receiver))
        .ok();

    dynamic_config::set_log_sink(move |level, line| {
        if CLOSING.load(Ordering::Acquire) {
            return;
        }

        match sender.try_send((level, line.to_string())) {
            Ok(()) => {}
            Err(TrySendError::Full(_)) | Err(TrySendError::Disconnected(_)) => {
                DROPPED.fetch_add(1, Ordering::Relaxed);
            }
        }
    });
}

/// Stops the bridge: the engine falls back to stderr, the forwarder
/// drains what is queued and exits. Exposed to Python as the
/// `raw_stderr=True` path and wired to `atexit` so the forwarder never
/// touches a finalising interpreter.
#[pyfunction]
pub(crate) fn _stop_log_bridge() {
    CLOSING.store(true, Ordering::Release);
    dynamic_config::clear_log_sink();

    if let Some(slot) = SENDER.get() {
        // Dropping the last sender disconnects the channel; the forwarder
        // finishes the queue and returns.
        slot.lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
    }
}

/// Restarts the bridge after `_stop_log_bridge` — `raw_stderr=False`.
#[pyfunction]
pub(crate) fn _start_log_bridge() {
    CLOSING.store(false, Ordering::Release);
    install();
}

/// The engine's own volume knob, for `configure_logging(level=...)`.
/// Levels follow `logging`'s numbers: 0 and anything above 30 map to
/// warnings-only, 30 and below to everything, negative to off.
#[pyfunction]
pub(crate) fn _set_engine_log_level(level: i64) {
    let mapped = if level < 0 {
        LogLevel::Off
    } else if level > 20 {
        LogLevel::Warn
    } else {
        LogLevel::Info
    };

    dynamic_config::set_log_level(mapped);
}

/// One record through `logging`, built directly so `caplog`, filters and
/// formatters all see an ordinary record.
fn deliver(py: Python<'_>, level: LogLevel, line: &str) -> PyResult<()> {
    let logging = PyModule::import(py, "logging")?;
    let logger = logging.call_method1("getLogger", ("dynamic_config.engine",))?;

    let levelno: u8 = match level {
        LogLevel::Warn => 30,
        _ => 20,
    };

    let dropped = DROPPED.swap(0, Ordering::Relaxed);
    let message = if dropped > 0 {
        format!("{line} ({dropped} earlier line(s) dropped: the log bridge was full)")
    } else {
        line.to_string()
    };

    // `Logger.makeRecord` and `Logger.handle`: the documented seam for
    // records built by hand, and the one `caplog` hooks.
    let record = logger.call_method1(
        "makeRecord",
        (
            "dynamic_config.engine",
            levelno,
            "dynamic-config",
            0,
            message,
            py.None(),
            py.None(),
        ),
    )?;

    logger.call_method1("handle", (record,))?;

    Ok(())
}

fn forward(receiver: Receiver<Line>) {
    while let Ok((level, line)) = receiver.recv() {
        if CLOSING.load(Ordering::Acquire) {
            // Finalisation started: whatever remains is not worth a GIL
            // acquisition against a dying interpreter.
            return;
        }

        let outcome = Python::attach(|py| deliver(py, level, &line));

        // A logging failure must not kill the bridge; the next line tries
        // again, and `logging`'s own lastResort already spoke for this one.
        drop(outcome);
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(pyo3::wrap_pyfunction!(_stop_log_bridge, module)?)?;
    module.add_function(pyo3::wrap_pyfunction!(_start_log_bridge, module)?)?;
    module.add_function(pyo3::wrap_pyfunction!(_set_engine_log_level, module)?)?;

    Ok(())
}
