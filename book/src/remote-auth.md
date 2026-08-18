# Credentials, TLS & the Runtime

The operational half of the remote wheel: how a credential rotates
without a restart, how a private authority is trusted, and where the
tokio runtime that drives it all comes from.

## Credentials may be callables

**Every credential argument accepts a `str` or a callable returning
one.** This is the feature rather than a convenience: a configuration
watcher outlives its credentials. A Vault token has a TTL, an AppRole
secret id is rewritten by a sidecar, a workload-identity JWT is refreshed
by a daemon on the node, an etcd password is rotated by whoever rotates
passwords. A store holding the string it was constructed with is a 403
three hours later that nobody can fix without a restart.

```python
from pathlib import Path

from dynamic_config.remote import (
    S3, Auth, Consul, ConsulAuth, Etcd, Firestore, FirestoreAuth, Git,
    GitAuth, Nats, NatsAuth, Redis, Vault,
)

Etcd(
    ["http://etcd.internal:2379"], "myapp/db.json",
    user="myapp",
    password=lambda: os.environ["ETCD_PASSWORD"],
)

Vault(
    "https://vault.internal:8200", "secret", "myapp/db",
    auth=Auth.token(lambda: Path("/var/run/vault/token").read_text()),
)

Vault(
    "https://vault.internal:8200", "secret", "myapp/db",
    auth=Auth.app_role(role_id, secret_id=lambda: read_the_secret_id()),
)

Consul(
    "http://consul.internal:8500", "myapp/db.json",
    auth=ConsulAuth.token(lambda: os.environ["CONSUL_HTTP_TOKEN"]),
)

Redis(
    "redis://redis.internal:6379", "myapp/db.json",
    password=lambda: Path("/var/run/redis/password").read_text(),
)

Nats(
    ["nats://nats.internal:4222"], "config", "db.json",
    # Reads the file on every fetch, which is what an operator replacing
    # a `.creds` under a running process needs.
    auth=NatsAuth.credentials_file("/etc/myapp/nats.creds"),
)

Firestore(
    "my-project", "config/db",
    auth=FirestoreAuth.access_token(lambda: mint_a_token()),
)

S3(
    "myapp-config", "prod/db.json",
    access_key_id=lambda: current_key_id(),
    secret_access_key=lambda: current_secret(),
)

Git(
    "https://github.com/acme/config.git", "services/api/db.yaml",
    # A GitHub App installation token lives one hour, and this is the
    # store where that is the ordinary case rather than the exotic one.
    auth=GitAuth.token(lambda: mint_an_installation_token()),
)
```

The callable is invoked **on every fetch**, as ordinary Python, on the
thread that asked for the refresh — before the GIL is released for the
network read. It may block, and it may raise, in which case the refresh
fails the way any other fetch failure does: the previous document and the
previous model both keep serving.

When what it returns has **changed**, the store rebuilds its client with
the new credential. When it has not — overwhelmingly the common case —
nothing is rebuilt and the store's own token cache is untouched. That
second half matters as much as the first: rebuilding per fetch would turn
one Vault login into one per refresh, which is a poor thing to do to a
secrets store, and one Redis connection into one per refresh, which is a
poor thing to do to anything.

