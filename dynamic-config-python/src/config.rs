//! The engine object: one configuration, owned by a Python value.
//!
//! Rust resolves; Pydantic validates; Python reads a cache. The order is
//! the whole design:
//!
//! ```text
//! reload trigger (watcher / reload() / init())
//!     → Rust: load, merge, strict checks          no GIL
//!     → Rust: resolved tree → dict                GIL, microseconds
//!     → Python: the schema's validator(dict)      GIL, once per reload
//!     → ok:  swap the cached model, bump, wake, hooks
//!     → err: the install is refused — the previous model keeps serving
//! ```
//!
//! Validation runs inside the engine's `validate` hook, which the loader
//! calls *before* it installs anything. That placement is what makes a
//! Pydantic rejection behave exactly like a Rust one: nothing installs,
//! the last-known-good cache is not written, and the previous snapshot
//! keeps serving. The validated model is staged there and published by
//! whichever path finished the install.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock, Weak};
use std::time::Duration;

use dynamic_config::watch::{WatchHandle, WatchMode};
use dynamic_config::{Aliases, Builder, CacheMode, Dynamic, EnvBindings, Error, Layer, Remote};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use pyo3::{PyTraverseError, PyVisit};
use serde_json::Value;

use crate::convert;
use crate::errors;

/// What the validation closure needs — and nothing that points back at
/// the configuration, so the closure the watcher thread holds can never
/// keep the Python object alive.
struct Shared {
    /// Whatever turns a resolved dict into an instance: Pydantic's
    /// `model_validate`, a builder for a plain dataclass, anything a
    /// caller hands over. Calling a *method by name* here would have made
    /// Pydantic the only schema this binding could ever have.
    validate: Py<PyAny>,
    /// The tree that was validated, the model it produced, and the
    /// sequence number of that validation.
    ///
    /// The sequence is what makes committing idempotent: an install is
    /// followed by *two* commit attempts — the engine's own reload hook
    /// (which does not fire for the very first install) and the explicit
    /// one on the `init`/`reload` path — and publishing twice would fire
    /// every hook twice and bump the generation twice for one reload.
    staged: Mutex<Option<Staged>>,
    /// Handed out by the validate hook.
    next_sequence: AtomicU64,
    /// Pydantic's own report for the most recent refusal, scrubbed of the
    /// offending values — attached to the exception the caller sees, so a
    /// Python program can branch on `error.errors` the way it would on a
    /// `ValidationError`.
    reports: Mutex<Option<Py<PyList>>>,
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
struct Layers {
    defaults: &'static Layer,
    overrides: &'static Layer,
    flags: &'static Layer,
    bindings: &'static EnvBindings,
    aliases: &'static Aliases,
}

impl Layers {
    fn leak() -> (Self, &'static Remote) {
        (
            Self {
                defaults: Box::leak(Box::new(Layer::new())),
                overrides: Box::leak(Box::new(Layer::new())),
                flags: Box::leak(Box::new(Layer::new())),
                bindings: Box::leak(Box::new(EnvBindings::new())),
                aliases: Box::leak(Box::new(Aliases::new())),
            },
            Box::leak(Box::new(Remote::new())),
        )
    }
}

/// A configuration is a builder until something loads through it.
///
/// The ready form is an `Arc` so that a caller can *clone it out* and let
/// the lock go before doing anything slow. Holding this lock across a load
/// would be a deadlock waiting for a second thread: the loader needs the
/// GIL to validate, and a thread blocked on a Rust mutex is a thread
/// holding the GIL while it waits.
enum Engine {
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
struct Wake {
    generation: Mutex<u64>,
    changed: Condvar,
}

/// One validated tree, waiting to be published.
struct Staged {
    sequence: u64,
    tree: Value,
    model: Py<PyAny>,
}

struct Inner {
    key: String,
    /// The sequence of the most recently published model; a commit for
    /// anything at or below it has already happened.
    last_committed: AtomicU64,
    shared: Arc<Shared>,
    engine: Mutex<Engine>,
    /// The published model. The read path, and the only lock `current()`
    /// takes.
    cache: RwLock<Option<Py<PyAny>>>,
    wake: Wake,
    hooks: Mutex<Vec<(u64, Py<PyAny>)>>,
    next_hook: AtomicU64,
    layers: Layers,
}

impl Inner {
    /// Converts and validates `tree`, returning the model instance.
    ///
    /// The GIL is taken here and nowhere else on the load path.
    fn validate(shared: &Shared, tree: &Value) -> Result<(), Error> {
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
    fn commit(&self, py: Python<'_>, tree: &Value) -> PyResult<()> {
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
    fn run_hooks(&self, py: Python<'_>, previous: Option<&Py<PyAny>>, current: &Py<PyAny>) {
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
    fn dynamic(&self, py: Python<'_>, this: &Arc<Inner>) -> PyResult<Arc<Dynamic<Value>>> {
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
    fn configure(
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

/// A short, value-free rendering of a Python failure.
fn describe(py: Python<'_>, failure: &PyErr) -> String {
    let _ = py;

    failure.to_string()
}

/// Pydantic's report, minus the offending values.
///
/// `ValidationError`'s own `str()` embeds `input_value=...`, which is the
/// one place its defaults and this crate's rules disagree — and this
/// crate's rule wins at the boundary. The location, the message and the
/// error type are kept, because those are what a person fixes.
fn scrub_validation(py: Python<'_>, failure: &PyErr) -> String {
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
fn scrubbed_reports<'py>(py: Python<'py>, failure: &PyErr) -> Option<Bound<'py, PyList>> {
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

/// One configuration: sources, storage, lifecycle and diagnostics.
#[pyclass(module = "dynamic_config._core", name = "Config", frozen)]
pub(crate) struct Config {
    inner: Arc<Inner>,
}

#[pymethods]
impl Config {
    /// Builds a configuration for `model` under the section `key`.
    ///
    /// `secrets` is the list the caller derived from the model's own
    /// fields — nobody re-declares which values are sensitive — and
    /// `fields` are the model's top-level field names, which is what
    /// `check()` needs to call a key unknown.
    #[new]
    #[pyo3(signature = (validate, key, secrets, fields))]
    fn new(
        py: Python<'_>,
        validate: Py<PyAny>,
        key: &str,
        secrets: Vec<String>,
        fields: Vec<String>,
    ) -> PyResult<Self> {
        let shared = Arc::new(Shared {
            validate,
            staged: Mutex::new(None),
            next_sequence: AtomicU64::new(0),
            reports: Mutex::new(None),
        });

        let (layers, remote) = Layers::leak();

        // `with_fields` wants names that outlive the load; the model's
        // field list is fixed at class-definition time, so one leak per
        // configuration is the whole cost.
        let field_names: &'static [&'static str] = Box::leak(
            fields
                .into_iter()
                .map(|name| &*Box::leak(name.into_boxed_str()))
                .collect::<Vec<&'static str>>()
                .into_boxed_slice(),
        );

        let secret_refs: Vec<&str> = secrets.iter().map(String::as_str).collect();
        let validating = Arc::clone(&shared);

        let builder = Builder::<Value>::new(key)
            .with_secrets(&secret_refs)
            .with_fields(field_names)
            .with_type_statics(
                layers.defaults,
                layers.overrides,
                layers.flags,
                layers.bindings,
                layers.aliases,
                remote,
                // An instance has no type-level slot to remember it in.
                |_| {},
            )
            .validate(move |tree: &Value| Inner::validate(&validating, tree));

        let _ = py;

        Ok(Self {
            inner: Arc::new(Inner {
                key: key.to_owned(),
                last_committed: AtomicU64::new(0),
                shared,
                engine: Mutex::new(Engine::Building(Box::new(builder))),
                cache: RwLock::new(None),
                wake: Wake {
                    generation: Mutex::new(0),
                    changed: Condvar::new(),
                },
                hooks: Mutex::new(Vec::new()),
                next_hook: AtomicU64::new(1),
                layers,
            }),
        })
    }

    /// The section key this configuration reads.
    #[getter]
    fn key(&self) -> &str {
        &self.inner.key
    }

    // ── Sources ────────────────────────────────────────────────────────

    fn file(&self, py: Python<'_>, path: String) -> PyResult<()> {
        self.inner.configure(py, |builder| builder.file(path))
    }

    // No `encrypted_file` here, deliberately: decryption needs a
    // `Decryptor` implementation, which is a Rust trait with no Python
    // side — and shipping `age` to make one would put a crypto stack in
    // every wheel for a door nobody could open from Python. A deployment
    // that needs it decrypts with the CLI and points this at the result.

    fn discover(&self, py: Python<'_>, name: String, paths: Vec<String>) -> PyResult<()> {
        self.inner
            .configure(py, |builder| builder.discover(name, paths))
    }

    fn env(&self, py: Python<'_>, prefix: String) -> PyResult<()> {
        self.inner.configure(py, |builder| builder.env(prefix))
    }

    fn nest(&self, py: Python<'_>, separator: String) -> PyResult<()> {
        self.inner.configure(py, |builder| builder.nest(separator))
    }

    fn allow_empty_env(&self, py: Python<'_>) -> PyResult<()> {
        self.inner.configure(py, Builder::allow_empty_env)
    }

    fn strict_env(&self, py: Python<'_>) -> PyResult<()> {
        self.inner.configure(py, Builder::strict_env)
    }

    fn env_file(&self, py: Python<'_>, path: String) -> PyResult<()> {
        self.inner.configure(py, |builder| builder.env_file(path))
    }

    fn profile_env(&self, py: Python<'_>, variable: String) -> PyResult<()> {
        self.inner
            .configure(py, |builder| builder.profile_env(variable))
    }

    fn cache(&self, py: Python<'_>, path: String, mode: &str) -> PyResult<()> {
        let mode = match mode {
            "redacted" => CacheMode::Redacted,
            "full" => CacheMode::Full,
            "fingerprint" => CacheMode::Fingerprint,
            other => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "unknown cache mode {other:?}: expected \"redacted\", \"full\" \
                     or \"fingerprint\""
                )))
            }
        };

        self.inner
            .configure(py, move |builder| builder.cache(path, mode))
    }

    // ── Loading and installing ─────────────────────────────────────────

    /// Loads, validates and installs as this configuration's snapshot.
    fn init(&self, py: Python<'_>) -> PyResult<()> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.init());

        match outcome {
            Ok(()) => self.publish_current(py),
            Err(error) => Err(self.raise(py, &error)),
        }
    }

    /// Loads and validates, installing nothing — the candidate is
    /// returned instead.
    fn load(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.load());

        match outcome {
            Ok(tree) => {
                // `load` validated on the way through and staged the model
                // it produced; nothing installs, so nothing is published.
                //
                // Matched against *this* call's tree, not taken on trust:
                // the GIL was released for `dynamic.load()`, so another
                // load or a watcher-driven reload can have staged its own
                // model in the meantime, and returning that one would hand
                // back a candidate resolved from different sources than
                // the ones this call just read.
                let staged = self
                    .inner
                    .shared
                    .staged
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);

                if let Some(pending) = staged.as_ref() {
                    if pending.tree == tree {
                        // Left staged rather than taken: a load installs
                        // nothing, and stealing the slot would make the
                        // next install validate the same tree again.
                        return Ok(pending.model.clone_ref(py));
                    }
                }

                drop(staged);

                // Somebody else's model is in the slot. Validating again
                // costs one pass and answers the question this call asked.
                Inner::validate(&self.inner.shared, &tree)
                    .map_err(|error| errors::to_py_err(py, &error))?;

                match self
                    .inner
                    .shared
                    .staged
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .as_ref()
                {
                    Some(pending) => Ok(pending.model.clone_ref(py)),
                    None => Err(pyo3::exceptions::PyRuntimeError::new_err(
                        "the load produced no model, which should be impossible",
                    )),
                }
            }
            Err(error) => Err(self.raise(py, &error)),
        }
    }

