# Remote Stores in Rust, from Python

```sh
pip install dynamic-config-py[remote]
```

```python
from dynamic_config import DynamicConfig
from dynamic_config.remote import Etcd

config = DynamicConfig(Database, key="db").remote(
    Etcd(["http://etcd.internal:2379"], "myapp/db.json")
)
config.refresh_remote()
config.init()
```

The other half of [Remote Stores in Python](remote-stores.md). That page
is the door a Python `fetch()` goes through; this one is the Rust store
clients, compiled, behind an opt-in install.

**All eight stores are here** — Consul, etcd, Firestore, git, NATS, Redis,
S3 and Vault — each mirroring its Rust crate's builder.

| Store | Client | Runtime | The credential |
|---|---|---|---|
| `Consul` | `ureq`, blocking | none | An ACL token, or a bearer exchanged for one |
| `Etcd` | gRPC, async | tokio | A user and password, in the connection |
| `Firestore` | `ureq`, blocking | none | An access token, or the metadata server |
| `Git` | `gix`, blocking | none | An https token, or an ssh key |
| `Nats` | `async-nats` | tokio | A token, NKey, user pair or `.creds`, on connect |
| `Redis` | `redis`, blocking | none | A user and password, in the URL |
| `S3` | AWS SDK, async | tokio | A `ProvideCredentials` chain — see [below](remote-auth.md#s3-the-credential-that-is-a-trait) |
| `Vault` | `ureq`, blocking | none | A login that buys a token with a TTL |

## A second wheel

A wheel is built per platform, so a dependency in the ordinary wheel is
in every install of it — including the ones reading a single TOML file.
etcd speaks gRPC, the AWS SDK brings its own runtime and signing stack,
NATS brings a protocol, git brings `gix` and three more bring HTTP over
rustls: eight clients is most of an async ecosystem, in every install
that wanted a `config.toml`.

An extra alone cannot do it. `pip install dynamic-config-py[etcd]`
installs *Python distributions*; it cannot turn on a Cargo feature in a
binary that was compiled weeks ago on a release runner. So the extra
resolves to **a second wheel**, built from a second crate with the store
clients in it.

| | base wheel | remote wheel |
|---|---|---|
| Distribution | `dynamic-config-py` | `dynamic-config-py-remote` |
| Import | `dynamic_config` | `dynamic_config.remote` |
| Size (Linux x86-64, release) | 1.36 MB | 11.68 MB |
| Extension module | `dynamic_config._core` | `dynamic_config_remote._core` |
| tokio | never | one runtime, lazily |
| MSRV | 1.88 | 1.88 — one floor for the whole organisation since the 0.3.1 round |

One wheel rather than one per store, and the size table is why. Eight
build matrices is eight times the release runner for a saving nobody has
asked for, and 11.68 MB is a wheel: `numpy` is 18 MB and `cryptography`
is 4 MB. The measurement that decided it: **etcd and Vault alone were
2.39 MB**, so the other five — the AWS SDK, `async-nats`, `redis` and two
HTTP clients — cost about 6 MB between them, and no single one of them
dominates enough to be worth splitting out on its own.

**git added 2.99 MB of that**, measured the same way: 8.69 MB before and
11.68 MB after. It is `gix` and the `reqwest` client the store crate builds
for a private authority, plus the engine's three format features — which
this wheel did not need until git, because git is the one store here that
folds several files into one document and a fold is a parse and a
re-render. It is the second largest single contributor after the AWS SDK
and still nowhere near being worth its own build matrix.

The number to revisit at is around **50 MB**, where a wheel starts to be
something people notice in a container build. If it is ever crossed, the
split to make is **S3 alone**: the AWS SDK is the largest single
contributor and the only one that brings its own credential machinery, so
`dynamic-config-py[s3]` would be a clean seam and the other seven would
stay one wheel.

## The import name

`dynamic_config.remote` is a module in the **base** wheel that re-exports
`dynamic_config_remote`, which is what the second wheel installs. Both
names work; the dotted one is the one to write.

The obvious alternative was a namespace package — the base wheel shipping
`dynamic_config/*` and the remote wheel `dynamic_config/remote/*`, one
import tree. It was built and measured before being rejected, and it
fails in two ways that matter:

- **An editable base install cannot see it.** `maturin develop` installs
  a `.pth` pointing at the source tree, so `dynamic_config.__path__` is
  that one directory and a `dynamic_config/remote/` in site-packages is
  invisible. That is how this package is developed and tested, so the
  remote wheel could never have been tested against a developed base.
- **Uninstalling the base leaves an orphan.** With `dynamic_config/__init__.py`
  gone and `dynamic_config/remote/` still there, `import dynamic_config`
  still succeeds — as a PEP 420 namespace package with no API at all.

Two distributions that never share a directory have neither problem, at
the cost of one re-exporting module. Without the second wheel, importing
that module raises an `ImportError` naming the extra rather than a
`ModuleNotFoundError` naming a distribution nobody has heard of.


## Where the rest is

- [Credentials, TLS & the Runtime](remote-auth.md) — callables that
  rotate, private authorities, client certificates, the tokio runtime,
  and the S3 credential that is a trait. The cookbook.
- [Store Reference](remote-reference.md) — every constructor, argument
  by argument, and the error taxonomy.

## What is not exposed

- **The clients' own configuration types.** `etcd`'s `ConnectOptions`,
  Vault's, Consul's and Firestore's `ureq::Agent`, NATS' `ConnectOptions`
  and S3's `SdkConfig` are all taken by their Rust builders,
  so that options this project has never heard of keep working. There is
  no Python spelling for a `tonic` configuration or a `ureq` agent, so a
  **custom proxy**, a hand-built connector or a DNS resolver belongs to a
  deployment that uses the Rust crate directly.

  [TLS](remote-auth.md#tls-a-private-authority-and-a-client-certificate) used to be on
  this list and no longer is, and the difference is the shape of the
  surface rather than the effort: a certificate authority and a client
  certificate are *data*, so they cross. Anything still here is a client
  type, and a client type does not.

  One of those types also holds a credential kind that is therefore
  absent: NATS' JWT-with-signing-callback.
- **Several keys as one document — for the seven that are not git.**
  Consul, Redis and the rest can read a list of keys or a whole prefix and
  merge them; here each of them reads **one key**. That is a second
  vocabulary — a merge order, an overlap rule — and for a key/value store
  it is also a document that never existed at any instant, because the set
  is one request per key. A Python deployment that needs it can merge two
  sources instead, which is what the layering is already for.

  **`Git` is the exception, and the reason is the object model rather
  than the effort.** One fetch resolves one commit, and a commit has one
  tree, so a list of paths or a whole directory is read as of one instant
  with nothing arranged for it: no transaction, no listing race, no second
  round trip. So `GitKeys` is bound and the other seven still read one
  key.
- **`watch()`.** Every crate here can watch its store and push changes.
  That is a Rust callback on a Rust thread calling into Python, which is a
  second GIL story on top of this one; `refresh_remote()` on a timer is
  what Python has, and it is what the base wheel's remote path already
  offers.

  **Git is where that costs something**, and it is left costing it on
  purpose. It is the only store in the family whose *multi-file* sources
  can be watched at all — what moves is a ref, and what a ref names is a
  commit, so a watch wakes on the repository rather than on one file and
  the re-read that follows takes every file out of that one commit. Making
  git the exception would mean the second GIL story for one store, and a
  binding whose watch works for one of eight is a worse surface than one
  whose watch is absent for all eight. Against git a poll is cheap
  anyway: each tick is one ref advertisement, and only a ref that moved
  costs a transfer.
- **A credential with a lifetime the issuer stamped on it.**
  `Credential::expiring` hands the Rust crate a token *and its TTL*, so it
  is refreshed within a minute of expiry and after a refusal rather than
  per fetch. Python has the per-fetch shape only — every credential
  argument is called on every fetch — because the caching would have to
  live in Python anyway: nothing on a fetch may call back into the
  interpreter, so a Rust-side cache could not invoke a Python closure. A
  caller who mints an installation token per hour caches it in their own
  closure, which is four lines and is where the exchange already lives.
- **`from_client` / a shared connection.** Nothing in a Python process is
  holding an `etcd_client::Client` or an `aws_sdk_s3::Client` to share.
- **A Firestore service-account JSON key.** Absent in the Rust crate too,
  and as a recommendation rather than a gap: signing one means an RS256
  stack in a configuration library, and Google's own guidance is that a
  downloaded key is the option of last resort.