`S3` is the exception and the better end of the same bargain: its
credential is not inside the client at all, so a rotation rebuilds
**nothing**. [Why](#s3-the-credential-that-is-a-trait) is a section of
its own.

Two layers of renewal are at work and they do not fight. The Rust crate
already refreshes the *token* it was issued, within a minute of expiry
and reactively after a 403. What a callable adds is the layer above — the
**login credential itself** rotating, which the Rust crate cannot notice
because it was handed a `String`.

Which layer a callable belongs to differs per store, and the ones where
it is *not* needed are worth naming:

| Store | Where a callable earns its place |
|---|---|
| `Vault` | The login credential — a secret id, a password, a JWT. The Vault token is the crate's to renew |
| `Consul` | The **bearer** presented to an auth method. The ACL token is the crate's |
| `Firestore` | `FirestoreAuth.access_token`, which cannot renew. `metadata_server` renews itself and needs none |
| `Etcd`, `Redis`, `Nats` | The credential itself: it lives in the connection, so a new value is a reconnection |
| `S3` | The key pair — but nothing is rebuilt, because the SDK asks per request |
| `Git` | The https token — and nothing is rebuilt either, for a different reason: rebuilding would throw away the object database |

Three of them read a *file* instead:
`ConsulAuth.kubernetes(...)` and `Auth.kubernetes(...)` carry a **path**,
which the Rust crate re-reads at every login, so a projected
service-account token the kubelet rotates already works with no callable
at all — the path is `SERVICE_ACCOUNT_TOKEN`, exported so a deployment
that mounts it somewhere else can say where without spelling
`/var/run/secrets/...` from memory; `GitAuth.ssh_key(...)` is the same, one layer further out, because
`ssh` opens the key file itself at every fetch.
`NatsAuth.credentials_file(...)` is the idea one layer up: it *is* a
callable, reading the file on every fetch.

**`Git` is the second store whose rotation rebuilds nothing**, and the
reason is not S3's. A `GitSource` owns a
working directory — a bare object database, filled by the first fetch —
so rebuilding one would discard every object it holds and re-transfer the
repository's whole tree, for a store whose headline property is that an
unchanged ref transfers nothing. A named `cache_dir` is worse: it is
claimed by the source that holds it, so a rebuild would be refused by the
source it was replacing. The credential is therefore a **slot** the fetch
path writes and the source reads, exactly as it is for S3, and a rotation
costs a mutex write.

That has one visible edge. A closure credential is *replaceable* as far
as the store crate is concerned, so a host that refuses one costs an
extra attempt: the source invalidates what it holds and tries once more,
and the slot answers with the same value because Python resolved it for
this fetch already. One wasted round trip on a refusal, in exchange for a
rotation that costs no transfer.

## Credentials never appear in a diagnostic

Not in an exception message, not in a `repr`, not in `describe()` — which
is what `Origin` records and what every remote error carries.

```python
>>> Etcd(["http://app:hunter2@etcd.internal:2379"], "myapp/db.json")
<etcd http://app:***@etcd.internal:2379 key myapp/db.json>

>>> Auth.app_role("role-id", "hunter2-secret-id")
Auth.app_role('role-id', '***')
```

Two rules, and they are belt and braces:

- **A URL loses its password**, by the rule the Rust store crates use
  rather than a second copy of it: split on the *last* `@`, because a
  password may itself contain one. The user name survives, because
  redaction that hides the half worth seeing is redaction nobody can
  debug through.

  The one thing the stores disagree about is what an authority with **no
  colon** in it means, and the shared rule takes it as an argument rather
  than being forked: `nats://token@host` is a *secret*, and
  `redis://user@host` is a *user name*.

  ```python
  >>> Nats(["nats://hunter2@nats.internal:4222"], "config", "db.json")
  <nats nats://***@nats.internal:4222 bucket config key db.json>
  >>> Redis("redis://app@redis.internal:6379", "myapp/db.json")
  <redis redis://app:***@redis.internal:6379 key myapp/db.json>
  ```
- **Every credential resolved for a call is scrubbed by value** from
  anything that call reports. The URL rule only reaches a credential that
  is *in* a URL; this one reaches a client library that decided to be
  helpful in a version nobody here has read.

The non-secret half of an auth method stays printable, and the split is
the one the Rust crate's `Debug` makes: an AppRole role id and a userpass
user name are shown, the secret id and the password are not.

## TLS: a private authority and a client certificate

A deployment behind an **internal certificate authority** — an enterprise
CA, a TLS-inspecting proxy, a MinIO with its own certificate, a GitLab on
the company's own root — needs to trust one more certificate than the
platform does. A hardened one needs to *present* one as well. Every store
here takes both, spelled the same way, through one type:

```python
from dynamic_config.remote import Auth, TlsConfig, Vault

vault = Vault(
    "https://vault.internal:8200", "secret", "myapp/db",
    auth=Auth.kubernetes("myapp"),
    tls=TlsConfig()
        .with_ca_certificate_file("/etc/ssl/private-ca.pem")
        .with_client_certificate_files("/etc/ssl/app.crt", "/etc/ssl/app.key"),
)
```

This is the one client-configuration surface that crosses into Python at
all, and that is a property of how it was built rather than of how hard
anyone tried. `TlsConfig` is `dynamic_config_store_core::tls::TlsConfig`,
which holds **paths and PEM bytes and nothing else** — no `tonic`
configuration, no `ureq::Agent`, no `SdkConfig` anywhere in it. A surface
made of data has a Python spelling; a surface made of a client's own types
does not, which is why [everything else on that list](remote-wheel.md#what-is-not-exposed)
is still on it.

Four settings, in two spellings each:

| | |
|---|---|
| `TlsConfig()` | The platform's own trust store, no client certificate |
| `.with_ca_certificate_file(path)` | Trust the authority in this PEM file |
| `.with_ca_certificate_pem(bytes)` | Trust the authority in these PEM bytes |
| `.with_client_certificate_files(certificate, key)` | Present this certificate and key (mTLS) |
| `.with_client_certificate_pem(certificate, key)` | The same, from bytes |
| `.is_empty()` | Whether this asks for anything at all |

Both spellings exist because both deployments do. A file is what a
Kubernetes secret mount or an `/etc/ssl` layout produces; bytes are what a
program that already fetched its material from a secrets manager has, and
writing those to a temporary file so a client could read them back would
put a private key on a disk that never asked for one.

Three smaller things hold everywhere. An **empty** `TlsConfig` is not "no
TLS" — it is the platform's trust store, so `tls=None` and
`tls=TlsConfig()` are the same store. A **named authority replaces** the
platform trust store rather than joining it, because that is what pinning
means; a deployment needing both puts both in the one file — with one
exception, `Git`, where the authority is **added** to the platform's, so
that one source configuration reaches both a private GitLab and
github.com. And **nothing is read at construction**: the files are opened when the store builds its
client, so a missing certificate is an error naming the path at the first
refresh rather than an exception in the middle of building a
configuration.

`Pem`, `ClientCertificate` and `CertificateAndKey` are **not** bound. They
exist in Rust so a store can ask *which spelling was this*; a Python caller
never asks — they say what they have — so binding them would add three
names to the surface and three more places a private key could be
rendered.

### What each store accepts, and what two of them refuse

| Store | CA from a file | CA from bytes | Client certificate |
|---|---|---|---|
| `Consul`, `Etcd`, `Firestore`, `Redis`, `Vault` | yes | yes | yes |
| `Git` | yes | yes | yes — on an `https://` url, and **refused** on any other |
| `Nats` | yes | **no** — `async-nats` opens the file itself | file paths only |
| `S3` | yes | yes | **no** — the SDK's TLS context has no slot for one |

**The refusals are refusals, not omissions.** A binding that quietly
ignored either would leave a program believing it had pinned a private
authority when it had not, which is worse than a program that will not
start — so both raise a `ValueError` at construction, in the store crates'
own wording, naming the call and the way out:

```python
>>> Nats(["tls://nats.internal:4222"], "config", "db.json",
...      tls=TlsConfig().with_ca_certificate_pem(ca_bytes))
ValueError: nats tls://nats.internal:4222 bucket config key db.json: a
certificate authority from PEM bytes cannot be expressed here, and is
refused rather than ignored; `async-nats` opens the file itself; name a
file with `with_ca_certificate_file`

>>> S3("myapp-config", "prod/db.json",
...    tls=TlsConfig().with_client_certificate_files("app.crt", "app.key"))
ValueError: s3 myapp-config/prod/db.json: a client certificate cannot be
expressed here, and is refused rather than ignored; the AWS SDK's TLS
context has a trust store and no client-certificate slot; mTLS to an
S3-compatible server means building the connector, which only the Rust
crate can do
```

`Git`'s is the same rule pointed at a different mistake. It expresses all
four settings, and only over `https://`: an `ssh://` remote authenticates
its host through `known_hosts` and its client through a key, so a
certificate authority has nothing to do with it and is **refused rather
than half-applied**.

```python
>>> Git("ssh://git@github.com/acme/config.git", "db.yaml",
...     auth=GitAuth.ssh_agent(),
...     tls=TlsConfig().with_ca_certificate_file("/etc/ssl/private-ca.pem"))
ValueError: git: `tls` configures the https transport and this url is not
an https one, so it cannot be applied here and is refused rather than
ignored; an ssh remote authenticates its host through `known_hosts` and
its client through a key — GitAuth.ssh_agent(), GitAuth.ssh_key(path) or
GitAuth.ssh_command(command)
```

Its refusals are the one place this wheel writes its own wording rather
than borrowing the store crate's, and for a reason: **a git remote url
routinely carries a token**, the redaction that removes one lives in Rust,
and a message assembled in Python has no way to reach it. So these
messages name the argument and never the url. The store crate refuses the
same configuration again when it builds, which is the belt to this
braces.

At construction rather than at the first fetch, and before the tokio
runtime is started: a configuration a store cannot express should be an
error at the line it was written on, not two worker threads and a failure
one refresh later. The Rust crates refuse the same thing again when they
build their client, which is the belt to this braces.

Two stores have one more thing worth knowing. **`Nats` turns TLS on** when
an authority is named — the store crate sets `require_tls`, so a `nats://`
URL that would have negotiated plaintext fails rather than quietly
connecting without the authority just named. And **`Redis` needs a
`rediss://` URL**: TLS material on a `redis://` one is refused too, but by
the client as it is built, so that arrives as a `RemoteError` at the first
fetch. The URL is parsed where it is used rather than twice by two
implementations that could disagree.

One consequence of how the wheel is compiled: it enables the `tls` feature
of `dynamic-config-etcd` and `dynamic-config-redis`, which is what makes
`Etcd` and `Redis` able to speak TLS at all — an extra cannot turn on a
Cargo feature, so the choice is made once, here, and costs about 0.2 MB in
the wheel every install carries. It does **not** enable
etcd's `tls-roots`, which would resolve to a `tonic` call the store crate
never makes and add a native-certificate crate to every wheel for no
change in behaviour. So etcd over TLS from Python trusts the authority you
name, and a client certificate for etcd goes with one.

### The private key never appears

It is the sharpest secret this package handles, and `repr` is where a
`repr` usually leaks one. `TlsConfig.__repr__` **delegates to the Rust
type's own `Debug`** rather than rendering the arguments again:

```python
>>> TlsConfig().with_client_certificate_files("/etc/ssl/app.crt", "/etc/ssl/app.key")
TlsConfig { ca_certificate: None, client_certificate: ClientCertificate {
certificate: file /etc/ssl/app.crt, key: "file /etc/ssl/app.key" } }

>>> TlsConfig().with_client_certificate_pem(certificate, private_key)
TlsConfig { ca_certificate: None, client_certificate: ClientCertificate {
certificate: <pem bytes>, key: "<redacted>" } }
```

A **path** is printed, because it names which key and is the question
somebody debugging this is actually asking; **bytes** are withheld, because
they are the key. One implementation of that rule, in the crate that owns
the type, with a planted-key test over it — a second copy here would be the
one nobody thinks to check. The remote wheel's suite plants a private key
of its own and greps every diagnostic this surface can produce: each
store's `repr` and `describe()`, what the engine records as provenance, and
the text of both refusals.

There is **no way to turn verification off**, and the
[Rust chapter](https://dynamic-config-rs.github.io/remote/remote-stores.html#there-is-no-way-to-turn-verification-off)
argues that at length. The short version: it could not be uniform, it
answers nothing `with_ca_certificate_file` does not, and every client
underneath still has its own dangerous switch under its own frightening
name for the case nobody anticipated.

## The tokio runtime

One, lazily, owned by the module.

Three of the eight are async — etcd speaks gRPC, NATS has its own
protocol, and the AWS SDK is async throughout — so something has to drive
their futures. The other five are `ureq`, a plain socket or a git fetch:
blocking, no executor anywhere in them, and no runtime at all.

```python
>>> from dynamic_config.remote import Auth, Etcd, Vault, runtime_started
>>> runtime_started()                                    # import starts nothing
False
>>> Vault("https://vault:8200", "secret", "a/b", auth=Auth.token("t"))
>>> runtime_started()                                    # nor does a blocking store
False
>>> Etcd(["http://etcd:2379"], "myapp/db.json")
>>> runtime_started()                                    # this one needs it
True
```

It starts at **construction** rather than at the first fetch, because
construction is the moment a user can observe; a reactor appearing on a
later network call is a thread count nobody can explain. It has two
worker threads — the work is one request per configuration refresh, and
sizing it to the machine would put sixty-four parked threads in a
container that reads one key.

It is **multi-threaded rather than current-thread** so that two Python
threads refreshing two stores do not serialise behind each other, and it
is **never shut down**. `Runtime::drop` blocks until its workers park;
registering that at `atexit` would put a join between the interpreter and
its own exit, behind whatever a worker happens to be doing — which for a
store is a network read with a ten-second deadline on it. The base wheel
already registers an `atexit` sweep, and two that can each block would be
ordered by registration accident.

An immortal runtime is dangerous in a binding whose tasks call Python, and
it is safe here because **no task on this runtime ever touches Python**:
credentials are resolved in Python before the call, and the futures driven
here hold nothing but Rust. A worker still running while CPython finalises
cannot re-enter a dying interpreter.

That invariant is what shapes S3's credential provider, which is the one
piece of this wheel the SDK calls back into: it reads a value the fetch
path already resolved, and never calls Python.

If the calling thread is *already* inside a tokio runtime — a Rust program
embedding CPython, calling in from a task — `block_on` would panic with
*Cannot start a runtime from within a runtime*. That case is detected with
`Handle::try_current()` and the future is handed to this wheel's own
workers instead: a different runtime, so no reentrancy and no deadlock.

`set_executor` is unchanged and still means what it meant: which Python
pool pays for the blocking half. `refresh_remote_async()` runs the whole
fetch on that pool, and the tokio runtime is what the fetch uses once it
gets there.

## S3: the credential that is a trait

Seven of the eight stores take their credential as a string, so a callable
resolves to a string and the string is handed over. The AWS SDK does not.
Its credential surface is a **`ProvideCredentials` implementation**, asked
per request, from inside the SDK's own async machinery — and that shape is
the reason S3 was the first store here with a design to make rather than a
builder to mirror. `Git` was the second, and it reuses this one: the slot
below is the shape its credential takes too, for a different reason
([above](#credentials-may-be-callables)).

**Passing no credentials is a first-class mode**, not a fallback. With
`access_key_id` and `secret_access_key` absent, the SDK's own chain runs
untouched: environment variables, the shared profile, the EC2 instance
role, the ECS task role, IRSA on EKS. On anything running inside AWS that
is the right answer, and a second credential chain in a program that
already has one is a bug waiting for a rotation.

**When they are passed, the callable becomes a shim.** A provider is
installed in the chain's place which answers from a slot the fetch path
writes:

```text
S3.fetch()
  → resolves access_key_id / secret_access_key   Python, on the caller's thread
  → writes them into the slot                    still holding the GIL
  → py.detach                                    GIL released
      → the SDK signs a request
          → asks the provider                    a mutex read; no Python
```

The alternative — a provider that called the Python callable when the SDK
asked — was rejected for a reason the [runtime](#the-tokio-runtime)
section already states: **nothing on that runtime may touch Python.** The
runtime is never shut down, which is safe precisely because a worker
outliving the interpreter holds nothing but Rust. A provider acquiring the
GIL from a tokio worker would have turned the one safe immortal runtime
here into the dangerous kind.

Two consequences fall out of the slot, and both are improvements:

- **A rotated key rebuilds nothing.** Six of the other seven rebuild a
  client when their credential moves, because the credential is inside
  it. S3's is not: the next request signs with the new value, and the
  connection pool and the resolved endpoint are untouched. (`Git` is the
  seventh, and does not rebuild either — it has an object database to
  protect rather than a connection pool.)
- **The SDK's identity cache has to be off.** It exists to keep a provider
  that calls IMDS from being called per request, and it caches a
  credential with no expiry *indefinitely* — which for a
  mutable slot would mean the first key signing every request forever.
  `IdentityCache::no_cache()` costs nothing here, because the provider it
  defeats is a mutex read.