    /// One reload: load, validate, install, rewrite the cache.
    fn reload(&self, py: Python<'_>) -> PyResult<()> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.reload());

        match outcome {
            Ok(()) => self.publish_current(py),
            Err(error) => Err(self.raise(py, &error)),
        }
    }

    /// The installed model, or `None` before the first successful load.
    fn current(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.inner
            .cache
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .map(|model| model.clone_ref(py))
    }

    /// Installs a model the caller built, bumping the generation and
    /// firing the hooks as any other install does.
    fn replace(&self, py: Python<'_>, model: Py<PyAny>) -> PyResult<()> {
        let previous = self
            .inner
            .cache
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .replace(model.clone_ref(py));

        {
            let mut generation = self
                .inner
                .wake
                .generation
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            *generation += 1;
            self.inner.wake.changed.notify_all();
        }

        self.inner.run_hooks(py, previous.as_ref(), &model);

        Ok(())
    }

    // ── Watching, waking, hooks ────────────────────────────────────────

    /// The edges Python's cycle collector cannot otherwise see.
    ///
    /// A registered hook is held here, and the hooks people write capture
    /// the configuration they were registered on — `lambda old, new:
    /// config.current()` is the documented idiom. That closes a cycle
    /// `DynamicConfig → Config → hook → DynamicConfig` running through a
    /// `#[pyclass]`, and an object with no `tp_traverse` is a wall the
    /// collector stops at: every configuration built that way leaked,
    /// with its models and its leaked layers, until the process exited.
    ///
    /// Visiting the hooks is enough to break it. The collector only has
    /// to *reach* the cycle; clearing it happens through the closure,
    /// which has a `tp_clear` of its own.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        // A hook may be running on another thread, and a traverse that
        // blocked on the lock would block the collector. Skipping the
        // visit is safe — the collector treats it as "no edges this pass"
        // and comes back — where deadlocking is not.
        let Ok(hooks) = self.inner.hooks.try_lock() else {
            return Ok(());
        };

