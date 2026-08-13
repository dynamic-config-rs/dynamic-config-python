"""NATS JetStream, as a configuration source."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from dynamic_config import Format, RemoteSource

from . import _core
from ._credential import Credential, resolve
from ._errors import translated
from ._format import DEFAULT_TIMEOUT, checked
from ._tls import TlsConfig, unwrapped


class NatsAuth:
    """How to authenticate to NATS.

    NATS puts every credential on the connection, so this mirrors the
    constructors of ``async_nats::ConnectOptions`` rather than an `Auth` enum
    the Rust crate does not have::

        NatsAuth.anonymous()
        NatsAuth.token(token)                       # nats://token@host, as an argument
        NatsAuth.user_and_password(user, password)
        NatsAuth.nkey_seed(seed)                    # an NKey seed, "SU..."
        NatsAuth.credentials(contents)              # a .creds file's contents
        NatsAuth.credentials_file("/etc/myapp/nats.creds")

    **Every credential argument accepts a callable**, and the credential is
    resolved on every fetch: a value that has changed reconnects with it,
    which is what a rotated token or a re-issued `.creds` file needs.

    :meth:`credentials_file` is a callable already — it reads the file on
    every fetch — which is why it takes a path and not a credential. A
    `.creds` file holds a JWT and an NKey seed, so its whole contents are
    treated as the secret and never appear in a message.

    The Rust crate takes `ConnectOptions` itself, where a signing callback
    also lives; that one has no Python spelling and is listed in the book.
    TLS does have one — the ``tls`` argument on
    :class:`~dynamic_config_remote.Nats` — and it takes certificate *paths*,
    because ``async-nats`` opens the files itself.
    """

    __slots__ = ("_first", "_kind", "_second")

    def __init__(self, kind: str, first: Credential, second: Credential) -> None:
        """Not for direct use — build one with the classmethods."""
        self._kind = kind
        self._first = first
        self._second = second

    @classmethod
    def anonymous(cls) -> NatsAuth:
        """No credential at all, which is what a development server wants."""
        return cls("anonymous", "", "")

    @classmethod
    def token(cls, token: Credential) -> NatsAuth:
        """A token, as `nats-server --auth` accepts."""
        return cls("token", token, "")

    @classmethod
    def user_and_password(cls, user: str, password: Credential) -> NatsAuth:
        """A user name and password."""
        return cls("user_password", user, password)

    @classmethod
    def nkey_seed(cls, seed: Credential) -> NatsAuth:
        """An NKey seed — the ``SU…`` half, which signs the server's nonce."""
        return cls("nkey", seed, "")

    @classmethod
    def credentials(cls, contents: Credential) -> NatsAuth:
        """A `.creds` file's **contents**, as an account issues them.

        The usual way a NATS account authenticates. Take the contents rather
        than the path when something other than a file produces them — a
        secrets manager, a sidecar — and :meth:`credentials_file` otherwise.
        """
        return cls("credentials", contents, "")

    @classmethod
    def credentials_file(cls, path: str) -> NatsAuth:
        """A `.creds` file, read on every fetch.

        Read here rather than in Rust, and re-read rather than cached: a
        `.creds` file is exactly the thing an operator replaces without
        restarting anything, and a copy taken at construction would be the
        credential that was true when the process started.
        """
        return cls("credentials", lambda: Path(path).read_text(), "")

    def _resolve(self) -> tuple[str, str, str]:
        """This credential's current value, as the compiled store takes it."""
        return (
            self._kind,
            resolve(self._first, f"{self._kind}'s first argument"),
            resolve(self._second, f"{self._kind}'s second argument"),
        )

    def __repr__(self) -> str:
        """The method and its non-secret half. Never the credential."""
        if self._kind == "anonymous":
            return "NatsAuth.anonymous()"

        if self._kind == "user_password":
            user = self._first if isinstance(self._first, str) else "<callable>"

            return f"NatsAuth.user_and_password({user!r}, '***')"

        return f"NatsAuth.{self._kind}('***')"


