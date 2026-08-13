"""Against real servers, which is the only place the protocol is real.

The scripted Vault in `conftest.py` proves the credential *rotation*, because
only a server whose request log can be read can prove it. These prove the
other half: that the wire format, the auth handshake and the document these
wheels agree on are the ones a real etcd and a real Vault speak.

Skipped without Docker, and never silently: a run with no Docker says so.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Optional

import pytest

from conftest import container, stop
from dynamic_config import AuthError, DynamicConfig
from dynamic_config_remote import (
    S3,
    Auth,
    Consul,
    ConsulAuth,
    Etcd,
    Nats,
    NatsAuth,
    Redis,
    Vault,
)

ETCD = "quay.io/coreos/etcd:v3.5.17"
VAULT = "hashicorp/vault:1.17"
CONSUL = "hashicorp/consul:1.20"
REDIS = "redis:7-alpine"
NATS = "nats:2.10"
#: The NATS CLI, which the server image does not carry: creating a JetStream
#: bucket is a client operation and there is no client in `nats:2.10`.
NATS_BOX = "natsio/nats-box:latest"
MINIO = "minio/minio:RELEASE.2025-02-28T09-55-16Z"

DOCUMENT = json.dumps({"db": {"host": "from-the-container", "port": 6000}})


@dataclass
class FromEtcd:
    """The schema for the etcd end-to-end test, used by exactly one test."""

    host: str = "localhost"
    port: int = 5432


@dataclass
class FromVault:
    """The schema for the Vault end-to-end test, used by exactly one test."""

    host: str = "localhost"
    port: int = 5432


@dataclass
class FromConsul:
    """The schema for the Consul end-to-end test, used by exactly one test."""

    host: str = "localhost"
    port: int = 5432


@dataclass
class FromRedis:
    """The schema for the Redis end-to-end test, used by exactly one test."""

    host: str = "localhost"
    port: int = 5432


@dataclass
class FromNats:
    """The schema for the NATS end-to-end test, used by exactly one test."""

    host: str = "localhost"
    port: int = 5432


@dataclass
class FromS3:
    """The schema for the S3 end-to-end test, used by exactly one test."""

    host: str = "localhost"
    port: int = 5432


def run_in(
    identifier: str, *command: str, feed: Optional[str] = None, check: bool = True
) -> str:
    """Runs a command inside a container, optionally feeding it stdin."""
    finished = subprocess.run(
        ["docker", "exec", *(["-i"] if feed is not None else []), identifier, *command],
        input=feed,
        capture_output=True,
        check=check,
        text=True,
        timeout=120,
    )

    return finished.stdout


def wait_for(identifier: str, *command: str, seconds: int = 60) -> None:
    """Waits until a command inside the container succeeds."""
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        finished = subprocess.run(
            ["docker", "exec", identifier, *command],
            capture_output=True,
            check=False,
            timeout=30,
        )

        if finished.returncode == 0:
            return

        time.sleep(0.5)

    raise AssertionError(f"the container never became ready: {' '.join(command)}")


@pytest.fixture
def etcd(docker: bool) -> Iterator[str]:
    """A running etcd with the configuration document in it."""
    if not docker:
        pytest.skip("Docker is not available; the container tests cannot run")

    identifier, port = container(
        ETCD,
        "etcd",
        "--advertise-client-urls=http://0.0.0.0:2379",
        "--listen-client-urls=http://0.0.0.0:2379",
        port=2379,
    )

    try:
        wait_for(identifier, "etcdctl", "endpoint", "health")
        run_in(identifier, "etcdctl", "put", "myapp/db.json", DOCUMENT)

        yield f"http://127.0.0.1:{port}"
    finally:
        stop(identifier)


@pytest.fixture
def vault_container(docker: bool) -> Iterator[str]:
    """A running Vault in dev mode, with the secret in it."""
    if not docker:
        pytest.skip("Docker is not available; the container tests cannot run")

    # A known root token, because a dev-mode Vault prints a random one and
    # scraping it out of the log is a race with the log being written.
    identifier, port = container(VAULT, port=8200, env=["VAULT_DEV_ROOT_TOKEN_ID=root"])

    try:
        environment = ["env", "VAULT_ADDR=http://127.0.0.1:8200", "VAULT_TOKEN=root"]
        wait_for(identifier, *environment, "vault", "status")
        # Fed as JSON rather than as `key=value` pairs: the CLI's pair form
        # stores every value as a string, and a dataclass schema validates
        # structurally without coercing — so `port=6000` would arrive as
        # `"6000"` and be refused. Vault itself holds JSON types perfectly
        # well; only the CLI shorthand flattens them.
        run_in(
            identifier,
            *environment,
            "vault",
            "kv",
            "put",
            "secret/myapp/db",
            "-",
            feed=json.dumps({"host": "from-the-container", "port": 6000}),
        )

        yield f"http://127.0.0.1:{port}"
    finally:
        stop(identifier)


@pytest.fixture
def consul_container(docker: bool) -> Iterator[str]:
    """A running Consul with ACLs on, the document in it, and a known token."""
    if not docker:
        pytest.skip("Docker is not available; the container tests cannot run")

    # ACLs on and `default_policy = "deny"`, because a Consul that answers
    # every reader is a Consul the credential tests cannot prove anything
    # against.
    identifier, port = container(
        CONSUL,
        "agent",
        "-dev",
        "-client=0.0.0.0",
        '-hcl=acl { enabled = true default_policy = "deny" '
        'tokens { initial_management = "root-token" } }',
        port=8500,
    )

    try:
        management = ["-token=root-token"]
        wait_for(identifier, "consul", "kv", "put", *management, "ready", "yes")
        run_in(
            identifier, "consul", "kv", "put", *management, "myapp/db.json", DOCUMENT
        )

        yield f"http://127.0.0.1:{port}"
    finally:
        stop(identifier)


@pytest.fixture
def redis_container(docker: bool) -> Iterator[str]:
    """A running Redis with a password, and the document in it."""
    if not docker:
        pytest.skip("Docker is not available; the container tests cannot run")

    identifier, port = container(
        REDIS, "redis-server", "--requirepass", "right-password", port=6379
    )

    try:
        client = ["redis-cli", "-a", "right-password", "--no-auth-warning"]
        wait_for(identifier, *client, "ping")
        run_in(identifier, *client, "set", "myapp/db.json", DOCUMENT)

        yield f"redis://127.0.0.1:{port}"
    finally:
        stop(identifier)


@pytest.fixture
def nats_container(docker: bool) -> Iterator[str]:
    """A running NATS with JetStream, a token, a bucket and the document."""
    if not docker:
        pytest.skip("Docker is not available; the container tests cannot run")

    identifier, port = container(
        NATS, "-js", "--auth", "right-token", "-p", "4222", port=4222
    )
    server = f"nats://right-token@127.0.0.1:{port}"

    try:
        # The bucket is a client operation and the server image has no client,
        # so the CLI comes from its own image on the host's network.
        wait_for_box(server, "kv", "add", "config", "--history=1")
        in_box(server, "kv", "put", "config", "db.json", DOCUMENT)

        yield f"nats://127.0.0.1:{port}"
    finally:
        stop(identifier)


def in_box(server: str, *command: str, check: bool = True) -> str:
    """Runs the NATS CLI against `server`, from the `nats-box` image."""
    finished = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            NATS_BOX,
            "nats",
            "--server",
            server,
            *command,
        ],
        capture_output=True,
        check=check,
        text=True,
        timeout=300,
    )

    return finished.stdout


def wait_for_box(server: str, *command: str, seconds: int = 60) -> None:
    """Waits until a NATS CLI command succeeds, for the server's first boot."""
    deadline = time.monotonic() + seconds
    last = ""

    while time.monotonic() < deadline:
        try:
            in_box(server, *command)
        except subprocess.CalledProcessError as failed:
            last = failed.stderr
            time.sleep(1)
        else:
            return

    raise AssertionError(f"NATS never became ready: {last}")


