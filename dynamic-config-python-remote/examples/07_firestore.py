r"""Firestore: the identity that renews itself, and the token that cannot.

Firestore is the store where a callable is *sometimes* pointless and it is worth
knowing which time is which. There are three ways to authenticate and they are
not three flavours of the same thing:

- `FirestoreAuth.metadata_server()` — the workload's own identity on GKE, Cloud
  Run or GCE. The Rust crate asks the metadata server for a token, caches it,
  and mints another as expiry approaches. **It renews itself, so it needs no
  callable**, and a callable would only add a second cache in front of a
  working one.
- `FirestoreAuth.access_token(...)` — anything that already has a token: a
  `gcloud auth print-access-token`, a workload-identity exchange, a sidecar.
  **This one cannot renew.** An access token lives an hour and the crate has
  nothing to obtain another with, so this is the variant a callable exists for.
- `FirestoreAuth.emulator()` — no token at all.

`auth` is **required** here, which is a deliberate deviation from the Rust
builder: that one defaults to the emulator, and *send no credentials* is a
reasonable default for a builder being filled in and a poor one for a
constructor, where it would quietly produce a 401 against the real service.

Like Vault, Firestore hands back a document's **fields** rather than a document,
so `key` says which section they are.

Something to point it at, and the document it should hold — the emulator has no
CLI, so a document is written with the REST API the store reads:

    docker run --rm -d -p 8080:8080 \\
        gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators \\
        gcloud emulators firestore start --host-port=0.0.0.0:8080 \\
        --project=my-project

    document=/v1/projects/my-project/databases/'(default)'/documents/config/db
    curl -X PATCH -H 'Content-Type: application/json' \\
        "http://127.0.0.1:8080$document" \\
        -d '{"fields":{"host":{"stringValue":"firestore-db"},
             "port":{"integerValue":"6004"}}}'

With the SDK on the host, `gcloud emulators firestore start
--host-port=127.0.0.1:8080` is the same thing without the container.

Then `python examples/07_firestore.py`. With nothing listening it says so and
carries on.
"""

import os
import subprocess
from dataclasses import dataclass

from dynamic_config import DynamicConfig, DynamicConfigError
from dynamic_config_remote import Firestore, FirestoreAuth

PROJECT = os.environ.get("GCP_PROJECT", "my-project")
ENDPOINT = os.environ.get("FIRESTORE", "http://127.0.0.1:8080")


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432


def a_document_wrapped_under_the_section_key() -> None:
    """`config/db` is a collection and a document; `key` is what they are."""
    config = DynamicConfig(Database, key="db").remote(
        Firestore(
            PROJECT,
            "config/db",
            # The emulator wants no credential at all, which is what makes
            # this example runnable. In production it is `metadata_server()`.
            auth=FirestoreAuth.emulator(),
            key="db",
            endpoint=ENDPOINT,
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
        print(f"  no Firestore to read: {unreachable.__cause__ or unreachable}")


def the_identity_that_renews_itself() -> None:
    """On GKE, Cloud Run or GCE this is the whole configuration.

    The address is movable for a sidecar that terminates the metadata protocol
    somewhere else, and that is the only reason `with_url` exists.
    """
    print("   ", FirestoreAuth.metadata_server())
    print(
        "   ", FirestoreAuth.metadata_server().with_url("http://127.0.0.1:8081/token")
    )
    print("  the Rust crate mints one token and keeps it until it is nearly")
    print("  expired — one trip to a service that rate-limits, not one per fetch.")


def the_token_that_cannot_renew_is_the_one_a_callable_is_for() -> None:
    """An access token minted outside the process expires in an hour.

    The callable is invoked on every fetch, so whatever produces the token —
    a workload-identity exchange, a sidecar, a shelling-out to `gcloud` as
    here — is asked again, and a value that has changed is the one the next
    read carries.
    """
    minted = FirestoreAuth.access_token(_an_access_token)

    print("   ", minted)
    print("  resolved right now to:", _describe(_an_access_token()))


def _an_access_token() -> str:
    """Whatever this machine can produce, which may well be nothing."""
    printed = os.environ.get("GOOGLE_ACCESS_TOKEN")

    if printed:
        return printed

    try:
        return subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        # A callable may fail, and a refresh that fails is not fatal: the
        # previous document and the previous model both keep serving.
        return ""


def _describe(token: str) -> str:
    """A token's shape, never a token.

    The same rule the whole package follows — a credential in a diagnostic is
    a credential in a log — applied to an example's own output.
    """
    return f"{len(token)} characters" if token else "nothing on this machine"


def main() -> None:
    print("a document, wrapped under the section key it belongs to:")
    a_document_wrapped_under_the_section_key()
    print()

    print("the credential that needs no callable:")
    the_identity_that_renews_itself()
    print()

    print("the credential that does:")
    the_token_that_cannot_renew_is_the_one_a_callable_is_for()
    print()

    print("a service-account JSON key is deliberately absent — here and in the")
    print("Rust crate. Signing one means an RS256 stack in a configuration")
    print("library, and Google's own guidance is that a downloaded key is the")
    print("option of last resort.")


if __name__ == "__main__":
    main()