        for (_, hook) in hooks.iter() {
            visit.call(hook)?;
        }

        Ok(())
    }

    /// Reloads on file changes until the returned handle is stopped.
    #[pyo3(signature = (debounce, poll_interval = None))]
    fn watch(&self, py: Python<'_>, debounce: f64, poll_interval: Option<f64>) -> PyResult<Watch> {
        let mode = match poll_interval {
            Some(interval) => WatchMode::Poll {
                interval: seconds(interval)?,
            },
            None => WatchMode::Native,
        };
        let debounce = seconds(debounce)?;
        let inner = Arc::clone(&self.inner);

        let dynamic = self.inner.dynamic(py, &inner)?;
        let handle = py
            .detach(|| dynamic.watch_with(debounce, mode))
            .map_err(|failure| {
                pyo3::exceptions::PyOSError::new_err(format!("could not start watching: {failure}"))
            })?;

        Ok(Watch {
            handle: Mutex::new(Some(handle)),
        })
    }

    /// The generation of the published model: bumped once per install.
    #[getter]
    fn generation(&self) -> u64 {
        *self
            .inner
            .wake
            .generation
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// Blocks until the published generation moves past `seen`.
    ///
    /// Returns `(generation, model)`, or `None` when the timeout elapses
    /// first. The GIL is released for the wait, so the thread that
    /// performs the reload can take it.
    #[pyo3(signature = (seen, timeout = None))]
    fn wait_for_change(
        &self,
        py: Python<'_>,
        seen: u64,
        timeout: Option<f64>,
    ) -> PyResult<Option<(u64, Py<PyAny>)>> {
        let timeout = timeout.map(seconds).transpose()?;

        let reached = py.detach(|| -> u64 {
            let mut generation = self
                .inner
                .wake
                .generation
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);

            match timeout {
                Some(limit) => {
                    let (guard, _timed_out) = self
                        .inner
                        .wake
                        .changed
                        .wait_timeout_while(generation, limit, |current| *current <= seen)
                        .unwrap_or_else(std::sync::PoisonError::into_inner);

                    *guard
                }
                None => {
                    while *generation <= seen {
                        generation = self
                            .inner
                            .wake
                            .changed
                            .wait(generation)
                            .unwrap_or_else(std::sync::PoisonError::into_inner);
                    }

                    *generation
                }
            }
        });

        if reached <= seen {
            return Ok(None);
        }

        Ok(self.current(py).map(|model| (reached, model)))
    }

