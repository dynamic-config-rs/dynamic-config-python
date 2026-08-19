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
//!
//! ## The files
//!
//! `Config` is the surface a Python program touches and it is here, with
//! the rest split by concern: `inner` is the state the surface owns and
//! the validate/commit protocol it runs; `scrub` is how a Python failure
//! becomes a message with no values in it; `handles` is `Watch` and
//! `Snapshot`, the two smaller classes; and `convert` next door is
//! unchanged. Nothing Python can name moved.

mod handles;
mod inner;
mod scrub;

pub(crate) use handles::{Snapshot, Watch};

use handles::changes_as_pairs;

use inner::{Engine, Inner, Layers, Shared, Wake};
use scrub::{describe, scrub_validation, scrubbed_reports};

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock};
use std::time::Duration;

use dynamic_config::watch::WatchMode;
use dynamic_config::{Builder, CacheMode, Error};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use pyo3::{PyTraverseError, PyVisit};
use serde_json::Value;

use crate::convert;
use crate::errors;
use crate::remote::{PyRemoteSource, PySource};

///         nothing to call itself, which goes with `whole_document()`.
///     secrets: the dotted paths whose values must never reach a
///         diagnostic, or `None` for *not known*. `None` is not `[]`: an
///         empty list is the knowledge that there are no secrets, and a
///         redacting cache mode is refused without that knowledge rather
///         than writing a cache that claims a redaction it never did.
///     fields: the model's top-level field names, which `check()`
///         compares a document against to call a key unknown. Empty
///         means *no field list*, and `check()` reports that rather than
///         an all-clear it did not earn.
/// One configuration: sources, storage, lifecycle and diagnostics.
///
/// The compiled half of `dynamic_config.DynamicConfig`, which is what a
/// program builds. This class is documented all the same, because it is
/// what `help()` reaches and what a debugger shows.
///
/// `Config(validate, key, secrets, fields)`
///
/// Parameters:
///     validate: the callable the engine hands a resolved mapping, once
///         per resolve. It answers with the model instance, or raises
///         describing what is wrong — a raise refuses the install and
///         leaves the previous snapshot serving.
///     key: the section key. Selects this configuration's table out of
///         each document, and names the environment prefix, the cache
///         entry and every diagnostic. `""` is a configuration with
#[pyclass(module = "dynamic_config._core", name = "Config", frozen)]
pub(crate) struct Config {
    inner: Arc<Inner>,
}

#[pymethods]
impl Config {
    /// Builds a configuration: `Config(validate, key, secrets, fields)`.
    ///
    /// Not called directly. `DynamicConfig` builds one per configuration
    /// and derives all four arguments from the model, which is what keeps
    /// the two lists below from being a second declaration somebody has
    /// to keep in step with the first.
    ///
    /// Parameters:
    ///     validate: the callable the engine hands a resolved mapping,
    ///         once per resolve, and which answers with the model
    ///         instance or raises describing what is wrong.
    ///     key: the section key. Selects this configuration's table out
    ///         of each document, and names the environment prefix, the
    ///         cache entry and every diagnostic.
    ///     secrets: the dotted paths whose values must never reach a
    ///         diagnostic — or `None` for *not known*, which is what a
    ///         schemaless configuration that named none has. `None` is
    ///         not the same as `[]`: an empty list is the knowledge that
    ///         there are no secrets, and a redacting cache mode is
    ///         refused without that knowledge rather than writing a cache
    ///         that claims a redaction it did not perform.
    ///     fields: the model's top-level field names, which is what
    ///         `check()` compares a document against to call a key
    ///         unknown. Empty means *no field list*, and `check()` says
    ///         so rather than reporting an all-clear it did not earn.
    #[new]
    #[pyo3(signature = (validate, key, secrets, fields))]
    fn new(
        py: Python<'_>,
        validate: Py<PyAny>,
        key: &str,
        secrets: Option<Vec<String>>,
        fields: Vec<String>,
    ) -> PyResult<Self> {
        let shared = Arc::new(Shared {
            validate,
            staged: Mutex::new(None),
            next_sequence: AtomicU64::new(0),
            reports: Mutex::new(None),
        });

        let layers = Layers::leak();

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

        let validating = Arc::clone(&shared);

        let mut builder = Builder::<Value>::new(key).with_fields(field_names);

        // Only when the caller *knows*: see the `secrets` parameter. A
        // schemaless configuration that declared nothing leaves the list
        // unset, so the engine goes on refusing a redacting cache.
        if let Some(secrets) = &secrets {
            let secret_refs: Vec<&str> = secrets.iter().map(String::as_str).collect();

            builder = builder.with_secrets(&secret_refs);
        }

        let builder = builder
            .with_type_statics(
                layers.defaults,
                layers.overrides,
                layers.flags,
                layers.bindings,
                layers.aliases,
                layers.remote,
                // An instance has no type-level slot to remember it in.
                |_| {},
            )
            .validate(move |tree: &Value| Inner::validate(&validating, tree));

        let _ = py;

        Ok(Self {
            inner: Arc::new(Inner {
                prepared: Mutex::new(std::collections::HashMap::new()),
                next_prepared: AtomicU64::new(1),
                key: key.to_owned(),
                last_committed: AtomicU64::new(0),
                shared,
                engine: Mutex::new(Engine::Building(Box::new(builder))),
                cache: RwLock::new(None),
                wake: Wake {
                    generation: Mutex::new(0),
                    changed: Condvar::new(),
                    closed: AtomicBool::new(false),
                    refusals: AtomicU64::new(0),
                },
                hooks: Mutex::new(Vec::new()),
                next_hook: AtomicU64::new(1),
                layers,
                source: Arc::new(PySource::default()),
            }),
        })
    }

