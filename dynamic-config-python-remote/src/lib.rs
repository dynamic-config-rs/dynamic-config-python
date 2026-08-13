//! The Rust remote stores, for the Python bindings.
//!
//! A second wheel, `dynamic-config-py-remote`, imported as
//! `dynamic_config_remote` and reached through `dynamic_config.remote` — the
//! name the base wheel keeps free for it. It exists because a wheel is built
//! per platform: a gRPC stack and an HTTP client in the ordinary wheel would
//! be in every install, including the ones reading a single TOML file.
//!
//! ## It is a client, not a second binding
//!
//! Nothing here talks to the base wheel in Rust. The two are separate
//! extension modules with separate static copies of the engine, so a
//! `RemoteSource` built here is not the trait the base wheel knows and
//! `pyo3` could not carry it across if it were. What crosses is what crosses
//! for any Python store: a `(text, format)` pair, through the path item 05
//! already built.
//!
//! That is the whole architecture, and it is why the base wheel needed no
//! change to accept these stores. It also means the established rules apply
//! unaltered — the GIL is released for the fetch, a failure is reported and
//! never fatal, and the document never appears in a diagnostic.
//!
//! ```text
//! config.refresh_remote()                base wheel, GIL released
//!   → the Python facade's Etcd.fetch()   GIL, briefly
//!       → resolves the credentials       a Python callable, if there is one
//!       → EtcdStore.fetch(user, pw)      this wheel
//!           → py.detach                  GIL released again
//!           → tokio, one request
//!   → (text, "json") back to the base wheel
//! ```
//!
//! ## What is here
//!
//! All eight stores: Consul, etcd, Firestore, git, NATS, Redis, S3 and Vault.
//!
//! They are four shapes rather than eight. **Blocking HTTP with a token that
//! expires** — Vault, Consul, Firestore — where a login is cached by the Rust
//! crate and the *login credential* above it is what a Python callable
//! rotates. **Async, credential in the connection** — etcd, NATS — where a
//! rotated credential is a reconnection and the runtime below drives it.
//! **Redis**, which is the first shape with the login removed. And then the
//! two whose credential is not inside a client at all, so that rotating it
//! rebuilds nothing: [`s3`], whose credential is a `ProvideCredentials`
//! implementation asked per request, and [`git`], which owns an object
//! database a rebuild would throw away. Both are a slot the fetch path writes,
//! and both files say why.
//!
//! ## TLS crosses because it was built to
//!
//! Every store here takes a [`tls::Tls`] — the Python spelling of the store
//! crates' one `TlsConfig` — and it is the only client-configuration surface
//! that crosses at all. That is not luck: `TlsConfig` holds paths and PEM
//! bytes and names no client type anywhere, which is what makes it
//! expressible in a language with no word for a `tonic` TLS configuration.
//! Two stores cannot express all of it and **refuse the part they cannot**,
//! at construction, in the store crates' own wording. [`git`] takes all of it
//! and refuses something else: a `TlsConfig` on a url that is not `https://`,
//! because an ssh remote's trust lives in `known_hosts` rather than in a
//! certificate authority.

use pyo3::prelude::*;

mod consul;
mod errors;
mod etcd;
mod firestore;
mod format;
mod git;
mod nats;
mod redact;
mod redis;
mod runtime;
mod s3;
mod tls;
mod vault;

#[pymodule]
fn _core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    errors::register(module)?;
    module.add_class::<tls::Tls>()?;
    module.add_class::<consul::ConsulStore>()?;
    module.add_class::<etcd::EtcdStore>()?;
    module.add_class::<firestore::FirestoreStore>()?;
    module.add_class::<git::GitStore>()?;
    module.add_class::<nats::NatsStore>()?;
    module.add_class::<redis::RedisStore>()?;
    module.add_class::<s3::S3Store>()?;
    module.add_class::<vault::VaultStore>()?;
    module.add_function(wrap_pyfunction!(runtime_started, module)?)?;
    module.add(
        "__doc__",
        "The compiled remote stores behind dynamic_config.remote.",
    )?;

    // Three numbers, because they move on three schedules: this wheel's, the
    // engine it was built against, and the base wheel it must agree with —
    // which is `__version__` here only because the two ship together.
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add("__engine_version__", dynamic_config::VERSION)?;

    Ok(())
}

/// Whether the tokio runtime has been started.
///
/// Public because it is the only way to *test* the promise this wheel makes —
/// that importing it starts no threads, and that a store which needs no
/// runtime does not start one either. Nothing else should read it.
#[pyfunction]
fn runtime_started() -> bool {
    runtime::started()
}
