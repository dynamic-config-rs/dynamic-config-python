"""HashiCorp Vault, as a configuration source."""

from __future__ import annotations

from typing import Optional

from dynamic_config import Format, RemoteSource

from . import _core
from ._credential import Credential, resolve
from ._errors import translated
from ._tls import TlsConfig, unwrapped

#: Where a Kubernetes service-account token is mounted, by convention. The
#: same constant the Rust crate exports, repeated rather than imported
#: because there is nothing to import it from on this side.
SERVICE_ACCOUNT_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"

#: The default deadline for one fetch, in seconds.
DEFAULT_TIMEOUT = 10.0

#: Which half of each method's pair is the secret, for `__repr__`. The other
#: half stays printable on purpose: redaction that hides the role id as well
#: as the secret id is redaction nobody can debug through, and it is the
#: same split the Rust crate's `Debug` makes.
_SECRET_SLOT = {
    "token": 0,
    "jwt": 0,
    "app_role": 1,
    "userpass": 1,
    "ldap": 1,
}


class Auth:
    """How to obtain a Vault token.

    Mirrors ``dynamic_config_vault::Auth``, one classmethod per variant::

        Auth.token("hvs....")                    # a token somebody obtained
        Auth.app_role(role_id, secret_id)        # a service outside Kubernetes
        Auth.kubernetes("myapp")                 # the pod's own identity
        Auth.jwt(jwt)                            # JWT/OIDC
        Auth.userpass("admin", password)
        Auth.ldap("admin", password)
        Auth.certificate()                       # a TLS client certificate

    **Every credential argument accepts a callable.** ``Auth.token(...)``
    takes the token or a function returning it; ``Auth.app_role(...)`` takes
    the secret id or a function returning it, and so on. The function is
    called on every fetch, and a value that has changed rebuilds the client —
    which is what lets a watcher outlive the credential it started with. See
    :data:`~dynamic_config_remote.Credential`.

    ``Auth.certificate()`` says only *log in with a certificate*; the
    certificate itself is the ``tls`` argument on
    :class:`~dynamic_config_remote.Vault`, presented with
    :meth:`~dynamic_config_remote.TlsConfig.with_client_certificate_files`.

    The builder methods return a new ``Auth`` rather than mutating this one,
    so an ``Auth`` shared between two stores cannot be changed by either.
    """

    __slots__ = ("_first", "_kind", "_mount", "_second")

    def __init__(
        self, kind: str, mount: str, first: Credential, second: Credential
    ) -> None:
        """Not for direct use — build one with the classmethods."""
        self._kind = kind
        self._mount = mount
        self._first = first
        self._second = second

    # ── The methods ────────────────────────────────────────────────────

    @classmethod
    def token(cls, token: Credential) -> Auth:
        """A token somebody already obtained.

        The simplest thing that works, and — with a plain string — the only
        one that cannot recover on its own: there are no credentials here to
        log in again with. A *callable* changes that, which is the whole
        point of one being accepted: a token read from a file the Vault agent
        rewrites recovers exactly as a login would.
        """
        return cls("token", "", token, "")

    @classmethod
    def app_role(cls, role_id: Credential, secret_id: Credential) -> Auth:
        """AppRole: a role id and a secret id, on the ``approle`` mount."""
        return cls("app_role", "approle", role_id, secret_id)

    @classmethod
    def kubernetes(cls, role: str) -> Auth:
        """Kubernetes: the pod's service-account token, plus a Vault role.

        No credential argument, because there is no credential to pass: the
        JWT is read from disk at every login by the Rust crate, which is what
        keeps a rotated projected token working.
        """
        return cls("kubernetes", "kubernetes", role, SERVICE_ACCOUNT_TOKEN)

    @classmethod
    def jwt(cls, jwt: Credential) -> Auth:
        """A JWT or OIDC token, on the ``jwt`` mount."""
        return cls("jwt", "jwt", jwt, "")

    @classmethod
    def userpass(cls, username: str, password: Credential) -> Auth:
        """Username and password, on the ``userpass`` mount."""
        return cls("userpass", "userpass", username, password)

    @classmethod
    def ldap(cls, username: str, password: Credential) -> Auth:
        """Username and password against LDAP, on the ``ldap`` mount."""
        return cls("ldap", "ldap", username, password)

    @classmethod
    def certificate(cls) -> Auth:
        """A TLS client certificate, on the ``cert`` mount."""
        return cls("certificate", "cert", "", "")

    # ── The modifiers ──────────────────────────────────────────────────

    def at_mount(self, path: str) -> Auth:
        """Puts this method on a different mount path.

        No effect on :meth:`token`, which has no mount.
        """
        if self._kind == "token":
            return self

        return Auth(self._kind, path, self._first, self._second)

    def with_role(self, role: str) -> Auth:
        """Names the role, for the methods that take one.

        :meth:`kubernetes`, :meth:`jwt` and :meth:`certificate`; anything
        else is unchanged, as it is in Rust.
        """
        if self._kind == "kubernetes":
            return Auth(self._kind, self._mount, role, self._second)

        if self._kind in ("jwt", "certificate"):
            return Auth(self._kind, self._mount, self._first, role)

        return self

    def with_token_path(self, path: str) -> Auth:
        """Reads the service-account token from somewhere else.

        :meth:`kubernetes` only.
        """
        if self._kind != "kubernetes":
            return self

        return Auth(self._kind, self._mount, self._first, path)

    # ── What the store asks for ────────────────────────────────────────

    def _resolve(self) -> tuple[str, str, str, str]:
        """This credential's current value, as the compiled store takes it."""
        return (
            self._kind,
            self._mount,
            resolve(self._first, f"{self._kind}'s first argument"),
            resolve(self._second, f"{self._kind}'s second argument"),
        )

    def __repr__(self) -> str:
        """The method and its non-secret half. Never the credential."""
        secret = _SECRET_SLOT.get(self._kind)
        halves = [self._first, self._second]
        shown = [
            "***" if index == secret else _printable(half)
            for index, half in enumerate(halves)
        ]

        return f"Auth.{self._kind}({shown[0]!r}, {shown[1]!r})"


