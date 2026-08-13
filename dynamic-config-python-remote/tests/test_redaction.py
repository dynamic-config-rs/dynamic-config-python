"""Credentials never appear in a diagnostic.

Not in an exception message, not in a `repr`, not in what `describe()`
records as provenance. This wheel adds four surfaces that could leak one —
the two stores' `repr` and `describe`, the `Auth` `repr`, and the message of
a failed fetch — and there is a test here for each.

The hard case is the one the store crates already learned: a store URL may
embed `user:password@host`, and the password may itself contain `@`. The rule
is shared with them rather than reimplemented, and this file is what says so
from the Python side.
"""

from __future__ import annotations

import pytest

from conftest import S3Log, VaultLog
from dynamic_config import AuthError, RemoteError
from dynamic_config_remote import (
    S3,
    Auth,
    Consul,
    ConsulAuth,
    Etcd,
    Firestore,
    FirestoreAuth,
    Git,
    Nats,
    NatsAuth,
    Redis,
    Vault,
)

#: A password nobody would choose, containing the character that broke the
#: first implementation of this rule in three crates.
NASTY = "p@ss@w@rd"


# ── repr ───────────────────────────────────────────────────────────────


def test_an_etcd_repr_never_shows_a_password_in_an_endpoint() -> None:
    store = Etcd([f"http://app:{NASTY}@etcd.internal:2379"], "myapp/db.json")

    printed = repr(store)

    assert NASTY not in printed, printed
    assert "p@ss" not in printed, printed
    # The half worth seeing survives: redaction that hides the host is
    # redaction nobody can debug through.
    assert "app:***@etcd.internal:2379" in printed, printed


def test_a_vault_repr_never_shows_a_password_in_the_address() -> None:
    store = Vault(
        f"https://app:{NASTY}@vault.internal:8200",
        "secret",
        "myapp/db",
        auth=Auth.token("hvs.hunter2"),
    )

    printed = repr(store)

    assert NASTY not in printed, printed
    assert "hunter2" not in printed, printed
    assert "app:***@vault.internal:8200" in printed, printed


def test_no_store_repr_shows_a_password_in_its_url() -> None:
    # One rule, seven URLs. The user name survives in every one of them,
    # because redaction that hides the half worth seeing is redaction nobody
    # can debug through.
    printed = " ".join(
        repr(store)
        for store in (
            Consul(f"http://app:{NASTY}@consul.internal:8500", "myapp/db.json"),
            Etcd([f"http://app:{NASTY}@etcd.internal:2379"], "myapp/db.json"),
            Git(f"https://app:{NASTY}@git.internal/acme/config.git", "db.json"),
            Firestore(
                "project",
                "config/db",
                auth=FirestoreAuth.emulator(),
                endpoint=f"http://app:{NASTY}@firestore.internal:8080",
            ),
            Nats([f"nats://app:{NASTY}@nats.internal:4222"], "config", "db.json"),
            Redis(f"redis://app:{NASTY}@redis.internal:6379", "myapp/db.json"),
            S3(
                "bucket",
                "prod/db.json",
                endpoint=f"http://app:{NASTY}@minio.internal:9000",
            ),
        )
    )

    assert NASTY not in printed, printed
    assert "p@ss" not in printed, printed
    assert printed.count("app:***@") == 7, printed


def test_a_lone_authority_is_a_secret_to_nats_and_a_user_name_to_redis() -> None:
    # The one thing the two stores disagree about, and the reason the shared
    # redaction takes it as an argument rather than being forked. NATS'
    # `nats://token@host` puts the credential *in* the authority; Redis'
    # `redis://user@host` puts a user name there and the password elsewhere.
    nats = repr(Nats(["nats://hunter2-token@nats.internal:4222"], "config", "db.json"))
    redis = repr(Redis("redis://app@redis.internal:6379", "myapp/db.json"))

    assert "hunter2" not in nats, nats
    assert "nats://***@nats.internal:4222" in nats, nats
    # The user name is not a secret and stays; what would be the password is
    # `***` because there is nothing there to show.
    assert "redis://app:***@redis.internal:6379" in redis, redis


def test_an_auth_repr_never_shows_the_secret_half() -> None:
    printed = " ".join(
        repr(auth)
        for auth in (
            Auth.token("hvs.hunter2-token"),
            Auth.app_role("role-id-public", "hunter2-secret-id"),
            Auth.userpass("admin", "hunter2-password"),
            Auth.ldap("admin", "hunter2-password"),
            Auth.jwt("hunter2-jwt"),
            Auth.kubernetes("myapp"),
            Auth.certificate(),
        )
    )

    assert "hunter2" not in printed, printed
    # And the non-secret halves stay, matching what the Rust crate's `Debug`
    # prints — the same split, so the two do not have to be learned twice.
    assert "role-id-public" in printed, printed
    assert "admin" in printed, printed
    assert "myapp" in printed, printed


def test_no_auth_repr_shows_its_credential() -> None:
    # The other three auth types, by the same rule as Vault's `Auth`: the
    # secret half is `***` and the half worth seeing — an auth method's name,
    # a bearer file's path, a user name, a metadata address — stays.
    printed = " ".join(
        repr(auth)
        for auth in (
            ConsulAuth.anonymous(),
            ConsulAuth.token("hunter2-acl-token"),
            ConsulAuth.kubernetes("kubernetes"),
            ConsulAuth.jwt("oidc", "hunter2-bearer"),
            NatsAuth.anonymous(),
            NatsAuth.token("hunter2-nats-token"),
            NatsAuth.user_and_password("app", "hunter2-password"),
            NatsAuth.nkey_seed("hunter2-seed"),
            NatsAuth.credentials("hunter2-creds-file-contents"),
            FirestoreAuth.emulator(),
            FirestoreAuth.access_token("hunter2-access-token"),
            FirestoreAuth.metadata_server(),
        )
    )

    assert "hunter2" not in printed, printed
    assert "oidc" in printed, printed
    assert "serviceaccount" in printed, printed
    assert "'app'" in printed, printed
    assert "metadata.google.internal" in printed, printed


