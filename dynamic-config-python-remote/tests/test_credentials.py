"""Credentials that change while the process is still running.

This is the file the item exists for. A configuration watcher outlives its
credentials — a Vault token has a TTL, an AppRole secret id is rewritten by a
sidecar, an etcd password is rotated — and a store holding the string it was
constructed with is a 403 nobody can fix without a restart.

The assertion that matters is not that a callable is *accepted*. It is that
the value it returned on the **second** call is the one that reached the
server, which is why these tests read the scripted Vault's log rather than
the store's state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from conftest import ConsulLog, FirestoreLog, S3Log, VaultLog
from dynamic_config import AuthError, DynamicConfig, Format
from dynamic_config_remote import (
    S3,
    Auth,
    Consul,
    ConsulAuth,
    Etcd,
    Firestore,
    FirestoreAuth,
    Nats,
    NatsAuth,
    Redis,
    Vault,
)


@dataclass
class Rotated:
    """The schema for the one test here that builds a configuration.

    Declared at module level, and used by exactly one test: a config type
    keys the engine's `static`s, so two tests sharing one would race.
    """

    host: str = "localhost"
    port: int = 5432


class Rotating:
    """A credential that is different every time it is asked."""

    def __init__(self, *values: str) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> str:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1

        return value


# ── The test that matters ──────────────────────────────────────────────


def test_a_token_callable_returning_a_new_token_is_the_one_the_store_uses(
    vault: VaultLog,
) -> None:
    token = Rotating("token-first", "token-second")
    store = Vault(vault.address, "secret", "myapp/db", auth=Auth.token(token))

    first, _ = store.fetch()
    second, _ = store.fetch()

    # What the server saw, which is the only place the claim can be checked.
    assert vault.tokens == ["token-first", "token-second"]
    # And what came back followed the credential rather than a cached client.
    assert json.loads(first)["db"]["host"] == "host-for-token-first"
    assert json.loads(second)["db"]["host"] == "host-for-token-second"
    assert token.calls == 2, "a credential is resolved once per fetch"


def test_a_secret_id_callable_that_moves_buys_a_new_token(vault: VaultLog) -> None:
    # AppRole is the stronger version of the same claim: the credential is
    # not sent with the read, it is exchanged for a token — so a rotated
    # secret id has to produce a *second login*, not merely a second header.
    secret_id = Rotating("secret-first", "secret-second")
    store = Vault(
        vault.address,
        "secret",
        "myapp/db",
        auth=Auth.app_role("role-id", secret_id),
    )

    store.fetch()
    store.fetch()

    assert [login["secret_id"] for login in vault.logins] == [
        "secret-first",
        "secret-second",
    ]
    assert vault.tokens == ["issued-for-secret-first", "issued-for-secret-second"]


def test_a_credential_that_has_not_moved_reuses_the_session(vault: VaultLog) -> None:
    # The other half of the bargain. Rebuilding on every fetch would throw
    # away the Rust crate's token cache and turn one login into one per
    # refresh — a secrets store is not a thing to do that to.
    store = Vault(
        vault.address,
        "secret",
        "myapp/db",
        auth=Auth.app_role("role-id", lambda: "steady"),
    )

    store.fetch()
    store.fetch()
    store.fetch()

    assert len(vault.logins) == 1, "one login, not three"
    assert vault.tokens == ["issued-for-steady"] * 3


def test_a_password_callable_is_resolved_on_every_userpass_login(
    vault: VaultLog,
) -> None:
    password = Rotating("pw-first", "pw-second")
    store = Vault(
        vault.address,
        "secret",
        "myapp/db",
        auth=Auth.userpass("admin", password),
    )

    store.fetch()
    store.fetch()

    assert [login["password"] for login in vault.logins] == ["pw-first", "pw-second"]


def test_an_etcd_password_callable_is_resolved_on_every_fetch() -> None:
    # etcd's credentials live in the connection, so the proof that a rotated
    # one is picked up is that it is asked for again — the connection is then
    # rebuilt with it, which the container test exercises end to end.
    password = Rotating("pw-first", "pw-second")
    store = Etcd(
        ["http://127.0.0.1:1"],
        "myapp/db.json",
        user="myapp",
        password=password,
    )

    for _ in range(2):
        with pytest.raises(Exception, match="etcd"):
            store.fetch()

    assert password.calls == 2


# ── Consul: the same claim, against a scripted agent ───────────────────


def test_a_consul_token_callable_returning_a_new_token_is_the_one_the_store_uses(
    consul: ConsulLog,
) -> None:
    token = Rotating("token-first", "token-second")
    store = Consul(consul.address, "myapp/db.json", auth=ConsulAuth.token(token))

    first, _ = store.fetch()
    second, _ = store.fetch()

    assert consul.tokens == ["token-first", "token-second"]
    assert json.loads(first)["db"]["host"] == "host-for-token-first"
    assert json.loads(second)["db"]["host"] == "host-for-token-second"
    assert token.calls == 2, "a credential is resolved once per fetch"


def test_a_consul_bearer_callable_that_moves_buys_a_new_acl_token(
    consul: ConsulLog,
) -> None:
    # The stronger version, as with Vault's AppRole: the bearer is not sent
    # with the read, it is exchanged at the auth method — so a rotated one has
    # to produce a *second login*, not merely a second header.
    bearer = Rotating("jwt-first", "jwt-second")
    store = Consul(consul.address, "myapp/db.json", auth=ConsulAuth.jwt("oidc", bearer))

    store.fetch()
    store.fetch()

    assert [login["BearerToken"] for login in consul.logins] == [
        "jwt-first",
        "jwt-second",
    ]
    assert consul.tokens == ["issued-for-jwt-first", "issued-for-jwt-second"]


def test_a_consul_bearer_that_has_not_moved_reuses_the_session(
    consul: ConsulLog,
) -> None:
    # The other half of the bargain. Consul issues a token with an expiry and
    # the Rust crate caches it; rebuilding per fetch would throw that away and
    # turn one login into one per refresh.
    store = Consul(
        consul.address,
        "myapp/db.json",
        auth=ConsulAuth.jwt("oidc", lambda: "steady"),
    )

    store.fetch()
    store.fetch()
    store.fetch()

    assert len(consul.logins) == 1, "one login, not three"
    assert consul.tokens == ["issued-for-steady"] * 3


def test_a_refused_consul_token_raises_AuthError(consul: ConsulLog) -> None:  # noqa: N802
    store = Consul(consul.address, "myapp/db.json", auth=ConsulAuth.token("refused"))

    with pytest.raises(AuthError):
        store.fetch()


# ── Firestore: an access token that cannot renew itself ────────────────


def test_a_firestore_token_callable_returning_a_new_token_is_the_one_the_store_uses(
    firestore: FirestoreLog,
) -> None:
    # The variant a callable exists for: an access token minted outside the
    # process expires in an hour and the Rust crate has nothing to replace it
    # with.
    token = Rotating("ya29.first", "ya29.second")
    store = Firestore(
        "project",
        "config/db",
        auth=FirestoreAuth.access_token(token),
        endpoint=firestore.address,
    )

    first, _ = store.fetch()
    second, _ = store.fetch()

    assert firestore.tokens == ["ya29.first", "ya29.second"]
    assert json.loads(first)["db"]["host"] == "host-for-ya29.first"
    assert json.loads(second)["db"]["host"] == "host-for-ya29.second"


def test_a_firestore_credential_that_has_not_moved_reuses_the_session(
    firestore: FirestoreLog,
) -> None:
    # The metadata server mints a token with a lifetime and the Rust crate
    # keeps it until it is nearly expired. A rebuilt client would mint one per
    # fetch — three trips to a service that rate-limits.
    store = Firestore(
        "project",
        "config/db",
        auth=FirestoreAuth.metadata_server().with_url(f"{firestore.address}/token"),
        endpoint=firestore.address,
    )

    store.fetch()
    store.fetch()
    store.fetch()

    assert firestore.mints == ["minted-0"], "one token, not three"
    assert firestore.tokens == ["minted-0"] * 3


def test_a_refused_firestore_token_raises_AuthError(  # noqa: N802
    firestore: FirestoreLog,
) -> None:
    # Firestore's refusal is a 401 where Consul's and Vault's are a 403; what
    # a caller backing off sees has to be the same either way.
    store = Firestore(
        "project",
        "config/db",
        auth=FirestoreAuth.access_token("refused"),
        endpoint=firestore.address,
    )

    with pytest.raises(AuthError):
        store.fetch()


# ── S3: the credential that is a trait rather than a string ────────────


def test_an_s3_key_callable_returning_a_new_key_is_the_one_that_signs(
    s3: S3Log,
) -> None:
    # SigV4 puts the access key id in the `Authorization` header of every
    # request, which is the only place S3 says which credential signed one —
    # so it is the only place this claim can be checked.
    access_key_id = Rotating("AKIDFIRST", "AKIDSECOND")
    store = S3(
        "bucket",
        "prod/db.json",
        region="us-east-1",
        endpoint=s3.address,
        access_key_id=access_key_id,
        secret_access_key="secret",
    )

    first, _ = store.fetch()
    second, _ = store.fetch()

    assert s3.signers == ["AKIDFIRST", "AKIDSECOND"]
    assert json.loads(first)["db"]["host"] == "host-for-AKIDFIRST"
    assert json.loads(second)["db"]["host"] == "host-for-AKIDSECOND"


def test_an_s3_credential_that_moves_does_not_rebuild_the_client(s3: S3Log) -> None:
    # The other six stores rebuild a client when their credential moves,
    # because the credential is inside it. S3's is not: the provider is asked
    # per request, so a rotation is a mutex write and the SDK client — its
    # connection pool, its resolved endpoint — is built exactly once.
    #
    # `clients_built` exists for this assertion and for no other reason: S3
    # has no login and no reconnection to count, so the client is the only
    # observable there is.
    store = S3(
        "bucket",
        "prod/db.json",
        region="us-east-1",
        endpoint=s3.address,
        access_key_id=Rotating("AKIDFIRST", "AKIDSECOND"),
        secret_access_key="secret",
    )

    store.fetch()
    store.fetch()
    store.fetch()

    assert s3.signers == ["AKIDFIRST", "AKIDSECOND", "AKIDSECOND"]
    assert store._store.clients_built() == 1, "one client, whatever the key does"


def test_a_refused_s3_key_raises_AuthError(s3: S3Log) -> None:  # noqa: N802
    # Sorted on the error *code* rather than on the 403 that carries it: a
    # clock too far out of step arrives as a 403 too and is not an auth
    # failure, because that one does come right.
    store = S3(
        "bucket",
        "prod/db.json",
        region="us-east-1",
        endpoint=s3.address,
        access_key_id="refused",
        secret_access_key="secret",
    )

    with pytest.raises(AuthError):
        store.fetch()


# ── Redis and NATS: resolved per fetch, proven on the wire elsewhere ────


def test_a_redis_password_callable_is_resolved_on_every_fetch() -> None:
    # Redis' credential is in the connection string, so the proof that a
    # rotated one is picked up is that it is asked for again — the client is
    # then rebuilt with it, which `test_containers.py` exercises against a
    # real server that really refuses the old one.
    password = Rotating("pw-first", "pw-second")
    store = Redis("redis://127.0.0.1:1", "myapp/db.json", password=password)

    for _ in range(2):
        with pytest.raises(Exception, match="redis"):
            store.fetch()

    assert password.calls == 2


def test_a_nats_token_callable_is_resolved_on_every_fetch() -> None:
    token = Rotating("token-first", "token-second")
    store = Nats(
        ["nats://127.0.0.1:1"],
        "config",
        "db.json",
        auth=NatsAuth.token(token),
        timeout=5.0,
    )

    for _ in range(2):
        with pytest.raises(Exception, match="nats"):
            store.fetch()

    assert token.calls == 2


# ── What a callable may and may not answer ─────────────────────────────


def test_a_callable_returning_something_other_than_a_str_says_which_argument(
    vault: VaultLog,
) -> None:
    store = Vault(
        vault.address,
        "secret",
        "myapp/db",
        auth=Auth.token(lambda: 42),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(TypeError, match="token's first argument"):
        store.fetch()


def test_a_raising_callable_is_reported_and_never_fatal(vault: VaultLog) -> None:
    def unavailable() -> str:
        raise RuntimeError("the token file is not there yet")

    store = Vault(vault.address, "secret", "myapp/db", auth=Auth.token(unavailable))

    with pytest.raises(RuntimeError, match="not there yet"):
        store.fetch()

    # The store is not poisoned by it: a later fetch with a working
    # credential works.
    working = Vault(vault.address, "secret", "myapp/db", auth=Auth.token("fine"))
    assert json.loads(working.fetch()[0])["db"]["host"] == "host-for-fine"


def test_a_plain_string_credential_still_works(vault: VaultLog) -> None:
    store = Vault(vault.address, "secret", "myapp/db", auth=Auth.token("plain"))

    text, fmt = store.fetch()

    assert fmt is Format.JSON
    assert json.loads(text)["db"] == {"host": "host-for-plain", "port": 5432}
    assert vault.tokens == ["plain"]


# ── The distinction a caller backs off on ──────────────────────────────


def test_a_refused_credential_raises_AuthError_rather_than_RemoteError(  # noqa: N802
    vault: VaultLog,
) -> None:
    store = Vault(vault.address, "secret", "myapp/db", auth=Auth.token("refused"))

    with pytest.raises(AuthError):
        store.fetch()


# ── Through a configuration, which is the point of all of it ───────────


def test_a_rotated_credential_survives_a_refresh_through_a_configuration(
    vault: VaultLog,
) -> None:
    token = Rotating("token-first", "token-second")
    config = DynamicConfig(Rotated, key="db").remote(
        Vault(vault.address, "secret", "myapp/db", auth=Auth.token(token))
    )

    config.refresh_remote()
    config.init()
    assert config.current().host == "host-for-token-first"

    config.refresh_remote()
    config.reload()
    assert config.current().host == "host-for-token-second"
