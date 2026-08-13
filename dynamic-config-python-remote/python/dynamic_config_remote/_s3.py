"""S3, as a configuration source."""

from __future__ import annotations

from typing import Optional

from dynamic_config import Format, RemoteSource

from . import _core
from ._credential import Credential, resolve
from ._errors import translated
from ._format import DEFAULT_TIMEOUT, checked
from ._tls import TlsConfig, unwrapped


class S3(RemoteSource):
    """An object in S3, whose body is a configuration document.

    Mirrors ``dynamic_config_s3::S3``: the bucket, the key, the format and the
    per-fetch deadline — plus the region and endpoint, which in Rust are an
    `SdkConfig` the caller builds and here are two arguments.

        from dynamic_config import DynamicConfig
        from dynamic_config.remote import S3

        # Credentials from the environment, the way every other AWS tool
        # finds them.
        config = DynamicConfig(Database, key="db").remote(
            S3("myapp-config", "prod/db.json")
        )
        config.refresh_remote()
        config.init()

    **Passing no credentials is a feature, not a default.** The AWS SDK's own
    chain — ``AWS_ACCESS_KEY_ID``, the shared profile, the EC2 instance role,
    the ECS task role, IRSA on EKS — is what runs when ``access_key_id`` and
    ``secret_access_key`` are absent, and on anything running in AWS it is the
    right answer. A second credential chain in a program that already has one
    is a bug waiting for a rotation.

    **When they are passed, each may be a callable.** They then replace the
    chain with a provider that answers from what the callable last returned —
    which is what reaches a store outside AWS, or a deployment whose
    credentials arrive from somewhere the chain has never heard of. Unlike
    every other store here, a rotated credential rebuilds *nothing*: the
    credential is not baked into the SDK client, so the next request simply
    signs with the new one.

    ``endpoint`` is what makes this work against anything that is not AWS —
    MinIO, Ceph, R2, B2 all speak this API — and path-style addressing is
    always on, because virtual-host style needs DNS only AWS has.
    """

    __slots__ = ("_access_key_id", "_secret_access_key", "_session_token", "_store")

    def __init__(
        self,
        bucket: str,
        key: str,
        *,
        format: Optional[str] = None,  # noqa: A002
        region: Optional[str] = None,
        endpoint: Optional[str] = None,
        access_key_id: Optional[Credential] = None,
        secret_access_key: Optional[Credential] = None,
        session_token: Optional[Credential] = None,
        timeout: float = DEFAULT_TIMEOUT,
        tls: Optional[TlsConfig] = None,
    ) -> None:
        """Builds the store.

        ``bucket`` and ``key`` name the object, whose body is a whole
        configuration document; ``format`` is taken from the key's extension
        when it has one.

        ``region`` and ``endpoint`` are resolved from the environment when
        absent, the same way the SDK resolves them for every other AWS tool.

        ``access_key_id`` and ``secret_access_key`` go together: one without
        the other cannot sign anything, so passing one alone is a `ValueError`
        rather than a chain that silently ignores it. ``session_token`` is for
        assumed credentials — ``sts:AssumeRole``, IRSA, an SSO session — and
        needs the other two.

        ``timeout`` is the deadline for **one fetch attempt**, in seconds. The
        SDK retries on its own, so a five-second timeout with three attempts
        is a fifteen-second call; that is the Rust crate's documented
        behaviour and this changes none of it.

        ``tls`` is a private certificate authority — see
        :class:`~dynamic_config_remote.TlsConfig` — and is what reaches a
        MinIO, Ceph or company gateway presenting a certificate AWS' public
        chain has never heard of. **S3 takes no client certificate**: the AWS
        SDK's TLS context is a trust store with no slot for one, so a
        ``TlsConfig`` naming one is refused here rather than half applied.

        Raises `ValueError` if the format can be neither read nor deduced, if
        the key pair is half given, or if ``tls`` names a client certificate,
        and `TypeError` if ``tls`` is not a `TlsConfig`.
        """
        if (access_key_id is None) != (secret_access_key is None):
            raise ValueError(
                "S3 needs both access_key_id and secret_access_key; one "
                "without the other cannot sign a request"
            )

        if session_token is not None and access_key_id is None:
            raise ValueError(
                "a session_token belongs to a key pair; pass access_key_id "
                "and secret_access_key with it"
            )

        format = checked(key, format)  # noqa: A001

        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self._store = _core.S3Store(
            bucket, key, format, region, endpoint, timeout, unwrapped(tls)
        )

    def fetch(self) -> tuple[str, Format]:
        """Reads the object, and answers ``(document, format)``.

        The credentials, if there are any, are resolved first on this thread
        and written where the SDK's credential provider reads them; the
        request itself releases the GIL. A rotated key signs the next request
        with no client rebuilt and no connection reopened.

        Raises :class:`dynamic_config.AuthError` if S3 refused the credentials
        — ``AccessDenied``, ``InvalidAccessKeyId``, ``SignatureDoesNotMatch``,
        an expired token — and :class:`dynamic_config.RemoteError` otherwise.
        A clock too far out of step is deliberately the second kind: that one
        does come right.
        """
        access_key_id = (
            None
            if self._access_key_id is None
            else resolve(self._access_key_id, "access_key_id")
        )
        secret_access_key = (
            None
            if self._secret_access_key is None
            else resolve(self._secret_access_key, "secret_access_key")
        )
        session_token = (
            None
            if self._session_token is None
            else resolve(self._session_token, "session_token")
        )

        with translated():
            text, named = self._store.fetch(
                access_key_id, secret_access_key, session_token
            )

        return text, Format(named)

    def describe(self) -> str:
        """Names the store, with any credential in the endpoint removed."""
        return self._store.describe()

    def __repr__(self) -> str:
        """The store's description, which is already redacted."""
        return repr(self._store)