    /// The section key this configuration reads. A property, not a call.
    #[getter]
    fn key(&self) -> &str {
        &self.inner.key
    }

    // ── Sources ────────────────────────────────────────────────────────

    /// Adds a configuration file: `file(path)`.
    ///
    /// Merged in call order and later files win; the format comes from
    /// the extension at load time, and a file that is not there is
    /// skipped, which is what makes an optional secrets file work.
    ///
    /// Parameters:
    ///     path: the file to read, resolved against the working
    ///         directory. `.json`, `.toml`, `.yaml`/`.yml`.
    fn file(&self, py: Python<'_>, path: String) -> PyResult<()> {
        self.inner.configure(py, |builder| builder.file(path))
    }

    // No `encrypted_file` here, deliberately: decryption needs a
    // `Decryptor` implementation, which is a Rust trait with no Python
    // side — and shipping `age` to make one would put a crypto stack in
    // every wheel for a door nobody could open from Python. A deployment
    // that needs it decrypts with the CLI and points this at the result.

    /// Looks for a named file in several directories: `discover(name, paths)`.
    ///
    /// Every directory that has a match contributes one layer, so the
    /// search order *is* the layering order — and discovered files sit
    /// **below** everything `file()` listed, because a listed file is a
    /// deliberate statement and a search result is a guess about the
    /// machine.
    ///
    /// Parameters:
    ///     name: the stem to look for. `config` finds `config.toml`,
    ///         `config.json` or `config.yaml`.
    ///     paths: the directories to look in, in order.
    fn discover(&self, py: Python<'_>, name: String, paths: Vec<String>) -> PyResult<()> {
        self.inner
            .configure(py, |builder| builder.discover(name, paths))
    }

    /// The environment layer: `env(prefix)`.
    ///
    /// Read after every file and above all of them. The prefix combines
    /// with the section key, so `env("APP_")` on a `db` configuration
    /// reads `APP_DB_*`; an empty key makes it the prefix alone.
    ///
    /// Parameters:
    ///     prefix: the variable prefix, trailing underscore included.
    fn env(&self, py: Python<'_>, prefix: String) -> PyResult<()> {
        self.inner.configure(py, |builder| builder.env(prefix))
    }

    /// The separator that means nesting in a variable name: `nest(separator)`.
    ///
    /// `__` unless said, so `APP_DB_POOL__MAX_SIZE` is `pool.max_size`.
    /// A single separator cannot mean both "word break" and "nesting",
    /// so whatever this is set to has to be something a field name will
    /// not contain. Meaningful only alongside `env()`.
    ///
    /// Parameters:
    ///     separator: what introduces one level of nesting.
    fn nest(&self, py: Python<'_>, separator: String) -> PyResult<()> {
        self.inner.configure(py, |builder| builder.nest(separator))
    }

    /// Treats `FOO=` as set-to-empty rather than unset. No parameters.
    ///
    /// By default an empty variable is *unset* and the file's value
    /// survives: an unset value rendered into a deployment template
    /// leaves exactly `FOO=`, and letting that blank out a good
    /// configured value is a bad afternoon.
    fn allow_empty_env(&self, py: Python<'_>) -> PyResult<()> {
        self.inner.configure(py, Builder::allow_empty_env)
    }