@pytest.fixture
def minio_container(docker: bool) -> Iterator[str]:
    """A running MinIO with a bucket and the document in it."""
    if not docker:
        pytest.skip("Docker is not available; the container tests cannot run")

    identifier, port = container(
        MINIO,
        "server",
        "/data",
        port=9000,
        env=["MINIO_ROOT_USER=right-key", "MINIO_ROOT_PASSWORD=right-secret"],
    )

    try:
        # `mc` ships inside the MinIO image, which is what keeps this to one
        # container: creating a bucket is an S3 write, and this store only
        # reads.
        alias = ["mc", "alias", "set", "local", "http://127.0.0.1:9000"]
        wait_for(identifier, *alias, "right-key", "right-secret")
        run_in(identifier, "mc", "mb", "local/myapp-config")
        run_in(
            identifier, "mc", "pipe", "local/myapp-config/prod/db.json", feed=DOCUMENT
        )

        yield f"http://127.0.0.1:{port}"
    finally:
        stop(identifier)


# ── etcd ───────────────────────────────────────────────────────────────


def test_a_key_in_a_real_etcd_reaches_a_model(etcd: str) -> None:
    config = DynamicConfig(FromEtcd, key="db").remote(Etcd([etcd], "myapp/db.json"))

    config.refresh_remote()
    config.init()

    assert config.current().host == "from-the-container"
    assert config.current().port == 6000


