# Store Reference

Every store constructor, argument by argument. The narrative — what a
store *is* here, and how credentials behave — lives in
[Remote Stores in Rust](remote-wheel.md) and
[Credentials, TLS & the Runtime](remote-auth.md).

## API

### `Etcd(endpoints, key, *, format=None, timeout=10.0, user=None, password=None, tls=None)`

A key in an etcd v3 store. Mirrors `dynamic_config_etcd::Etcd`.

| Argument | Meaning |
|---|---|
| `endpoints` | etcd's, as `http://host:port`. At least one |
| `key` | The key whose value is the configuration document |
| `format` | `"json"`, `"toml"` or `"yaml"`. Read from the key's extension when it has one |
| `timeout` | The deadline for **one fetch attempt**, in seconds |
| `user`, `password` | etcd's own authentication. Either both or neither; each may be a callable |
| `tls` | A [`TlsConfig`](#tlsconfig): a private certificate authority, a client certificate, or both |

Nothing connects at construction — etcd's client connects lazily, and a
bad endpoint surfaces at the first refresh, exactly as in Rust.

### `Vault(address, mount, path, *, auth, key="db", namespace=None, timeout=10.0, tls=None)`

A secret in Vault's KV v2 store. Mirrors `dynamic_config_vault::Vault`.

| Argument | Meaning |
|---|---|
| `address` | `https://vault.internal:8200` |
| `mount`, `path` | The secret at `{mount}/{path}` |
| `auth` | An [`Auth`](#auth). Required — a `Vault` with no credentials could only ever produce a 403 |
| `key` | The section key the secret is wrapped under. Must match the configuration's |
| `namespace` | The Vault Enterprise namespace, if there is one |
| `timeout` | The deadline for **one fetch attempt**, in seconds |
| `tls` | A [`TlsConfig`](#tlsconfig): a private certificate authority, a client certificate, or both |

Vault stores a section's *contents* rather than a whole document, which
is why `key` exists and why it has to agree with the one
`DynamicConfig(..., key=...)` was given.

### `Auth`

How to obtain a Vault token — one classmethod per Rust variant. Every
credential argument accepts a callable. It is `Auth` rather than
`VaultAuth` because Vault's shipped first and an installed wheel already
imports it under that name; the three that followed are named for their
store. Redis and S3 have none, because neither has a login.

| | |
|---|---|
| `Auth.token(token)` | A token somebody already obtained |
| `Auth.app_role(role_id, secret_id)` | AppRole, on the `approle` mount |
| `Auth.kubernetes(role)` | The pod's service-account token, on `kubernetes` |
| `Auth.jwt(jwt)` | JWT/OIDC, on `jwt` |
| `Auth.userpass(username, password)` | On `userpass` |
| `Auth.ldap(username, password)` | On `ldap` |
| `Auth.certificate()` | A TLS client certificate, on `cert` |

| Modifier | Effect |
|---|---|
| `at_mount(path)` | A different mount path. No effect on `token`, which has none |
| `with_role(role)` | For `kubernetes`, `jwt` and `certificate` |
| `with_token_path(path)` | For `kubernetes` |

Each returns a **new** `Auth` rather than mutating: one shared between
two stores cannot be changed by either.

### `Consul(address, key, *, format=None, auth=None, datacenter=None, timeout=10.0, tls=None)`

A key in Consul's KV store. Mirrors `dynamic_config_consul::Consul`.

| Argument | Meaning |
|---|---|
| `address` | The agent's, as `http://host:8500` |
| `key` | The key whose value is the whole configuration document |
| `format` | Read from the key's extension when it has one |
| `auth` | A [`ConsulAuth`](#consulauth). Defaults to `anonymous()`, which is what a Consul with ACLs disabled wants |
| `datacenter` | One that is not the agent's own |
| `timeout` | The deadline for **one fetch attempt**, in seconds |
| `tls` | A [`TlsConfig`](#tlsconfig): a private certificate authority, a client certificate, or both |

Consul stores an opaque blob, so the value is a whole document — the
opposite of `Vault`, which wraps a secret's fields under a section key.

### `ConsulAuth`

| | |
|---|---|
| `ConsulAuth.anonymous()` | No token. ACLs disabled, or a readable `default` policy |
| `ConsulAuth.token(token)` | Usually `CONSUL_HTTP_TOKEN` |
| `ConsulAuth.kubernetes(method)` | The pod's service-account token, presented to an auth method |
| `ConsulAuth.jwt(method, token)` | A JWT or OIDC id token |
| `.with_bearer_file(path)` | Reads the bearer from a file, re-read at every login |
| `.with_meta(name, value)` | Consul's `Meta`, for the audit log |

### `Firestore(project, path, *, auth, key="db", database="(default)", endpoint=None, timeout=10.0, tls=None)`

A document in Firestore. Mirrors `dynamic_config_firestore::Firestore`.

| Argument | Meaning |
|---|---|
| `project`, `path` | The GCP project, and collection-then-document — `config/db` |
| `auth` | A [`FirestoreAuth`](#firestoreauth). Required |
| `key` | The section key the document is wrapped under. Must match the configuration's |
| `database` | One that is not `(default)` |
| `endpoint` | What the emulator needs — `http://127.0.0.1:8080` |
| `timeout` | The deadline for **one fetch attempt**, covering the token fetch too |
| `tls` | A [`TlsConfig`](#tlsconfig): a private certificate authority, a client certificate, or both |

`auth` is required where the Rust builder defaults to the emulator: *send
no credentials* is a reasonable default for a builder being filled in and
a poor one for a constructor, where it would quietly produce a 401 against
the real service.

### `FirestoreAuth`

| | |
|---|---|
| `FirestoreAuth.metadata_server()` | The workload's own identity: GKE, Cloud Run, GCE. Renews itself |
| `FirestoreAuth.access_token(token)` | Anything that already has one. Cannot renew — this is the one a callable is for |
| `FirestoreAuth.emulator()` | No token at all |
| `.with_url(url)` | A sidecar's metadata address |

### `Git(url, path, *, branch=None, tag=None, commit=None, format=None, auth=None, cache_dir=None, timeout=None, max_bytes=None, compact_after=None, tls=None)`

A file — or a set of them — in a git repository. Mirrors
`dynamic_config_git::GitSource`.

| Argument | Meaning |
|---|---|
| `url` | Anything git understands: `https://…`, `ssh://…`, `git@host:org/repo.git`, or a local path |
| `path` | One `/`-separated path relative to the repository root, a list of them, or a [`GitKeys`](#gitkeys) |
| `branch`, `tag`, `commit` | Three spellings of one reference — **name at most one**. `main` when none is named; a commit is the full hexadecimal object id |
| `format` | Read from a path's extension when it has one. Required for a name that does not say, for a list naming two formats, and always for a directory |
| `auth` | A [`GitAuth`](#gitauth). Defaults to `anonymous()`, which is what a public repository wants |
| `cache_dir` | Keeps the object database somewhere that survives restarts. Two sources may not name one directory |
| `timeout` | The deadline for **one fetch attempt**. The Rust crate's thirty seconds when absent |
| `max_bytes` | The largest **single file** that will be read. A megabyte when absent |
| `compact_after` | Transfers a working directory may accumulate before it is emptied and refilled. Thirty-two when absent; `0` never empties it |
| `tls` | A [`TlsConfig`](#tlsconfig) — on an `https://` url, and **refused** on any other |

A fetch is **shallow and single-ref**: the ref advertisement, then that one
commit at depth 1 if it is not already held, then one blob read out of its
tree. Nothing is ever checked out, so a repository containing a symlink to
`/etc/shadow` cannot make a checkout that never happens write anywhere. **An
unchanged ref transfers nothing**, which is what makes polling a git host
reasonable — and what the first fetch costs is the repository's whole tree,
because a commit's tree is what the protocol delivers.

`describe()` names the **commit** once one has been read, and the ref that was
asked for until then: *which commit is this program actually serving* is the
first question of every configuration-in-git incident, and a branch name does
not answer it.

Three things are refused at construction rather than half-applied, each naming
the call and the way out: **two references** (three keyword arguments have no
order, where Rust's three builder calls do), **a credential for the transport
this url does not use** (an ssh key on an `https://` remote is not
half-configured, it is silently anonymous), and **`tls` on a url with no TLS in
it**.

The default working directory is a **private temporary one**, `0700` from the
moment it exists, removed with the store. It is the one construction in this
wheel that touches the filesystem, and it touches no network.

### `GitKeys`

What a source reads. Mirrors `dynamic_config_git::Keys`; a bare string is
`one` and a list of strings is `several`, so only a directory needs the import.

| | |
|---|---|
| `Git(url, "services/api/db.yaml")` | One file, handed to the loader byte for byte |
| `Git(url, ["base.yaml", "local.yaml"])` | Several, merged **in the order given — later wins** |
| `GitKeys.prefix("services/api")` | Every file under a directory, merged as **disjoint sections** — an overlap is a deployment bug and is reported as one |

A directory, not a string prefix: `prefix("services/api")` reads
`services/api/db.yaml` and does not read `services/api-old.yaml`. It needs
`format`, because a directory has no extension to read one from.

It is `GitKeys` rather than `Keys` because eight stores share one namespace and
only this one has it — see [what is not exposed](remote-wheel.md#what-is-not-exposed) for why
the other seven read a single key.

### `GitAuth`

How to authenticate to a git host — one classmethod per Rust `Credential`
constructor. git has exactly two places a credential can go, so this is those
two plus the absence of both.

| | |
|---|---|
| `GitAuth.anonymous()` | A public repository |
| `GitAuth.token(token)` | A PAT, an installation token, a deploy token. Travels as basic auth with `x-access-token` in the user half |
| `GitAuth.basic(username, password)` | For the host that reads the user half — a GitLab deploy token, `gitlab-ci-token` with `CI_JOB_TOKEN` |
| `GitAuth.ssh_agent()` | Whatever `ssh` would do unaided: `SSH_AUTH_SOCK`, `~/.ssh/config`, a `ProxyJump`, a hardware key |
| `GitAuth.ssh_key(path)` | One private key file, with `-o IdentitiesOnly=yes` so an agent cannot offer others first |
| `GitAuth.ssh_command(command)` | Run this instead of `ssh`. Redacted whole, because it may be carrying a secret |

The `ssh` **binary must be on the host** for the last three: `gix` carries an
SSH stream by spawning it, exactly as `git` does — and in exchange everything
already configured for `ssh` works.

`ssh_key` takes a path and no callable: `ssh` opens the file at
every fetch, so a key an operator replaces is picked up already. **A passphrase
is not accepted in any spelling** — `ssh` has no way to take one that does not
put it on a command line where `ps` can read it, so a passphrase-protected key
belongs in an agent.

### `Nats(servers, bucket, key, *, format=None, auth=None, timeout=10.0, tls=None)`

A key in a JetStream KV bucket. Mirrors `dynamic_config_nats::Nats`.

| Argument | Meaning |
|---|---|
| `servers` | NATS URLs, as `nats://host:4222`. A list, because a cluster is ordinary |
| `bucket`, `key` | The KV bucket, and the key in it |
| `format` | Read from the key's extension when it has one |
| `auth` | A [`NatsAuth`](#natsauth). Defaults to `anonymous()` |
| `timeout` | The deadline for **one fetch attempt**, in seconds |
| `tls` | A [`TlsConfig`](#tlsconfig) — **file paths only**, and naming an authority turns TLS on |

Nothing connects at construction, which is a **difference from the Rust
crate**: `Nats::with_options` connects and resolves the bucket in its
constructor. Here construction touches no network, like every other store
in this wheel. The bucket is never created.

### `NatsAuth`

| | |
|---|---|
| `NatsAuth.anonymous()` | No credential |
| `NatsAuth.token(token)` | What `nats://token@host` carries, as an argument |
| `NatsAuth.user_and_password(user, password)` | |
| `NatsAuth.nkey_seed(seed)` | The `SU…` half, which signs the server's nonce |
| `NatsAuth.credentials(contents)` | A `.creds` file's **contents** |
| `NatsAuth.credentials_file(path)` | The same, read on every fetch |

### `Redis(url, key, *, format=None, user=None, password=None, timeout=10.0, tls=None)`

A key in Redis. Mirrors `dynamic_config_redis::Redis`.

| Argument | Meaning |
|---|---|
| `url` | `redis://host:6379`, or `rediss://` for TLS |
| `key` | The key whose value is the whole configuration document |
| `format` | Read from the key's extension when it has one |
| `user`, `password` | Each may be a callable. `password` alone is `requirepass`, which implies the default user |
| `timeout` | The deadline for **one fetch attempt** — connecting, writing and waiting |
| `tls` | A [`TlsConfig`](#tlsconfig). Needs a `rediss://` URL, and material on a `redis://` one is refused at the first fetch |

The credentials are arguments rather than part of `url` on purpose: a
callable cannot rotate a substring of a string somebody passed at
construction. They are spliced into the authority, percent-encoded, and
they replace any the URL already carried.

### `S3(bucket, key, *, format=None, region=None, endpoint=None, access_key_id=None, secret_access_key=None, session_token=None, timeout=10.0, tls=None)`

An object in S3. Mirrors `dynamic_config_s3::S3`.

| Argument | Meaning |
|---|---|
| `bucket`, `key` | The object, whose body is a whole configuration document |
| `format` | Read from the key's extension when it has one |
| `region`, `endpoint` | Resolved from the environment when absent. `endpoint` is what reaches MinIO, Ceph, R2 or B2 |
| `access_key_id`, `secret_access_key` | Both or neither; each may be a callable. Absent means [the SDK's own chain](remote-auth.md#s3-the-credential-that-is-a-trait) |
| `session_token` | For assumed credentials. Needs the pair |
| `timeout` | The deadline for **one fetch attempt** — and the SDK retries, so a fetch can take this three times over |
| `tls` | A [`TlsConfig`](#tlsconfig) — **a certificate authority only**; a client certificate is refused |

Path-style addressing is always on, because the virtual-host form needs
DNS entries only AWS has.

### `TlsConfig`

A private certificate authority and a client certificate, as data. Mirrors
`dynamic_config_store_core::tls::TlsConfig` method for method, and every
store takes one as `tls`. Each method answers a **new** `TlsConfig`, so one
shared between two stores cannot be changed by either.

| | |
|---|---|
| `TlsConfig()` | The platform's own trust store, no client certificate |
| `.with_ca_certificate_file(path)` | Trust the authority in this PEM file. `str` or `os.PathLike` |
| `.with_ca_certificate_pem(pem)` | Trust the authority in these PEM `bytes` |
| `.with_client_certificate_files(certificate, key)` | Present this certificate and key (mTLS) |
| `.with_client_certificate_pem(certificate, key)` | The same, from `bytes` |
| `.is_empty()` | Whether this asks for anything at all |

`repr()` is the Rust type's own `Debug`: a path where there is one,
`<redacted>` where the key is bytes, and never the material.
[The section above](remote-auth.md#tls-a-private-authority-and-a-client-certificate) has
what each store accepts, what `Nats` and `S3` refuse, and why refusing is
the only honest answer.

### `runtime_started()`

Whether the tokio runtime has been started. It exists so the promise
above is testable rather than merely stated.

### `Credential`

`Union[str, Callable[[], str]]` — the type every credential argument
takes. Not a coroutine: the fetch path is synchronous by construction, so
an `async def` here would never be awaited by anything.

## Errors

A failed fetch raises the base wheel's exceptions, not a second set:

| | |
|---|---|
| `dynamic_config.AuthError` | The credential was refused. Waiting will not fix it |
| `dynamic_config.RemoteError` | Anything else. Waiting might |

The compiled half raises its own pair — it is a different extension
module and cannot raise the base wheel's class objects — and the Python
facade translates, with the original attached as `__cause__`. Unlike a
store written in Python, the *message* is repeated here, because this
wheel wrote it and has already taken the credentials out.

## Testing without a store

The scripted servers in this project's own suite are the pattern: a
`ThreadingHTTPServer` speaking enough of Vault's KV v2 API — or Consul's,
or Firestore's, or S3's `GET` — to be read by the real client, whose
request log is then asserted on. It is the only way to prove a credential
*rotation*: the claim is about which bytes reached the server, so only a
server that records them can settle it.

Four of the eight can be scripted that way because they speak HTTP. Three
speak binary protocols — etcd's gRPC, NATS', Redis' RESP — where a
scripted server would be a protocol implementation rather than a fixture,
so those prove the same claim against a **real** server, by rotating from
a credential it refuses to one it accepts: the first fetch raises
`AuthError`, the second returns the document, and the same store object
did both.

**Git is scripted and real at once.** Its host is a `ThreadingHTTPServer`
that checks the `Authorization` header and hands the git half to a real
`git upload-pack --stateless-rpc` over a real repository — so the protocol
is the one GitHub serves, and the token is the one a test chose. It is
served over TLS behind a throwaway authority rather than over plain HTTP,
and not for symmetry: `gix` refuses to put a credential on an unencrypted
connection unless it is compiled with
`gix-transport/http-client-insecure-credentials`, which this wheel is not
and should not be, so a scripted host over `http://` would never be shown
a token at all. Everything that is not authentication is read over
`file://` from a repository the suite builds with `git` — no network, no
container, no mock of the thing under test.

S3's proof is worth naming separately, because S3 has no login and no
reconnection to count. What names the credential there is the SigV4
`Authorization` header — `Credential=AKID/…` — so the scripted server
reads the access key id out of it, and the rotation is the two ids it
saw. The containers prove the protocol; the scripted servers prove the
rotation.