    /// Refuses ambiguous environment spellings. No parameters.
    ///
    /// `APP_DB_TLS=off` reads like a boolean and arrives as the string
    /// `"off"`. With this on, the yes/no/on/off family — and
    /// `null`/`nil`/`none` — is an error naming the variable. `.env`
    /// files are held to the same standard.
    fn strict_env(&self, py: Python<'_>) -> PyResult<()> {
        self.inner.configure(py, Builder::strict_env)
    }

    /// Reads each document as this section's values. No parameters.
    ///
    /// The default is one file, several sections: every top-level key
    /// names one. This says the document *is* the configuration —
    /// `{"host": "0.0.0.0", "port": 8000}` with no header above it. The
    /// key still names the environment prefix, the cache entry and the
    /// diagnostics; it stops being looked for inside the file.
    fn whole_document(&self, py: Python<'_>) -> PyResult<()> {
        self.inner.configure(py, Builder::whole_document)
    }

    /// A `.env` file read as the environment layer: `env_file(path)`.
    ///
    /// Merged in call order and just **below** the real environment: a
    /// variable somebody exported for this run should beat a file in the
    /// repository.
    ///
    /// Parameters:
    ///     path: the `.env` file. A missing one is skipped.
    fn env_file(&self, py: Python<'_>, path: String) -> PyResult<()> {
        self.inner.configure(py, |builder| builder.env_file(path))
    }

    /// A directory where each file is one key: `secrets_dir(path)`.
    ///
    /// How Docker and Kubernetes hand a container its credentials: the
    /// filename is the key, the contents are the value, one trailing
    /// newline is trimmed. One directory level — nesting is spelled in
    /// the filename with the `nest()` separator — and provenance names
    /// the individual file.
    ///
    /// Parameters:
    ///     path: the directory. One that is not there is skipped, like a
    ///         missing file.
    fn secrets_dir(&self, py: Python<'_>, path: String) -> PyResult<()> {
        self.inner
            .configure(py, |builder| builder.secrets_dir(path))
    }

    /// The variable naming the active profile: `profile_env(variable)`.
    ///
    /// With it set to `production`, every file gains a sibling layer:
    /// `config.toml` is followed by `config.production.toml`, discovered
    /// or listed alike, and a variant that does not exist is skipped. A
    /// profile has to be a plain word: one with a path separator in it is
    /// refused rather than followed.
    ///
    /// Parameters:
    ///     variable: the environment variable to read, e.g. `APP_ENV`.
    fn profile_env(&self, py: Python<'_>, variable: String) -> PyResult<()> {
        self.inner
            .configure(py, |builder| builder.profile_env(variable))
    }

    /// A last-known-good cache: `cache(path, mode)`.
    ///
    /// Written after every clean load and read when the sources will not
    /// load, so a restart during an outage starts from what worked
    /// rather than not at all.
    ///
    /// Parameters:
    ///     path: where to write it. The format comes from the extension.
    ///     mode: `"redacted"` — every secret path dropped, the usual
    ///         choice — or `"full"`, which writes the secrets too and is
    ///         a file to protect accordingly, or `"fingerprint"`, which
    ///         stores no values at all and only reports whether the
    ///         configuration changed. Anything else is a `ValueError`
    ///         naming the three.
    ///
    /// A redacting mode needs to know which paths are secret. A declared
    /// model says so; a schemaless configuration says so with
    /// `DynamicConfig(..., secrets=[...])`, and without it the cache is
    /// refused rather than written unredacted.
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

    // ── The remote store ───────────────────────────────────────────────

