//! A git repository, as a compiled object a Python `RemoteSource` wraps.
//!
//! The eighth store, and the one that is not a key/value client at all: a
//! shallow single-ref fetch into a bare object database, one blob — or one
//! tree — read out of one commit. Blocking, like [`crate::vault`] and
//! [`crate::consul`], so a program reading only git starts no tokio runtime.
//!
//! ## The source is built once, and the credential is a slot
//!
//! Six of the eight stores rebuild their client when the resolved credential
//! moves, because the credential is *inside* the client. That would be the
//! wrong shape here, and expensively so: a `GitSource` owns a working
//! directory, so rebuilding one throws away the object database and the next
//! fetch transfers the repository's whole tree again — for a store whose
//! headline property is that an unchanged ref transfers nothing. A named
//! [`cache_dir`](dynamic_config_git::Builder::cache_dir) is worse still: it is
//! claimed by the source that names it, so a rebuild would be refused by the
//! source it is replacing.
//!
//! So this file takes [`crate::s3`]'s shape instead. The source is built once,
//! with a [`Credential::from_fn`] closure reading a slot, and the fetch path
//! writes the credential Python resolved into that slot before the read. A
//! rotated token is then a mutex write: nothing is rebuilt, the object database
//! survives, and the next fetch presents the new value.
//!
//! Nothing in that closure touches Python — it reads a `Mutex<Resolved>` — for
//! the same reason S3's provider does not: it is called from inside the fetch,
//! with the GIL released.
//!
//! One consequence is worth naming rather than discovering. A closure
//! credential is *replaceable* as far as the store crate is concerned, so a
//! host that refuses it costs one extra attempt: `GitSource` invalidates what
//! it holds and tries once more, and the slot answers with the same value
//! because Python resolved it for this fetch already. One wasted round trip on
//! a refusal, in exchange for a rotation that costs no transfer at all.
//!
//! ## Everything that can be wrong is wrong at construction
//!
//! `GitSource::builder(..).build()` decides the path, the format, the commit
//! id, the working directory and whether a `TlsConfig` belongs on this url —
//! all of it before any network. It runs *here*, in the constructor, and its
//! complaint becomes a `ValueError` for the reason `_format.py` gives: a
//! configuration mistake should fail on the line it was written on, not at the
//! first refresh.

use std::path::PathBuf;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use dynamic_config::{Error, RemoteSource};
use dynamic_config_git::{Auth, Credential, GitSource, Keys, SshAuth};
use dynamic_config_store_core::LoneAuthority;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::errors::raised;
use crate::format;
use crate::redact::Redactor;
use crate::tls::Tls;

/// What to present to the host, as the facade resolved it for one call.
///
/// `first` and `second` are the two halves the shapes need: an HTTPS user name
/// and its token, an SSH key path, an SSH command. Which of them is a secret
/// depends on the kind, which is what [`secrets`](Self::secrets) is for.
#[derive(Clone, Default)]
struct Resolved {
    kind: String,
    first: String,
    second: String,
}

impl Resolved {
    /// The parts of this credential that must never appear in a message.
    ///
    /// A key *path* is not one: it names which key and is the question somebody
    /// debugging a refused login is actually asking, which is the split the
    /// Rust crate's `Debug` makes too. A custom `ssh` command is redacted
    /// whole, because a caller reaching for that hatch may well have put
    /// `sshpass -p …` in it and nothing here can tell.
    fn secrets(&self) -> Vec<String> {
        match self.kind.as_str() {
            "https" => vec![self.second.clone()],
            "ssh_command" => vec![self.first.clone()],
            _ => Vec::new(),
        }
    }

    /// The store crate's `Auth` for this credential.
    fn auth(&self) -> Result<Auth, Error> {
        Ok(match self.kind.as_str() {
            "anonymous" => Auth::Anonymous,
            "https" => Auth::Https {
                username: self.first.clone(),
                password: self.second.clone(),
            },
            "ssh_agent" => Auth::Ssh(SshAuth::Agent),
            // The path, not the key: `ssh` opens the file itself at every
            // fetch, so a rotated key needs no callable — the same reason
            // `ConsulAuth.kubernetes` carries a path rather than a token.
            "ssh_key" => Auth::Ssh(SshAuth::Key(PathBuf::from(&self.first))),
            "ssh_command" => Auth::Ssh(SshAuth::Command(self.first.clone())),
            other => return Err(Error::remote(format!("{other:?} is not a git credential"))),
        })
    }
}

/// A file, a list of them or a directory in a git repository, read through the
/// Rust client.
#[pyclass(module = "dynamic_config_remote._core", name = "GitStore", frozen)]
pub(crate) struct GitStore {
    source: GitSource,
    /// What the next fetch presents. Written by [`Self::fetch`] before the
    /// read, and read by the closure the source was built with.
    credential: Arc<Mutex<Resolved>>,
    /// Held across the slot write *and* the read, so two Python threads
    /// fetching one store cannot have one's credential used for the other's
    /// fetch. It costs nothing: `GitSource` serialises its own fetches anyway.
    reading: Mutex<()>,
    redactor: Redactor,
}