def test_a_rotated_etcd_password_is_picked_up_without_a_restart(etcd: str) -> None:
    """The claim, end to end, against a server that really refuses.

    A watcher outlives the password it started with. What this asserts is
    that the *same store object* keeps working across a rotation — and that
    a store pinned to the old password does not, so the first half is not
    passing for the wrong reason.
    """
    identifier = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"publish={etcd.rsplit(':', 1)[1]}"],
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    ).stdout.strip()

    run_in(identifier, "etcdctl", "user", "add", "root:rootpw")
    run_in(identifier, "etcdctl", "user", "add", "app:pw-first")
    run_in(identifier, "etcdctl", "user", "grant-role", "app", "root")
    run_in(identifier, "etcdctl", "auth", "enable")

    passwords: list[str] = ["pw-first"]
    rotating = Etcd(
        [etcd],
        "myapp/db.json",
        user="app",
        password=lambda: passwords[-1],
    )
    pinned = Etcd([etcd], "myapp/db.json", user="app", password="pw-first")

    assert json.loads(rotating.fetch()[0])["db"]["host"] == "from-the-container"
    assert json.loads(pinned.fetch()[0])["db"]["host"] == "from-the-container"

    # `--interactive=false` reads the new password from stdin; etcdctl has no
    # form that takes it as an argument, which is a kindness to shell history.
    run_in(
        identifier,
        "etcdctl",
        "--user",
        "root:rootpw",
        "user",
        "passwd",
        "--interactive=false",
        "app",
        feed="pw-second\n",
    )
    passwords.append("pw-second")

    # The store whose credential moved keeps working...
    assert json.loads(rotating.fetch()[0])["db"]["host"] == "from-the-container"

    # ...and the one holding the old password does not, which is what makes
    # the assertion above mean something.
    with pytest.raises(AuthError):
        pinned.fetch()


# ── Vault ──────────────────────────────────────────────────────────────


def test_a_secret_in_a_real_vault_reaches_a_model(vault_container: str) -> None:
    config = DynamicConfig(FromVault, key="db").remote(
        Vault(vault_container, "secret", "myapp/db", auth=Auth.token("root"))
    )

    config.refresh_remote()
    config.init()

    assert config.current().host == "from-the-container"
    # Vault's KV stores strings; the engine coerces on the way into the model,
    # which is the same thing it does for an environment variable.
    assert config.current().port == 6000


def test_a_real_vault_refusing_a_token_raises_AuthError(  # noqa: N802
    vault_container: str,
) -> None:
    store = Vault(
        vault_container, "secret", "myapp/db", auth=Auth.token("not-the-root-token")
    )

    with pytest.raises(AuthError) as refusal:
        store.fetch()

    assert "not-the-root-token" not in str(refusal.value), refusal.value


# ── Consul ─────────────────────────────────────────────────────────────


def test_a_key_in_a_real_consul_reaches_a_model(consul_container: str) -> None:
    config = DynamicConfig(FromConsul, key="db").remote(
        Consul(
            consul_container,
            "myapp/db.json",
            auth=ConsulAuth.token("root-token"),
        )
    )

    config.refresh_remote()
    config.init()

    assert config.current().host == "from-the-container"
    assert config.current().port == 6000


def test_a_rotated_consul_token_is_picked_up_without_a_restart(
    consul_container: str,
) -> None:
    """The claim against a server that really refuses.

    The scripted agent proves which token was *sent*; this proves that the
    second one is the one a real Consul accepts — the first fetch is refused
    by an agent with `default_policy = "deny"`, and the same store object
    succeeds on the next one with nothing rebuilt by hand.
    """
    tokens = ["wrong-token"]
    store = Consul(
        consul_container, "myapp/db.json", auth=ConsulAuth.token(lambda: tokens[-1])
    )

    with pytest.raises(AuthError):
        store.fetch()

    tokens.append("root-token")

    assert json.loads(store.fetch()[0])["db"]["host"] == "from-the-container"


# ── Redis ──────────────────────────────────────────────────────────────


def test_a_key_in_a_real_redis_reaches_a_model(redis_container: str) -> None:
    config = DynamicConfig(FromRedis, key="db").remote(
        Redis(redis_container, "myapp/db.json", password="right-password")
    )

    config.refresh_remote()
    config.init()

    assert config.current().host == "from-the-container"
    assert config.current().port == 6000