    /// Registers a callback run after every install, with the outgoing
    /// and incoming models. Returns the token that unregisters it.
    fn on_reload(&self, hook: Py<PyAny>) -> u64 {
        let token = self.inner.next_hook.fetch_add(1, Ordering::Relaxed);

        self.inner
            .hooks
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push((token, hook));

        token
    }

    /// Unregisters a hook by token; `False` when it was already gone.
    fn remove_hook(&self, token: u64) -> bool {
        let mut hooks = self
            .inner
            .hooks
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let before = hooks.len();

        hooks.retain(|(registered, _)| *registered != token);

        hooks.len() != before
    }

    // ── Runtime layers ─────────────────────────────────────────────────

    fn set_default(&self, py: Python<'_>, path: &str, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let value = convert::from_py(value)?;

        self.inner
            .layers
            .defaults
            .set(path, value)
            .map_err(|error| errors::to_py_err(py, &error))
    }

    fn set_defaults(&self, py: Python<'_>, values: &Bound<'_, PyAny>) -> PyResult<()> {
        let values = convert::from_py(values)?;

        self.inner
            .layers
            .defaults
            .set_struct(&values)
            .map_err(|error| errors::to_py_err(py, &error))
    }

    fn set_override(&self, py: Python<'_>, path: &str, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let value = convert::from_py(value)?;

        self.inner
            .layers
            .overrides
            .set(path, value)
            .map_err(|error| errors::to_py_err(py, &error))
    }

