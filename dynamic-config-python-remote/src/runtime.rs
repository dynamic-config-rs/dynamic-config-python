//! The one tokio runtime this wheel owns.
//!
//! ## Why there is one, and why the module owns it
//!
//! etcd speaks gRPC, so its client is async and its `fetch` is a future.
//! Something has to drive it. A runtime per store would be a thread pool per
//! store; a runtime per fetch would be two thread spawns per configuration
//! reload. So: one, lazily, for the process.
//!
//! It is created on the first *store construction* rather than on import or
//! on the first fetch, and that placement is deliberate — it is the only
//! moment a user can observe. `import dynamic_config_remote` starts no
//! threads, which `tests/test_runtime.py` asserts through [`started`];
//! constructing a store that needs a runtime starts it, and constructing one
//! that does not (Vault is `ureq`, blocking, runtime-free) still does not.
//!
//! ## Why it is never shut down
//!
//! `Runtime::drop` blocks until its workers park. Registering that at
//! `atexit` would put a join between the interpreter and its own exit —
//! behind whatever a worker happens to be doing, which for a store is a
//! network read with a ten-second deadline on it. The base wheel already
//! registers an `atexit` sweep for `release()`; a second one that can block
//! for ten seconds would turn a clean exit into a hang, and the two would be
//! ordered by registration accident.
//!
//! So the runtime lives in a `OnceLock` and is never dropped. That is safe
//! *here* for a reason worth stating rather than assuming: **no task on this
//! runtime ever touches Python.** Credentials are resolved in Python, before
//! the call; the futures driven here hold nothing but Rust. A worker still
//! running while CPython finalises therefore cannot re-enter a dying
//! interpreter — which is the failure mode that makes immortal runtimes
//! dangerous in other bindings, and the one this arrangement does not have.
//!
//! The cost is honest and small: two parked threads for the life of a process
//! that asked for a remote store.

use std::future::Future;
use std::sync::OnceLock;

use dynamic_config::Error;
use tokio::runtime::{Builder, Handle, Runtime};

/// How many workers the runtime gets.
///
/// Two, not `available_parallelism()`: the work here is one HTTP/2 request
/// per configuration refresh, and a refresh happens on a watch tick rather
/// than on a request. Sizing this to the machine would put sixty-four parked
/// threads in a container that reads one key.
const WORKERS: usize = 2;

static RUNTIME: OnceLock<Runtime> = OnceLock::new();

/// The runtime, starting it if this is the first store to need one.
///
/// # Errors
///
/// If the runtime cannot be built — which in practice means the process
/// cannot spawn threads.
pub(crate) fn runtime() -> Result<&'static Runtime, Error> {
    if let Some(started) = RUNTIME.get() {
        return Ok(started);
    }

    let built = Builder::new_multi_thread()
        .worker_threads(WORKERS)
        .thread_name("dynamic-config-remote")
        .enable_all()
        .build()
        .map_err(|error| {
            Error::remote(format!("the remote store runtime could not start: {error}"))
        })?;

    // A racing thread may have won; then `built` is dropped here, which is
    // the one place a runtime *is* dropped in this module. It is empty and
    // has never been entered, so the drop parks two fresh threads and
    // returns.
    let _ = RUNTIME.set(built);

    RUNTIME
        .get()
        .ok_or_else(|| Error::remote("the remote store runtime could not start".to_owned()))
}

/// Whether the runtime has been started.
///
/// Exists for the test that asserts importing this package starts no threads.
pub(crate) fn started() -> bool {
    RUNTIME.get().is_some()
}

/// Drives `future` to completion on this wheel's runtime.
///
/// Called with the GIL released, always — every caller wraps it in
/// `py.detach`.
///
/// The `Handle::try_current()` branch is the case the design note asked for.
/// If this thread is already inside a runtime — a Rust program embedding
/// CPython, calling into the interpreter from a tokio task — then
/// `Runtime::block_on` panics with *Cannot start a runtime from within a
/// runtime*. Handing the future to this wheel's own workers and waiting on a
/// channel avoids that: it is a different runtime, so there is no reentrancy
/// and no deadlock, and the caller blocks a thread they had already chosen to
/// block by calling a synchronous API.
///
/// # Errors
///
/// If the runtime cannot start, or if the task carrying the answer was lost —
/// which can only happen if it panicked.
pub(crate) fn block_on<F, T>(future: F) -> Result<T, Error>
where
    F: Future<Output = T> + Send + 'static,
    T: Send + 'static,
{
    let runtime = runtime()?;

    if Handle::try_current().is_err() {
        return Ok(runtime.block_on(future));
    }

    let (sender, receiver) = std::sync::mpsc::sync_channel(1);

    runtime.spawn(async move {
        // A receiver that has gone away means the waiting thread unwound;
        // there is nobody left to tell.
        let _ = sender.send(future.await);
    });

    receiver.recv().map_err(|_| {
        Error::remote("the remote store task ended without an answer; it panicked".to_owned())
    })
}