#[pymethods]
impl GitStore {
    /// Builds the store, and refuses at once anything the source cannot be.
    ///
    /// Nothing here reaches the network. It does create the working directory
    /// — a private temporary one unless `cache_dir` names another — because
    /// that is where the object database will go, and a directory that cannot
    /// be made is better reported now than at the first refresh.
    #[new]
    #[pyo3(signature = (
        url,
        keys,
        paths,
        reference,
        format,
        cache_dir,
        timeout,
        max_bytes,
        compact_after,
        tls,
    ))]
    #[allow(clippy::too_many_arguments)] // the builder's surface, one for one
    fn new(
        url: String,
        keys: &str,
        paths: Vec<String>,
        reference: Option<(String, String)>,
        format: Option<&str>,
        cache_dir: Option<PathBuf>,
        timeout: Option<f64>,
        max_bytes: Option<u64>,
        compact_after: Option<u32>,
        tls: Option<&Tls>,
    ) -> PyResult<Self> {
        // A git remote url carries a credential in the ordinary case rather
        // than the exotic one: `https://x-access-token:ghs_…@github.com/…` is
        // what every CI system writes. `LoneAuthority::Secret` because
        // `https://ghp_…@github.com/…` is a documented GitHub form in which
        // the whole authority is the token — the store crate's rule, taken
        // rather than re-derived.
        let redactor = Redactor::new([&url], LoneAuthority::Secret);

        let keys = match keys {
            // `unwrap_or_default` rather than an error: an empty path is
            // refused by `build` below, in the store crate's own wording.
            "one" => Keys::one(paths.into_iter().next().unwrap_or_default()),
            "several" => Keys::several(paths),
            "prefix" => Keys::prefix(paths.into_iter().next().unwrap_or_default()),
            other => {
                return Err(PyValueError::new_err(format!(
                    "{other:?} is not a way of naming what to read"
                )))
            }
        };

        let credential = Arc::new(Mutex::new(Resolved::default()));
        let slot = Arc::clone(&credential);

        let mut builder = GitSource::builder(url).path(keys).credential(
            // Read per fetch, and never anything but a mutex read: this
            // closure runs inside the store crate's fetch, where the GIL is
            // released and Python may not be touched.
            Credential::from_fn(move || slot.lock().unwrap_or_else(PoisonError::into_inner).auth()),
        );

        if let Some((kind, name)) = reference {
            builder = match kind.as_str() {
                "branch" => builder.branch(name),
                "tag" => builder.tag(name),
                "commit" => builder.commit(name),
                other => {
                    return Err(PyValueError::new_err(format!(
                        "{other:?} is not a git reference"
                    )))
                }
            };
        }

        if let Some(format) = format::maybe(format)? {
            builder = builder.format(format);
        }

        if let Some(directory) = cache_dir {
            builder = builder.cache_dir(directory);
        }

        if let Some(timeout) = timeout {
            builder = builder.with_timeout(Duration::from_secs_f64(timeout));
        }

        if let Some(bytes) = max_bytes {
            builder = builder.max_bytes(bytes);
        }

        if let Some(transfers) = compact_after {
            builder = builder.compact_after(transfers);
        }

        // git expresses the whole vocabulary — `reqwest` takes a root
        // certificate and an identity as bytes, and the store crate reads a
        // file where one was named. What it refuses is a `TlsConfig` on a url
        // with no TLS in it, which `build` says below.
        if let Some(tls) = crate::tls::wanted(tls) {
            builder = builder.tls(tls);
        }

        let source = builder
            .build()
            .map_err(|error| refused(&error, &redactor))?;

        Ok(Self {
            source,
            credential,
            reading: Mutex::new(()),
            redactor,
        })
    }

    /// Fetches the ref and reads the file, or files, at that commit.
    ///
    /// The three credential arguments are the auth method as the facade
    /// resolved it for *this* call — every callable already called.
    #[pyo3(signature = (kind, first, second))]
    fn fetch(
        &self,
        py: Python<'_>,
        kind: String,
        first: String,
        second: String,
    ) -> PyResult<(String, String)> {
        let credential = Resolved {
            kind,
            first,
            second,
        };
        let secrets = credential.secrets();

        // The GIL is released for the whole of it — the handshake, the
        // negotiation, the transfer and the decompression. Nothing below
        // re-enters Python, which is what makes that safe rather than merely
        // fast: the credential was resolved above, and what the source's own
        // closure reads is the slot written on the next line.
        let read = py.detach(move || {
            let _reading = self.reading.lock().unwrap_or_else(PoisonError::into_inner);

            *self
                .credential
                .lock()
                .unwrap_or_else(PoisonError::into_inner) = credential;

            self.read()
        });

        let secrets: Vec<&str> = secrets.iter().map(String::as_str).collect();

        read.map_err(|error| raised(&error, &self.redactor, &secrets))
    }

    /// Names the store, redacted. What the engine records as provenance.
    ///
    /// It gains the **commit** once one has been read, because "which commit
    /// is this program actually serving" is the first question of every
    /// configuration-in-git incident and a branch name does not answer it.
    fn describe(&self) -> String {
        self.source.describe()
    }

    fn __repr__(&self) -> String {
        format!("<{}>", self.source.describe())
    }
}

impl GitStore {
    /// One fetch, and the document it resolved to.
    fn read(&self) -> Result<(String, String), Error> {
        let fetched = self.source.fetch()?;

        Ok((fetched.text, format::named(fetched.format).to_owned()))
    }
}

/// A source that cannot be built, as the exception the mistake deserves.
///
/// `ValueError` rather than this wheel's `StoreError`: nothing remote has
/// happened, and the line the argument was written on is where a reader wants
/// to be sent. Scrubbed through the redactor as well as by the store crate,
/// which already redacts the url in the messages that quote it — the belt to
/// its braces, and the one that reaches the messages that do not.
fn refused(error: &Error, redactor: &Redactor) -> PyErr {
    PyValueError::new_err(redactor.scrub(&error.to_string(), &[]))
}
