//! The state one configuration owns, and the protocol that installs into
//! it.
//!
//! Rust resolves, the schema validates, Python reads a cache — and the
//! ordering rules that make a Pydantic rejection behave exactly like a Rust
//! one live here: validation runs inside the engine's `validate` hook,
//! before anything installs, and the validated model is staged there and
//! published by whichever path finished the install.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock, Weak};

use dynamic_config::{Aliases, Builder, Dynamic, EnvBindings, Error, Layer, Remote};
use pyo3::prelude::*;
use pyo3::types::PyList;
use serde_json::Value;

use crate::convert;
use crate::errors;
use crate::remote::PySource;

use super::{describe, scrub_validation, scrubbed_reports};

/// What the validation closure needs — and nothing that points back at
/// the configuration, so the closure the watcher thread holds can never
/// keep the Python object alive.
pub(super) struct Shared {
    /// Whatever turns a resolved dict into an instance: Pydantic's
    /// `model_validate`, a builder for a plain dataclass, anything a
    /// caller hands over. Calling a *method by name* here would have made
    /// Pydantic the only schema this binding could ever have.
    pub(super) validate: Py<PyAny>,
    /// The tree that was validated, the model it produced, and the
    /// sequence number of that validation.
    ///
    /// The sequence is what makes committing idempotent: an install is
    /// followed by *two* commit attempts — the engine's own reload hook
    /// (which does not fire for the very first install) and the explicit
    /// one on the `init`/`reload` path — and publishing twice would fire
    /// every hook twice and bump the generation twice for one reload.
    pub(super) staged: Mutex<Option<Staged>>,
    /// Handed out by the validate hook.
    pub(super) next_sequence: AtomicU64,
    /// Pydantic's own report for the most recent refusal, scrubbed of the
    /// offending values — attached to the exception the caller sees, so a
    /// Python program can branch on `error.errors` the way it would on a
    /// `ValidationError`.
    pub(super) reports: Mutex<Option<Py<PyList>>>,
}

/// The runtime layers, which the core takes as `&'static`.
///
/// Leaked once per configuration, deliberately: the core's runtime layers
/// are shaped for the `static`s a `#[dynamic_config]` type generates, and
/// an instance is the same long-lived thing wearing a different hat. It is
/// a few hundred bytes per configuration object, never per reload — the
/// same trade the core's memoized watch names make. Building thousands of
/// configurations in a loop is the one shape that should not.
#[derive(Clone, Copy)]
pub(super) struct Layers {
    pub(super) defaults: &'static Layer,
    pub(super) overrides: &'static Layer,
    pub(super) flags: &'static Layer,
    pub(super) bindings: &'static EnvBindings,
    pub(super) aliases: &'static Aliases,
    /// The remote slot. Kept here rather than only handed to the builder,
    /// because `refresh_remote()` reaches it directly — a refresh is a
    /// fetch into this slot and nothing else, exactly as the generated
    /// Rust `refresh_remote()` is.
    pub(super) remote: &'static Remote,
}

impl Layers {
    pub(super) fn leak() -> Self {
        Self {
            defaults: Box::leak(Box::new(Layer::new())),
            overrides: Box::leak(Box::new(Layer::new())),
            flags: Box::leak(Box::new(Layer::new())),
            bindings: Box::leak(Box::new(EnvBindings::new())),
            aliases: Box::leak(Box::new(Aliases::new())),
            remote: Box::leak(Box::new(Remote::new())),
        }
    }
}

/// A configuration is a builder until something loads through it.
///
/// The ready form is an `Arc` so that a caller can *clone it out* and let
/// the lock go before doing anything slow. Holding this lock across a load
/// would be a deadlock waiting for a second thread: the loader needs the
/// GIL to validate, and a thread blocked on a Rust mutex is a thread
/// holding the GIL while it waits.
pub(super) enum Engine {
    /// Boxed: a builder carries every source it was told about, and the
    /// enum is only ever this large while one is being configured.
    Building(Box<Builder<Value>>),
    Ready(Arc<Dynamic<Value>>),
    /// Held for the instant a transition takes; never observed.
    Moving,
}

