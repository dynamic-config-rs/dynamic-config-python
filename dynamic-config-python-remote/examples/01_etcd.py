r"""etcd: a credential that lives in the connection, and the runtime that drives it.

etcd speaks gRPC, so its client is **async** — and it is the store that makes
the wheel's one tokio runtime appear. Watch `runtime_started()` below: importing
the package starts nothing, and constructing this store starts two worker
threads, at construction rather than at the first fetch because construction is
the moment a user can see it happen.

Its credential is the other characteristic thing. etcd authenticates the
*connection*, not the request, so a rotated password is not a header this store
can change — it is a different client. `user` and `password` are therefore
separate arguments rather than something written into the endpoint: a callable
can rotate an argument and cannot rotate a substring of a string somebody passed
at construction.

A server to point it at:

    docker run --rm -d -p 2379:2379 quay.io/coreos/etcd:v3.5.17 etcd \\
        --advertise-client-urls=http://0.0.0.0:2379 \\
        --listen-client-urls=http://0.0.0.0:2379

    etcdctl put myapp/db.json '{"db": {"host": "etcd-db", "port": 6000}}'

Then `python examples/01_etcd.py`. With nothing listening it says so and
carries on, which is why it is not in the base package's numbered examples,
where every one has to run in the gate with no servers at all.
"""

import os
from dataclasses import dataclass

from dynamic_config import DynamicConfig, DynamicConfigError
from dynamic_config_remote import Etcd, runtime_started

ENDPOINT = os.environ.get("ETCD", "http://127.0.0.1:2379")


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432


def the_runtime_is_the_visible_difference() -> None:
    """One of the three stores here that need an executor at all."""
    print("  before any store is built:", runtime_started())

    Etcd([ENDPOINT], "myapp/db.json")

    print("  after building an etcd store:", runtime_started())
    print("  two worker threads, shared by every async store in the process")


def a_password_that_rotates_under_a_running_process() -> None:
    """The credential is resolved on every fetch, and it is a reconnection.

    A watcher outlives the password it started with. Here the callable reads
    the environment; yours might read a file a sidecar rewrites, or ask a
    secrets manager. A value that has *not* moved reuses the connection, which
    is the difference between one connection and one per refresh.
    """
    reads = []

    def password() -> str:
        reads.append(len(reads))

        return os.environ.get("ETCD_PASSWORD", "")

    config = DynamicConfig(Database, key="db").remote(
        Etcd(
            [ENDPOINT],
            "myapp/db.json",
            # Both or neither: one without the other would connect
            # unauthenticated, which looks like a permissions problem in etcd's
            # log and like nothing at all here.
            user=os.environ.get("ETCD_USER") or None,
            password=password if os.environ.get("ETCD_USER") else None,
        )
    )

    try:
        config.refresh_remote()
        config.init()

        print("  read:", config.current())
        print("  provenance:", config.source_of("host"))
    except DynamicConfigError as unreachable:
        # The engine deliberately does not repeat a store's own message — a
        # store's exception routinely carries the URL it called. The detail is
        # on `__cause__`, which is this wheel's, and which has already had
        # every credential taken out of it.
        print(f"  no etcd to read: {unreachable.__cause__ or unreachable}")

    if os.environ.get("ETCD_USER"):
        print(f"  the password callable was asked {len(reads)} time(s)")


def half_a_credential_is_refused_where_it_was_written() -> None:
    """A user with no password would connect anonymously, so it does not."""
    try:
        Etcd([ENDPOINT], "myapp/db.json", user="myapp")
    except ValueError as refusal:
        print(f"  {refusal}")


def a_key_says_what_format_it_is() -> None:
    """`myapp/db.json` is JSON; a key that names no format has to be told."""
    try:
        Etcd([ENDPOINT], "myapp/db")
    except ValueError as refusal:
        print(f"  {refusal}")

    print(
        "  and naming one is all it takes:", Etcd([ENDPOINT], "myapp/db", format="json")
    )


def main() -> None:
    print("the tokio runtime, which etcd is the reason for:")
    the_runtime_is_the_visible_difference()
    print()

    print("a credential in the connection:")
    a_password_that_rotates_under_a_running_process()
    print()

    print("what is refused at the line it was written on:")
    half_a_credential_is_refused_where_it_was_written()
    a_key_says_what_format_it_is()
    print()

    print("etcd over TLS trusts the authority you name and no other — the")
    print("wheel does not enable etcd's `tls-roots`, which would resolve to a")
    print(
        "call the store crate never makes. See 09_private_ca_and_client_certificate.py."
    )


if __name__ == "__main__":
    main()