class Nats(RemoteSource):
    """A key in a NATS JetStream key/value bucket.

    Mirrors ``dynamic_config_nats::Nats``: the servers, the bucket, the key,
    the format and the per-fetch deadline.

        from dynamic_config import DynamicConfig
        from dynamic_config.remote import Nats, NatsAuth

        config = DynamicConfig(Database, key="db").remote(
            Nats(
                ["nats://nats.internal:4222"], "config", "db.json",
                auth=NatsAuth.credentials_file("/etc/myapp/nats.creds"),
            )
        )
        config.refresh_remote()
        config.init()

    Nothing connects here, and that is a **difference from the Rust crate**:
    ``Nats::with_options`` connects and resolves the bucket in its
    constructor, because there it is fallible and the caller is already in a
    runtime. Here construction touches no network, like every other store in
    this wheel, so an unreachable server is one error at the first refresh
    rather than an exception in the middle of building a configuration.

    The bucket is **not created**. A configuration reader that provisioned
    storage would hide a misconfigured deployment behind an empty one.
    """

    __slots__ = ("_auth", "_store")

    def __init__(
        self,
        servers: Sequence[str],
        bucket: str,
        key: str,
        *,
        format: Optional[str] = None,  # noqa: A002
        auth: Optional[NatsAuth] = None,
        timeout: float = DEFAULT_TIMEOUT,
        tls: Optional[TlsConfig] = None,
    ) -> None:
        """Builds the store.

        ``servers`` are NATS URLs, as ``nats://host:4222``; a list, because a
        cluster is ordinary and the client fails over between them. ``bucket``
        is the JetStream KV bucket and ``key`` the key in it; ``format`` is
        taken from the key's extension when it has one.

        ``auth`` defaults to :meth:`NatsAuth.anonymous`, which is what an
        unauthenticated development server wants. ``timeout`` is the deadline
        for **one fetch attempt**, in seconds.

        ``tls`` is a private certificate authority, a client certificate, or
        both — see :class:`~dynamic_config_remote.TlsConfig`. **NATS takes
        paths only**: ``async-nats`` opens the files itself, so
        ``with_ca_certificate_pem`` and ``with_client_certificate_pem`` are
        refused here rather than ignored. Naming an authority also turns TLS
        on, so a ``nats://`` URL that would have negotiated plaintext fails
        instead of connecting without it.

        Raises `ValueError` if there are no servers, if the format can be
        neither read nor deduced, or if ``tls`` names PEM bytes, and
        `TypeError` if ``auth`` is not a `NatsAuth` or ``tls`` is not a
        `TlsConfig`.
        """
        if not servers:
            raise ValueError("nats needs at least one server")

        if auth is not None and not isinstance(auth, NatsAuth):
            raise TypeError(
                f"auth has to be a NatsAuth, not {type(auth).__name__}; build "
                f"one with NatsAuth.token(...) or another of its methods"
            )

        format = checked(key, format)  # noqa: A001

        self._auth = auth if auth is not None else NatsAuth.anonymous()
        self._store = _core.NatsStore(
            list(servers), bucket, key, format, timeout, unwrapped(tls)
        )

    def fetch(self) -> tuple[str, Format]:
        """Reads the key, and answers ``(document, format)``.

        The credential is resolved first, on this thread; the connection and
        the read release the GIL. A credential that has changed since the last
        fetch reconnects with it, because NATS authenticates on connect and
        there is no header to change instead.

        Raises :class:`dynamic_config.AuthError` if NATS refused the
        credential — an authorization violation or a nonce it would not take —
        and :class:`dynamic_config.RemoteError` otherwise.
        """
        kind, first, second = self._auth._resolve()

        with translated():
            text, named = self._store.fetch(kind, first, second)

        return text, Format(named)

    def describe(self) -> str:
        """Names the store, with any credential in a server URL removed."""
        return self._store.describe()

    def __repr__(self) -> str:
        """The store's description, which is already redacted."""
        return repr(self._store)
