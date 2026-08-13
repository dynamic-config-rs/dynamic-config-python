r"""Consul: two credentials on two clocks, and a bearer that may be a file.

Consul's KV holds an **opaque blob**, so the value under a key is a whole
configuration document — the same bytes that would be in a file. That is the
opposite of Vault, which wraps a secret's fields under a section key, and it is
why this store takes `format` and Vault does not.

Its credential story is Vault's with the names changed, and the part worth
knowing is which of the two secrets a callable moves:

- **The ACL token** is Consul's to expire and the Rust crate's to replace. It
  logs in again as expiry approaches and after a 403.
- **The bearer** presented to an auth method — a projected service-account
  JWT, an OIDC id token — is the deployment's. A callable rotates that one, and
  a bearer that has moved logs in again, which correctly throws away the ACL
  token bought with the credential that has since been replaced.

And one method needs no callable at all: `ConsulAuth.kubernetes(...)` carries a
**path**, which the Rust crate re-reads at every login.

A server to point it at:

    docker run --rm -d -p 8500:8500 hashicorp/consul:1.20

    consul kv put myapp/db.json '{"db": {"host": "consul-db", "port": 6000}}'

Then `python examples/03_consul.py`. With nothing listening it says so and
carries on.
"""

import os
from dataclasses import dataclass

from dynamic_config import DynamicConfig, DynamicConfigError
from dynamic_config_remote import Consul, ConsulAuth

ADDRESS = os.environ.get("CONSUL", "http://127.0.0.1:8500")


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432


def a_key_whose_value_is_the_whole_document() -> None:
    """The blob, the ACL token, and the datacenter that is not the agent's own."""
    config = DynamicConfig(Database, key="db").remote(
        Consul(
            ADDRESS,
            "myapp/db.json",
            # `anonymous()` is the default and is right for a Consul with ACLs
            # disabled — unlike Vault, where no credentials could only ever
            # produce a 403. This one reads the variable every operator
            # already has, on every fetch.
            auth=ConsulAuth.token(lambda: os.environ.get("CONSUL_HTTP_TOKEN", "")),
            datacenter=os.environ.get("CONSUL_DATACENTER") or None,
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
        print(f"  no Consul to read: {unreachable.__cause__ or unreachable}")


def the_bearer_is_what_a_callable_moves() -> None:
    """A JWT exchanged at an auth method, rather than a token handed over."""
    exchanged = ConsulAuth.jwt("oidc", lambda: os.environ.get("OIDC_TOKEN", ""))

    print("   ", exchanged)
    print("  a rotated bearer buys a new ACL token, because a token bought")
    print("  with a credential that has since been replaced is one nobody")
    print("  should keep. A bearer that has not moved reuses the session.")


def the_method_that_reads_a_file_instead() -> None:
    """`kubernetes` carries a path the Rust crate re-reads at every login.

    A projected service-account token is rewritten by the kubelet, so what has
    to be fresh is the file rather than a string somebody read once. That is
    why this variant takes no credential argument at all — and why a callable
    here would be a file read nobody asked for.
    """
    print("   ", ConsulAuth.kubernetes("kubernetes"))
    print("   ", ConsulAuth.kubernetes("kubernetes").with_bearer_file("/var/run/token"))


def meta_is_for_the_audit_log_and_not_a_credential() -> None:
    """`Meta` is attached to the issued token: which workload, which pod."""
    audited = (
        ConsulAuth.kubernetes("kubernetes")
        .with_meta("pod", os.environ.get("HOSTNAME", "myapp-7f9"))
        .with_meta("component", "configuration")
    )

    print("   ", audited)
    # A token has no login to attach it to, and the Rust builder ignores it
    # there too rather than inventing an error.
    print(
        "    on a plain token, which has no login:",
        ConsulAuth.token("t").with_meta("pod", "x"),
    )


def main() -> None:
    print("a blob under a key, which is a whole configuration document:")
    a_key_whose_value_is_the_whole_document()
    print()

    print("the credential a callable moves:")
    the_bearer_is_what_a_callable_moves()
    print()

    print("the one that needs no callable, because it names a file:")
    the_method_that_reads_a_file_instead()
    print()

    print("what rides along for the audit log:")
    meta_is_for_the_audit_log_and_not_a_credential()
    print()

    print("Consul expresses the whole TLS vocabulary — a private authority and")
    print("a client certificate, as files or as bytes. See")
    print("09_private_ca_and_client_certificate.py.")


if __name__ == "__main__":
    main()
