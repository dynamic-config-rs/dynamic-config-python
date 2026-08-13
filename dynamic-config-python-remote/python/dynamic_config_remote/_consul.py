"""Consul's key/value store, as a configuration source."""

from __future__ import annotations

from typing import Optional

from dynamic_config import Format, RemoteSource

from . import _core
from ._credential import Credential, resolve
from ._errors import translated
from ._format import DEFAULT_TIMEOUT, checked
from ._tls import TlsConfig, unwrapped

#: Where a Kubernetes service-account token is mounted, by convention. The
#: same path the Vault store uses and the same one the Rust crates export;
#: repeated rather than imported because there is nothing to import it from
#: on this side.
SERVICE_ACCOUNT_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"


class ConsulAuth:
    """How to obtain a Consul ACL token.

    Mirrors ``dynamic_config_consul::Auth``, which is a shorter story than
    Vault's — a token is either handed to the process or bought at an auth
    method with a bearer token::

        ConsulAuth.anonymous()                  # ACLs disabled, or a readable default
        ConsulAuth.token(token)                 # usually CONSUL_HTTP_TOKEN
        ConsulAuth.kubernetes("kubernetes")     # the pod's own identity
        ConsulAuth.jwt("oidc", token)           # a JWT or OIDC id token

    **The credential arguments accept callables.** ``ConsulAuth.token(...)``
    takes the ACL token or a function returning it, and ``ConsulAuth.jwt(...)``
    takes the bearer token or a function returning it. The function runs on
    every fetch and a value that has changed logs in again — which is what
    lets a watcher outlive the credential it started with. See
    :data:`~dynamic_config_remote.Credential`.

    :meth:`kubernetes` needs no callable and takes none: it names a *file*,
    and the Rust crate re-reads that file at every login, so a projected
    service-account token the kubelet rotates already works.

    Two credentials are at play and only one of them is here. The ACL token
    Consul issues expires, and replacing it is the Rust crate's job — it logs
    in again as expiry approaches and after a 403. The **bearer** token
    presented to get it is the deployment's, and that is the one a callable
    rotates.

    The builder methods return a new ``ConsulAuth`` rather than mutating this
    one, so an auth shared between two stores cannot be changed by either.
    """

    __slots__ = ("_bearer", "_kind", "_meta", "_method")

    def __init__(
        self,
        kind: str,
        method: str,
        bearer: Credential,
        meta: tuple[tuple[str, str], ...],
    ) -> None:
        """Not for direct use — build one with the classmethods."""
        self._kind = kind
        self._method = method
        self._bearer = bearer
        self._meta = meta

    # ── The methods ────────────────────────────────────────────────────

    @classmethod
    def anonymous(cls) -> ConsulAuth:
        """No token at all.

        Correct for a Consul with ACLs disabled and for a `default` policy
        that allows reads, both of which are ordinary in development — which
        is why this is what a `Consul` with no ``auth`` uses.
        """
        return cls("anonymous", "", "", ())

    @classmethod
    def token(cls, token: Credential) -> ConsulAuth:
        """A token somebody already obtained, usually ``CONSUL_HTTP_TOKEN``.

        With a plain string this is the one variant that cannot recover on its
        own: there are no credentials here to log in again with. A *callable*
        changes that, which is the whole point of one being accepted.
        """
        return cls("token", "", token, ())

    @classmethod
    def kubernetes(cls, method: str) -> ConsulAuth:
        """The pod's service-account token, presented to ``method``.

        No credential argument, because there is no credential to pass: the
        JWT is read from disk at every login by the Rust crate, which is what
        keeps a rotated projected token working. :meth:`with_bearer_file`
        moves the path.
        """
        return cls("login_file", method, SERVICE_ACCOUNT_TOKEN, ())

    @classmethod
    def jwt(cls, method: str, token: Credential) -> ConsulAuth:
        """A JWT or OIDC id token presented to ``method``."""
        return cls("login", method, token, ())

    # ── The modifiers ──────────────────────────────────────────────────

    def with_bearer_file(self, path: str) -> ConsulAuth:
        """Reads the bearer token from a file, rather than holding one.

        For a login method only; :meth:`anonymous` and :meth:`token` are
        unchanged, as they are in Rust. The file is re-read at every login,
        so this is the right spelling for any token something else rewrites.
        """
        if self._kind not in ("login", "login_file"):
            return self

        return ConsulAuth("login_file", self._method, path, self._meta)

    def with_meta(self, name: str, value: str) -> ConsulAuth:
        """Adds a ``Meta`` entry, which Consul attaches to the issued token.

        For a login method only. It is for the audit log — which workload,
        which pod — so it is a value rather than a credential.
        """
        if self._kind not in ("login", "login_file"):
            return self

        return ConsulAuth(
            self._kind, self._method, self._bearer, (*self._meta, (name, value))
        )

    # ── What the store asks for ────────────────────────────────────────

    def _resolve(self) -> tuple[str, str, str, list[tuple[str, str]]]:
        """This credential's current value, as the compiled store takes it.

        A ``login_file`` carries a path rather than a token, and `resolve`
        hands a plain string straight back — so the file is not read here. It
        is read by the Rust crate, at every login, which is what a rotated
        projected token needs.
        """
        return (
            self._kind,
            self._method,
            resolve(self._bearer, f"{self._kind}'s token"),
            list(self._meta),
        )

    def __repr__(self) -> str:
        """The method, and never the token.

        The auth method's name and a bearer *file's path* are printed: neither
        is a secret, and both are the first thing somebody debugging a refused
        login needs. It is the same split the Rust crate's `Debug` makes.
        """
        if self._kind == "anonymous":
            return "ConsulAuth.anonymous()"

        if self._kind == "token":
            return "ConsulAuth.token('***')"

        if self._kind == "login_file":
            return f"ConsulAuth.kubernetes({self._method!r}, file={self._bearer!r})"

        return f"ConsulAuth.jwt({self._method!r}, '***')"


