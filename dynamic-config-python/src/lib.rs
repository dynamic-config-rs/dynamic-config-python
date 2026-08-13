//! Python bindings for `dynamic-config`.
//!
//! The compiled half of the `dynamic_config` package: the engine, the
//! conversion, and the one place Python is entered on a reload. The
//! ergonomic half — the generic `DynamicConfig`, the decorator, the
//! asyncio bridge — is the Python facade next door, because typing,
//! introspection and event loops are all easier there and none of them
//! belong on the fast path.

use pyo3::prelude::*;

mod config;
mod convert;
mod errors;
mod remote;
mod telemetry;

// `gil_used = false` sets `Py_mod_gil = Py_MOD_GIL_NOT_USED`, which is what
// stops a free-threaded interpreter re-enabling the GIL for the whole process
// at import. It is written out even though PyO3 0.28 made it the default,
// because a claim this size should be in the source that makes it rather than
// in a dependency's default — and because the default has already moved once,
// in the other direction. `tests/test_free_threaded.py` asserts the effect
// rather than the spelling.
//
// What earns it: no `static` and no `unsendable` `#[pyclass]` in this crate,
// every class `frozen` with its mutable state behind a lock, and the suite
// green on CPython 3.14.0t. `book/src/python/free-threading.md` is the audit.
#[pymodule(gil_used = false)]
fn _core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    errors::register(module)?;
    config::register(module)?;
    telemetry::register(module)?;
    module.add("__doc__", "The compiled engine behind dynamic_config.")?;

    // Two numbers, because they move on two schedules: the package's
    // own, and the engine it was built against.
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add("__engine_version__", dynamic_config::VERSION)?;

    Ok(())
}
