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

#[pymodule]
fn _core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    errors::register(module)?;
    config::register(module)?;
    module.add("__doc__", "The compiled engine behind dynamic_config.")?;

    // Two numbers, because they move on two schedules: the package's
    // own, and the engine it was built against.
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add("__engine_version__", dynamic_config::VERSION)?;

    Ok(())
}
