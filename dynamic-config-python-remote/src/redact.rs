//! Keeping credentials out of everything this wheel says.
//!
//! Two rules travel together here, and both exist because a store's own error
//! message is not written with a Python traceback in mind.
//!
//! **A URL loses its password.** `etcd://app:hunter2@etcd.internal:2379` is a
//! perfectly ordinary endpoint, and the store crates quote the endpoint into
//! `describe()` — which is what `Origin::Remote` carries, what every error
//! message names, and what a `repr()` shows. The rule for removing it is
//! [`dynamic_config_store_core::redacted`], borrowed rather than re-derived:
//! it splits on the *last* `@`, because a password may itself contain one.
//!
//! **A resolved credential is scrubbed by value.** The URL rule only reaches
//! credentials that are *in* a URL. A Vault token, an AppRole secret id or an
//! etcd password is a bare string, and a store that quotes what it sent —
//! or an HTTP client that quotes a header — would put it in the message.
//! Every credential this wheel resolves is therefore replaced by `***` in any
//! text on its way to Python, whether or not anything is known to leak it.
//!
//! The second rule is the belt to the first's braces. It costs a handful of
//! `str::replace` calls on an error path that happens at most once per
//! refresh, and it is the only defence that survives a client library
//! deciding to be helpful in a version nobody here has read.

use dynamic_config_store_core::{redacted, LoneAuthority};

/// What has to be removed from anything this wheel says.
pub(crate) struct Redactor {
    /// `(raw, redacted)` for each configured URL that actually carries a
    /// credential. A URL with none is not in the list: replacing a string
    /// with itself is work, and an empty list is the common case.
    urls: Vec<(String, String)>,
}

impl Redactor {
    /// The redactor for a set of store URLs.
    ///
    /// `lone` is how a `scheme://something@host` authority with no colon is
    /// read — the one thing the store crates disagree about, which is why it
    /// is a parameter there and here.
    pub(crate) fn new<I, S>(urls: I, lone: LoneAuthority) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        Self {
            urls: urls
                .into_iter()
                .filter_map(|url| {
                    let raw = url.as_ref().to_owned();
                    let safe = redacted(&raw, lone);

                    (safe != raw).then_some((raw, safe))
                })
                .collect(),
        }
    }

    /// `text` with every configured URL and every named secret removed.
    ///
    /// URLs first: a URL is replaced by its redacted form, which keeps the
    /// host readable. Bare secrets after, because a secret that was *inside*
    /// a URL has already gone with it and what is left is the ones that
    /// travelled on their own.
    pub(crate) fn scrub(&self, text: &str, secrets: &[&str]) -> String {
        let mut scrubbed = text.to_owned();

        for (raw, safe) in &self.urls {
            scrubbed = scrubbed.replace(raw, safe);
        }

        for secret in secrets {
            // An empty credential is not a credential, and replacing the
            // empty string would put `***` between every character.
            if !secret.is_empty() {
                scrubbed = scrubbed.replace(secret, "***");
            }
        }

        scrubbed
    }
}

/// One URL with its credentials removed.
///
/// For the description a store reports, which is built once at construction
/// rather than per message.
pub(crate) fn safe(url: &str, lone: LoneAuthority) -> String {
    redacted(url, lone)
}

// No `#[cfg(test)]` module here, for the same reason the base binding has
// none: an extension module links no libpython, so this crate has no
// linkable Rust test target at all. The suite is pytest, and this module is
// covered through the surfaces that actually carry the risk — `repr()`,
// `describe()` and the message of a failed fetch. See
// `tests/test_redaction.py`.
