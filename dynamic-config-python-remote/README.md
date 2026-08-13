# dynamic-config-py-remote

All eight Rust remote stores — **Consul**, **etcd**, **Firestore**,
**git**, **NATS**, **Redis**, **S3** and **HashiCorp Vault** — for
[`dynamic-config-py`](https://pypi.org/project/dynamic-config-py/).

```sh
pip install dynamic-config-py[remote]
```

```python
from dataclasses import dataclass

from dynamic_config import DynamicConfig
from dynamic_config.remote import Auth, Etcd, Vault


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432


config = DynamicConfig(Database, key="db").remote(
    Etcd(["http://etcd.internal:2379"], "myapp/db.json")
)
config.refresh_remote()      # reads the store, keeps the document
config.init()                # merges it, validates, installs

db = config.current()
```

Configuration in git, which is where a great many teams already keep it —
one commit, one tree, so a set of files is read as of one instant:

```python
from dynamic_config.remote import Git, GitAuth, GitKeys

config = DynamicConfig(Database, key="db").remote(
    Git(
        "https://github.com/acme/config.git",
        GitKeys.several(["services/api/base.yaml", "services/api/local.yaml"]),
        branch="main",
        # An installation token lives an hour; a watcher lives for the life
        # of the process. So the credential is a callable, and a rotated one
        # rebuilds nothing — the object database survives it.
        auth=GitAuth.token(mint_an_installation_token),
    )
)
```

Vault, with a credential that outlives the process that started:

```python
from pathlib import Path

config = DynamicConfig(Database, key="db").remote(
    Vault(
        "https://vault.internal:8200",
        "secret",
        "myapp/db",
        auth=Auth.token(lambda: Path("/var/run/vault/token").read_text()),
    )
)
```

## Why this is a second wheel

A wheel is built per platform, so a dependency in the ordinary wheel is in
every install of it — including the ones reading a single TOML file. etcd
speaks gRPC, the AWS SDK brings its own runtime and signing stack, NATS
brings a protocol, git brings `gix` and three more bring HTTP over rustls:
eight clients is most of an async ecosystem. They stay out until somebody
asks for them.

A pip extra installs *distributions*, not Cargo features: a wheel compiled
weeks ago on a release runner cannot have a feature turned on at install
time. So `dynamic-config-py[remote]` resolves to this distribution, which
contains the same engine built with the stores in it.

## What it is not

It is not a second binding. These stores are ordinary
`dynamic_config.RemoteSource` implementations — they answer `fetch()` with a
`(document, format)` pair, and the base wheel merges it through the path it
already had for a store written in Python. Nothing Rust crosses between the
two extension modules, which is why the base wheel needed no change to accept
these.

## Credentials

**Every credential argument accepts a callable**, and that is the feature
rather than a convenience: a configuration watcher outlives its credentials.
A Vault token expires, an AppRole secret id is rewritten by a sidecar, an
etcd password is rotated. A callable is invoked on every fetch, and a value
that has changed rebuilds the client with it.

```python
Etcd(["http://etcd:2379"], "myapp/db.json",
     user="myapp", password=lambda: os.environ["ETCD_PASSWORD"])

Auth.app_role(role_id, secret_id=lambda: read_secret_id())

Redis("redis://redis:6379", "myapp/db.json", password=lambda: read_password())

S3("myapp-config", "prod/db.json", access_key_id=lambda: current_key_id(),
   secret_access_key=lambda: current_secret())
```

S3 is the one whose credential is not a string. The AWS SDK takes a
`ProvideCredentials` implementation and asks it per request, so a callable
becomes a shim over a slot the fetch path writes — which means a rotated key
rebuilds *nothing*, and which is also why passing no credentials at all is a
first-class mode: the SDK's own chain (environment, profile, instance role,
IRSA) is what most deployments should use.

Credentials never appear in a diagnostic — not in an exception message, not
in a `repr`, not in `describe()`. A store URL that embeds `user:password@host`
loses the password by the same rule the Rust store crates use, and every
credential resolved for a call is scrubbed by value from anything that call
reports.

## TLS

A private certificate authority and a client certificate, spelled the same
way for all eight stores:

```python
Vault("https://vault.internal:8200", "secret", "myapp/db",
      auth=Auth.kubernetes("myapp"),
      tls=TlsConfig()
          .with_ca_certificate_file("/etc/ssl/private-ca.pem")
          .with_client_certificate_files("/etc/ssl/app.crt", "/etc/ssl/app.key"))
```

Each setting has a file spelling and a PEM-bytes one, because a Kubernetes
secret mount produces files and a secrets manager produces bytes — and
writing bytes to a temporary file so a client could read them back would put
a private key on a disk that never asked for one. An empty `TlsConfig` is the
platform's own trust store; a named authority replaces it, which is what
pinning means.

Two stores cannot express all of it and **refuse the part they cannot**, at
construction, naming the call and the way out. `Nats` takes certificate paths
and not bytes (`async-nats` opens the files itself) and `S3` takes no client
certificate (the AWS SDK's TLS context has no slot for one). Ignoring either
would leave a program believing it had pinned an authority when it had not.
`Git` expresses all four settings and refuses them on a url that is not
`https://`, because an ssh remote's trust lives in `known_hosts`; it is also
the one store that **adds** a named authority to the platform's trust store
rather than replacing it.

The private key is never rendered: `repr()` delegates to the Rust type's
`Debug`, which prints a path where there is one and `<redacted>` where the
key is bytes.

## The tokio runtime

One, lazily, owned by the module. It starts when the first store that needs
one is constructed — `Etcd`, `Nats` and `S3` do; `Consul`, `Firestore`,
`Git`, `Redis` and `Vault` are blocking and do not — and never shuts down;
`dynamic_config_remote.runtime_started()` is how that promise is tested.
Nothing running on it touches Python, which is what makes an immortal
runtime safe here. See the book's
[Remote stores in Rust, from Python](https://ctolon.github.io/dynamic-config/python/remote-wheel.html).

## Which stores

All eight, one Python class each: `Consul`, `Etcd`, `Firestore`, `Git`,
`Nats`, `Redis`, `S3` and `Vault`, with `ConsulAuth`, `FirestoreAuth`,
`GitAuth`, `NatsAuth` and `Auth` (Vault's) for the ones that log in,
`GitKeys` for the one that reads a set, and `TlsConfig` for all of them.
What is deliberately not here — each client's own configuration type, and
with it a custom proxy; the multi-key forms for the seven that are not git;
`watch()` — is listed in the book, with the reason in each case.

## License

MIT