/// The generation of the *validated model*, which is what Python waits on.
///
/// Deliberately not the engine's own generation: a reload that resolves
/// but fails validation moves the engine and must not wake a reader.
pub(super) struct Wake {
    pub(super) generation: Mutex<u64>,
    pub(super) changed: Condvar,
}

/// One validated tree, waiting to be published.
pub(super) struct Staged {
    pub(super) sequence: u64,
    pub(super) tree: Value,
    pub(super) model: Py<PyAny>,
}

pub(super) struct Inner {
    pub(super) key: String,
    /// The sequence of the most recently published model; a commit for
    /// anything at or below it has already happened.
    pub(super) last_committed: AtomicU64,
    pub(super) shared: Arc<Shared>,
    pub(super) engine: Mutex<Engine>,
    /// The published model. The read path, and the only lock `current()`
    /// takes.
    pub(super) cache: RwLock<Option<Py<PyAny>>>,
    pub(super) wake: Wake,
    pub(super) hooks: Mutex<Vec<(u64, Py<PyAny>)>>,
    pub(super) next_hook: AtomicU64,
    pub(super) layers: Layers,
    /// The Python object a remote fetch calls, if one was installed.
    ///
    /// Held here rather than inside the shim the engine owns: that shim
    /// lives in a leaked `&'static Remote`, so anything Python it held
    /// would never be freed. See `remote::PySource`.
    pub(super) source: Arc<PySource>,
}

