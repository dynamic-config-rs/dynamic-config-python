"""Flask: configuration read per request, not copied into app.config.

    pip install flask
    python examples/11_flask_service.py

Copying values into `app.config` at startup is the usual Flask habit, and
it is exactly what makes configuration un-reloadable: the copy never
changes. Reading `config.current()` inside the view keeps hot reload
working, and costs an attribute lookup.
"""

from __future__ import annotations

from pathlib import Path

from _shared import Database, show, workspace
from dynamic_config import DynamicConfig

try:
    from flask import Flask, jsonify
except ImportError:  # pragma: no cover - the example says how to fix it
    raise SystemExit("this example needs Flask: pip install flask") from None


def build(path: Path) -> tuple[Flask, DynamicConfig[Database]]:
    config = DynamicConfig(Database, key="db").file(str(path)).env("APP_")
    config.init()

    application = Flask(__name__)

    @application.get("/health")
    def health():  # type: ignore[no-untyped-def]
        db = config.current()  # once per request, then reused

        return jsonify(host=db.host, port=db.port, pool=db.pool_size)

    return application, config


def main() -> None:
    """Runs the flask service example end to end."""
    with workspace() as path:
        application, config = build(path)

        show("serving requests")
        client = application.test_client()
        print(f"  GET /health → {client.get('/health').get_json()}")

        path.write_text('[db]\nhost = "db.internal"\npool_size = 64\n')
        config.reload()

        print(f"  after a reload → {client.get('/health').get_json()}")


if __name__ == "__main__":
    main()
