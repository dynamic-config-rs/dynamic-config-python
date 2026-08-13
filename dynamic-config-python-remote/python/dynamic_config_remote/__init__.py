"""The Rust remote stores, for `dynamic-config-py`.

A second wheel — `pip install dynamic-config-py[remote]` — because a wheel is
built per platform and a gRPC stack in the ordinary one would be in every
install, including the ones reading a single TOML file.

Import it through the base package, which keeps the name free for it::

    from dynamic_config import DynamicConfig
    from dynamic_config.remote import Etcd

    config = DynamicConfig(Database, key="db").remote(
        Etcd(["http://etcd.internal:2379"], "myapp/db.json")
    )
    config.refresh_remote()
    config.init()

`dynamic_config.remote` and `dynamic_config_remote` are the same module;
the first is the name to write, and the second is what pip installs. The
split is what makes the extra safe to add and remove: two distributions never
share a directory, so uninstalling either leaves the other whole.

All eight stores are here: `Consul`, `Etcd`, `Firestore`, `Git`, `Nats`,
`Redis`, `S3` and `Vault`. Each mirrors its Rust crate's builder, and each is
an ordinary `RemoteSource` as far as the base wheel is concerned — what
crosses between the two wheels is a `(document, format)` pair, never a Rust
value.

Four things are worth knowing before using any of them:

- **Every credential argument accepts a callable.** A watcher outlives its
  credentials — a Vault token expires, a secret id is rotated — so the
  callable is invoked on every fetch and a value that has changed rebuilds
  the client. See :data:`Credential`.
- **Fetching is explicit**, exactly as it is everywhere else here:
  `refresh_remote()` reads the store and keeps the document; a load merges
  what was kept and touches no network.
- **One tokio runtime, started when the first store that needs one is
  built.** Importing this package starts no threads, and neither does
  building a store that is blocking — `Vault`, `Consul`, `Firestore`, `Git`
  and `Redis` need no runtime at all. See :func:`runtime_started`.
- **TLS is one vocabulary for all eight.** A private certificate authority
  and a client certificate, as file paths or PEM bytes, through the ``tls``
  argument every store takes. Two stores cannot express all of it and refuse
  the part they cannot rather than ignoring it. See :class:`TlsConfig`.

`Auth` is Vault's, because Vault's is the one that shipped first and renaming
it would break an installed wheel; the others are named for their store —
`ConsulAuth`, `FirestoreAuth`, `GitAuth`, `NatsAuth`. Redis and S3 have none,
because neither has a login: Redis authenticates from its connection string
and S3 signs each request, so both take their credentials as arguments.

`Git` is the one store that reads **several files as one document** — one
commit has one tree, so a set is read as of one instant — which is what
`GitKeys` is for. `watch()` is exposed for no store here, git included, and
that is said out loud in :class:`Git` rather than left to be noticed.
"""

from __future__ import annotations

from . import _core
from ._consul import Consul, ConsulAuth
from ._credential import Credential
from ._etcd import Etcd
from ._firestore import Firestore, FirestoreAuth
from ._git import Git, GitAuth, GitKeys
from ._nats import Nats, NatsAuth
from ._redis import Redis
from ._s3 import S3
from ._tls import TlsConfig
from ._vault import SERVICE_ACCOUNT_TOKEN, Auth, Vault

#: This wheel's version. It moves with the base wheel's, because the two ship
#: together — the base wheel's `dynamic_config.remote` is the door into this
#: one, and a release that moves this alone would have nothing on the other
#: side of that door.
__version__: str = _core.__version__

#: The `dynamic-config` Rust crate this wheel was built against. It must be
#: the one the base wheel was built against too, and `dynamic_config.remote`
#: says so if it is not.
__engine_version__: str = _core.__engine_version__


def runtime_started() -> bool:
    """Whether the tokio runtime this wheel owns has been started.

    It exists so the promise is testable rather than merely stated: importing
    this package starts no threads, and neither does building a store that
    does not need one.

    The runtime is started by the first store whose client is async — `Etcd`,
    `Nats` or `S3` — at **construction**, not at the first fetch, because
    construction is the moment a user can see it happen. It has two worker
    threads, and it is never shut down: `Runtime::drop` blocks until its
    workers park, and an `atexit` hook that can block for the length of a
    network read would turn a clean exit into a hang. Nothing running on it
    ever touches Python, so there is nothing for interpreter shutdown to race
    with — including S3's credential provider, which reads a value the fetch
    path resolved rather than calling back into the interpreter.
    """
    return bool(_core.runtime_started())


__all__ = [
    "S3",
    "SERVICE_ACCOUNT_TOKEN",
    "Auth",
    "Consul",
    "ConsulAuth",
    "Credential",
    "Etcd",
    "Firestore",
    "FirestoreAuth",
    "Git",
    "GitAuth",
    "GitKeys",
    "Nats",
    "NatsAuth",
    "Redis",
    "TlsConfig",
    "Vault",
    "__engine_version__",
    "__version__",
    "runtime_started",
]
