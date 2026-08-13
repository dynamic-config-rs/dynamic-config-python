r"""NATS: a `.creds` file read on every fetch, and a TLS spelling that is refused.

NATS puts every credential on the **connection**, so `NatsAuth` mirrors the
constructors of `async_nats::ConnectOptions` rather than an `Auth` enum the Rust
crate does not have. A rotated credential is a reconnection, and the store
notices because the callable is resolved before every fetch.

The characteristic one is `NatsAuth.credentials_file(...)`. A `.creds` file
holds an account's JWT and an NKey seed, and it is exactly the thing an operator
replaces without restarting anything — so this variant *is* a callable: it reads
the file on every fetch rather than taking a copy at construction. This example
proves that with a real file it rewrites, which needs no server at all.

Two more things are NATS' own. **Nothing connects at construction**, which is a
deliberate difference from the Rust crate, where `Nats::with_options` connects
and resolves the bucket. And **TLS takes file paths only** — `async-nats` opens
the files itself — so the PEM-bytes spellings are refused rather than ignored.

A server to point it at:

    docker run --rm -d -p 4222:4222 nats:2.10 -js

    nats kv add config
    nats kv put config db.json '{"db": {"host": "nats-db", "port": 6002}}'

Then `python examples/05_nats.py`. With nothing listening it says so and
carries on.
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dynamic_config import DynamicConfig, DynamicConfigError
from dynamic_config_remote import Nats, NatsAuth, TlsConfig

SERVER = os.environ.get("NATS", "nats://127.0.0.1:4222")


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432


def a_key_in_a_jetstream_bucket() -> None:
    """A key in a bucket that is never created.

    A configuration reader that provisioned storage would hide a
    misconfigured deployment behind an empty one.
    """
    credentials = os.environ.get("NATS_CREDS")

    config = DynamicConfig(Database, key="db").remote(
        Nats(
            [SERVER],
            "config",
            "db.json",
            auth=(
                NatsAuth.credentials_file(credentials)
                if credentials
                else NatsAuth.anonymous()
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
        print(f"  no NATS to read: {unreachable.__cause__ or unreachable}")


def a_creds_file_is_read_again_every_time() -> None:
    """The claim, demonstrated rather than stated — and with no server needed.

    `_resolve()` is what the store calls on the fetch path, so asking it twice
    across a rewrite is exactly what a refresh across a rotation does.
    """
    with tempfile.TemporaryDirectory() as directory:
        creds = Path(directory) / "nats.creds"
        creds.write_text("-----BEGIN NATS USER JWT-----\nfirst\n")
        auth = NatsAuth.credentials_file(str(creds))

        first = auth._resolve()[1]

        creds.write_text("-----BEGIN NATS USER JWT-----\nsecond\n")
        second = auth._resolve()[1]

    print("  the same NatsAuth object, across a file an operator replaced:")
    print("    first fetch would send: ", first.splitlines()[-1])
    print("    second fetch would send:", second.splitlines()[-1])
    print("  and the contents never appear in a message: a `.creds` holds a")
    print("  JWT and an NKey seed, so the whole blob is treated as the secret.")


def every_credential_shape_has_a_python_spelling() -> None:
    """`ConnectOptions`' constructors, minus the two that are not a credential."""
    for auth in (
        NatsAuth.anonymous(),
        NatsAuth.token(lambda: os.environ.get("NATS_TOKEN", "")),
        NatsAuth.user_and_password("app", lambda: os.environ.get("NATS_PASSWORD", "")),
        NatsAuth.nkey_seed(lambda: os.environ.get("NATS_NKEY_SEED", "")),
        NatsAuth.credentials("-----BEGIN NATS USER JWT-----"),
        NatsAuth.credentials_file("/etc/myapp/nats.creds"),
    ):
        print("   ", auth)

    print("  TLS and the JWT-with-signing-callback are the two `ConnectOptions`")
    print("  settings that are not here: the first has its own argument, and")
    print("  the second is a Rust callback with no Python spelling.")


def pem_bytes_are_refused_rather_than_ignored() -> None:
    """`async-nats` opens the certificate files itself, so bytes have nowhere to go.

    Writing them to a temporary file so the client could read them back would
    put a private key on a disk that never asked for one — so this raises where
    the mistake was written, before the tokio runtime is even asked for.
    """
    try:
        Nats(
            [SERVER],
            "config",
            "db.json",
            tls=TlsConfig().with_ca_certificate_pem(b"-----BEGIN CERTIFICATE-----"),
        )
    except ValueError as refusal:
        print(f"  {refusal}")

    print("  the same setting as a file is accepted, because that is the")
    print("  spelling every NATS deployment actually hands out:")
    print(
        "   ",
        Nats(
            [SERVER],
            "config",
            "db.json",
            tls=TlsConfig().with_ca_certificate_file("/etc/nats/ca.pem"),
        ),
    )


def main() -> None:
    print("a key in a JetStream KV bucket:")
    a_key_in_a_jetstream_bucket()
    print()

    print("the credential that is already a callable:")
    a_creds_file_is_read_again_every_time()
    print()

    print("the shapes NATS accepts:")
    every_credential_shape_has_a_python_spelling()
    print()

    print("what NATS cannot express, and refuses:")
    pem_bytes_are_refused_rather_than_ignored()
    print()

    print("naming an authority also turns TLS *on*: the store crate sets")
    print("`require_tls`, so a `nats://` URL that would have negotiated")
    print("plaintext fails rather than quietly connecting without it.")


if __name__ == "__main__":
    main()
