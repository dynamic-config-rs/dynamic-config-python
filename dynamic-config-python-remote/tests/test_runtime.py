"""One tokio runtime, lazily, owned by the module.

The promise is that importing this package costs nothing — no threads, no
reactor — and that a store which needs no runtime does not start one either.
Both halves are checked in a **fresh interpreter**, because a runtime is a
process-wide `OnceLock` and any test running before this one in the same
process would have started it.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


def run(script: str) -> str:
    """Runs `script` in a clean interpreter, and answers what it printed."""
    finished = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        check=True,
        text=True,
        timeout=120,
    )

    return finished.stdout.strip()


def test_importing_the_package_starts_no_runtime() -> None:
    assert (
        run(
            """
            import dynamic_config_remote
            print(dynamic_config_remote.runtime_started())
            """
        )
        == "False"
    )


def test_importing_it_through_the_base_package_starts_no_runtime() -> None:
    # `dynamic_config.remote` re-exports the same module; the indirection
    # must not be the thing that starts a reactor.
    assert (
        run(
            """
            import dynamic_config.remote as remote
            print(remote.runtime_started())
            """
        )
        == "False"
    )


def test_the_blocking_stores_start_no_runtime() -> None:
    # Five of the eight are `ureq`, a plain socket or a git fetch: HTTP, RESP
    # or a negotiation and a decompression — blocking, with no executor
    # anywhere in them. A program that reads only those should pay for no
    # threads at all, and this is the assertion that keeps that true when
    # somebody moves the runtime's start somewhere convenient.
    assert (
        run(
            """
            from dynamic_config_remote import (
                Auth, Consul, Firestore, FirestoreAuth, Git, Redis, Vault,
                runtime_started,
            )

            Vault("https://vault:8200", "secret", "a/b", auth=Auth.token("t"))
            Consul("http://consul:8500", "a/db.json")
            Firestore("project", "config/db", auth=FirestoreAuth.emulator())
            Git("https://github.com/acme/config.git", "a/db.json")
            Redis("redis://redis:6379", "a/db.json")
            print(runtime_started())
            """
        )
        == "False"
    )


@pytest.mark.parametrize(
    "store",
    [
        pytest.param('Etcd(["http://etcd:2379"], "a/db.json")', id="etcd"),
        pytest.param('Nats(["nats://nats:4222"], "config", "db.json")', id="nats"),
        pytest.param('S3("bucket", "a/db.json", region="us-east-1")', id="s3"),
    ],
)
def test_an_async_store_starts_the_runtime_at_construction(store: str) -> None:
    # At construction, not at the first fetch: construction is the moment a
    # user can observe, and a runtime appearing on a later network call is a
    # thread count nobody can explain.
    #
    # S3 is here for a second reason. Its credential provider runs *on* this
    # runtime, so the runtime's promise — that nothing on it touches Python —
    # is the thing that keeps the provider a mutex read rather than a callback
    # into a possibly-finalising interpreter.
    assert (
        run(
            f"""
            from dynamic_config_remote import Etcd, Nats, S3, runtime_started

            print(runtime_started())
            {store}
            print(runtime_started())
            """
        )
        == "False\nTrue"
    )


def test_two_stores_share_one_runtime() -> None:
    # Nothing here can count tokio's threads from Python, so what is checked
    # is the thread *delta*: the second store adds none.
    assert (
        run(
            """
            import threading

            from dynamic_config_remote import Etcd

            Etcd(["http://etcd:2379"], "a/db.json")
            after_first = threading.active_count()

            for index in range(5):
                Etcd(["http://etcd:2379"], f"key-{index}/db.json")

            print(threading.active_count() == after_first)
            """
        )
        == "True"
    )


def test_the_interpreter_exits_cleanly_with_the_runtime_running() -> None:
    # The shutdown story, as a test rather than as a paragraph. The runtime
    # is never dropped and nothing on it touches Python, so `atexit` — which
    # the base wheel already uses for its own sweep — has nothing to wait
    # for. A process that hung here would fail this by timeout.
    run(
        """
        import atexit

        from dynamic_config_remote import Etcd

        atexit.register(lambda: print("atexit ran"))
        Etcd(["http://etcd:2379"], "a/db.json")
        """
    )


def test_a_fetch_from_several_threads_at_once_does_not_deadlock() -> None:
    # The reason the runtime is multi-threaded rather than current-thread:
    # `Runtime::block_on` on a current-thread runtime is driven by the
    # calling thread, so two Python threads refreshing two stores would
    # serialise behind each other for reasons neither could see.
    assert (
        run(
            """
            import threading

            from dynamic_config_remote import Etcd

            store = Etcd(["http://127.0.0.1:1"], "a/db.json", timeout=5.0)
            failures = []

            def read():
                try:
                    store.fetch()
                except Exception:
                    failures.append(1)

            threads = [threading.Thread(target=read) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            print(len(failures) == 8 and not any(t.is_alive() for t in threads))
            """
        )
        == "True"
    )