    fn set_assignments(&self, py: Python<'_>, assignments: Vec<String>) -> PyResult<()> {
        self.inner
            .layers
            .flags
            .set_assignments(assignments)
            .map_err(|error| errors::to_py_err(py, &error))
    }

    fn clear_defaults(&self) {
        self.inner.layers.defaults.clear();
    }

    fn clear_overrides(&self) {
        self.inner.layers.overrides.clear();
    }

    fn clear_assignments(&self) {
        self.inner.layers.flags.clear();
    }

    fn alias(&self, py: Python<'_>, from: &str, to: &str) -> PyResult<()> {
        self.inner
            .layers
            .aliases
            .add(from, to)
            .map_err(|error| errors::to_py_err(py, &error))
    }

    fn bind_env(&self, py: Python<'_>, path: &str, variable: &str) -> PyResult<()> {
        self.inner
            .layers
            .bindings
            .bind(path, variable)
            .map_err(|error| errors::to_py_err(py, &error))
    }

    // ── Diagnostics ────────────────────────────────────────────────────

    /// Where the value at `path` would come from: `(kind, detail)`.
    fn source_of(&self, py: Python<'_>, path: &str) -> PyResult<Option<(String, Option<String>)>> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.builder().source_of(path));

        match outcome {
            Ok(origin) => Ok(origin.map(|origin| {
                let (kind, detail) = errors::origin_parts(&origin);

                (kind.to_owned(), detail)
            })),
            Err(error) => Err(self.raise(py, &error)),
        }
    }