def _printable(half: Credential) -> str:
    """A non-secret half, as text — or what it is, if it is a callable."""
    return half if isinstance(half, str) else "<callable>"


class Vault(RemoteSource):
    """A secret in Vault's KV v2 store.

    Mirrors ``dynamic_config_vault::Vault``: the address, the mount, the
    path, the section key the secret is wrapped under, the Enterprise
    namespace and the per-fetch deadline.

        from dynamic_config import DynamicConfig
        from dynamic_config.remote import Auth, Vault

        config = DynamicConfig(Database, key="db").remote(
            Vault(
                "https://vault.internal:8200", "secret", "myapp/db",
                key="db",
                auth=Auth.kubernetes("myapp"),
            )
        )

    Vault stores a section's *contents* rather than a whole document, so what
    is read is wrapped under ``key`` before it is merged — which has to be the
    same key the configuration was built with, or the values land under a
    section nothing reads.

    Nothing logs in here. The first :meth:`fetch` does, and the token it gets
    is cached and renewed by the Rust crate for as long as the login
    credential stays the same.
    """

    __slots__ = ("_auth", "_store")

    def __init__(
        self,
        address: str,
        mount: str,
        path: str,
        *,
        auth: Auth,
        key: str = "db",
        namespace: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        tls: Optional[TlsConfig] = None,
    ) -> None:
        """Builds the store.

        ``auth`` is required and has no default: a `Vault` with no
        credentials could only ever produce a 403, and the Rust crate's
        empty-token default exists for a builder that is filled in
        afterwards, which this constructor is not.

        ``timeout`` is the deadline for **one fetch attempt**, in seconds.

        ``tls`` is a private certificate authority, a client certificate, or
        both — see :class:`~dynamic_config_remote.TlsConfig`. `None` and an
        empty ``TlsConfig`` both mean the platform's own trust store.
        Raises `TypeError` if ``auth`` is not an `Auth` or ``tls`` is not a
        `TlsConfig`.
        """
        if not isinstance(auth, Auth):
            raise TypeError(
                f"auth has to be an Auth, not {type(auth).__name__}; build "
                f"one with Auth.token(...), Auth.app_role(...) or another of "
                f"its methods"
            )

        self._auth = auth
        self._store = _core.VaultStore(
            address, mount, path, key, namespace, timeout, unwrapped(tls)
        )

    def fetch(self) -> tuple[str, Format]:
        """Reads the secret, and answers ``(document, format)``.

        The login credential is resolved first, on this thread; the read
        itself releases the GIL. A credential that has changed since the last
        fetch replaces the cached Vault token, because a token obtained with
        a credential that has been rotated is a token nobody should keep.

        Raises :class:`dynamic_config.AuthError` if Vault refused the
        credentials and :class:`dynamic_config.RemoteError` otherwise. Neither
        message carries the token, the secret id or the password.
        """
        kind, mount, first, second = self._auth._resolve()

        with translated():
            text, named = self._store.fetch(kind, mount, first, second)

        return text, Format(named)

    def describe(self) -> str:
        """Names the store, with any credential in the address removed."""
        return self._store.describe()

    def __repr__(self) -> str:
        """The store's description, which is already redacted."""
        return repr(self._store)
