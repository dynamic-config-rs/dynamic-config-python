"""TLS: one vocabulary, seven stores, and two of them refusing part of it.

Three claims are made here and they need three different kinds of test.

**The vocabulary crosses.** `TlsConfig` is the Rust type method for method,
it is immutable, and nothing but strings, bytes and paths goes over.

**The material reaches the client.** That is a claim about a handshake, so it
is tested against a server that will not complete one — a scripted Vault
behind a throwaway certificate authority, and the same server demanding a
client certificate. Both spellings of both settings are exercised, and each
has its negative control: a store with no authority is refused, and a store
with no client certificate is refused.

**The refusals survive.** NATS cannot take PEM bytes and S3 cannot take a
client certificate. A binding that quietly ignored either would leave a
program believing it had pinned a private authority when it had not, so the
tests here are that both raise, at construction, naming the call and the way
out.

And over all of it: a private key is the sharpest secret this repository
handles, so one test plants a real one and greps every diagnostic this
surface can produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import Certificates, VaultLog, serving_vault_tls
from dynamic_config import DynamicConfig, RemoteError
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
    TlsConfig,
    Vault,
)

#: A token long enough to survive redaction without eating ordinary words. A
#: one-letter token is scrubbed by value out of "vault" itself, which is a
#: property of the redaction rather than of this file — but it makes an
#: assertion on a message impossible to read.
TOKEN = "hvs.a-planted-vault-token"


@dataclass
class TlsDatabase:
    """The schema for the one test here that builds a configuration."""

    host: str = "localhost"
    port: int = 5432


#: A configuration every one of the eight accepts: an authority named as a
#: file, which is the only setting NATS and S3 can both express.
EVERYWHERE = TlsConfig().with_ca_certificate_file("/etc/ssl/private-ca.pem")

#: The most any store here takes — an authority and a client certificate, both
#: as files. S3 refuses it, so it is not `EVERYWHERE`.
MUTUAL = EVERYWHERE.with_client_certificate_files(
    "/etc/ssl/app.crt", "/etc/ssl/app.key"
)


def stores_taking(tls: TlsConfig, *, mutual: bool) -> list[object]:
    """Every store that accepts `tls`, built and never fetched from.

    Redis is absent from both lists because its constructor takes the URL that
    TLS material has to agree with; it has tests of its own below. S3 is
    absent from the `mutual` list because it refuses a client certificate,
    which is the refusal this file exists to pin.

    `Git` takes all four settings and refuses something else — a `TlsConfig`
    on a url that is not `https://`, because an ssh remote's trust lives in
    `known_hosts`. That refusal is git's own and is pinned in `test_git.py`.
    """
    stores: list[object] = [
        Consul("https://consul.internal:8500", "myapp/db.json", tls=tls),
        Etcd(["https://etcd.internal:2379"], "myapp/db.json", tls=tls),
        Firestore("project", "config/db", auth=FirestoreAuth.emulator(), tls=tls),
        Git("https://gitlab.internal/acme/config.git", "myapp/db.json", tls=tls),
        Nats(["tls://nats.internal:4222"], "config", "db.json", tls=tls),
        Vault(
            "https://vault.internal:8200",
            "secret",
            "myapp/db",
            auth=Auth.token(TOKEN),
            tls=tls,
        ),
    ]

    if not mutual:
        stores.append(S3("bucket", "prod/db.json", region="us-east-1", tls=tls))

    return stores


# ── The vocabulary ─────────────────────────────────────────────────────


def test_the_python_vocabulary_is_the_rust_one_method_for_method() -> None:
    # The mirror is the contract. A setting with no Python spelling is a
    # deployment a Python caller cannot reach, and nothing but this notices.
    tls = (
        TlsConfig()
        .with_ca_certificate_file("/etc/ssl/private-ca.pem")
        .with_client_certificate_files("/etc/ssl/app.crt", "/etc/ssl/app.key")
    )
    from_bytes = (
        TlsConfig()
        .with_ca_certificate_pem(b"-----BEGIN CERTIFICATE-----")
        .with_client_certificate_pem(b"-----BEGIN CERTIFICATE-----", b"key")
    )

    assert isinstance(tls, TlsConfig)
    assert isinstance(from_bytes, TlsConfig)


def test_an_empty_configuration_is_the_platform_trust_store() -> None:
    # Not "no TLS": an empty one asks for the platform's own trust store,
    # which is what every store here already did and what a public authority
    # needs. It is the difference between leaving a client alone and building
    # one.
    assert TlsConfig().is_empty()
    assert not TlsConfig().with_ca_certificate_file("/ca.pem").is_empty()
    assert not TlsConfig().with_ca_certificate_pem(b"ca").is_empty()
    assert not TlsConfig().with_client_certificate_files("/c", "/k").is_empty()
    assert not TlsConfig().with_client_certificate_pem(b"c", b"k").is_empty()


def test_a_builder_answers_a_new_configuration_rather_than_mutating() -> None:
    # Rust's take `mut self` and hand back a new one; a Python object shared
    # between two stores must not be changed by either.
    original = TlsConfig()
    named = original.with_ca_certificate_file("/etc/ssl/private-ca.pem")

    assert original.is_empty()
    assert not named.is_empty()
    assert original is not named


def test_a_path_object_is_a_path() -> None:
    # `pathlib.Path` is how a Python program spells a file, and refusing it
    # would make every call site a `str(...)`.
    tls = TlsConfig().with_ca_certificate_file(Path("/etc/ssl/private-ca.pem"))

    assert "/etc/ssl/private-ca.pem" in repr(tls)


def test_the_rust_plumbing_types_are_not_bound() -> None:
    # `Pem`, `ClientCertificate` and `CertificateAndKey` exist so a store can
    # ask *which spelling was this*. A Python caller never asks — they say
    # what they have — so binding them would add three names to the surface
    # and one more place a private key could be rendered.
    import dynamic_config_remote
    from dynamic_config_remote import _core

    for absent in ("Pem", "ClientCertificate", "CertificateAndKey"):
        assert not hasattr(dynamic_config_remote, absent), absent
        assert not hasattr(_core, absent), absent


def test_every_store_takes_a_tls_configuration() -> None:
    # Eight stores, one argument, one spelling. Nothing connects.
    stores = stores_taking(EVERYWHERE, mutual=False)
    stores.append(
        Redis("rediss://redis.internal:6379", "myapp/db.json", tls=EVERYWHERE)
    )

    assert len(stores) == 8


def test_seven_of_the_eight_take_a_client_certificate() -> None:
    # S3 is the one that cannot, and its absence here is the whole of the
    # difference. Redis can, and is tested through its own URL below.
    stores = stores_taking(MUTUAL, mutual=True)
    stores.append(Redis("rediss://redis.internal:6379", "myapp/db.json", tls=MUTUAL))

    assert len(stores) == 7


def test_a_store_takes_an_empty_configuration_and_is_unchanged() -> None:
    # `tls=None` and `tls=TlsConfig()` are the same store, because an empty
    # configuration asks for what the store already did.
    with_nothing = Etcd(["http://etcd.internal:2379"], "myapp/db.json")
    with_empty = Etcd(["http://etcd.internal:2379"], "myapp/db.json", tls=TlsConfig())

    assert with_nothing.describe() == with_empty.describe()


def test_something_that_is_not_a_tls_configuration_is_refused() -> None:
    # Without this the caller meets an `AttributeError` about a private
    # attribute, which names neither the argument nor the type.
    with pytest.raises(TypeError, match="has to be a TlsConfig"):
        Etcd(["http://etcd:2379"], "a/db.json", tls="/etc/ssl/ca.pem")  # type: ignore[arg-type]


# ── The private key ────────────────────────────────────────────────────


#: Planted in a real private key's shape. If this string ever appears in a
#: diagnostic, something rendered key material.
PLANTED = "PLANTED-PRIVATE-KEY-MATERIAL"

PLANTED_KEY = (
    f"-----BEGIN PRIVATE KEY-----\n{PLANTED}\n-----END PRIVATE KEY-----\n"
).encode()


def test_a_planted_private_key_never_reaches_any_diagnostic() -> None:
    # Every surface this change adds, in one place: the configuration's own
    # `repr`, each store's `repr` and `describe`, what the engine records as
    # provenance, and the message of each refusal. A private key is the
    # sharpest secret in this project and the grep is the proof.
    tls = TlsConfig().with_client_certificate_pem(b"certificate", PLANTED_KEY)
    tls = tls.with_ca_certificate_pem(b"-----BEGIN CERTIFICATE-----\nca\n")

    diagnostics = [repr(tls)]

    for store in stores_taking(MUTUAL, mutual=True) + stores_taking(
        EVERYWHERE, mutual=False
    ):
        diagnostics.extend([repr(store), store.describe()])  # type: ignore[attr-defined]

    # The two refusals, which are the only messages that see a `TlsConfig`
    # the store could not use — and therefore the only ones written with the
    # material in hand.
    for build in (
        lambda: Nats(["tls://nats:4222"], "config", "db.json", tls=tls),
        lambda: S3("bucket", "prod/db.json", tls=tls),
    ):
        with pytest.raises(ValueError, match="refused rather than ignored") as refusal:
            build()

        diagnostics.append(str(refusal.value))

    # And provenance, which travels furthest of all: it is what `Origin`
    # carries and what every remote error the engine raises repeats.
    config = DynamicConfig(TlsDatabase, key="db").remote(
        Vault(
            "https://vault.internal:8200",
            "secret",
            "myapp/db",
            auth=Auth.token(TOKEN),
            tls=tls,
        )
    )
    # `Optional[str]` on the engine's side, because a configuration may have
    # no remote source; this one does.
    diagnostics.append(str(config.remote_description))

    for rendered in diagnostics:
        assert PLANTED not in rendered, rendered
        assert "BEGIN PRIVATE KEY" not in rendered, rendered


def test_a_repr_is_shape_and_the_rust_types_own_debug() -> None:
    # Delegated rather than rebuilt from the arguments: the rule that a key is
    # `<redacted>` and a certificate's bytes are `<pem bytes>` lives in the
    # crate that owns the type, with a planted-key test over it. A second
    # implementation here would be the copy nobody thinks to check.
    from_bytes = TlsConfig().with_client_certificate_pem(b"certificate", PLANTED_KEY)
    from_files = TlsConfig().with_client_certificate_files(
        "/etc/ssl/app.crt", "/etc/ssl/app.key"
    )

    assert "<redacted>" in repr(from_bytes), repr(from_bytes)
    # A path is a diagnostic rather than a secret, and it is the question
    # somebody debugging this is actually asking.
    assert "/etc/ssl/app.key" in repr(from_files), repr(from_files)
    assert repr(from_bytes) == repr(from_bytes._tls)


def test_a_certificates_bytes_are_noise_in_a_log_and_are_withheld_too() -> None:
    tls = TlsConfig().with_ca_certificate_pem(b"the-authoritys-material")

    assert "the-authoritys-material" not in repr(tls), repr(tls)
    assert "<pem bytes>" in repr(tls), repr(tls)


# ── What NATS refuses ──────────────────────────────────────────────────


def test_nats_refuses_a_certificate_authority_from_bytes() -> None:
    # `async-nats` opens the file itself, and the only byte-taking door is a
    # hand-built `rustls::ClientConfig`. Refused rather than ignored: a caller
    # who supplied an authority and got the public trust store has a program
    # that believes it is pinned and is not.
    with pytest.raises(ValueError, match="a certificate authority from PEM bytes") as e:
        Nats(
            ["tls://nats:4222"],
            "config",
            "db.json",
            tls=TlsConfig().with_ca_certificate_pem(b"ca"),
        )

    assert "refused rather than ignored" in str(e.value)
    assert "with_ca_certificate_file" in str(e.value)


def test_nats_refuses_a_client_certificate_from_bytes() -> None:
    with pytest.raises(ValueError, match="a client certificate from PEM bytes") as e:
        Nats(
            ["tls://nats:4222"],
            "config",
            "db.json",
            tls=TlsConfig().with_client_certificate_pem(b"certificate", PLANTED_KEY),
        )

    assert "with_client_certificate_files" in str(e.value)


def test_nats_takes_both_settings_as_files() -> None:
    # The refusal is about the spelling, not about the setting: everything
    # NATS deployments actually hand out is a pair of files.
    store = Nats(
        ["tls://nats:4222"],
        "config",
        "db.json",
        auth=NatsAuth.token(TOKEN),
        tls=TlsConfig()
        .with_ca_certificate_file("/etc/nats/ca.pem")
        .with_client_certificate_files("/etc/nats/app.crt", "/etc/nats/app.key"),
    )

    assert "nats" in store.describe()


# ── What S3 refuses ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "client_certificate",
    [
        pytest.param(
            TlsConfig().with_client_certificate_files("/app.crt", "/app.key"),
            id="files",
        ),
        pytest.param(
            TlsConfig().with_client_certificate_pem(b"certificate", PLANTED_KEY),
            id="bytes",
        ),
    ],
)
def test_s3_refuses_a_client_certificate_in_either_spelling(
    client_certificate: TlsConfig,
) -> None:
    # The SDK reaches TLS through `aws-smithy-http-client`, whose TLS context
    # is a trust store and nothing else — there is no slot to fill. A caller
    # who asked to present a certificate and did not would meet it as an
    # authentication failure a long way from the cause.
    with pytest.raises(ValueError, match="a client certificate") as refusal:
        S3("bucket", "prod/db.json", tls=client_certificate)

    assert "refused rather than ignored" in str(refusal.value)
    assert "no client-certificate slot" in str(refusal.value)


def test_s3_takes_a_certificate_authority() -> None:
    # Which is the setting that matters for S3 anyway: a private authority is
    # what MinIO, Ceph and a company's own gateway present.
    store = S3(
        "bucket",
        "prod/db.json",
        region="us-east-1",
        endpoint="https://minio.internal:9000",
        tls=TlsConfig().with_ca_certificate_file("/etc/ssl/private-ca.pem"),
    )

    assert store.describe() == "s3 https://minio.internal:9000 bucket/prod/db.json"


def test_a_refusal_names_the_store_the_setting_and_the_way_out() -> None:
    # One sentence, shared with the Rust store crates rather than written
    # twice: what was asked for, that it was *not* applied, and what to do.
    with pytest.raises(ValueError, match="refused rather than ignored") as refusal:
        Nats(
            ["tls://nats.internal:4222"],
            "config",
            "db.json",
            tls=TlsConfig().with_ca_certificate_pem(b"ca"),
        )

    message = str(refusal.value)

    assert "nats tls://nats.internal:4222" in message, message
    assert "cannot be expressed here" in message, message


def test_a_refused_store_starts_no_runtime() -> None:
    # The refusal comes before the tokio runtime is asked for, so a
    # configuration NATS cannot express is an error and not also two worker
    # threads that outlive it.
    from dynamic_config_remote import runtime_started

    already = runtime_started()

    with pytest.raises(ValueError, match="refused rather than ignored"):
        Nats(
            ["tls://nats:4222"],
            "config",
            "db.json",
            tls=TlsConfig().with_ca_certificate_pem(b"ca"),
        )

    assert runtime_started() == already


# ── Redis, whose URL has to agree ──────────────────────────────────────


def test_redis_refuses_tls_material_for_a_plaintext_url() -> None:
    # A `redis://` URL with an authority named is a deployment that believes
    # it is encrypted and is not. The refusal is the Rust client's, as it is
    # built — the URL is parsed where it is used — so it arrives at the first
    # fetch rather than at construction.
    store = Redis(
        "redis://127.0.0.1:1",
        "myapp/db.json",
        tls=TlsConfig().with_ca_certificate_file("/etc/ssl/private-ca.pem"),
    )

    with pytest.raises(RemoteError, match="refused rather than ignored") as refusal:
        store.fetch()

    assert "rediss://" in str(refusal.value), refusal.value


# ── The material actually reaches the client ───────────────────────────


def test_a_store_behind_a_private_authority_is_read(
    certificates: Certificates,
) -> None:
    with serving_vault_tls(certificates) as vault:
        store = Vault(
            vault.address,
            "secret",
            "myapp/db",
            auth=Auth.token(TOKEN),
            tls=TlsConfig().with_ca_certificate_file(certificates.ca),
        )

        text, _ = store.fetch()

    assert f"host-for-{TOKEN}" in text


def test_an_authority_from_bytes_reaches_the_client_too(
    certificates: Certificates,
) -> None:
    # The spelling a program that fetched its material from a secrets manager
    # has, and the one that exists so nobody writes a key to a disk that never
    # asked for one.
    with serving_vault_tls(certificates) as vault:
        store = Vault(
            vault.address,
            "secret",
            "myapp/db",
            auth=Auth.token(TOKEN),
            tls=TlsConfig().with_ca_certificate_pem(certificates.ca.read_bytes()),
        )

        text, _ = store.fetch()

    assert f"host-for-{TOKEN}" in text


def test_without_the_authority_the_server_is_not_trusted(
    certificates: Certificates,
) -> None:
    # The control. Without it the two tests above would pass against a client
    # that trusted everything, which is the failure this whole surface exists
    # to prevent.
    with serving_vault_tls(certificates) as vault:
        store = Vault(vault.address, "secret", "myapp/db", auth=Auth.token(TOKEN))

        with pytest.raises(RemoteError, match="UnknownIssuer") as untrusted:
            store.fetch()

    assert TOKEN not in str(untrusted.value), untrusted.value


def test_a_client_certificate_is_presented(certificates: Certificates) -> None:
    # The server refuses to complete a handshake with a client that presents
    # none, so a document coming back is proof that one was presented.
    with serving_vault_tls(certificates, require_client_certificate=True) as vault:
        store = Vault(
            vault.address,
            "secret",
            "myapp/db",
            auth=Auth.token(TOKEN),
            tls=TlsConfig()
            .with_ca_certificate_file(certificates.ca)
            .with_client_certificate_files(
                certificates.client_certificate, certificates.client_key
            ),
        )

        text, _ = store.fetch()

    assert f"host-for-{TOKEN}" in text


def test_a_client_certificate_from_bytes_is_presented(
    certificates: Certificates,
) -> None:
    with serving_vault_tls(certificates, require_client_certificate=True) as vault:
        store = Vault(
            vault.address,
            "secret",
            "myapp/db",
            auth=Auth.token(TOKEN),
            tls=TlsConfig()
            .with_ca_certificate_pem(certificates.ca.read_bytes())
            .with_client_certificate_pem(
                certificates.client_certificate.read_bytes(),
                certificates.client_key.read_bytes(),
            ),
        )

        text, _ = store.fetch()

    assert f"host-for-{TOKEN}" in text


def test_a_server_demanding_a_certificate_refuses_a_store_without_one(
    certificates: Certificates,
) -> None:
    # The other control: mTLS that was configured and not applied looks
    # exactly like mTLS that worked, until the server is one that checks.
    with serving_vault_tls(certificates, require_client_certificate=True) as vault:
        store = Vault(
            vault.address,
            "secret",
            "myapp/db",
            auth=Auth.token(TOKEN),
            tls=TlsConfig().with_ca_certificate_file(certificates.ca),
        )

        with pytest.raises(RemoteError, match="CertificateRequired"):
            store.fetch()


def test_a_missing_certificate_file_is_an_error_naming_the_path(
    certificates: Certificates,
) -> None:
    # Nothing is read at construction, deliberately: a rotated authority is
    # picked up by rebuilding the store, and a missing file is an error naming
    # it rather than a surprise in the middle of building a configuration.
    with serving_vault_tls(certificates) as vault:
        store = Vault(
            vault.address,
            "secret",
            "myapp/db",
            auth=Auth.token(TOKEN),
            tls=TlsConfig().with_ca_certificate_file("/nonexistent/private-ca.pem"),
        )

        with pytest.raises(
            RemoteError, match=r"/nonexistent/private-ca\.pem"
        ) as absent:
            store.fetch()

    assert "the CA certificate" in str(absent.value), absent.value


def test_the_engine_reads_a_configuration_through_a_pinned_store(
    certificates: Certificates,
) -> None:
    # The whole path, the way a service reaches it: a store behind a private
    # authority, installed on a configuration, refreshed and loaded.
    with serving_vault_tls(certificates) as vault:
        config = DynamicConfig(TlsDatabase, key="db").remote(
            Vault(
                vault.address,
                "secret",
                "myapp/db",
                auth=Auth.token(TOKEN),
                tls=TlsConfig().with_ca_certificate_file(certificates.ca),
            )
        )
        config.refresh_remote()
        config.init()

        current = config.current()

    assert current.host == f"host-for-{TOKEN}"
    assert current.port == 5432


def test_a_consul_auth_is_unchanged_by_any_of_this() -> None:
    # TLS is orthogonal to the credential, in all seven: naming an authority
    # does not change who the store logs in as.
    store = Consul(
        "https://consul.internal:8500",
        "myapp/db.json",
        auth=ConsulAuth.token(TOKEN),
        tls=TlsConfig().with_ca_certificate_file("/etc/ssl/private-ca.pem"),
    )

    kind, _, bearer, _ = store._auth._resolve()

    assert (kind, bearer) == ("token", TOKEN)


def test_a_planted_key_stays_out_of_a_failed_fetch(
    certificates: Certificates,
    vault: VaultLog,
) -> None:
    # The by-value belt, pointed at TLS: a client certificate whose *key* is
    # planted, a fetch that fails, and the message it produced. The engine's
    # own rule is that a failure names the store and the reason and nothing
    # else, and this is the surface that could have broken it.
    planted = certificates.directory / "planted.key"
    planted.write_bytes(PLANTED_KEY)

    store = Vault(
        vault.address.replace("http://", "https://"),
        "secret",
        "myapp/db",
        auth=Auth.token(TOKEN),
        tls=TlsConfig()
        .with_ca_certificate_file(certificates.ca)
        .with_client_certificate_files(certificates.client_certificate, planted),
    )

    with pytest.raises(RemoteError) as failure:
        store.fetch()

    assert PLANTED not in str(failure.value), failure.value
    assert TOKEN not in str(failure.value), failure.value
