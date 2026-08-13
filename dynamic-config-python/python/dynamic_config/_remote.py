"""A remote store written in Python.

The engine reads etcd, Consul, Vault, NATS, Redis, S3 and Firestore from
Rust, and none of those clients ride into this wheel. What does ride in is
the door: a plain Python object with ``fetch()`` and ``describe()`` is a
remote store here, so a company's own service, a file a sidecar writes or
an API nobody will ever write a Rust client for needs no Rust at all.

    class OurService(RemoteSource):
        def fetch(self):
            response = httpx.get(URL, timeout=5)
            response.raise_for_status()
            return response.text, Format.JSON

        def describe(self):
            return "our service"

    config = DynamicConfig(Database, key="db").remote(OurService())
    config.refresh_remote()
    config.init()

Fetching is **explicit**, exactly as it is in Rust: `refresh_remote()`
reads the store and keeps the document; a load merges what was kept and
touches no network. Configuration is read on nearly every request, and a
round trip there would be indefensible.
"""

from __future__ import annotations

import abc
import enum


class Format(str, enum.Enum):
    """The format a fetched document is written in.

    A ``str`` enum, so ``Format.JSON`` and ``"json"`` are both accepted by
    :meth:`RemoteSource.fetch` — the enum is what an editor completes and
    a type checker knows, and the string is what a store that already
    speaks in content types can hand over unchanged.
    """

    JSON = "json"
    TOML = "toml"
    YAML = "yaml"

    def __str__(self) -> str:
        """The format's name, so an f-string reads as the document does."""
        return self.value


class RemoteSource(abc.ABC):
    """A remote store this configuration can read, implemented in Python.

    Subclass it, implement the two methods, and hand an instance to
    :meth:`DynamicConfig.remote`. Both are abstract, so a class missing
    one cannot be instantiated at all — which is a `TypeError` where the
    store is constructed, rather than something a deployment discovers at
    its first refresh.

    Three things are worth knowing before writing one:

    - **The timeout is yours.** Nothing here can interrupt a ``fetch()``
      that never returns — Rust cannot cancel arbitrary Python — so the
      deadline belongs to the client the method calls
      (``httpx.get(..., timeout=5)``). A store with no timeout is a
      ``refresh_remote()`` with no timeout.
    - **A raising ``fetch()`` is reported, never fatal.** The exception
      arrives at the caller as :class:`RemoteError` — or
      :class:`AuthError`, if what you raise is one — with the original
      attached as ``__cause__``. The previous document and the previous
      model both keep serving.
    - **The GIL is not held for you.** ``fetch()`` runs as ordinary Python
      on the thread that asked for the refresh, so a blocking call inside
      it releases the GIL the way it does anywhere else. It may also call
      back into the configuration — ``current()``, ``snapshot()``,
      ``explain()`` — with one exception: it must not call
      ``refresh_remote()``, which is the refresh it is answering.
    """

    __slots__ = ()

    @abc.abstractmethod
    def fetch(self) -> tuple[str, Format]:
        """Reads the store, and answers ``(document, format)``.

        The document is text in that format — JSON, TOML or YAML — and is
        merged as a file would be, above the files and below the
        environment.

        Raise to report a failure. Raising :class:`AuthError` says *this
        credential was refused*, which a caller can back off differently
        from a store that is merely unreachable; anything else is reported
        as :class:`RemoteError`.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def describe(self) -> str:
        """Names this store, for provenance and for error messages.

        Asked **once**, when the source is installed, because the engine
        reads it on the load path and a load must not re-enter Python. It
        is rendered in diagnostics (`source_of("port")` answers
        ``Origin(kind='remote', detail=...)``), so name the store rather
        than the credential that reaches it.
        """
        raise NotImplementedError
