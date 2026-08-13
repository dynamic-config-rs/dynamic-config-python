r"""Redis: the credential is in the URL, which is why it is not in the URL.

Redis has no login and no token. A client authenticates by sending `AUTH` from
whatever its connection string carried, and `redis::Client` is built from that
string — so a rotated password is not a header this store can change. It is a
different URL, and therefore a different client.

That is the whole reason `user` and `password` are arguments here rather than
something you write into `url` yourself: **a callable cannot rotate a substring
of a string somebody passed at construction.** The two are spliced into the
authority on every fetch, percent-encoded, and a pair that has not moved reuses
the client and its open connection.

Percent-encoding is not a detail. `redis-rs` percent-*decodes* the authority it
parses, so a password containing `@`, `:` or a space would arrive truncated —
or, worse, would move the host — if it went in raw.

A server to point it at:

    docker run --rm -d -p 6379:6379 redis:7-alpine \\
        redis-server --requirepass hunter2

    redis-cli -a hunter2 set myapp/db.json \\
        '{"db": {"host": "redis-db", "port": 6001}}'

Then `python examples/04_redis.py`. With nothing listening it says so and
carries on.
"""

import os
from dataclasses import dataclass

from dynamic_config import DynamicConfig, DynamicConfigError, RemoteError
from dynamic_config_remote import Redis, TlsConfig

URL = os.environ.get("REDIS", "redis://127.0.0.1:6379")


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432


def a_password_that_is_an_argument_because_it_rotates() -> None:
    """`requirepass` implies the default user, so a password alone is a pair."""
    config = DynamicConfig(Database, key="db").remote(
        Redis(
            URL,
            "myapp/db.json",
            # No `user`: `requirepass` predates ACLs and implies `default`, so
            # the pair is built from whichever halves are there.
            password=lambda: os.environ.get("REDIS_PASSWORD", "hunter2"),
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
        print(f"  no Redis to read: {unreachable.__cause__ or unreachable}")


def a_password_is_not_a_url_and_is_encoded_like_one() -> None:
    """A password nobody would choose, which is exactly why it is here.

    Everything outside RFC 3986's unreserved set is percent-encoded on the way
    into the authority. And whatever it contains, it does not come back out: a
    `repr` shows the host and the user name and never the secret, splitting on
    the **last** `@` because a password may itself contain one.
    """
    awkward = "p@ss:w rd"
    store = Redis(f"redis://app:{awkward}@127.0.0.1:6379", "myapp/db.json")

    print("   ", store)
    print("  the password above never appears:", awkward not in repr(store))


def tls_needs_a_rediss_url_and_says_so_when_it_does_not_have_one() -> None:
    """The one refusal that arrives at the first fetch rather than at construction.

    The URL is parsed where it is used — by the client, as it is built — rather
    than twice by two implementations that could disagree. So a `redis://` URL
    with TLS material on it is refused, but a refresh later than the others.
    """
    store = Redis(
        "redis://127.0.0.1:1",
        "myapp/db.json",
        tls=TlsConfig().with_ca_certificate_file("/etc/ssl/private-ca.pem"),
        timeout=5.0,
    )

    try:
        store.fetch()
    except RemoteError as refusal:
        print(f"  {refusal}")


def main() -> None:
    print("a key whose value is the whole document:")
    a_password_that_is_an_argument_because_it_rotates()
    print()

    print("a password that is not a URL:")
    a_password_is_not_a_url_and_is_encoded_like_one()
    print()

    print("TLS, and the URL it has to agree with:")
    tls_needs_a_rediss_url_and_says_so_when_it_does_not_have_one()
    print()

    print("Redis is blocking — `redis-rs` over a plain socket — so a program")
    print("that reads only this store starts no worker threads at all.")


if __name__ == "__main__":
    main()
