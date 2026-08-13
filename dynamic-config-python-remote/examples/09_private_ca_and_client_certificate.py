r"""Reading configuration from a store behind a private certificate authority.

**The cross-cutting example.** The other eight are one per store; this one is
one *setting* across all eight, because TLS here is deliberately one vocabulary
rather than eight — `dynamic_config_store_core::tls::TlsConfig`, the same type
in every store crate, spelled the same way in every Python constructor. What
each store does with it differs, and that is what this file is for.

Two deployments, one vocabulary. A Vault whose certificate is signed by an
**internal CA** the machine has never heard of, and an etcd started with
`--client-cert-auth`, which will not talk to a client that presents no
**certificate of its own**. Both are ordinary in a hardened estate, and
neither was reachable from Python before `TlsConfig`.

With nothing running it says so and carries on — but the first section runs
anywhere, because the thing worth seeing there needs no server: a `TlsConfig`
holding a private key prints its *shape* and never its material.

Mint a throwaway authority and two certificates. The directory first, because
`openssl` will not create one and every path below is inside it:

    mkdir -p /tmp/tls

    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \\
        -keyout /tmp/tls/ca.key -out /tmp/tls/ca.pem -days 2 \\
        -subj "/CN=example-ca" -addext "basicConstraints=critical,CA:TRUE"

    { echo 'basicConstraints=critical,CA:FALSE'
      echo 'extendedKeyUsage=serverAuth'
      echo 'subjectAltName=IP:127.0.0.1'; } > /tmp/tls/server.ext
    openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \\
        -keyout /tmp/tls/server.key -out /tmp/tls/server.csr -subj "/CN=127.0.0.1"
    openssl x509 -req -in /tmp/tls/server.csr -CA /tmp/tls/ca.pem \\
        -CAkey /tmp/tls/ca.key -CAcreateserial -out /tmp/tls/server.crt \\
        -days 2 -extfile /tmp/tls/server.ext

    { echo 'basicConstraints=critical,CA:FALSE'
      echo 'extendedKeyUsage=clientAuth'; } > /tmp/tls/client.ext
    openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \\
        -keyout /tmp/tls/client.key -out /tmp/tls/client.csr -subj "/CN=myapp"
    openssl x509 -req -in /tmp/tls/client.csr -CA /tmp/tls/ca.pem \\
        -CAkey /tmp/tls/ca.key -CAcreateserial -out /tmp/tls/client.crt \\
        -days 2 -extfile /tmp/tls/client.ext

    # `openssl` writes a private key `0600` and owned by you; the Vault image
    # runs as its own unprivileged user, which then cannot read the key it was
    # pointed at — `error loading TLS cert: permission denied`, and a container
    # that exits before it has said anything else. Throwaway material in
    # /tmp is the only thing this loosens.
    chmod 644 /tmp/tls/server.key /tmp/tls/client.key

Then the two servers:

    # The dev listener moves to 8201 so the TLS one can have 8200; two
    # listeners on one address is the error you get otherwise.
    docker run --rm -d -p 8200:8200 -v /tmp/tls:/tls \\
        -e VAULT_DEV_ROOT_TOKEN_ID=root \\
        -e VAULT_DEV_LISTEN_ADDRESS=127.0.0.1:8201 \\
        -e 'VAULT_LOCAL_CONFIG={"listener":[{"tcp":{"address":"0.0.0.0:8200",
            "tls_cert_file":"/tls/server.crt","tls_key_file":"/tls/server.key"}}]}' \\
        hashicorp/vault:1.17
    VAULT_ADDR=https://127.0.0.1:8200 VAULT_CACERT=/tmp/tls/ca.pem \\
        VAULT_TOKEN=root vault kv put secret/myapp/db host=vault-db port=7000

    docker run --rm -d -p 2379:2379 -v /tmp/tls:/tls \\
        quay.io/coreos/etcd:v3.5.17 etcd \\
        --advertise-client-urls=https://0.0.0.0:2379 \\
        --listen-client-urls=https://0.0.0.0:2379 \\
        --cert-file=/tls/server.crt --key-file=/tls/server.key \\
        --client-cert-auth --trusted-ca-file=/tls/ca.pem
    etcdctl --endpoints=https://127.0.0.1:2379 --cacert=/tmp/tls/ca.pem \\
        --cert=/tmp/tls/client.crt --key=/tmp/tls/client.key \\
        put myapp/db.json '{"db": {"host": "etcd-db", "port": 6000}}'

Then `python examples/09_private_ca_and_client_certificate.py`. Point it
somewhere else with `TLS_DIR`, `VAULT` and `ETCD`.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dynamic_config import DynamicConfig, DynamicConfigError
from dynamic_config_remote import S3, Auth, Etcd, Git, GitAuth, Nats, TlsConfig, Vault

#: Where the material above was written. A Kubernetes deployment mounts a
#: secret and this is the mount path; nothing else changes.
TLS = Path(os.environ.get("TLS_DIR", "/tmp/tls"))


@dataclass
class Database:
    host: str = "localhost"
    # A string, because Vault's KV v2 store holds strings: `port=7000` comes
    # back as `"7000"`, and a schema that said `int` would be a validation
    # failure rather than a coercion.
    port: str = "5432"


@dataclass
class EtcdDatabase:
    host: str = "localhost"
    port: int = 5432


def the_private_key_never_prints() -> None:
    """What a `TlsConfig` says about itself, which is deliberately not much.

    This is the one section that runs with nothing installed and nothing
    listening, and it is here because it is the property most worth seeing: a
    private key handed to this type is never rendered by anything. The
    `repr` is the Rust type's own `Debug`, so there is one implementation of
    that rule rather than a Python copy of it.
    """
    from_files = TlsConfig().with_client_certificate_files(
        TLS / "client.crt", TLS / "client.key"
    )
    from_bytes = TlsConfig().with_client_certificate_pem(
        b"-----BEGIN CERTIFICATE-----\n...\n",
        b"-----BEGIN PRIVATE KEY-----\nnobody-should-ever-see-this\n",
    )

    print("  a path names which key, and is not itself a secret:")
    print("   ", from_files)
    print("  bytes are withheld, because they are the key:")
    print("   ", from_bytes)
    print("  an empty configuration is the platform trust store:")
    print("   ", TlsConfig().is_empty())


def from_a_vault_behind_a_private_ca() -> None:
    """A Vault whose certificate this machine has no reason to trust.

    One line of configuration is the whole difference. Naming an authority
    **replaces** the platform's trust store rather than joining it, which is
    what pinning means: a deployment that needs both puts both certificates
    in the one file.
    """
    config = DynamicConfig(Database, key="db").remote(
        Vault(
            os.environ.get("VAULT", "https://127.0.0.1:8200"),
            "secret",
            "myapp/db",
            key="db",
            auth=Auth.token(lambda: os.environ.get("VAULT_TOKEN", "root")),
            tls=TlsConfig().with_ca_certificate_file(TLS / "ca.pem"),
        )
    )

    config.refresh_remote()
    config.init()

    print("  from Vault:", config.current())


def from_an_etcd_that_demands_a_certificate() -> None:
    """An etcd started with `--client-cert-auth`, which checks who is calling.

    The certificate is the credential here — there is no password — and the
    pair is one call, because presenting either half alone is not mTLS.

    Bytes would work just as well: a program that already has its material
    from a secrets manager says `with_client_certificate_pem(...)` and never
    writes a key to a disk. etcd expresses both spellings; NATS is the one
    store that takes paths only, and it says so if you hand it bytes.
    """
    config = DynamicConfig(EtcdDatabase, key="db").remote(
        Etcd(
            [os.environ.get("ETCD", "https://127.0.0.1:2379")],
            "myapp/db.json",
            tls=TlsConfig()
            .with_ca_certificate_file(TLS / "ca.pem")
            .with_client_certificate_files(TLS / "client.crt", TLS / "client.key"),
        )
    )

    config.refresh_remote()
    config.init()

    print("  from etcd:", config.current())
    print("  provenance:", config.source_of("host"))


def what_each_store_does_with_the_same_four_settings() -> None:
    """One vocabulary, eight stores — and three of them with something to say.

    The refusals are the interesting part, and they are refusals rather than
    omissions: a binding that quietly ignored one would leave a program
    believing it had pinned a private authority when it had not, which is worse
    than a program that will not start.
    """
    for refused in (
        # `async-nats` opens the certificate files itself, so the byte
        # spellings have nowhere to go.
        lambda: Nats(
            ["tls://nats.internal:4222"],
            "config",
            "db.json",
            tls=TlsConfig().with_ca_certificate_pem(b"-----BEGIN CERTIFICATE-----"),
        ),
        # The AWS SDK's TLS context is a trust store with no slot for a client
        # certificate.
        lambda: S3(
            "myapp-config",
            "prod/db.json",
            tls=TlsConfig().with_client_certificate_files("/app.crt", "/app.key"),
        ),
        # And git's own: `tls` configures the https transport, and an ssh
        # remote's trust lives in `known_hosts` instead.
        lambda: Git(
            "ssh://git@github.com/acme/config.git",
            "db.yaml",
            auth=GitAuth.ssh_agent(),
            tls=TlsConfig().with_ca_certificate_file(TLS / "ca.pem"),
        ),
    ):
        try:
            refused()
        except ValueError as refusal:
            print(f"  {refusal}")
            print()

    print("  and one difference worth knowing rather than discovering: a named")
    print("  authority *replaces* the platform trust store in seven of the")
    print("  eight, because that is what pinning means — and git **adds** to")
    print("  it, so one source configuration reaches both a private GitLab and")
    print("  github.com.")


def main() -> None:
    print("a TlsConfig, printed:")
    the_private_key_never_prints()
    print()

    print("what each store can express, and what three of them refuse:")
    what_each_store_does_with_the_same_four_settings()
    print()

    for name, read in (
        ("Vault behind a private CA", from_a_vault_behind_a_private_ca),
        (
            "etcd demanding a client certificate",
            from_an_etcd_that_demands_a_certificate,
        ),
    ):
        print(f"{name}:")

        try:
            read()
        except DynamicConfigError as unreachable:
            # The engine deliberately does not repeat a store's own message.
            # The detail is on `__cause__`, which is this wheel's — and which
            # has had every credential taken out of it, TLS material included.
            print(f"  not reachable: {unreachable.__cause__}")

    print()
    print("the same four settings, spelled the same way, in all eight stores.")
    print("there is no way to turn verification off, in any of them: the two")
    print("situations anybody reaches for one in — a development server with a")
    print("self-signed certificate, an enterprise private CA — are both")
    print("*trusting one more certificate*, which is what the file above is.")


if __name__ == "__main__":
    main()
