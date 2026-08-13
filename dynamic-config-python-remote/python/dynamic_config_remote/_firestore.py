"""Firestore, as a configuration source."""

from __future__ import annotations

from typing import Optional

from dynamic_config import Format, RemoteSource

from . import _core
from ._credential import Credential, resolve
from ._errors import translated
from ._format import DEFAULT_TIMEOUT
from ._tls import TlsConfig, unwrapped

#: Where a Google workload asks for its own token. Reachable from GKE, Cloud
#: Run, GCE and Cloud Functions, and from nowhere else — which is the security
#: property that makes it the right default.
METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)

#: Firestore's default database, which is what a project has unless somebody
#: made a second one. The parentheses are Google's, not a placeholder.
DEFAULT_DATABASE = "(default)"


class FirestoreAuth:
    """How to obtain an access token for the Firestore API.

    Mirrors ``dynamic_config_firestore::Auth``::

        FirestoreAuth.metadata_server()      # GKE, Cloud Run, GCE — no secret at all
        FirestoreAuth.access_token(token)    # anything that already has one
        FirestoreAuth.emulator()             # the emulator, which wants none

    **`access_token` accepts a callable**, and that is the variant a callable
    exists for. :meth:`metadata_server` renews on its own — the Rust crate
    asks the workload's own metadata server and replaces the token as it
    approaches expiry — so a deployment on Google's own infrastructure needs
    no callable and should not write one. An access token minted outside the
    process by `gcloud` or by a library that handles Google credentials cannot
    renew, expires in an hour, and a callable is exactly its replacement.

    **A service-account JSON key is deliberately absent**, and that is the
    Rust crate's decision taken as a recommendation rather than a gap:
    signing one means an RS256 stack in a configuration library, and Google's
    own guidance is that a downloaded key is the option of last resort.
    """

    __slots__ = ("_kind", "_value")

    def __init__(self, kind: str, value: Credential) -> None:
        """Not for direct use — build one with the classmethods."""
        self._kind = kind
        self._value = value

    @classmethod
    def emulator(cls) -> FirestoreAuth:
        """No token at all, for the Firestore emulator."""
        return cls("emulator", "")

    @classmethod
    def access_token(cls, token: Credential) -> FirestoreAuth:
        """A token somebody already obtained.

        ``gcloud auth print-access-token`` produces one; so does any library
        that already handles Google credentials.
        """
        return cls("access_token", token)

    @classmethod
    def metadata_server(cls) -> FirestoreAuth:
        """The workload's own identity, from the conventional address."""
        return cls("metadata_server", METADATA_TOKEN_URL)

    def with_url(self, url: str) -> FirestoreAuth:
        """Asks somewhere other than the conventional metadata address.

        For a sidecar that proxies it. No effect on the other two, as in Rust.
        """
        if self._kind != "metadata_server":
            return self

        return FirestoreAuth(self._kind, url)

    def _resolve(self) -> tuple[str, str]:
        """This credential's current value, as the compiled store takes it."""
        return self._kind, resolve(self._value, f"{self._kind}'s argument")

    def __repr__(self) -> str:
        """The method, and never the token.

        The metadata server's URL is printed: it is an address rather than a
        secret, and a sidecar's port is the first thing somebody debugging
        this needs. The Rust crate's `Debug` prints it too.
        """
        if self._kind == "access_token":
            return "FirestoreAuth.access_token('***')"

        if self._kind == "metadata_server":
            url = self._value if isinstance(self._value, str) else "<callable>"

            return f"FirestoreAuth.metadata_server({url!r})"

        return "FirestoreAuth.emulator()"


class Firestore(RemoteSource):
    """A document in Firestore.

    Mirrors ``dynamic_config_firestore::Firestore``: the project, the document
    path, the section key, the database, the endpoint and the per-fetch
    deadline.

        from dynamic_config import DynamicConfig
        from dynamic_config.remote import Firestore, FirestoreAuth

        config = DynamicConfig(Database, key="db").remote(
            Firestore("my-project", "config/db", auth=FirestoreAuth.metadata_server())
        )
        config.refresh_remote()
        config.init()

    Firestore stores a map of named fields, so — like
    :class:`~dynamic_config_remote.Vault` and unlike
    :class:`~dynamic_config_remote.Consul` — what is read is wrapped under
    ``key`` before it is merged, which has to be the same key the
    configuration was built with.

    Its types map onto configuration the obvious way; a ``timestampValue``,
    ``bytesValue`` or ``referenceValue`` becomes its string form, because a
    configuration file has no better answer for one either.
    """

    __slots__ = ("_auth", "_store")

    def __init__(
        self,
        project: str,
        path: str,
        *,
        auth: FirestoreAuth,
        key: str = "db",
        database: str = DEFAULT_DATABASE,
        endpoint: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        tls: Optional[TlsConfig] = None,
    ) -> None:
        """Builds the store.

        ``project`` is the GCP project and ``path`` is collection-then-document
        — ``config/db``, or ``environments/prod/config/db`` for a nested one.

        ``auth`` is required and has no default, which is where this differs
        from the Rust builder: that one defaults to the emulator, and a
        default of *send no credentials* is right for a builder being filled
        in and wrong for a constructor, where it would quietly produce a 401
        against the real service. Say :meth:`FirestoreAuth.emulator` to mean
        it.

        ``key`` is the section key the document is wrapped under and must
        match the configuration's. ``endpoint`` is what the emulator needs —
        ``http://127.0.0.1:8080``. ``timeout`` is the deadline for **one fetch
        attempt**, in seconds, and covers fetching a token as well as reading
        the document.

        ``tls`` is a private certificate authority, a client certificate, or
        both — see :class:`~dynamic_config_remote.TlsConfig`. `None` and an
        empty ``TlsConfig`` both mean the platform's own trust store.

        Raises `TypeError` if ``auth`` is not a `FirestoreAuth` or ``tls`` is
        not a `TlsConfig`.
        """
        if not isinstance(auth, FirestoreAuth):
            raise TypeError(
                f"auth has to be a FirestoreAuth, not {type(auth).__name__}; "
                f"build one with FirestoreAuth.metadata_server(), "
                f"FirestoreAuth.access_token(...) or FirestoreAuth.emulator()"
            )

        self._auth = auth
        self._store = _core.FirestoreStore(
            project, path, key, database, endpoint, timeout, unwrapped(tls)
        )

    def fetch(self) -> tuple[str, Format]:
        """Reads the document, and answers ``(document, "json")``.

        JSON always: the crate turns Firestore's typed fields into a JSON
        object wrapped under the section key, and there is nothing else it
        could be.

        The credential is resolved first, on this thread; the read itself
        releases the GIL.

        Raises :class:`dynamic_config.AuthError` if Firestore refused the
        token — a 401 for a token it will not take, a 403 for an identity the
        IAM binding does not allow — and
        :class:`dynamic_config.RemoteError` otherwise.
        """
        kind, value = self._auth._resolve()

        with translated():
            text, named = self._store.fetch(kind, value)

        return text, Format(named)

    def describe(self) -> str:
        """Names the store, with any credential in the endpoint removed."""
        return self._store.describe()

    def __repr__(self) -> str:
        """The store's description, which is already redacted."""
        return repr(self._store)
