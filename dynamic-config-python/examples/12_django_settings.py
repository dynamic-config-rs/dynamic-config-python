"""Django: settings that can change without a restart.

    pip install django
    python examples/12_django_settings.py

Django's `settings` are read once at import and frozen, which is right for
`INSTALLED_APPS` and wrong for the handful of values an operator actually
turns during an incident — a pool size, a feature flag, a timeout. The
split here keeps Django's settings for what Django needs and puts the
turnable half behind a `DynamicConfig` that reloads.
"""

from __future__ import annotations

from _shared import Database, show, workspace
from dynamic_config import DynamicConfig

try:
    from django.conf import settings
except ImportError:  # pragma: no cover - the example says how to fix it
    raise SystemExit("this example needs Django: pip install django") from None


def main() -> None:
    """Runs the django settings example end to end."""
    with workspace() as path:
        # The static half: what Django needs before anything can run.
        if not settings.configured:
            settings.configure(DEBUG=False, ALLOWED_HOSTS=["*"], DATABASES={})

        # The turnable half: reloadable, validated, and reachable from
        # anywhere the way `settings` is.
        runtime = DynamicConfig(Database, key="db").file(str(path)).env("DJANGO_APP_")
        runtime.init()
        settings.RUNTIME = runtime  # type: ignore[misc]

        show("a view reads it per request")

        def health_view() -> dict[str, object]:
            db = settings.RUNTIME.current()  # type: ignore[attr-defined]

            return {"host": db.host, "pool": db.pool_size}

        print(f"  {health_view()}")

        show("an operator edits the file; no restart")
        path.write_text('[db]\nhost = "db.internal"\npool_size = 128\n')
        runtime.reload()
        print(f"  {health_view()}")

        show("and a bad edit changes nothing")
        path.write_text("[db]\npool_size = 0\n")
        try:
            runtime.reload()
        except Exception as failure:
            print(f"  refused: {type(failure).__name__}")
        print(f"  still serving {health_view()}")


if __name__ == "__main__":
    main()