def test_a_callable_credential_reprs_as_a_callable_rather_than_being_called() -> None:
    # Calling it to print it would run a network request from inside a
    # `repr`, which is a debugger's worst afternoon.
    called = []

    def token() -> str:
        called.append(1)
        return "hunter2"

    printed = repr(Auth.app_role(token, "secret"))

    assert not called, "a repr must not resolve a credential"
    assert "<callable>" in printed, printed


# ── describe(), which is what provenance records ───────────────────────


def test_describe_is_redacted_because_it_becomes_provenance() -> None:
    # `describe()` is asked once at install and rendered into `Origin` and
    # into every remote error the engine raises. A raw URL here would reach
    # further than any of the others.
    etcd = Etcd([f"http://app:{NASTY}@etcd.internal:2379"], "myapp/db.json")
    vault = Vault(
        f"https://app:{NASTY}@vault.internal:8200",
        "secret",
        "myapp/db",
        auth=Auth.token("t"),
    )
    consul = Consul(f"http://app:{NASTY}@consul.internal:8500", "myapp/db.json")
    redis = Redis(f"redis://app:{NASTY}@redis.internal:6379", "myapp/db.json")
    nats = Nats([f"nats://app:{NASTY}@nats.internal:4222"], "config", "db.json")

    for described in (
        etcd.describe(),
        vault.describe(),
        consul.describe(),
        redis.describe(),
        nats.describe(),
    ):
        assert NASTY not in described, described
        assert "***" in described, described


# ── The message of a failed fetch ──────────────────────────────────────


def test_a_failed_etcd_fetch_does_not_repeat_the_password() -> None:
    store = Etcd(
        [f"http://app:{NASTY}@127.0.0.1:1"],
        "myapp/db.json",
        user="app",
        password=NASTY,
    )

    with pytest.raises(RemoteError) as failure:
        store.fetch()

    assert NASTY not in str(failure.value), failure.value
    assert "p@ss" not in str(failure.value), failure.value


def test_a_failed_redis_fetch_repeats_neither_password() -> None:
    # Two of them, and they are different rules. The one in the URL goes by
    # the shared redaction; the one in the `password` argument is spliced into
    # a URL this wheel builds, so it can only go by value — which is the
    # belt the by-value scrubbing exists to be.
    store = Redis(
        f"redis://app:{NASTY}@127.0.0.1:1",
        "myapp/db.json",
        password="hunter2-argument",
        timeout=2.0,
    )

    with pytest.raises(RemoteError) as failure:
        store.fetch()

    assert NASTY not in str(failure.value), failure.value
    assert "p@ss" not in str(failure.value), failure.value
    assert "hunter2" not in str(failure.value), failure.value


def test_a_refused_s3_fetch_does_not_repeat_the_secret_key(s3: S3Log) -> None:
    # The AWS SDK renders a great deal of context into its errors, which is
    # exactly the class of client library the by-value rule exists for.
    store = S3(
        "bucket",
        "prod/db.json",
        region="us-east-1",
        endpoint=s3.address,
        access_key_id="refused",
        secret_access_key="hunter2-secret-key",
        session_token="hunter2-session-token",
    )

    with pytest.raises(AuthError) as refusal:
        store.fetch()

    assert "hunter2" not in str(refusal.value), refusal.value


def test_a_refused_vault_fetch_does_not_repeat_the_token(vault: VaultLog) -> None:
    # The scripted server refuses the token named "refused"; the message that
    # comes back names the store and the status and nothing else.
    store = Vault(vault.address, "secret", "myapp/db", auth=Auth.token("refused"))

    with pytest.raises(AuthError) as refusal:
        store.fetch()

    assert "refused" not in str(refusal.value), refusal.value
    # What is left is the sentence a person needs: which store, and what it
    # said. The Rust crate is careful never to render the request, so the
    # `X-Vault-Token` header cannot ride along in the first place.
    assert "403" in str(refusal.value), refusal.value
    assert "vault" in str(refusal.value), refusal.value


def test_a_resolved_credential_is_scrubbed_from_a_message_by_value(
    vault: VaultLog,
) -> None:
    # The belt to the URL rule's braces. The URL rule only reaches a
    # credential that is *in* a URL; a client library that quoted what it
    # sent would defeat it entirely, so every credential resolved for a call
    # is also removed from that call's message by value.
    #
    # Demonstrating it needs a credential the message would contain anyway,
    # and the store crates are careful enough that no real one does. So the
    # trigger is synthetic: a token whose *value* is the host the refusal
    # names. Every occurrence goes, whether it arrived through the URL rule
    # or through this one.
    token = "127.0.0.1"
    vault.refuse.add(token)
    store = Vault(vault.address, "secret", "myapp/db", auth=Auth.token(token))

    with pytest.raises((AuthError, RemoteError)) as failure:
        store.fetch()

    assert token not in str(failure.value), failure.value
    assert "***" in str(failure.value), failure.value


def test_the_document_itself_never_reaches_a_message(vault: VaultLog) -> None:
    # The engine's own rule, checked from this side: the configuration is
    # what the store returns, and a message quoting it is the leak.
    store = Vault(vault.address, "secret", "myapp/db", auth=Auth.token("plain"))
    text, _ = store.fetch()

    assert "host-for-plain" in text, "the document is what came back"
    assert "host-for-plain" not in repr(store), repr(store)
    assert "host-for-plain" not in store.describe(), store.describe()
