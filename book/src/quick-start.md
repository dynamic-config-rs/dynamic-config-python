# Quick Start

```sh
pip install dynamic-config-py
```

```python
import time
from dataclasses import dataclass

from dynamic_config import DynamicConfig


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432


config = (
    DynamicConfig(Database, key="db")
    .file("config.toml")      # later sources win
    .file("secrets.json")
    .env("APP_")              # APP_DB_PORT=5433 overrides both files
)

config.init()                 # load once, fail fast on a bad document

with config.watching(debounce=0.25):     # reload on file changes from here on
    while True:
        db = config.current()            # one atomic read, no I/O
        print(f"{db.host}:{db.port}")
        time.sleep(2)
```

Five things happened, and they are the whole model:

1. **Your class is the schema.** A dataclass here; Pydantic and msgspec
   models work the same way, and validation is theirs —
   [Data Types](types.md) is the chapter.
2. **Sources layer, later wins.** Files, then the environment; the same
   precedence chain as every other `dynamic-config` binding —
   [API Reference](reference.md#sources) lists all of them.
3. **`init()` fails fast.** A broken document stops startup rather than
   the first request an hour later.
4. **The watcher is explicit and scoped.** `watching()` is a context
   manager; the watcher stops when the block does. Long-lived services
   usually call `config.watch()` and keep the handle.
5. **`current()` is the read.** ~29 ns, no lock, no I/O — call it where
   you use the value, every time, and reloads reach you for free.

Edit `config.toml` while it runs and watch the printed line move. A bad
edit changes nothing: the engine keeps the last good document and reports
through `logging` (`dynamic_config.engine` is the logger name).

From here: [Data Types](types.md) for real schemas,
[Callbacks](callbacks.md) to react to reloads,
[Web Frameworks](frameworks.md) for the request-scoped story — and
[`dynamic-config-py-web`](https://dynamic-config-rs.github.io/web/) when
you want that story installed rather than described.