class Consul(RemoteSource):
    """A key in Consul's key/value store.

    Mirrors ``dynamic_config_consul::Consul``: the agent address, the key, the
    format, the datacenter, the per-fetch deadline and the ACL token.

        from dynamic_config import DynamicConfig
        from dynamic_config.remote import Consul, ConsulAuth

        config = DynamicConfig(Database, key="db").remote(
            Consul(
                "http://consul.internal:8500", "myapp/db.json",
                auth=ConsulAuth.token(lambda: os.environ["CONSUL_HTTP_TOKEN"]),
            )
        )
        config.refresh_remote()
        config.init()

    Consul stores an opaque blob, so **the value is a whole configuration
    document** — the same bytes that would be in a file. That is the opposite
    of :class:`~dynamic_config_remote.Vault`, which wraps a secret's fields
    under a section key, and the difference is not a whim: Vault stores a map
    of named secrets and Consul stores a blob.

    Nothing reaches the agent here. The first :meth:`fetch` does.
    """

    __slots__ = ("_auth", "_store")

    def __init__(
        self,
        address: str,
        key: str,
        *,
        format: Optional[str] = None,  # noqa: A002
        auth: Optional[ConsulAuth] = None,
        datacenter: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        tls: Optional[TlsConfig] = None,
    ) -> None:
        """Builds the store.

        ``address`` is the agent's, as ``http://host:8500``; ``key`` is the
        key whose value is the configuration document. ``format`` is taken
        from the key's extension when it has one and has to be given
        otherwise.

        ``auth`` defaults to :meth:`ConsulAuth.anonymous`, which is what a
        Consul with ACLs disabled wants — unlike Vault, where no credentials
        could only ever produce a 403.

        ``datacenter`` reads from one that is not the agent's own.
        ``timeout`` is the deadline for **one fetch attempt**, in seconds.

        ``tls`` is a private certificate authority, a client certificate, or
        both — see :class:`~dynamic_config_remote.TlsConfig`. `None` and an
        empty ``TlsConfig`` both mean the platform's own trust store.

        Raises `ValueError` if the format can be neither read nor deduced, and
        `TypeError` if ``auth`` is not a `ConsulAuth` or ``tls`` is not a
        `TlsConfig`.
        """
        if auth is not None and not isinstance(auth, ConsulAuth):
            raise TypeError(
                f"auth has to be a ConsulAuth, not {type(auth).__name__}; "
                f"build one with ConsulAuth.token(...) or another of its "
                f"methods"
            )

        format = checked(key, format)  # noqa: A001

        self._auth = auth if auth is not None else ConsulAuth.anonymous()
        self._store = _core.ConsulStore(
            address, key, format, datacenter, timeout, unwrapped(tls)
        )

    def fetch(self) -> tuple[str, Format]:
        """Reads the key, and answers ``(document, format)``.

        The credential is resolved first, on this thread; the read itself
        releases the GIL. A bearer token that has changed since the last fetch
        logs in again, because an ACL token bought with a credential that has
        been rotated is a token nobody should keep.

        Raises :class:`dynamic_config.AuthError` if Consul refused the token
        and :class:`dynamic_config.RemoteError` otherwise. Neither message
        carries a credential.
        """
        kind, method, bearer, meta = self._auth._resolve()

        with translated():
            text, named = self._store.fetch(kind, method, bearer, meta)

        return text, Format(named)

    def describe(self) -> str:
        """Names the store, with any credential in the address removed."""
        return self._store.describe()

    def __repr__(self) -> str:
        """The store's description, which is already redacted."""
        return repr(self._store)
