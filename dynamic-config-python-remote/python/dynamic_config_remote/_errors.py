"""Turning this wheel's failures into the base wheel's exceptions.

The compiled half raises ``_core.StoreError`` and ``_core.StoreAuthError``,
which are this extension module's own classes — it cannot raise the base
wheel's, because those live in a different extension module with its own copy
of them. The translation happens here, where it is one function and a test
rather than an import held in Rust for the life of the process.

Two things are preserved across it, and they are the two that matter:

- **The distinction.** ``AuthError`` means *this credential was refused*,
  which waiting will not fix; ``RemoteError`` means *the store is unhappy*,
  which waiting might. A caller backing off needs to tell those apart, and
  the base wheel's remote path already carries the distinction the rest of
  the way — a Python source raising ``AuthError`` is reported as one.
- **The message, redacted.** Unlike the base wheel's handling of an arbitrary
  Python ``fetch()``, the message here *is* repeated, because this wheel
  wrote it: every store URL has been through the shared redaction and every
  credential resolved for the call has been scrubbed by value. What a user
  gets is "vault https://vault.internal:8200 secret/db: 403", which is the
  sentence they need, with nothing in it they must not see.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from dynamic_config import AuthError, RemoteError

from . import _core


@contextmanager
def translated() -> Iterator[None]:
    """Re-raises this wheel's store failures as the base wheel's.

    The original is attached as ``__cause__``, so a traceback still shows
    where in the store the failure came from.
    """
    try:
        yield
    except _core.StoreAuthError as refused:
        raise AuthError(str(refused)) from refused
    except _core.StoreError as failure:
        raise RemoteError(str(failure)) from failure
