"""A private certificate authority and a client certificate, as data.

Every store here reaches TLS through one type, and it is the same type in all
seven Rust crates — ``dynamic_config_store_core::tls::TlsConfig``. That it can
be spelled in Python at all is a property of how it was designed rather than a
translation done here: it holds **paths and PEM bytes and nothing else**, so
there is no ``tonic`` configuration, no ``ureq`` agent and no ``SdkConfig``
anywhere in it to have no Python word for.

    from dynamic_config.remote import TlsConfig, Vault, Auth

    vault = Vault(
        "https://vault.internal:8200", "secret", "myapp/db",
        auth=Auth.kubernetes("myapp"),
        tls=TlsConfig()
            .with_ca_certificate_file("/etc/ssl/private-ca.pem")
            .with_client_certificate_files(
                "/etc/ssl/app.crt", "/etc/ssl/app.key"
            ),
    )

An **empty** ``TlsConfig`` is not "no TLS": it is the platform's own trust
store, which is what a public certificate authority needs and what every store
here already did. Passing one changes nothing, which is why ``tls=None`` and
``tls=TlsConfig()`` are the same store.

Two stores cannot express all of it and **refuse the part they cannot**, with
a `ValueError` at construction naming the call and the way out — `Nats` takes
certificate *paths* only, and `S3` takes no client certificate at all. A
binding that quietly dropped either would leave a program believing it had
pinned a private authority when it had not, which is worse than one that will
not start.

There is no way to turn verification off. That is the Rust crates' decision
and the book argues it; the short version is that naming one more certificate
answers the two situations people reach for it in, and keeps the server
authenticated.
"""

from __future__ import annotations

import os
from typing import Optional, Union

from . import _core

#: A path: a ``str`` or anything with ``__fspath__``, so a `pathlib.Path` is
#: an ordinary argument. Quoted because ``os.PathLike`` is only subscriptable
#: from 3.9, and this package's floor is exactly 3.9 — the annotation is never
#: evaluated at runtime, and both type checkers read it.
PathArgument = Union[str, "os.PathLike[str]"]


class TlsConfig:
    """What a store needs to speak TLS to somewhere this machine distrusts.

    Mirrors ``dynamic_config_store_core::tls::TlsConfig``, method for method::

        TlsConfig()                                            # platform trust store
        TlsConfig().with_ca_certificate_file("/etc/ssl/ca.pem")
        TlsConfig().with_ca_certificate_pem(pem_bytes)
        TlsConfig().with_client_certificate_files("app.crt", "app.key")
        TlsConfig().with_client_certificate_pem(cert_bytes, key_bytes)

    Both spellings of each setting exist because both deployments do. A file
    is what a Kubernetes secret mount or an ``/etc/ssl`` layout produces;
    bytes are what a program that already fetched its material from a secrets
    manager has, and writing those to a temporary file so a client could read
    them back would put a private key on a disk that never asked for one.

    **Nothing is read here.** The files are opened when the store builds its
    client, so a missing certificate is an error naming the path at the first
    fetch rather than an exception in the middle of building a configuration,
    and a rotated authority is picked up by rebuilding the store.

    **A named authority replaces the platform trust store** rather than
    joining it: naming a private one is saying the public ones do not apply to
    this host. A deployment that needs both puts both in the one file.

    Every method answers a **new** ``TlsConfig`` rather than mutating this
    one, so a configuration shared between two stores cannot be changed by
    either.
    """

    __slots__ = ("_tls",)

    def __init__(self) -> None:
        """An empty configuration: platform trust store, no client certificate."""
        self._tls = _core.TlsConfig()

    @classmethod
    def _wrapping(cls, tls: _core.TlsConfig) -> TlsConfig:
        """The facade for a compiled configuration a builder just produced."""
        wrapper = cls.__new__(cls)
        wrapper._tls = tls

        return wrapper

    def with_ca_certificate_file(self, path: PathArgument) -> TlsConfig:
        """Trusts the certificate authority in this PEM file.

        The file may hold several certificates and all of them are trusted,
        which is what a private CA with an intermediate needs.
        """
        return self._wrapping(self._tls.with_ca_certificate_file(path))

    def with_ca_certificate_pem(self, pem: bytes) -> TlsConfig:
        """Trusts the certificate authority in these PEM bytes.

        For a program that already has the material — from a secrets manager,
        from its own configuration — and should not have to put it on a disk
        for a client to read back.

        `Nats` refuses this spelling: ``async-nats`` opens the file itself.
        """
        return self._wrapping(self._tls.with_ca_certificate_pem(pem))

    def with_client_certificate_files(
        self, certificate: PathArgument, key: PathArgument
    ) -> TlsConfig:
        """Presents this client certificate and private key (mTLS).

        Both are PEM files; ``certificate`` may be a chain, leaf first, as
        every TLS stack here expects. The pair is one argument list because
        presenting either half alone is not mTLS, it is a misconfiguration.

        `S3` refuses a client certificate in either spelling: the AWS SDK's
        TLS context has no slot for one.
        """
        return self._wrapping(self._tls.with_client_certificate_files(certificate, key))

    def with_client_certificate_pem(self, certificate: bytes, key: bytes) -> TlsConfig:
        """Presents this client certificate and private key (mTLS), from bytes.

        The private key is the sharpest secret this package handles. It is
        never rendered by :meth:`__repr__`, never quoted into an exception and
        never written anywhere: it goes from here into the client's own key
        type and stops.

        `Nats` and `S3` both refuse this one, for their own reasons.
        """
        return self._wrapping(self._tls.with_client_certificate_pem(certificate, key))

    def is_empty(self) -> bool:
        """Whether this asks for nothing — the platform's own trust store.

        A store uses it to tell "the caller wants the defaults" from "the
        caller wants something", which is the difference between leaving its
        client alone and building one.
        """
        return bool(self._tls.is_empty())

    def __repr__(self) -> str:
        """Shape, from the Rust type's own `Debug`. Never the material.

        Delegated rather than rebuilt from the arguments, and deliberately:
        the rule that a private key is printed as ``<redacted>`` and a
        certificate's bytes as ``<pem bytes>`` is implemented once, in the
        crate that owns the type, with a planted-key test over it. A second
        implementation here would be the copy nobody thinks to check.
        """
        return repr(self._tls)


def unwrapped(tls: Optional[TlsConfig]) -> Optional[_core.TlsConfig]:
    """The compiled configuration a store constructor passes down.

    One copy, because seven constructors want the same three lines, and the
    `TypeError` is worth having in all seven: without it, a caller who passed
    something else meets an `AttributeError` about a private attribute.
    """
    if tls is None:
        return None

    if not isinstance(tls, TlsConfig):
        raise TypeError(
            f"tls has to be a TlsConfig, not {type(tls).__name__}; build one "
            f"with TlsConfig().with_ca_certificate_file(...) or another of "
            f"its methods"
        )

    return tls._tls
