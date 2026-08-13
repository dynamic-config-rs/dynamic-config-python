"""etcd, as a configuration source."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

from dynamic_config import Format, RemoteSource

from . import _core
from ._credential import Credential, resolve
from ._errors import translated
from ._format import DEFAULT_TIMEOUT, checked
from ._tls import TlsConfig, unwrapped


class Etcd(RemoteSource):
    """A key in an etcd v3 store.

    Mirrors ``dynamic_config_etcd::Etcd``: the endpoints, the key, the
    format, the per-fetch deadline, and etcd's user/password authentication.

        from dynamic_config import DynamicConfig
        from dynamic_config.remote import Etcd

        config = DynamicConfig(Database, key="db").remote(
            Etcd(["http://etcd.internal:2379"], "myapp/db.json")
        )
        config.refresh_remote()
        config.init()

    Nothing connects here. etcd's client connects lazily and this store
    builds it on the first :meth:`fetch`, so a constructor that succeeded is
    not a promise that etcd is reachable — which is the Rust crate's contract
    too, and the reason a bad endpoint surfaces at the first refresh.

    **Credentials may be callables.** ``user`` and ``password`` each accept a
    ``str`` or a callable returning one, resolved on every fetch; a password
    that has been rotated is picked up without rebuilding the store. See
    :data:`~dynamic_config_remote.Credential`.
    """

    __slots__ = ("_password", "_store", "_user")

    def __init__(
        self,
        endpoints: Sequence[str],
        key: str,
        *,
        format: Optional[str] = None,  # noqa: A002
        timeout: float = DEFAULT_TIMEOUT,
        user: Optional[Credential] = None,
        password: Optional[Credential] = None,
        tls: Optional[TlsConfig] = None,
    ) -> None:
        """Builds the store.

        ``endpoints`` are etcd's, as ``http://host:port``; ``key`` is the key
        whose value is the configuration document. ``format`` is taken from
        the key's extension when it has one — ``myapp/db.json`` is JSON — and
        has to be given otherwise.

        ``timeout`` is the deadline for **one fetch attempt**, in seconds; it
        covers the wait for the client lock as well as the request, because
        time spent queued behind another fetch is time the caller waited.

        ``user`` and ``password`` are etcd's own authentication and go
        together: neither on its own authenticates anything, so passing one
        without the other is a `ValueError` rather than an unauthenticated
        connection nobody asked for.

        ``tls`` is a private certificate authority, a client certificate, or
        both — see :class:`~dynamic_config_remote.TlsConfig`. etcd is the one
        store here whose gRPC stack carries **no platform roots** in this
        wheel, so a ``TlsConfig`` for etcd should name the authority it
        trusts; a client certificate on its own leaves nothing to verify the
        server against. The book says why that feature is not enabled.

        Raises `ValueError` if the endpoints are empty, if the format can be
        neither read nor deduced, or if exactly one of ``user`` and
        ``password`` is given, and `TypeError` if ``tls`` is not a
        `TlsConfig`.
        """
        if not endpoints:
            raise ValueError("etcd needs at least one endpoint")

        if (user is None) != (password is None):
            raise ValueError(
                "etcd authentication needs both user and password; one "
                "without the other would connect unauthenticated"
            )

        format = checked(key, format)  # noqa: A001

        self._user = user
        self._password = password
        self._store = _core.EtcdStore(
            list(endpoints), key, format, timeout, unwrapped(tls)
        )

    def fetch(self) -> tuple[str, Format]:
        """Reads the key, and answers ``(document, format)``.

        The credentials are resolved first, on this thread; the read itself
        releases the GIL, so a slow etcd does not stop the process.

        Raises :class:`dynamic_config.AuthError` if etcd refused the
        credentials and :class:`dynamic_config.RemoteError` otherwise. Neither
        message carries a credential — not the password, and not one embedded
        in an endpoint.
        """
        user = None if self._user is None else resolve(self._user, "user")
        password = (
            None if self._password is None else resolve(self._password, "password")
        )

        with translated():
            text, named = self._store.fetch(user, password)

        return text, Format(named)

    def describe(self) -> str:
        """Names the store, with any credential in an endpoint removed."""
        return self._store.describe()

    def __repr__(self) -> str:
        """The store's description, which is already redacted."""
        return repr(self._store)
