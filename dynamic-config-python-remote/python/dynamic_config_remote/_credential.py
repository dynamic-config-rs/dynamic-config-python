"""A credential that may be a value or a way of getting one.

Every credential argument in this package accepts a ``str`` **or a callable
returning one**, and the callable is the reason this module exists rather
than the string being enough.

A configuration watcher outlives its credentials. A Vault token has a TTL, an
AppRole secret id is rewritten by a sidecar, a workload-identity JWT is
refreshed by a daemon on the node, and an etcd password is rotated by whoever
rotates passwords. A store constructed with a ``str`` is a store holding the
credential that was true when the process started; three hours later that is
a 403 nobody can fix without a restart.

So a credential is resolved **on every fetch**, immediately before the store
is read::

    from dynamic_config_remote import Vault, Auth

    vault = Vault(
        "https://vault.internal:8200", "secret", "myapp/db",
        auth=Auth.token(lambda: Path("/var/run/vault/token").read_text()),
    )

The callable runs as ordinary Python, on the thread that asked for the
refresh, before the GIL is released for the network read. It may block — it
is often a file read or an HTTP call to a metadata server — and it may raise,
in which case the refresh fails the way any other fetch failure does: the
previous document keeps serving.

When what a callable returns is *different* from last time, the store rebuilds
its client with the new credential. When it is the same — overwhelmingly the
common case — nothing is rebuilt and the store's own token cache is untouched.
"""

from __future__ import annotations

from typing import Callable, Union

#: A credential: the value, or a callable that produces it.
#:
#: Not a ``Protocol`` and not a ``Coroutine``: the callable is invoked on the
#: fetch path, which is synchronous by construction — the base wheel's
#: ``refresh_remote_async`` runs the whole fetch on a worker thread rather
#: than making ``fetch()`` awaitable, so an ``async def`` here would never be
#: awaited by anything.
Credential = Union[str, Callable[[], str]]


def resolve(credential: Credential, argument: str) -> str:
    """The credential's current value.

    ``argument`` names the parameter it came from, for the error a callable
    returning the wrong type produces — which is a `TypeError` naming the
    argument rather than something the store reports as a malformed request
    much later.

    The value itself never appears in that error, for the reason every
    diagnostic in this project gives: a credential in an exception message is
    a credential in a log.
    """
    value = credential() if callable(credential) else credential

    if not isinstance(value, str):
        raise TypeError(
            f"{argument} resolved to {type(value).__name__}, not a str; a "
            f"credential callable has to return the credential"
        )

    return value
