"""Stubs for the compiled half of `dynamic_config_remote`.

Hand-written and hand-maintained, like the base wheel's: nothing generates
these, and nothing but `mypy --strict` and `pyright` notice when one drifts
from the `#[pymethods]` block it describes.

Nothing here is public. The classes take already-resolved credentials as
plain strings, because resolving them is the facade's job — see
`_credential.py` for why that placement is the design rather than an
accident.
"""

import os
from collections.abc import Sequence
from typing import Optional, Union

__version__: str
__engine_version__: str

_Path = Union[str, "os.PathLike[str]"]

class StoreError(Exception):
    """A store failed. Translated to `dynamic_config.RemoteError`."""

class StoreAuthError(StoreError):
    """A store refused the credentials. Translated to `dynamic_config.AuthError`."""

class TlsConfig:
    """The compiled TLS vocabulary behind `dynamic_config_remote.TlsConfig`.

    Immutable: every builder answers a new one. `__repr__` is the Rust type's
    own `Debug`, which prints shape and never key material.
    """

    def __init__(self) -> None: ...
    def with_ca_certificate_file(self, path: _Path) -> TlsConfig: ...
    def with_ca_certificate_pem(self, pem: bytes) -> TlsConfig: ...
    def with_client_certificate_files(
        self, certificate: _Path, key: _Path
    ) -> TlsConfig: ...
    def with_client_certificate_pem(
        self, certificate: bytes, key: bytes
    ) -> TlsConfig: ...
    def is_empty(self) -> bool: ...

class ConsulStore:
    """The compiled Consul client behind `dynamic_config_remote.Consul`."""

    def __init__(
        self,
        address: str,
        key: str,
        format: Optional[str],  # noqa: A002
        datacenter: Optional[str],
        timeout: float,
        tls: Optional[TlsConfig],
    ) -> None: ...
    def fetch(
        self,
        kind: str,
        method: str,
        bearer: str,
        meta: Sequence[tuple[str, str]],
    ) -> tuple[str, str]: ...
    def describe(self) -> str: ...

class EtcdStore:
    """The compiled etcd client behind `dynamic_config_remote.Etcd`."""

    def __init__(
        self,
        endpoints: Sequence[str],
        key: str,
        format: Optional[str],  # noqa: A002
        timeout: float,
        tls: Optional[TlsConfig],
    ) -> None: ...
    def fetch(
        self, user: Optional[str], password: Optional[str]
    ) -> tuple[str, str]: ...
    def describe(self) -> str: ...

class FirestoreStore:
    """The compiled Firestore client behind `dynamic_config_remote.Firestore`."""

    def __init__(
        self,
        project: str,
        path: str,
        key: str,
        database: str,
        endpoint: Optional[str],
        timeout: float,
        tls: Optional[TlsConfig],
    ) -> None: ...
    def fetch(self, kind: str, value: str) -> tuple[str, str]: ...
    def describe(self) -> str: ...

class GitStore:
    """The compiled git client behind `dynamic_config_remote.Git`."""

    def __init__(
        self,
        url: str,
        keys: str,
        paths: Sequence[str],
        reference: Optional[tuple[str, str]],
        format: Optional[str],  # noqa: A002
        cache_dir: Optional[str],
        timeout: Optional[float],
        max_bytes: Optional[int],
        compact_after: Optional[int],
        tls: Optional[TlsConfig],
    ) -> None: ...
    def fetch(self, kind: str, first: str, second: str) -> tuple[str, str]: ...
    def describe(self) -> str: ...

class NatsStore:
    """The compiled NATS client behind `dynamic_config_remote.Nats`."""

    def __init__(
        self,
        servers: Sequence[str],
        bucket: str,
        key: str,
        format: Optional[str],  # noqa: A002
        timeout: float,
        tls: Optional[TlsConfig],
    ) -> None: ...
    def fetch(self, kind: str, first: str, second: str) -> tuple[str, str]: ...
    def describe(self) -> str: ...

class RedisStore:
    """The compiled Redis client behind `dynamic_config_remote.Redis`."""

    def __init__(
        self,
        url: str,
        key: str,
        format: Optional[str],  # noqa: A002
        timeout: float,
        tls: Optional[TlsConfig],
    ) -> None: ...
    def fetch(
        self, user: Optional[str], password: Optional[str]
    ) -> tuple[str, str]: ...
    def describe(self) -> str: ...

class S3Store:
    """The compiled S3 client behind `dynamic_config_remote.S3`."""

    def __init__(
        self,
        bucket: str,
        key: str,
        format: Optional[str],  # noqa: A002
        region: Optional[str],
        endpoint: Optional[str],
        timeout: float,
        tls: Optional[TlsConfig],
    ) -> None: ...
    def fetch(
        self,
        access_key_id: Optional[str],
        secret_access_key: Optional[str],
        session_token: Optional[str],
    ) -> tuple[str, str]: ...
    def describe(self) -> str: ...
    def clients_built(self) -> int: ...

class VaultStore:
    """The compiled Vault client behind `dynamic_config_remote.Vault`."""

    def __init__(
        self,
        address: str,
        mount: str,
        path: str,
        key: str,
        namespace: Optional[str],
        timeout: float,
        tls: Optional[TlsConfig],
    ) -> None: ...
    def fetch(
        self, kind: str, mount: str, first: str, second: str
    ) -> tuple[str, str]: ...
    def describe(self) -> str: ...

def runtime_started() -> bool: ...
