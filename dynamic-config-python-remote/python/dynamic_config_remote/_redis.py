"""Redis, as a configuration source."""

from __future__ import annotations

from typing import Optional

from dynamic_config import Format, RemoteSource

from . import _core
from ._credential import Credential, resolve
from ._errors import translated
from ._format import DEFAULT_TIMEOUT, checked
from ._tls import TlsConfig, unwrapped


class Redis(RemoteSource):
    """A key in Redis, whose value is a configuration document.

    Mirrors ``dynamic_config_redis::Redis``: the URL, the key, the format and
    the per-fetch deadline — plus Redis' authentication, which this spells
    with arguments rather than leaving in the URL.

        from dynamic_config import DynamicConfig
        from dynamic_config.remote import Redis

        config = DynamicConfig(Database, key="db").remote(
            Redis(
                "redis://redis.internal:6379", "myapp/db.json",
                password=lambda: os.environ["REDIS_PASSWORD"],
            )
        )
        config.refresh_remote()
        config.init()

    **The credentials are arguments on purpose.** Redis has no login and no
    token: a client authenticates from what its connection string carried, so
    a rotated password is a different URL and therefore a different client. A
    callable cannot rotate a substring of a URL somebody passed at
    construction — it can rotate an argument, which is why ``user`` and
    ``password`` are separate and why each accepts a callable. They are
    spliced into the authority (percent-encoded, so a password containing
    ``@`` survives), and a pair that has not moved reuses the client and its
    open connection.

    ``password`` without ``user`` is Redis' own shape rather than a mistake:
    ``requirepass`` predates ACL users and implies the default one.

    Nothing connects here. The connection is opened on the first
    :meth:`fetch`, so a constructor that succeeded is not a promise that Redis
    is reachable — which is the Rust crate's contract too.
    """

    __slots__ = ("_password", "_store", "_user")

    def __init__(
        self,
        url: str,
        key: str,
        *,
        format: Optional[str] = None,  # noqa: A002
        user: Optional[Credential] = None,
        password: Optional[Credential] = None,
        timeout: float = DEFAULT_TIMEOUT,
        tls: Optional[TlsConfig] = None,
    ) -> None:
        """Builds the store.

        ``url`` is ``redis://host:6379``, or ``rediss://`` for TLS — the
        wheel is built with the Rust crate's `tls` feature on, so a
        ``rediss://`` URL works with no further argument and ``tls`` is what
        adds a *private* authority to it. ``key`` is the key whose value is
        the configuration document, and ``format`` is taken from its
        extension when it has one.

        ``user`` and ``password`` are Redis' authentication; each may be a
        callable, and a credential written into ``url`` instead is replaced by
        them. ``timeout`` is the deadline for **one fetch attempt**, in
        seconds — connecting, writing and waiting, because a deadline that
        only covers connecting sails straight past a wedged Redis.

        ``tls`` is a private certificate authority, a client certificate, or
        both — see :class:`~dynamic_config_remote.TlsConfig`. `None` and an
        empty ``TlsConfig`` both mean the platform's own trust store.

        TLS material with a ``redis://`` URL is refused rather than ignored —
        that is a deployment believing it is encrypted and is not — but by the
        Rust client as it is built, so the refusal arrives as a
        :class:`dynamic_config.RemoteError` at the first :meth:`fetch`: the
        URL is parsed where it is used, and one connection string is not
        parsed twice by two implementations that could disagree.

        Raises `ValueError` if the format can be neither read nor deduced, and
        `TypeError` if ``tls`` is not a `TlsConfig`.
        """
        format = checked(key, format)  # noqa: A001

        self._user = user
        self._password = password
        self._store = _core.RedisStore(url, key, format, timeout, unwrapped(tls))

    def fetch(self) -> tuple[str, Format]:
        """Reads the key, and answers ``(document, format)``.

        The credentials are resolved first, on this thread; the read itself
        releases the GIL.

        Raises :class:`dynamic_config.AuthError` if Redis refused them —
        ``NOAUTH``, ``WRONGPASS`` and ``NOPERM`` are its own words for that —
        and :class:`dynamic_config.RemoteError` otherwise. Neither message
        carries the password, nor one embedded in the URL.
        """
        user = None if self._user is None else resolve(self._user, "user")
        password = (
            None if self._password is None else resolve(self._password, "password")
        )

        with translated():
            text, named = self._store.fetch(user, password)

        return text, Format(named)

    def describe(self) -> str:
        """Names the store, with any credential in the URL removed."""
        return self._store.describe()

    def __repr__(self) -> str:
        """The store's description, which is already redacted."""
        return repr(self._store)