impl Inner {
    /// Converts and validates `tree`, returning the model instance.
    ///
    /// The GIL is taken here and nowhere else on the load path.
    pub(super) fn validate(shared: &Shared, tree: &Value) -> Result<(), Error> {
        Python::attach(|py| {
            let data = convert::to_py(py, tree)
                .map_err(|failure| Error::invalid(describe(py, &failure)))?;

            match shared.validate.bind(py).call1((data,)) {
                Ok(instance) => {
                    let sequence = shared.next_sequence.fetch_add(1, Ordering::SeqCst) + 1;

                    *shared
                        .staged
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(Staged {
                        sequence,
                        tree: tree.clone(),
                        model: instance.unbind(),
                    });

                    Ok(())
                }
                Err(failure) => {
                    *shared
                        .reports
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner) =
                        scrubbed_reports(py, &failure).map(pyo3::Bound::unbind);

                    Err(Error::invalid(scrub_validation(py, &failure)))
                }
            }
        })
    }

    /// Publishes the model that belongs to `tree` — once per install.
    ///
    /// Both commit paths call this: the engine's reload hook, and the
    /// explicit one after `init`/`reload` returns. Whichever arrives
    /// first publishes; the other finds the sequence already committed
    /// and does nothing, which is what keeps one reload to one generation
    /// and one round of hooks.
    pub(super) fn commit(&self, py: Python<'_>, tree: &Value) -> PyResult<()> {
        let (sequence, model) = {
            let staged = self
                .shared
                .staged
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);

            match staged.as_ref() {
                Some(pending) if pending.tree == *tree => {
                    // Claimed, not checked-then-claimed: the two commit
                    // paths for one install both reach here, and a read
                    // followed by a later store lets both of them win —
                    // one reload, two generations, every hook twice. The
                    // GIL happens to serialise this today; the invariant
                    // should not depend on that, and free-threaded
                    // CPython is on the roadmap.
                    if self
                        .last_committed
                        .fetch_max(pending.sequence, Ordering::SeqCst)
                        >= pending.sequence
                    {
                        // Already published by the other path.
                        return Ok(());
                    }

                    (pending.sequence, pending.model.clone_ref(py))
                }
                // A concurrent load staged something else in the window
                // between this install's validation and its commit. Rare,
                // and publishing the wrong model would be worse than
                // validating once more.
                _ => {
                    drop(staged);
                    Self::validate(&self.shared, tree)
                        .map_err(|error| errors::to_py_err(py, &error))?;

                    match self
                        .shared
                        .staged
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner)
                        .as_ref()
                    {
                        Some(pending) => (pending.sequence, pending.model.clone_ref(py)),
                        None => return Ok(()),
                    }
                }
            }
        };

        // A no-op for the claimed arm above (`fetch_max` already stored
        // it); the re-validation arm arrives here without having claimed.
        self.last_committed.fetch_max(sequence, Ordering::SeqCst);

        let previous = self
            .cache
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .replace(model.clone_ref(py));

        {
            let mut generation = self
                .wake
                .generation
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            *generation += 1;
            self.wake.changed.notify_all();
        }

        self.run_hooks(py, previous.as_ref(), &model);

        Ok(())
    }

    /// Every registered hook, each isolated from the others.
    ///
    /// A raising hook is reported through Python's unraisable channel and
    /// the rest still run — the crate's panic-isolation contract, in the
    /// vocabulary Python already uses for callbacks that cannot propagate.
    pub(super) fn run_hooks(
        &self,
        py: Python<'_>,
        previous: Option<&Py<PyAny>>,
        current: &Py<PyAny>,
    ) {
        let hooks: Vec<Py<PyAny>> = self
            .hooks
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .iter()
            .map(|(_, hook)| hook.clone_ref(py))
            .collect();

        let old = match previous {
            Some(model) => model.clone_ref(py),
            None => py.None(),
        };

        for hook in hooks {
            if let Err(failure) = hook.bind(py).call1((old.bind(py), current.bind(py))) {
                failure.write_unraisable(py, Some(hook.bind(py)));
            }
        }
    }

    /// The engine, built on first use — with the lock released before the
    /// caller does anything with it.
    ///
    /// Every slow path (a load, a reload, a watch start) runs *outside*
    /// this lock, and that is not tidiness: the loader takes the GIL to
    /// validate, so a thread that blocked on this mutex while holding the
    /// GIL would stop the lock's owner from ever finishing.
    pub(super) fn dynamic(
        &self,
        py: Python<'_>,
        this: &Arc<Inner>,
    ) -> PyResult<Arc<Dynamic<Value>>> {
        let mut engine = self
            .engine
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);

        if matches!(*engine, Engine::Building(_)) {
            let Engine::Building(builder) = std::mem::replace(&mut *engine, Engine::Moving) else {
                unreachable!("just checked")
            };

            let dynamic = Dynamic::new(*builder);

            // The watcher installs through the engine, so the commit for a
            // watch-driven reload has to ride the engine's own hook. Weak,
            // because the hook lives inside the `Dynamic` this `Inner`
            // owns — an `Arc` here would be a cycle that never frees.
            let weak: Weak<Inner> = Arc::downgrade(this);
            dynamic.on_reload(move |_previous, current| {
                let Some(inner) = weak.upgrade() else {
                    return;
                };

                Python::attach(|py| {
                    if let Err(failure) = inner.commit(py, current) {
                        failure.write_unraisable(py, None);
                    }
                });
            });

            *engine = Engine::Ready(Arc::new(dynamic));
        }

        let _ = py;

        match &*engine {
            Engine::Ready(dynamic) => Ok(Arc::clone(dynamic)),
            _ => Err(errors::BackendError::new_err(
                "this configuration is being reconfigured on another thread",
            )),
        }
    }

    /// Applies a fluent builder call, which is only meaningful before the
    /// first load.
    pub(super) fn configure(
        &self,
        py: Python<'_>,
        apply: impl FnOnce(Builder<Value>) -> Builder<Value>,
    ) -> PyResult<()> {
        let mut engine = self
            .engine
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);

        match std::mem::replace(&mut *engine, Engine::Moving) {
            Engine::Building(builder) => {
                *engine = Engine::Building(Box::new(apply(*builder)));

                Ok(())
            }
            other => {
                let already_loaded = matches!(other, Engine::Ready(_));
                *engine = other;
                let _ = py;

                Err(errors::BackendError::new_err(if already_loaded {
                    "the sources cannot change after the first load; build a \
                     second configuration instead"
                } else {
                    "this configuration is being reconfigured on another thread"
                }))
            }
        }
    }
}
