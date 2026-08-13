"""The Rust remote stores — Consul, etcd, Firestore, git, NATS, Redis, S3, Vault.

    pip install dynamic-config-py[remote]

    from dynamic_config import DynamicConfig
    from dynamic_config.remote import Etcd

    config = DynamicConfig(Database, key="db").remote(
        Etcd(["http://etcd.internal:2379"], "myapp/db.json")
    )

Without the extra, importing this raises `ImportError` naming it. That is the
whole of this module: it is a name, kept free by the base package, pointing
at a distribution that may or may not be installed.

## Why a module here rather than a package over there

The obvious shape is a namespace package — the base wheel ships
`dynamic_config/*`, the remote wheel ships `dynamic_config/remote/*`, and the
two are one import tree. It was built and measured before being rejected, and
it fails in a way that matters: under an **editable** base install (`maturin
develop`, which is how this package is developed and tested), `dynamic_config`
resolves to the source tree and its `__path__` is that one directory, so a
`dynamic_config/remote/` installed into site-packages is invisible. The remote
wheel could never be tested against a developed base. It also leaves an
orphan: uninstalling the base first turns `dynamic_config` into a PEP 420
namespace package that still imports and has no API at all.

Two distributions that never share a directory have neither problem. The cost
is this file, and a name that is one indirection away from the code — which
is a small price for an extra that can be added and removed without either
distribution knowing.

The module this re-exports is `dynamic_config_remote`; the two names are the
same objects, and either import works. This is the one to write.

`__all__` below is the authoritative list, and it is not maintained by hand
on trust: `dynamic-config-python-remote`'s
`test_the_dotted_name_exports_exactly_what_the_package_does` asserts that it
equals `dynamic_config_remote.__all__`, so a store added over there and not
re-exported here fails that suite rather than becoming an `ImportError` a
user finds first. That is how the five stores after etcd and Vault were
caught.
"""

from __future__ import annotations

#: What to install, named in the ImportError and nowhere else — so the
#: instruction and the extra cannot drift apart.
_EXTRA = "pip install dynamic-config-py[remote]"

try:
    from dynamic_config_remote import S3 as S3
    from dynamic_config_remote import (
        SERVICE_ACCOUNT_TOKEN as SERVICE_ACCOUNT_TOKEN,
    )
    from dynamic_config_remote import Auth as Auth
    from dynamic_config_remote import Consul as Consul
    from dynamic_config_remote import ConsulAuth as ConsulAuth
    from dynamic_config_remote import Credential as Credential
    from dynamic_config_remote import Etcd as Etcd
    from dynamic_config_remote import Firestore as Firestore
    from dynamic_config_remote import FirestoreAuth as FirestoreAuth
    from dynamic_config_remote import Git as Git
    from dynamic_config_remote import GitAuth as GitAuth
    from dynamic_config_remote import GitKeys as GitKeys
    from dynamic_config_remote import Nats as Nats
    from dynamic_config_remote import NatsAuth as NatsAuth
    from dynamic_config_remote import Redis as Redis
    from dynamic_config_remote import TlsConfig as TlsConfig
    from dynamic_config_remote import Vault as Vault
    from dynamic_config_remote import __engine_version__ as __engine_version__
    from dynamic_config_remote import __version__ as __version__
    from dynamic_config_remote import runtime_started as runtime_started
except ImportError as absent:  # pragma: no cover - covered in a subprocess
    raise ImportError(
        "the Rust remote stores are a separate wheel, because a gRPC stack "
        "and an HTTP client in the ordinary one would be in every install. "
        f"Install it with `{_EXTRA}`. A store written in Python needs "
        "nothing extra: dynamic_config.RemoteSource is in this wheel."
    ) from absent

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
