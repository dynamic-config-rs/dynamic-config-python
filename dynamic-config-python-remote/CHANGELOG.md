# Changelog

All notable changes to `dynamic-config-py-remote` are documented here. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Before 1.0, a breaking change bumps the **minor** version and anything else
bumps the patch. A change to the minimum supported Python version is
breaking.

This distribution moves with `dynamic-config-py` rather than with the Rust
crates: the two ship together, and `dynamic_config.remote` in the base wheel
is the door into this one.

<!-- Keep this template. Add entries under `Unreleased` as you go, and move
     the whole block under a new version heading at release time.
     (Spelled `_Unreleased_` here so cargo-release's `exactly = 1` search
     for the real heading matches only the real heading.)

## [_Unreleased_]

### Added
### Changed
### Deprecated
### Removed
### Fixed
### Security

-->

## [Unreleased]

## 0.1.0 — 2026-08-14

### Added

- **`Git`, `GitAuth` and `GitKeys`: the eighth store.** A file — or a list
  of them, or a whole directory — read out of one commit of a git
  repository, over HTTPS with a token or over SSH with a key. It mirrors
  `dynamic_config_git::GitSource`: the ref is `branch=`, `tag=` or
  `commit=`, the path is a `str`, a list or a `GitKeys`, and `cache_dir`,
  `max_bytes` and `compact_after` are the working directory's. Blocking, so
  it starts no tokio runtime.

  **It is the one store here that reads several files as one document**, and
  the reason is git's object model rather than any effort: one fetch
  resolves one commit, and a commit has one tree, so a set is read as of one
  instant with no transaction, no listing race and no second round trip. A
  list merges in call order — later wins — and a directory merges as
  disjoint sections, where an overlap is a deployment bug and is reported as
  one.

  **Every credential argument accepts a callable**, which matters more here
  than anywhere else: a GitHub App installation token lives one hour and a
  watcher lives for the life of the process. A rotated token rebuilds
  **nothing** — the credential is a slot the fetch path writes, as S3's is,
  because a rebuilt source would discard the object database and re-transfer
  the repository's whole tree.

  **Four things are refused at construction rather than half-applied**, each
  naming the call and the way out: two references (three keyword arguments
  have no order, where Rust's three builder calls do), an https credential
  on an ssh url, an ssh credential on an https url — silently anonymous
  otherwise — and `tls` on a url with no TLS in it. Those messages never
  quote the url, because a git remote url routinely carries a token and the
  redaction that removes one lives in Rust.

  `watch()` is still exposed for no store, git included, and git is the only
  one where that costs something: it is the only store in the family whose
  multi-file sources can be watched at all. The reason it stays unexposed is
  in the book, next to the reason the other seven cannot.

  The wheel grew from **8.69 MB to 11.68 MB**: `gix`, the `reqwest` client
  the store crate builds for a private authority, and the engine's three
  format features — which this wheel did not need until a store here folded
  several documents into one.

- **`TlsConfig`, and a `tls=` argument on all seven stores.** A private
  certificate authority and a client certificate (mTLS), each as a file path
  or as PEM bytes, mirroring `dynamic_config_store_core::tls::TlsConfig`
  method for method: `with_ca_certificate_file`, `with_ca_certificate_pem`,
  `with_client_certificate_files`, `with_client_certificate_pem` and
  `is_empty`. Every builder answers a new one, so a configuration shared
  between two stores cannot be changed by either, and an empty one is the
  platform's own trust store rather than "no TLS".

  It crosses because the Rust surface is **data** — paths and PEM bytes, no
  client type in any signature — which is the one thing that made a Python
  spelling possible at all. `Pem`, `ClientCertificate` and
  `CertificateAndKey` stay in Rust: a caller says what they have rather than
  asking which spelling it was, and each bound type would be one more place
  a private key could be rendered.

  **Two stores refuse part of it rather than ignoring it**, with a
  `ValueError` at construction in the store crates' own wording, naming the
  call and the way out. `Nats` takes certificate *paths* only, because
  `async-nats` opens the files itself; `S3` takes no client certificate,
  because the AWS SDK's TLS context is a trust store with no slot for one. A
  caller who believes they pinned an authority and did not is worse off than
  one whose program will not start. `Redis` needs a `rediss://` URL, and
  material on a `redis://` one is refused by the client as it is built.

  **The private key never appears in a diagnostic.** `repr()` delegates to
  the Rust type's `Debug` — a path where there is one, `<redacted>` where the
  key is bytes — rather than rendering the arguments a second time in the
  language nobody would think to check.

  The wheel now enables `dynamic-config-etcd/tls` and
  `dynamic-config-redis/tls`, without which `Etcd` and `Redis` would have no
  TLS constructor to call; an extra cannot turn on a Cargo feature, so the
  choice is made once, at build time. `tls-roots` is deliberately not
  enabled: it resolves to a `tonic` call the store crate never makes, so it
  would add a crate to every wheel and change nothing.

- **The first release.** All seven Rust store clients, compiled, for
  `dynamic-config-py`. Installed as `pip install dynamic-config-py[remote]`
  and imported as `dynamic_config.remote`.

- **`Etcd(endpoints, key, ...)`** — a key in an etcd v3 store, mirroring
  `dynamic_config_etcd::Etcd`: the endpoints, the key, the format (read from
  the key's extension when it has one), the per-fetch deadline and etcd's
  user/password authentication.

- **`Vault(address, mount, path, auth=..., ...)`** and **`Auth`** — a secret
  in Vault's KV v2 store, mirroring `dynamic_config_vault::Vault` and its
  `Auth` enum: token, AppRole, Kubernetes, JWT/OIDC, userpass, LDAP and
  certificate, with `at_mount`, `with_role` and `with_token_path`. The
  builders return a new `Auth` rather than mutating, so one shared between
  two stores cannot be changed by either.

- **`Consul(address, key, ...)`** and **`ConsulAuth`** — a key in Consul's
  KV store, mirroring `dynamic_config_consul::Consul` and its `Auth` enum:
  anonymous, a supplied ACL token, and a bearer token exchanged at an auth
  method (`kubernetes`, `jwt`), with `with_bearer_file` and `with_meta`. The
  bearer is what a callable rotates; the ACL token stays the Rust crate's to
  renew.

- **`Firestore(project, path, auth=..., ...)`** and **`FirestoreAuth`** — a
  document in Firestore, mirroring `dynamic_config_firestore::Firestore`:
  the section key, the database, the emulator endpoint, and
  `metadata_server` / `access_token` / `emulator`, with `with_url` for a
  sidecar. `auth` is **required** here where the Rust builder defaults to
  the emulator: as a constructor default, *send no credentials* would
  quietly produce a 401 against the real service.

- **`Nats(servers, bucket, key, ...)`** and **`NatsAuth`** — a key in a
  JetStream KV bucket, mirroring `dynamic_config_nats::Nats`. Every
  credential shape `ConnectOptions` constructs: a token, a user and
  password, an NKey seed, and a `.creds` file by contents or by path — the
  path form re-read on every fetch. Connecting is deferred to the first
  fetch, which is a deliberate difference from the Rust constructor.

- **`Redis(url, key, ...)`** — a key in Redis, mirroring
  `dynamic_config_redis::Redis`. `user` and `password` are **arguments**
  rather than part of the URL, because a callable cannot rotate a substring
  of a string passed at construction; they are percent-encoded into the
  authority and replace any the URL carried.

- **`S3(bucket, key, ...)`** — an object in S3, mirroring
  `dynamic_config_s3::S3`, with `region` and `endpoint` in place of an
  `SdkConfig` a Python caller cannot build. Its credential surface is the
  AWS SDK's `ProvideCredentials` chain rather than a string: **no
  credential arguments means the SDK's own chain** (environment, profile,
  instance role, ECS task role, IRSA), and credential arguments install a
  provider that answers from a slot the fetch path writes. A rotated key
  therefore rebuilds nothing, and the SDK's identity cache is switched off
  because it would otherwise cache the first key forever.

- **Every credential argument accepts a callable.** A configuration watcher
  outlives its credentials — a Vault token expires, an AppRole secret id is
  rewritten by a sidecar, an etcd password is rotated — so a credential is
  resolved on *every* fetch. A value that has changed rebuilds the client
  with it; a value that has not leaves the store's own token cache and open
  connection alone, which is what keeps one login from becoming one per
  refresh.

- **One tokio runtime**, lazily, owned by the module. Started when the first
  store that needs one is constructed — `Etcd`, `Nats` and `S3` do;
  `Consul`, `Firestore`, `Redis` and `Vault` are blocking and do not — and
  never shut down, because `Runtime::drop` blocks until its workers park and
  an `atexit` hook that can block for the length of a network read would
  turn a clean exit into a hang. Safe here because no task on it ever
  touches Python, S3's credential provider included: it reads a resolved
  value rather than calling back into the interpreter.

### Changed

- **One example per store, and one for what crosses all of them.** The three
  examples — etcd-and-Vault, "the other five", and TLS — are now nine:
  `01_etcd`, `02_vault`, `03_consul`, `04_redis`, `05_nats`, `06_s3`,
  `07_firestore`, `08_git`, and `09_private_ca_and_client_certificate`. Each
  of the eight shows that store's own credential story and what is
  characteristic of it — the runtime etcd starts, the two clocks Consul's
  two secrets keep, the `.creds` file NATS re-reads, the client S3 does not
  rebuild, the identity Firestore renews itself, the commit git reads a set
  of files out of — rather than a template with the name changed. The TLS
  one stays cross-cutting, because one vocabulary across eight stores is the
  claim it exists to demonstrate, and because the three refusals read as a
  set.

  Every one of them runs to completion with nothing installed and nothing
  listening, and each was verified against a real server: etcd, Vault,
  Consul, Redis, NATS, MinIO and the Firestore emulator in containers, and
  git against a repository built by `git`. Two errors in the TLS example's
  setup instructions were found that way and fixed — `openssl` will not
  create `/tmp/tls`, and it writes a private key `0600` and owned by you,
  which the Vault image's unprivileged user then cannot read.

### Security

- **Credentials never appear in a diagnostic** — not in an exception message,
  not in a `repr`, not in `describe()`, which is what provenance records. A
  store URL loses its password by the rule the Rust store crates share
  (`dynamic-config-store-core`), splitting on the *last* `@` so that a
  password containing one is removed whole; and every credential resolved for
  a call is additionally scrubbed by value from anything that call reports,
  which is the defence that survives a client library deciding to quote what
  it sent. The one thing the stores disagree about is an authority with no
  colon in it, and that stays a parameter rather than a fork:
  `nats://token@host` is a secret and `redis://user@host` is a user name.
