r"""S3: the credential that is a trait, and the rotation that rebuilds nothing.

Six of the eight stores take their credential as a string and hand it over. The
AWS SDK does not: its credential surface is a `ProvideCredentials`
implementation, asked **per request**, from inside the SDK's own async
machinery. That single fact is the whole of what is characteristic here.

**Passing no credentials is a first-class mode**, not a fallback. With
`access_key_id` and `secret_access_key` absent, the SDK's own chain runs
untouched — environment, shared profile, EC2 instance role, ECS task role, IRSA
on EKS. On anything running inside AWS that is the right answer, and a second
credential chain in a program that already has one is a bug waiting for a
rotation.

**When they are passed, the callable becomes a shim over a slot** the fetch path
writes. So a rotated key rebuilds *nothing*: the next request signs with the new
value, and the connection pool and the resolved endpoint are untouched. It also
means nothing on the wheel's tokio runtime ever calls back into Python, which is
what makes that runtime safe to leave running forever.

Something that speaks the API to point it at:

    docker run --rm -d -p 9000:9000 -e MINIO_ROOT_USER=minioadmin \\
        -e MINIO_ROOT_PASSWORD=minioadmin minio/minio server /data

    mc alias set local http://127.0.0.1:9000 minioadmin minioadmin
    mc mb local/myapp-config
    echo '{"db": {"host": "s3-db", "port": 6003}}' | \\
        mc pipe local/myapp-config/prod/db.json

Then `python examples/06_s3.py`. With nothing listening it says so and carries
on.
"""

import contextlib
import os
from dataclasses import dataclass

from dynamic_config import DynamicConfig, DynamicConfigError
from dynamic_config_remote import S3, TlsConfig

BUCKET = os.environ.get("S3_BUCKET", "myapp-config")
ENDPOINT = os.environ.get("S3_ENDPOINT", "http://127.0.0.1:9000")


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432


def an_object_in_anything_that_speaks_the_api() -> None:
    """`endpoint` is what reaches MinIO, Ceph, R2 or B2 rather than AWS."""
    config = DynamicConfig(Database, key="db").remote(
        S3(
            BUCKET,
            "prod/db.json",
            region=os.environ.get("AWS_REGION", "us-east-1"),
            endpoint=ENDPOINT,
            # Both or neither. Each may be a callable, and here they are: a
            # rotated key pair is picked up per request with no client rebuilt.
            access_key_id=lambda: os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
            secret_access_key=lambda: os.environ.get(
                "AWS_SECRET_ACCESS_KEY", "minioadmin"
            ),
            timeout=5.0,
        )
    )

    try:
        config.refresh_remote()
        config.init()

        print("  read:", config.current())
        print("  provenance:", config.source_of("host"))
    except DynamicConfigError as unreachable:
        # The engine deliberately does not repeat a store's own message. The
        # detail is on `__cause__`, which is this wheel's, and which has
        # already had every credential taken out of it.
        print(f"  no S3 to read: {unreachable.__cause__ or unreachable}")


def no_credentials_is_the_mode_most_deployments_want() -> None:
    """Writing nothing is what gets the SDK's own chain, and it has to be."""
    store = S3(BUCKET, "prod/db.json", region="us-east-1")

    print("   ", store)
    print("  no arguments, so: environment, shared profile, instance role,")
    print("  task role, IRSA — the chain the deployment already has.")


def the_client_is_built_once_whatever_the_key_does() -> None:
    """The observable that makes the claim a claim rather than a paragraph.

    `clients_built` exists for the wheel's own test and for nothing else: S3
    has no login and no reconnection to count, so the client is the only thing
    a rotation could be seen in — and it is built once.
    """
    keys = iter(["AKIDFIRST", "AKIDSECOND", "AKIDSECOND"])
    store = S3(
        BUCKET,
        "prod/db.json",
        region="us-east-1",
        endpoint=ENDPOINT,
        access_key_id=lambda: next(keys),
        secret_access_key="secret",
        timeout=5.0,
    )

    # Whether the reads succeed is beside the point: what is counted is the
    # client, and a refused or unreachable fetch signs a request all the same.
    for _ in range(3):
        with contextlib.suppress(Exception):
            store.fetch()

    print(f"  three fetches, three key ids, {store._store.clients_built()} client(s)")
    print("  the SDK's identity cache is off, deliberately: it caches a")
    print("  credential with no expiry indefinitely, which for a mutable slot")
    print("  would mean the first key signing every request forever.")


def half_a_key_pair_is_refused() -> None:
    """One without the other cannot sign anything.

    Falling back to the SDK's chain here would quietly use a credential the
    caller did not ask for, which is the failure this refusal exists to
    prevent.
    """
    for arguments in (
        {"access_key_id": "AKID"},
        {"secret_access_key": "secret"},
        {"session_token": "session"},
    ):
        try:
            S3(BUCKET, "prod/db.json", **arguments)  # type: ignore[arg-type]
        except ValueError as refusal:
            print(f"  {refusal}")


def a_client_certificate_is_refused_rather_than_ignored() -> None:
    """The AWS SDK's TLS context is a trust store with no slot for one.

    A caller who asked to present a certificate and did not would meet it as an
    authentication failure a long way from the cause — so it raises here, and
    the certificate authority, which is the setting that matters for a MinIO or
    a company gateway, is accepted.
    """
    try:
        S3(
            BUCKET,
            "prod/db.json",
            tls=TlsConfig().with_client_certificate_files("/app.crt", "/app.key"),
        )
    except ValueError as refusal:
        print(f"  {refusal}")

    print("  and the authority it can express:")
    print(
        "   ",
        S3(
            BUCKET,
            "prod/db.json",
            region="us-east-1",
            endpoint="https://minio.internal:9000",
            tls=TlsConfig().with_ca_certificate_file("/etc/ssl/private-ca.pem"),
        ),
    )


def main() -> None:
    print("an object whose body is a whole configuration document:")
    an_object_in_anything_that_speaks_the_api()
    print()

    print("the mode with no credentials at all:")
    no_credentials_is_the_mode_most_deployments_want()
    print()

    print("what a rotated key costs:")
    the_client_is_built_once_whatever_the_key_does()
    print()

    print("what is refused at the line it was written on:")
    half_a_key_pair_is_refused()
    a_client_certificate_is_refused_rather_than_ignored()
    print()

    print("path-style addressing is always on, because the virtual-host form")
    print("needs DNS entries only AWS has.")


if __name__ == "__main__":
    main()
