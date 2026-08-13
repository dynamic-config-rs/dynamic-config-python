r"""Vault: a login that buys a token, and two layers of renewal that do not fight.

Vault is the only store here where the credential you hand over is not the
credential that is presented. `Auth.app_role(role_id, secret_id)` is a *login*:
it is exchanged for a token with a lease, and that token is what every read
carries. So two things expire on two clocks, and only one of them is yours:

- **The Vault token** is the Rust crate's. It logs in again as expiry
  approaches and after a 403, and none of that is visible from Python.
- **The login credential** — a secret id a sidecar rewrites, a password
  somebody rotates, a workload-identity JWT a daemon refreshes — is the
  deployment's, and the Rust crate cannot notice it moving because it was
  handed a `String`. That is what a callable is for, and this file is mostly
  about it.

The other characteristic thing: Vault stores a secret's *fields*, not a
document. So `key="db"` says which section of the configuration this secret
**is**, and it has to agree with `DynamicConfig(..., key=...)`.

A server to point it at:

    docker run --rm -d -p 8200:8200 -e VAULT_DEV_ROOT_TOKEN_ID=root \\
        hashicorp/vault:1.17

    VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=root \\
        vault kv put secret/myapp/db host=vault-db port=7000

Then `python examples/02_vault.py`. With nothing listening it says so and
carries on.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dynamic_config import DynamicConfig, DynamicConfigError
from dynamic_config_remote import Auth, Vault

ADDRESS = os.environ.get("VAULT", "http://127.0.0.1:8200")


@dataclass
class Database:
    host: str = "localhost"
    # A string, because Vault's KV v2 store holds strings: `port=7000` comes
    # back as `"7000"`, and a schema that said `int` would be a validation
    # failure rather than a coercion.
    port: str = "5432"


def a_secret_is_a_section_rather_than_a_document() -> None:
    """`mount`, `path` and `key`, and why the third one exists."""
    config = DynamicConfig(Database, key="db").remote(
        Vault(
            ADDRESS,
            "secret",
            "myapp/db",
            # The section this secret's fields *are*. It has to be the same
            # word as `DynamicConfig(..., key="db")` above, because Vault hands
            # back `{host: …, port: …}` and something has to say what that is a
            # `db` of.
            key="db",
            # A Vault Agent rewrites this file under a running process; a
            # string read once at construction would be the token that was
            # true when the process started.
            auth=Auth.token(_token_file_or_root),
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
        print(f"  no Vault to read: {unreachable.__cause__ or unreachable}")


def _token_file_or_root() -> str:
    """The token a Vault Agent would have written, or the dev-mode default."""
    written = Path(os.environ.get("VAULT_TOKEN_FILE", "/var/run/vault/token"))

    if written.is_file():
        return written.read_text().strip()

    return os.environ.get("VAULT_TOKEN", "root")


def every_login_method_has_a_python_spelling() -> None:
    """One classmethod per Rust variant, and every secret half may be a callable."""
    methods = (
        Auth.token(_token_file_or_root),
        Auth.app_role("role-id", lambda: _read("/var/run/secrets/approle-secret-id")),
        Auth.kubernetes("myapp"),
        Auth.jwt(lambda: _read("/var/run/secrets/tokens/vault")),
        Auth.userpass("admin", lambda: os.environ.get("VAULT_PASSWORD", "")),
        Auth.ldap("admin", lambda: os.environ.get("VAULT_PASSWORD", "")),
        Auth.certificate(),
    )

    for method in methods:
        # The non-secret half stays printable — a role id, a user name, an auth
        # mount — and the secret half never is. It is the same split the Rust
        # crate's `Debug` makes, so the two do not have to be learned twice.
        print("   ", method)

    print("  and a modifier answers a new Auth rather than changing this one:")
    original = Auth.kubernetes("first")
    print("   ", original, "->", original.with_role("second").at_mount("k8s-eu"))


def _read(path: str) -> str:
    """A secret from a file, read afresh every time it is asked for."""
    return Path(path).read_text().strip() if Path(path).is_file() else ""


def the_one_method_that_needs_no_callable() -> None:
    """`Auth.kubernetes` carries a path, and the Rust crate re-reads it.

    A projected service-account token is rewritten by the kubelet, so the thing
    that has to be fresh is the *file*, not a string somebody read once. The
    crate opens it at every login — so this variant already survives a rotation
    with no callable at all, and takes none.
    """
    print("   ", Auth.kubernetes("myapp"))
    print("   ", Auth.kubernetes("myapp").with_token_path("/var/run/secrets/other"))


def main() -> None:
    print("a secret, wrapped under the section key it belongs to:")
    a_secret_is_a_section_rather_than_a_document()
    print()

    print("the login methods, and what each of them prints:")
    every_login_method_has_a_python_spelling()
    print()

    print("the one that needs no callable, because it names a file:")
    the_one_method_that_needs_no_callable()
    print()

    print("two layers of renewal: the Vault token is the crate's to refresh,")
    print("and the login credential above it is what a callable rotates. A")
    print("credential that has not moved reuses the session — one login, not")
    print("one per refresh, which is a poor thing to do to a secrets store.")


if __name__ == "__main__":
    main()