    /// Whether anything supplies `path`.
    fn is_set(&self, py: Python<'_>, path: &str) -> PyResult<bool> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.builder().is_set(path));

        outcome.map_err(|error| self.raise(py, &error))
    }

    /// Every layer's answer for `path`, as `(path, rows, rendered)`.
    ///
    /// The one diagnostic that carries values, and the one that redacts:
    /// a path under a `SecretStr` field comes back as `***` because the
    /// secret list was derived from the model at construction.
    fn explain(&self, py: Python<'_>, path: &str) -> PyResult<Py<PyAny>> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.builder().explain(path));

        let explanation = outcome.map_err(|error| self.raise(py, &error))?;
        let rows = PyList::empty(py);

        for row in explanation.rows() {
            let entry = PyDict::new(py);
            entry.set_item("layer", row.layer)?;
            entry.set_item("value", row.value.clone())?;

            match &row.origin {
                Some(origin) => {
                    let (kind, detail) = errors::origin_parts(origin);
                    entry.set_item("origin_kind", kind)?;
                    entry.set_item("origin", detail)?;
                }
                None => {
                    entry.set_item("origin_kind", py.None())?;
                    entry.set_item("origin", py.None())?;
                }
            }

            rows.append(entry)?;
        }

        let result = PyDict::new(py);
        result.set_item("path", explanation.path())?;
        result.set_item("rows", rows)?;
        result.set_item("rendered", explanation.to_string())?;
        result.set_item(
            "winner",
            explanation.winner().map(|winner| winner.layer.to_owned()),
        )?;

        Ok(result.into_any().unbind())
    }

    /// What this configuration resolves to, and whether it would load.
    fn check(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.builder().check());

        let report = outcome.map_err(|error| self.raise(py, &error))?;

        let resolved = PyList::empty(py);
        for value in &report.resolved {
            let (kind, detail) = errors::origin_parts(&value.origin);
            let entry = PyDict::new(py);
            entry.set_item("path", &value.path)?;
            entry.set_item("origin_kind", kind)?;
            entry.set_item("origin", detail)?;
            resolved.append(entry)?;
        }

        let unknown = PyList::empty(py);
        for key in &report.unknown {
            let entry = PyDict::new(py);
            entry.set_item("path", &key.path)?;
            entry.set_item("suggestion", key.suggestion.clone())?;
            unknown.append(entry)?;
        }

        let result = PyDict::new(py);
        result.set_item("key", &report.key)?;
        result.set_item("resolved", resolved)?;
        result.set_item("unknown", unknown)?;
        result.set_item("failure", report.failure.clone())?;
        result.set_item("is_clean", report.is_clean())?;

        Ok(result.into_any().unbind())
    }

    /// The resolved section, without deserializing it into the model.
    fn snapshot(&self, py: Python<'_>) -> PyResult<Snapshot> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.builder().snapshot());

        outcome
            .map(|snapshot| Snapshot { inner: snapshot })
            .map_err(|error| self.raise(py, &error))
    }

    /// Drops the cached model and every hook, without touching Python
    /// during interpreter teardown.
    ///
    /// What the module's `atexit` handler calls: a watcher thread that
    /// outlives finalization must not be the thing that discovers it.
    fn release(&self, py: Python<'_>) {
        let _ = self
            .inner
            .cache
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        self.inner
            .hooks
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clear();
        let _ = self
            .inner
            .shared
            .staged
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        let _ = self
            .inner
            .shared
            .reports
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        let _ = py;
    }

    fn __repr__(&self) -> String {
        format!(
            "<dynamic_config.Config key={:?} generation={}>",
            self.inner.key,
            self.generation()
        )
    }
}