def test_a_rotated_redis_password_is_picked_up_without_a_restart(
    redis_container: str,
) -> None:
    passwords = ["wrong-password"]
    store = Redis(
        redis_container, "myapp/db.json", password=lambda: passwords[-1], timeout=5.0
    )

    with pytest.raises(AuthError):
        store.fetch()

    passwords.append("right-password")

    assert json.loads(store.fetch()[0])["db"]["host"] == "from-the-container"


def test_a_redis_password_that_has_not_moved_reuses_the_connection(
    redis_container: str,
) -> None:
    """The other half of the bargain, counted on the server.

    Redis has no login to count, but it has connections: rebuilding the client
    per fetch would open one per refresh, and a configuration watcher on a
    thirty-second timer would be a connection storm nobody attributed to
    configuration.
    """
    identifier = subprocess.run(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            f"publish={redis_container.rsplit(':', 1)[1]}",
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    ).stdout.strip()

    def connections() -> int:
        stats = run_in(
            identifier,
            "redis-cli",
            "-a",
            "right-password",
            "--no-auth-warning",
            "info",
            "stats",
        )
        line = next(
            row
            for row in stats.splitlines()
            if row.startswith("total_connections_received")
        )

        return int(line.split(":", 1)[1])

    def opened_by(store: Redis) -> int:
        """How many connections three fetches through `store` opened.

        Each reading is itself a `redis-cli`, and therefore a connection: the
        second one counts a connection that is not the store's, which is what
        the subtracted 1 is.
        """
        before = connections()
        store.fetch()
        store.fetch()
        store.fetch()

        return connections() - before - 1

    steady = Redis(redis_container, "myapp/db.json", password=lambda: "right-password")

    assert opened_by(steady) == 1, "one connection, not three"

    # And the contrast that makes the number mean something: a credential that
    # really moves does rebuild, so the same three fetches cost three. Both
    # halves authenticate — `default` is the user `requirepass` implies — so
    # what differs is the credential and nothing else.
    users = iter(["", "default", ""])
    moving = Redis(
        redis_container,
        "myapp/db.json",
        user=lambda: next(users),
        password="right-password",
    )

    assert opened_by(moving) == 3, "a moved credential reconnects"


# ── NATS ───────────────────────────────────────────────────────────────


def test_a_key_in_a_real_nats_reaches_a_model(nats_container: str) -> None:
    config = DynamicConfig(FromNats, key="db").remote(
        Nats(
            [nats_container],
            "config",
            "db.json",
            auth=NatsAuth.token("right-token"),
        )
    )

    config.refresh_remote()
    config.init()

    assert config.current().host == "from-the-container"
    assert config.current().port == 6000


def test_a_rotated_nats_token_is_picked_up_without_a_restart(
    nats_container: str,
) -> None:
    # NATS authenticates on connect, so this is the strongest form of the
    # claim: the second token cannot be "the one that was used" unless the
    # connection was made again with it.
    tokens = ["wrong-token"]
    store = Nats(
        [nats_container],
        "config",
        "db.json",
        auth=NatsAuth.token(lambda: tokens[-1]),
        timeout=5.0,
    )

    with pytest.raises(AuthError):
        store.fetch()

    tokens.append("right-token")

    assert json.loads(store.fetch()[0])["db"]["host"] == "from-the-container"


# ── S3, against MinIO ──────────────────────────────────────────────────


def test_an_object_in_a_real_s3_reaches_a_model(minio_container: str) -> None:
    config = DynamicConfig(FromS3, key="db").remote(
        S3(
            "myapp-config",
            "prod/db.json",
            region="us-east-1",
            endpoint=minio_container,
            access_key_id="right-key",
            secret_access_key="right-secret",
        )
    )

    config.refresh_remote()
    config.init()

    assert config.current().host == "from-the-container"
    assert config.current().port == 6000


def test_a_rotated_s3_key_is_picked_up_without_a_restart(
    minio_container: str,
) -> None:
    # The scripted server proves which key *signed* the request; this proves
    # that a real S3 implementation accepts the second one — and, because the
    # SDK client is never rebuilt for a credential, that the provider really
    # is asked again per request rather than at construction.
    keys = ["wrong-key"]
    store = S3(
        "myapp-config",
        "prod/db.json",
        region="us-east-1",
        endpoint=minio_container,
        access_key_id=lambda: keys[-1],
        secret_access_key="right-secret",
        timeout=5.0,
    )

    with pytest.raises(AuthError):
        store.fetch()

    keys.append("right-key")

    assert json.loads(store.fetch()[0])["db"]["host"] == "from-the-container"
