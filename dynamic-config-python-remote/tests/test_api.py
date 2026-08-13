"""The surface: what the two constructors accept, and what they refuse.

A store that cannot authenticate is worse than an absent one, so the
refusals here are all of the *construction* kind — a `ValueError` where the
mistake was written, rather than a remote error on the first refresh naming a
Rust builder method a Python user cannot call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import dynamic_config
import dynamic_config.remote as through_the_base_package
import dynamic_config_remote
from dynamic_config import DynamicConfig, RemoteSource
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

#: Every store, for the properties all eight share. Built with arguments that
#: reach nothing: none of these constructors opens a socket, which is itself
#: one of the properties. `Git` does make a working directory, which is a
#: temporary one removed with the store and is not a socket.
STORES = (
    Consul("http://consul:8500", "myapp/db.json"),
    Etcd(["http://etcd:2379"], "myapp/db.json"),
    Firestore("project", "config/db", auth=FirestoreAuth.emulator()),
    Git("https://github.com/acme/config.git", "myapp/db.json"),
    Nats(["nats://nats:4222"], "config", "db.json"),
    Redis("redis://redis:6379", "myapp/db.json"),
    S3("bucket", "prod/db.json", region="us-east-1"),
    Vault("https://vault:8200", "secret", "myapp/db", auth=Auth.token("t")),
)


@dataclass
class Installed:
    """The schema for the one test here that builds a configuration."""

    host: str = "localhost"
    port: int = 5432


# ── The two names are one module ───────────────────────────────────────


def test_the_dotted_name_and_the_installed_name_are_the_same_objects() -> None:
    for name in through_the_base_package.__all__:
        assert getattr(through_the_base_package, name) is getattr(
            dynamic_config_remote, name
        ), name


def test_the_wheel_reports_the_engine_it_was_built_against() -> None:
    # A remote wheel built against a different engine than the base wheel is
    # the one mismatch that could produce a document the base cannot parse.
    assert dynamic_config_remote.__engine_version__ == dynamic_config.__engine_version__


def test_every_store_is_a_remote_source_the_base_wheel_accepts() -> None:
    # Not merely duck-typed: the base wheel's `remote()` is annotated with
    # `RemoteSource`, so a store that only *looked* like one would fail
    # pyright for every user who has both wheels.
    for store in STORES:
        assert isinstance(store, RemoteSource), store


def test_every_rust_store_crate_has_a_python_store() -> None:
    # The mirror is the contract, and this is the list. A crate with no
    # spelling here is a store a Python deployment cannot use, and nothing
    # else notices when one is added to the workspace.
    assert {type(store).__name__ for store in STORES} == {
        "Consul",
        "Etcd",
        "Firestore",
        "Git",
        "Nats",
        "Redis",
        "S3",
        "Vault",
    }


def test_a_store_installs_on_a_configuration_and_reports_its_description() -> None:
    config = DynamicConfig(Installed, key="db").remote(
        Etcd(["http://etcd.internal:2379"], "myapp/db.json")
    )

    assert (
        config.remote_description == "etcd http://etcd.internal:2379 key myapp/db.json"
    )


# ── etcd's arguments ───────────────────────────────────────────────────


def test_etcd_needs_at_least_one_endpoint() -> None:
    with pytest.raises(ValueError, match="at least one endpoint"):
        Etcd([], "myapp/db.json")


def test_etcd_refuses_a_user_without_a_password() -> None:
    # One without the other would connect unauthenticated, which is the
    # failure that looks like a permissions problem in etcd's log and like
    # nothing at all here.
    with pytest.raises(ValueError, match="both user and password"):
        Etcd(["http://etcd:2379"], "myapp/db.json", user="app")

    with pytest.raises(ValueError, match="both user and password"):
        Etcd(["http://etcd:2379"], "myapp/db.json", password="hunter2")


def test_a_key_with_no_extension_needs_a_format() -> None:
    with pytest.raises(ValueError, match="names no format"):
        Etcd(["http://etcd:2379"], "myapp/db")

    # And naming one is enough.
    Etcd(["http://etcd:2379"], "myapp/db", format="json")


def test_a_format_the_engine_does_not_read_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="not a document format"):
        Etcd(["http://etcd:2379"], "myapp/db", format="xml")


@pytest.mark.parametrize("key", ["a/db.json", "a/db.toml", "a/db.yaml", "a/db.yml"])
def test_a_format_is_read_from_the_key_the_way_the_engine_reads_it(key: str) -> None:
    Etcd(["http://etcd:2379"], key)


# ── Vault's arguments ──────────────────────────────────────────────────


def test_vault_requires_an_auth_method() -> None:
    with pytest.raises(TypeError):
        Vault("https://vault:8200", "secret", "a/b")  # type: ignore[call-arg]


def test_vault_refuses_something_that_is_not_an_auth() -> None:
    with pytest.raises(TypeError, match="has to be an Auth"):
        Vault("https://vault:8200", "secret", "a/b", auth="hvs.token")  # type: ignore[arg-type]


# ── Auth mirrors the Rust builder ──────────────────────────────────────


def test_every_rust_auth_variant_has_a_python_spelling() -> None:
    # The mirror is the contract: a Rust variant with no Python spelling is
    # an auth method a Python deployment cannot use, and nothing but this
    # notices when one is added.
    for auth in (
        Auth.token("t"),
        Auth.app_role("r", "s"),
        Auth.kubernetes("role"),
        Auth.jwt("j"),
        Auth.userpass("u", "p"),
        Auth.ldap("u", "p"),
        Auth.certificate(),
    ):
        assert isinstance(auth, Auth)


def test_at_mount_moves_a_method_and_leaves_a_token_alone() -> None:
    # `Auth::at_mount` is documented to have no effect on `Token`, which has
    # no mount; the Python side agrees rather than inventing an error.
    assert Auth.token("t").at_mount("elsewhere") is not None
    assert Auth.app_role("r", "s").at_mount("other") is not None


def test_the_builders_do_not_mutate_the_auth_they_are_called_on() -> None:
    # Rust's take `mut self` and hand back a new one; a Python object shared
    # between two stores must not be changed by either.
    original = Auth.kubernetes("first")
    moved = original.with_role("second")

    assert "first" in repr(original)
    assert "second" in repr(moved)


def test_the_service_account_token_path_is_the_conventional_one() -> None:
    assert (
        dynamic_config_remote.SERVICE_ACCOUNT_TOKEN
        == "/var/run/secrets/kubernetes.io/serviceaccount/token"
    )


# ── The other five stores' arguments ───────────────────────────────────


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: Consul("http://consul:8500", "myapp/db"), id="consul"),
        pytest.param(lambda: Etcd(["http://etcd:2379"], "myapp/db"), id="etcd"),
        pytest.param(lambda: Nats(["nats://nats:4222"], "config", "db"), id="nats"),
        pytest.param(lambda: Redis("redis://redis:6379", "myapp/db"), id="redis"),
        pytest.param(lambda: S3("bucket", "prod/db"), id="s3"),
    ],
)
def test_a_key_with_no_extension_needs_a_format_in_every_store(
    build: object,
) -> None:
    # One rule, five stores, one message. Firestore and Vault are absent
    # because neither reads a document: both wrap a map of fields under the
    # section key, and JSON is the only thing that could be. Git is absent for
    # the opposite reason — it can read a *set*, so what settles its format is
    # the store crates' shared `agreed_format` and its wording is asserted in
    # `test_git.py`.
    with pytest.raises(ValueError, match="names no format"):
        build()  # type: ignore[operator]


def test_consul_refuses_something_that_is_not_a_consul_auth() -> None:
    with pytest.raises(TypeError, match="has to be a ConsulAuth"):
        Consul("http://consul:8500", "a/db.json", auth="a-token")  # type: ignore[arg-type]


def test_consul_defaults_to_no_token_because_acls_are_often_off() -> None:
    # Unlike Vault, where no credentials could only ever produce a 403: a
    # Consul with ACLs disabled is an ordinary thing to point this at, and the
    # Rust crate defaults the same way.
    kind, _, _, _ = Consul("http://consul:8500", "a/db.json")._auth._resolve()

    assert kind == "anonymous"


def test_every_rust_consul_auth_variant_has_a_python_spelling() -> None:
    for auth in (
        ConsulAuth.anonymous(),
        ConsulAuth.token("t"),
        ConsulAuth.kubernetes("kubernetes"),
        ConsulAuth.jwt("oidc", "a.b.c"),
    ):
        assert isinstance(auth, ConsulAuth)


def test_a_consul_bearer_file_carries_the_path_rather_than_the_token() -> None:
    # The file is re-read by the Rust crate at every login, which is what
    # keeps a rotated projected token working — so what crosses is the path,
    # and a callable here would be a file read nobody asked for.
    kind, method, bearer, _ = (
        ConsulAuth.kubernetes("kubernetes").with_bearer_file("/token")._resolve()
    )

    assert (kind, method, bearer) == ("login_file", "kubernetes", "/token")


def test_consul_meta_reaches_the_login_body_and_only_a_login() -> None:
    _, _, _, meta = (
        ConsulAuth.jwt("oidc", "a.b.c").with_meta("pod", "myapp-7f9")._resolve()
    )

    assert meta == [("pod", "myapp-7f9")]
    # A token has no login to attach it to, and Rust's builder ignores it too.
    assert ConsulAuth.token("t").with_meta("pod", "x")._resolve()[3] == []


def test_the_consul_builders_do_not_mutate_the_auth_they_are_called_on() -> None:
    original = ConsulAuth.kubernetes("first")
    moved = original.with_bearer_file("/elsewhere")

    assert "serviceaccount" in repr(original)
    assert "/elsewhere" in repr(moved)


def test_nats_needs_at_least_one_server() -> None:
    with pytest.raises(ValueError, match="at least one server"):
        Nats([], "config", "db.json")


def test_nats_refuses_something_that_is_not_a_nats_auth() -> None:
    with pytest.raises(TypeError, match="has to be a NatsAuth"):
        Nats(["nats://nats:4222"], "config", "db.json", auth="token")  # type: ignore[arg-type]


def test_every_nats_credential_shape_has_a_python_spelling() -> None:
    # `ConnectOptions`' constructors are the list, minus the two that are not
    # a credential: TLS and the signing callback, both recorded in the book as
    # what the Rust crate can do and this cannot.
    for auth in (
        NatsAuth.anonymous(),
        NatsAuth.token("t"),
        NatsAuth.user_and_password("u", "p"),
        NatsAuth.nkey_seed("SUACSSL"),
        NatsAuth.credentials("-----BEGIN NATS USER JWT-----"),
        NatsAuth.credentials_file("/etc/myapp/nats.creds"),
    ):
        assert isinstance(auth, NatsAuth)


def test_a_nats_credentials_file_is_read_at_every_fetch(tmp_path: Path) -> None:
    # A `.creds` file is exactly the thing an operator replaces without
    # restarting anything, so a copy taken at construction would be the
    # credential that was true when the process started.
    creds = tmp_path / "nats.creds"
    creds.write_text("first")
    auth = NatsAuth.credentials_file(str(creds))

    assert auth._resolve()[1] == "first"

    creds.write_text("second")

    assert auth._resolve()[1] == "second"


def test_firestore_requires_an_auth_method() -> None:
    # Where this deviates from the Rust builder on purpose: that one defaults
    # to the emulator, which as a *constructor* default would quietly send no
    # credentials to the real service.
    with pytest.raises(TypeError):
        Firestore("project", "config/db")  # type: ignore[call-arg]

    with pytest.raises(TypeError, match="has to be a FirestoreAuth"):
        Firestore("project", "config/db", auth="ya29.token")  # type: ignore[arg-type]


def test_every_rust_firestore_auth_variant_has_a_python_spelling() -> None:
    for auth in (
        FirestoreAuth.emulator(),
        FirestoreAuth.access_token("ya29."),
        FirestoreAuth.metadata_server(),
    ):
        assert isinstance(auth, FirestoreAuth)


def test_the_firestore_metadata_url_can_be_moved_for_a_sidecar() -> None:
    moved = FirestoreAuth.metadata_server().with_url("http://127.0.0.1:8081/token")

    assert moved._resolve() == ("metadata_server", "http://127.0.0.1:8081/token")
    # And the other two have no URL to move, as in Rust.
    assert FirestoreAuth.emulator().with_url("http://x")._resolve()[1] == ""


def test_s3_refuses_half_a_key_pair() -> None:
    # One without the other cannot sign anything, and silently falling back to
    # the SDK's chain would use a credential the caller did not ask for.
    with pytest.raises(ValueError, match="both access_key_id and secret"):
        S3("bucket", "prod/db.json", access_key_id="AKID")

    with pytest.raises(ValueError, match="both access_key_id and secret"):
        S3("bucket", "prod/db.json", secret_access_key="secret")


def test_s3_refuses_a_session_token_with_no_key_pair() -> None:
    with pytest.raises(ValueError, match="session_token belongs to a key pair"):
        S3("bucket", "prod/db.json", session_token="token")


def test_s3_with_no_credentials_is_the_sdk_chain_rather_than_an_error() -> None:
    # The mode most deployments use: an instance role, a task role, IRSA, or
    # AWS_ACCESS_KEY_ID. Writing nothing has to be the thing that gets it.
    store = S3("bucket", "prod/db.json", region="us-east-1")

    assert store._access_key_id is None
    assert store.describe() == "s3 bucket/prod/db.json"


# ── The public surface is what `__all__` says ──────────────────────────


def test_everything_exported_exists() -> None:
    for name in dynamic_config_remote.__all__:
        assert hasattr(dynamic_config_remote, name), name


def test_the_dotted_name_exports_exactly_what_the_package_does() -> None:
    # The two lists are two files, and nothing but this notices when one
    # gains a name the other did not. It was `xfail(strict=True)` while the
    # base wheel re-exported only Etcd and Vault; the base wheel has caught
    # up, so the marker came off with the fix rather than outliving what it
    # documented. It stays an ordinary assertion: the next store added here
    # and not re-exported there fails this suite, instead of reaching a user
    # as `ImportError: cannot import name` from the dotted path the
    # documentation tells them to write.
    assert set(through_the_base_package.__all__) == set(dynamic_config_remote.__all__)