impl Config {
    /// Publishes the model for whatever the engine just installed.
    fn publish_current(&self, py: Python<'_>) -> PyResult<()> {
        let inner = Arc::clone(&self.inner);
        let installed = self.inner.dynamic(py, &inner)?.current();

        match installed {
            Some(tree) => self.inner.commit(py, &tree),
            None => Ok(()),
        }
    }

    /// A crate error as the exception that mirrors it — with Pydantic's
    /// own scrubbed report attached when validation is what refused.
    fn raise(&self, py: Python<'_>, error: &Error) -> PyErr {
        let failure = errors::to_py_err(py, error);

        if error.kind() == dynamic_config::ErrorKind::Invalid {
            if let Some(reports) = self
                .inner
                .shared
                .reports
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .take()
            {
                let _ = failure.value(py).setattr("errors", reports);
            }
        }

        failure
    }
}

/// A running watcher. Dropping or stopping it ends the watch.
#[pyclass(module = "dynamic_config._core", name = "Watch", frozen)]
pub(crate) struct Watch {
    handle: Mutex<Option<WatchHandle>>,
}

#[pymethods]
impl Watch {
    /// Stops watching. Idempotent.
    fn stop(&self) {
        let _ = self
            .handle
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
    }

    /// Watches for the rest of the process, whatever happens to this
    /// object.
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
    inner: dynamic_config::Snapshot,
}

#[pymethods]
impl Snapshot {
    /// The resolved values as plain Python data.
    fn to_dict(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let value = self.inner.to_value();
        let tree = value_to_json(&value);

        Ok(convert::to_py(py, &tree)?.unbind())
    }

    /// Where the value at `path` came from, in this snapshot.
    fn source_of(&self, path: &str) -> Option<(String, Option<String>)> {
        self.inner.source_of(path).map(|origin| {
            let (kind, detail) = errors::origin_parts(origin);

            (kind.to_owned(), detail)
        })
    }

    fn contains(&self, path: &str) -> bool {
        self.inner.contains(path)
    }

    fn leaf_paths(&self) -> Vec<String> {
        self.inner.leaf_paths()
    }

    fn top_level_keys(&self) -> Vec<String> {
        self.inner.top_level_keys()
    }

    fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }

    /// Which paths differ between two snapshots — paths, never values.
    ///
    /// `(path, kind)`, where the kind is `added`, `removed` or `changed`:
    /// structured rather than rendered, so the caller branches on it
    /// instead of parsing English.
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
fn changes_as_pairs(changes: Vec<dynamic_config::Change>) -> Vec<(String, String)> {
    changes
        .into_iter()
        .map(|change| (change.path, change.kind.to_string()))
        .collect()
}

/// Which paths differ between two configuration values — paths, never
/// values, which is the audit half of a change.
///
/// The Python-side twin of [`dynamic_config::changed_paths`]: it takes
/// what a model dumps to, so two snapshots of the same model can be
/// compared without either of them being installed.
#[pyfunction]
pub(crate) fn changed_paths(
    py: Python<'_>,
    previous: &Bound<'_, PyAny>,
    current: &Bound<'_, PyAny>,
) -> PyResult<Vec<(String, String)>> {
    let previous = convert::from_py(previous)?;
    let current = convert::from_py(current)?;

    dynamic_config::changed_paths(&previous, &current)
        .map(changes_as_pairs)
        .map_err(|error| errors::to_py_err(py, &error))
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

/// Seconds, the way Python spells a duration.
fn seconds(value: f64) -> PyResult<Duration> {
    if !value.is_finite() || value < 0.0 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "a duration is a non-negative number of seconds",
        ));
    }

    Ok(Duration::from_secs_f64(value))
}

/// Registers the classes on the module.
pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<Config>()?;
    module.add_class::<Watch>()?;
    module.add_class::<Snapshot>()?;
    module.add_function(pyo3::wrap_pyfunction!(changed_paths, module)?)?;

    Ok(())
}