    /// Installs a remote store: `remote(source)`.
    ///
    /// Not a builder call, deliberately: the engine's `set_remote` is on
    /// the remote slot rather than the builder, so a store can be
    /// installed or swapped after the first load, exactly as it can in
    /// Rust. Nothing is fetched here.
    ///
    /// Parameters:
    ///     source: an object with `fetch()` and `describe()` — a
    ///         `dynamic_config.RemoteSource` subclass, or one of the
    ///         compiled stores from `dynamic_config.remote`. Installing a
    ///         second one replaces the first, and drops the document the
    ///         first had given.
    fn remote(&self, py: Python<'_>, source: Py<PyAny>) -> PyResult<()> {
        let bound = source.bind(py);

        for name in ["fetch", "describe"] {
            if !bound.hasattr(name)? {
                return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                    "a remote source needs a {name}() method; subclass \
                     dynamic_config.RemoteSource, which refuses a class \
                     missing one where the store is constructed rather than \
                     at the first fetch"
                )));
            }
        }

        // Asked once, here, where a failure is the caller's own call
        // failing. See `PyRemoteSource::description` for why not per load.
        let description = bound
            .call_method0(pyo3::intern!(py, "describe"))?
            .extract::<String>()
            .map_err(|_| {
                pyo3::exceptions::PyTypeError::new_err(
                    "describe() has to return a str naming the store; it is \
                     what provenance and every remote error report",
                )
            })?;

        self.inner.source.install(source);
        self.inner.layers.remote.set(PyRemoteSource::new(
            Arc::downgrade(&self.inner.source),
            description,
        ));

        Ok(())
    }

    /// Reads the store, and keeps what came back for the next load.
    ///
    /// The GIL is released for the whole refresh and re-taken by the shim
    /// only for the Python call itself, so a slow `fetch()` does not stop
    /// the process — and no lock is held across it, so a `fetch()` may
    /// call back into this configuration.
    fn refresh_remote(&self, py: Python<'_>) -> PyResult<()> {
        if crate::remote::fetching() {
            return Err(errors::BackendError::new_err(
                "refresh_remote() was called from inside a remote source's own \
                 fetch(); a fetch must not drive the refresh it is answering. \
                 Read the store, return the document, and let the caller \
                 reload.",
            ));
        }

        let remote = self.inner.layers.remote;
        let outcome = py.detach(|| remote.refresh());

        match outcome {
            Ok(()) => Ok(()),
            Err(error) => Err(self.remote_failure(py, &error)),
        }
    }

    /// Drops the fetched document, so the next load sees no remote layer.
    ///
    /// The source stays installed, as the Rust `clear_remote()` leaves it:
    /// this drops what was fetched, not where to fetch from.
    /// Drops the document the store gave, keeping the store. No parameters.
    ///
    /// The next load sees no remote layer until something fetches again.
    /// The source stays installed and so does every watch taken from it:
    /// dropping a document is not replacing a store.
    fn clear_remote(&self) {
        self.inner.layers.remote.clear();
    }

    /// How the installed store names itself, or `None` if there is none.
    /// A property, not a call.
    #[getter]
    fn remote_description(&self) -> Option<String> {
        self.inner.layers.remote.describe()
    }

    // ── Loading and installing ─────────────────────────────────────────

    /// Loads, validates and installs the first snapshot. No parameters.
    fn init(&self, py: Python<'_>) -> PyResult<()> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.init());

        match outcome {
            Ok(()) => self.publish_current(py),
            Err(error) => Err(self.raise(py, &error)),
        }
    }

    /// Loads and validates, installing nothing. No parameters.
    ///
    /// The candidate is returned instead, which is what a `--check` flag
    /// and a test both want.
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

    /// One reload: load, validate, install, rewrite the cache. No
    /// parameters.
    ///
    /// A failure installs nothing and leaves the previous model serving.
    fn reload(&self, py: Python<'_>) -> PyResult<()> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.reload());

        match outcome {
            Ok(()) => self.publish_current(py),
            Err(error) => Err(self.raise(py, &error)),
        }
    }

    /// The first half of an all-or-nothing reload: `prepare()`.
    ///
    /// Loads and validates now, installs nothing, and answers a token the
    /// caller hands back to `commit`. Every configuration in a group
    /// prepares before any of them commits, so a member that refuses
    /// leaves every other member's snapshot untouched — the property
    /// `ConfigGroup.reload_atomic` is named for.
    ///
    /// A prepared commit that is never committed is dropped, and dropping
    /// one is exactly what *not* applying it means.
    fn prepare(&self, py: Python<'_>) -> PyResult<u64> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.builder().prepare());

        match outcome {
            Ok(commit) => {
                let token = self.inner.next_prepared.fetch_add(1, Ordering::Relaxed);

                self.inner
                    .prepared
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .insert(token, commit);

                Ok(token)
            }
            Err(error) => Err(self.raise(py, &error)),
        }
    }

    /// The second half: `commit(token)`.
    ///
    /// Installs what `prepare` validated — through the engine, so the
    /// generation, the reason and the hooks all move exactly as a reload
    /// moves them. An unknown token is a caller error rather than a
    /// silent no-op.
    fn commit(&self, py: Python<'_>, token: u64) -> PyResult<()> {
        let commit = self
            .inner
            .prepared
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .remove(&token);

        let Some(commit) = commit else {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "no prepared commit for this token: it was already \
                 committed, or it belongs to another configuration",
            ));
        };

        py.detach(commit);

        self.publish_current(py)
    }

    /// Drops a prepared commit without installing it: `discard(token)`.
    ///
    /// What a group does to every member's commit when one member
    /// refuses. Unknown tokens are ignored, because the caller's intent —
    /// *this must not install* — is already true.
    fn discard(&self, token: u64) {
        let _ = self
            .inner
            .prepared
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .remove(&token);
    }

    /// The installed model, or `None` before the first successful load.
    /// The model in force, or `None` before the first install. No
    /// parameters.
    ///
    /// An attribute read: the engine publishes each installed model onto
    /// this object, so nothing crosses back into Rust here.
    fn current(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.inner
            .cache
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .map(|model| model.clone_ref(py))
    }

    /// Publishes a model nothing loaded: `replace(model)`.
    ///
    /// Installs an instance the caller built, bumping the generation and
    /// firing the hooks as any other install does.
    ///
    /// Parameters:
    ///     model: the instance to publish as the current snapshot.
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
    ///
    /// A Python remote source is the same shape and worse: `fetch()`
    /// implementations hold the configuration they feed as a matter of
    /// course, and the source is reachable from here alone.
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

        self.inner.source.traverse(&visit)
    }

    /// Drops the edges `__traverse__` reports, so a cycle through them
    /// collects.
    ///
    /// The collector calls this only on an object it has already proved
    /// unreachable, so dropping the references is the whole job — the
    /// cached model and the staged tree are left alone deliberately, since
    /// neither can close a cycle back to this object and `release()` is
    /// what clears those at exit.
    fn __clear__(&self) {
        self.inner
            .hooks
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clear();
        self.inner.source.clear();
    }

    /// Reloads on file changes: `watch(debounce, poll_interval=None)`.
    ///
    /// Runs until the returned `Watch` is stopped or dropped.
    ///
    /// Parameters:
    ///     debounce: seconds to wait after a change before reloading.
    ///         An editor's atomic save is several filesystem events and
    ///         this is what makes them one reload; `0.25` is the usual
    ///         choice.
    ///     poll_interval: seconds between polls, for a filesystem the
    ///         platform's watcher cannot subscribe to — a network mount,
    ///         a container layer. `None` — the default — uses the
    ///         platform's own event API.
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

    /// How many reloads have been refused since start: bumped once per
    /// refusal, never reset. A property, not a call — the baseline an
    /// event stream starts from.
    #[getter]
    fn refusals(&self) -> u64 {
        self.inner.wake.refusals.load(Ordering::Acquire)
    }

    /// The generation of the published model: bumped once per install. A
    /// property, not a call.
    #[getter]
    fn generation(&self) -> u64 {
        *self
            .inner
            .wake
            .generation
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// Blocks until a new model is published:
    /// `wait_for_change(seen, timeout=None)`.
    ///
    /// Answers `(generation, model)`, or `None` when the timeout elapses
    /// first. The GIL is released for the wait, so the thread performing
    /// the reload can take it.
    ///
    /// Parameters:
    ///     seen: the generation the caller already has. The call returns
    ///         as soon as the published generation is past it, so a
    ///         reload that happened while the caller was working is not
    ///         missed.
    ///     timeout: seconds to wait at most. `None` waits forever.
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

            // `closed` is checked with the lock held and again after every
            // wake: `release` sets it and then notifies, so a waiter that
            // slept through the flag being set still sees it here.
            let closed = || self.inner.wake.closed.load(Ordering::Acquire);

            match timeout {
                Some(limit) => {
                    let (guard, _timed_out) = self
                        .inner
                        .wake
                        .changed
                        .wait_timeout_while(generation, limit, |current| {
                            *current <= seen && !closed()
                        })
                        .unwrap_or_else(std::sync::PoisonError::into_inner);

                    *guard
                }
                None => {
                    while *generation <= seen && !closed() {
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

    /// Blocks until an install *or* a refusal:
    /// `wait_for_event(seen_generation, seen_refusals, timeout=None)`.
    ///
    /// Answers `(generation, refusals, model_or_None)` as soon as either
    /// counter is past what the caller has seen — the model rides along
    /// only when the generation moved. `None` means the timeout elapsed,
    /// or the configuration was released; the caller tells them apart the
    /// same way `wait_for_change`'s callers do, by asking again.
    ///
    /// The refusal counter is this binding's, bumped by the engine's
    /// failure hook under the same lock this wait sleeps on — which is
    /// what retired the polling that used to stand in for it.
    #[pyo3(signature = (seen_generation, seen_refusals, timeout = None))]
    #[allow(clippy::type_complexity)] // the tuple IS the wire format the stub declares
    fn wait_for_event(
        &self,
        py: Python<'_>,
        seen_generation: u64,
        seen_refusals: u64,
        timeout: Option<f64>,
    ) -> PyResult<Option<(u64, u64, Option<Py<PyAny>>)>> {
        let timeout = timeout.map(seconds).transpose()?;

        let (generation, refusals) = py.detach(|| -> (u64, u64) {
            let refusals = || self.inner.wake.refusals.load(Ordering::Acquire);
            let closed = || self.inner.wake.closed.load(Ordering::Acquire);
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
                        .wait_timeout_while(generation, limit, |current| {
                            *current <= seen_generation && refusals() <= seen_refusals && !closed()
                        })
                        .unwrap_or_else(std::sync::PoisonError::into_inner);

                    (*guard, refusals())
                }
                None => {
                    while *generation <= seen_generation && refusals() <= seen_refusals && !closed()
                    {
                        generation = self
                            .inner
                            .wake
                            .changed
                            .wait(generation)
                            .unwrap_or_else(std::sync::PoisonError::into_inner);
                    }

                    (*generation, refusals())
                }
            }
        });

        if generation <= seen_generation && refusals <= seen_refusals {
            return Ok(None);
        }

        let model = if generation > seen_generation {
            self.current(py)
        } else {
            None
        };

        Ok(Some((generation, refusals, model)))
    }

    /// Registers a callback run after every install: `on_reload(hook)`.
    ///
    /// Answers the token that unregisters it.
    ///
    /// Parameters:
    ///     hook: a callable taking `(previous, current)` — the outgoing
    ///         model (`None` for the first install) and the incoming
    ///         one. It runs on whichever thread installed, which for a
    ///         watcher is the watcher's.
    fn on_reload(&self, hook: Py<PyAny>) -> u64 {
        let token = self.inner.next_hook.fetch_add(1, Ordering::Relaxed);

        self.inner
            .hooks
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push((token, hook));

        token
    }

    /// Unregisters a hook: `remove_hook(token)`.
    ///
    /// Answers `False` when the token was already gone.
    ///
    /// Parameters:
    ///     token: what `on_reload` returned.
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

    /// A value below every file: `set_default(path, value)`.
    ///
    /// The bottom layer, for a fallback the program can compute but a
    /// file need not state — cores × 4, a hostname read from the
    /// platform. Takes effect on the next load.
    ///
    /// Parameters:
    ///     path: the dotted path to set, e.g. `pool.max_size`.
    ///     value: any JSON-shaped Python value: `str`, `int`, `float`,
    ///         `bool`, `None`, `list` or `dict` of those.
    fn set_default(&self, py: Python<'_>, path: &str, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let value = convert::from_py(value)?;

        self.inner
            .layers
            .defaults
            .set(path, value)
            .map_err(|error| errors::to_py_err(py, &error))
    }

    /// A whole tree of defaults at once: `set_defaults(values)`.
    ///
    /// How a hand-written `Config.default()` becomes the bottom layer
    /// without naming each key.
    ///
    /// Parameters:
    ///     values: a mapping — or any object this binding can convert —
    ///         whose every leaf becomes one default.
    fn set_defaults(&self, py: Python<'_>, values: &Bound<'_, PyAny>) -> PyResult<()> {
        let values = convert::from_py(values)?;

        self.inner
            .layers
            .defaults
            .set_struct(&values)
            .map_err(|error| errors::to_py_err(py, &error))
    }

    /// A value above everything, the environment included:
    /// `set_override(path, value)`.
    ///
    /// What a test pins with, and what a `--set key=value` flag reaches.
    /// Takes effect on the next load.
    ///
    /// Parameters:
    ///     path: the dotted path to pin.
    ///     value: as `set_default`.
    fn set_override(&self, py: Python<'_>, path: &str, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let value = convert::from_py(value)?;

        self.inner
            .layers
            .overrides
            .set(path, value)
            .map_err(|error| errors::to_py_err(py, &error))
    }

    /// The command line's own layer: `set_assignments(assignments)`.
    ///
    /// Above the environment and below the overrides, because a flag is
    /// more specific than a variable and less specific than a value the
    /// program was told to pin.
    ///
    /// Parameters:
    ///     assignments: `"key=value"` strings, as a `--set` flag
    ///         collects them. A string with no `=` in it is an error
    ///         naming it.
    fn set_assignments(&self, py: Python<'_>, assignments: Vec<String>) -> PyResult<()> {
        self.inner
            .layers
            .flags
            .set_assignments(assignments)
            .map_err(|error| errors::to_py_err(py, &error))
    }

    /// Empties the defaults layer. No parameters.
    fn clear_defaults(&self) {
        self.inner.layers.defaults.clear();
    }

    /// Empties the override layer. No parameters.
    fn clear_overrides(&self) {
        self.inner.layers.overrides.clear();
    }

    /// Empties the command-line layer. No parameters.
    fn clear_assignments(&self) {
        self.inner.layers.flags.clear();
    }

    /// An old path that still resolves: `alias(from, to)`.
    ///
    /// Fills a gap rather than overriding: the new path wins wherever it
    /// is set, and the old one answers only where it is not. How a
    /// renamed key keeps working for a release.
    ///
    /// Parameters:
    ///     from: the old dotted path, as deployments still spell it.
    ///     to: the path it means now.
    fn alias(&self, py: Python<'_>, from: &str, to: &str) -> PyResult<()> {
        self.inner
            .layers
            .aliases
            .add(from, to)
            .map_err(|error| errors::to_py_err(py, &error))
    }

    /// One field, bound to a variable by name: `bind_env(path, variable)`.
    ///
    /// For the variable a platform chose and a prefix cannot reach —
    /// `DATABASE_URL`, `PORT`. Sits just above the prefixed environment
    /// layer, because naming a variable is the more specific statement.
    ///
    /// Parameters:
    ///     path: the dotted path to fill.
    ///     variable: the environment variable to read it from.
    fn bind_env(&self, py: Python<'_>, path: &str, variable: &str) -> PyResult<()> {
        self.inner
            .layers
            .bindings
            .bind(path, variable)
            .map_err(|error| errors::to_py_err(py, &error))
    }

    // ── Diagnostics ────────────────────────────────────────────────────

    /// Where a value would come from: `source_of(path)`.
    ///
    /// Answers `(kind, detail)` — `("file", "/etc/app/config.toml")`,
    /// `("env", "APP_DB_HOST")` — or `None` when nothing supplies it.
    /// Re-reads the sources rather than reporting the installed
    /// snapshot, so it answers before the first load and after a failed
    /// one.
    ///
    /// Parameters:
    ///     path: the dotted path to trace.
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

    /// Whether anything supplies a path: `is_set(path)`.
    ///
    /// Parameters:
    ///     path: the dotted path to look for.
    fn is_set(&self, py: Python<'_>, path: &str) -> PyResult<bool> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.builder().is_set(path));

        outcome.map_err(|error| self.raise(py, &error))
    }

    /// Every layer's answer for one key: `explain(path)`.
    ///
    /// Answers `(path, rows, rendered)`.
    ///
    /// Parameters:
    ///     path: the dotted path to explain.
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
        // Whether the unknown-key comparison ran at all. An empty list
        // from a configuration with no field names is not an all-clear,
        // and a caller must be able to tell the two apart.
        result.set_item("unknown_checked", report.unknown_checked)?;
        result.set_item("failure", report.failure.clone())?;
        result.set_item("is_clean", report.is_clean())?;

        Ok(result.into_any().unbind())
    }

    /// What is true of this configuration right now, as a dict. No
    /// parameters.
    ///
    /// A diagnostic like the four above it, and it fixes the sources for
    /// the same reason they do: the numbers live in the engine, and asking
    /// for them is what builds one.
    fn status(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let status = self.core_status(py)?;

        Ok(crate::telemetry::config_status(py, &status)?
            .into_any()
            .unbind())
    }

    /// How the fetches from this configuration's store have gone, as a
    /// dict. No parameters.
    ///
    /// Unlike `status()` this touches no engine: the remote slot is the
    /// same `&'static Remote` a `refresh_remote()` reaches, so asking a
    /// configuration that has never loaded does not fix its sources.
    fn remote_status(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let status = self.core_remote_status();

        Ok(crate::telemetry::remote_status(py, &status)?
            .into_any()
            .unbind())
    }

    /// The resolved section as data, without the model. No parameters.
    fn snapshot(&self, py: Python<'_>) -> PyResult<Snapshot> {
        let inner = Arc::clone(&self.inner);
        let dynamic = self.inner.dynamic(py, &inner)?;
        let outcome = py.detach(|| dynamic.builder().snapshot());

        outcome
            .map(|snapshot| Snapshot { inner: snapshot })
            .map_err(|error| self.raise(py, &error))
    }

    /// Drops the cached model and every hook. No parameters.
    ///
    /// Touches no Python during interpreter teardown.
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
        // A commit that was prepared and never applied holds a validated
        // tree. Nothing can apply it now, so it is dropped here with
        // everything else this configuration was holding.
        self.inner
            .prepared
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clear();
        // A Python remote source outliving finalization is the same crash
        // class a watcher thread is: the shim the engine holds is reached
        // from a leaked `static`, and dropping the object here is what
        // makes a late fetch answer "released" instead of calling into a
        // Python that is no longer there.
        self.inner.source.clear();

        // Every waiter ends here, and this is the only wake that is not an
        // install: a configuration nobody reloads again would otherwise
        // hold its notifier thread until the process exits.
        self.inner.wake.closed.store(true, Ordering::Release);
        self.inner.wake.changed.notify_all();

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
    /// The engine's own `ConfigStatus`, for the exposition next door.
    ///
    /// Not a conversion: `Exposition` renders from the engine's struct, so
    /// no number is turned into a Python float and back — and, more to the
    /// point, the monotonic instants inside it are never reconstructed
    /// from an elapsed time they cannot be recovered from.
    pub(crate) fn core_status(&self, py: Python<'_>) -> PyResult<dynamic_config::ConfigStatus> {
        let inner = Arc::clone(&self.inner);

        Ok(self.inner.dynamic(py, &inner)?.status())
    }

    /// The engine's own `RemoteStatus`, on the same terms.
    pub(crate) fn core_remote_status(&self) -> dynamic_config::RemoteStatus {
        self.inner.layers.remote.status()
    }

    /// Publishes the model for whatever the engine just installed.
    fn publish_current(&self, py: Python<'_>) -> PyResult<()> {
        let inner = Arc::clone(&self.inner);
        let installed = self.inner.dynamic(py, &inner)?.current();

        match installed {
            Some(tree) => self.inner.commit(py, &tree),
            None => Ok(()),
        }
    }

    /// A failed refresh as the exception a caller catches.
    ///
    /// The categorised error carries no part of the Python exception's own
    /// message — a store's exception routinely carries the URL it called —
    /// so the exception itself is attached as `__cause__`, where the
    /// traceback is and where a `raise ... from ...` would have put it.
    fn remote_failure(&self, py: Python<'_>, error: &Error) -> PyErr {
        let Some(original) = self.inner.source.take_failure() else {
            // Nothing Python refused: no source installed, or an async one.
            return errors::to_py_err(py, error);
        };

        let original = original.bind(py).clone();

        // `KeyboardInterrupt` and `SystemExit` are the interpreter
        // talking, not the store. Re-raising a Ctrl-C as `RemoteError`
        // would make a hung fetch uninterruptible in the one way Python
        // guarantees it is not.
        if !original.is_instance_of::<pyo3::exceptions::PyException>() {
            return PyErr::from_value(original);
        }

        let failure = errors::to_py_err(py, error);
        failure.set_cause(py, Some(PyErr::from_value(original)));

        failure
    }

    /// A crate error as the exception that mirrors it — with Pydantic's
    /// own scrubbed report attached when validation is what refused.
    ///
    /// `errors` is set for *every* refusal, empty when the schema had no
    /// report to give: a dataclass raises a message, and msgspec raises a
    /// message, so only Pydantic fills it. The attribute existing either
    /// way is what makes the stub's `errors: list[dict[str, Any]]` true —
    /// a program that reads it after catching `InvalidError` should find
    /// an empty list rather than an `AttributeError` that depends on
    /// which schema library the configuration happened to use.
    fn raise(&self, py: Python<'_>, error: &Error) -> PyErr {
        let failure = errors::to_py_err(py, error);

        if error.kind() == dynamic_config::ErrorKind::Invalid {
            let reports = self
                .inner
                .shared
                .reports
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .take();

            let _ = match reports {
                Some(reports) => failure.value(py).setattr("errors", reports),
                None => failure
                    .value(py)
                    .setattr("errors", pyo3::types::PyList::empty(py)),
            };
        }

        failure
    }
}

/// Which paths differ between two configuration values:
/// `changed_paths(previous, current)`.
///
/// Paths, never values, which is the audit half of a change. The
/// Python-side twin of [`dynamic_config::changed_paths`]: it takes what a
/// model dumps to, so two snapshots of the same model can be compared
/// without either of them being installed.
///
/// Parameters:
///     previous: the older value — a mapping, or anything this binding
///         can convert.
///     current: the newer one. A path only this has reads as `added`.
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
