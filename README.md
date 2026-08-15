<div align="center">

# dynamic-config-python

**Hot-reloadable configuration for Python: Rust resolves, your schema validates.**

[![CI](https://github.com/dynamic-config-rs/dynamic-config-python/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/dynamic-config-rs/dynamic-config-python/actions/workflows/ci.yml)
[![Security](https://github.com/dynamic-config-rs/dynamic-config-python/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/dynamic-config-rs/dynamic-config-python/actions/workflows/security.yml)
[![PyPI](https://img.shields.io/pypi/v/dynamic-config-py.svg)](https://pypi.org/project/dynamic-config-py/)
[![Python](https://img.shields.io/pypi/pyversions/dynamic-config-py.svg)](https://pypi.org/project/dynamic-config-py/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[**The Book**](https://dynamic-config-rs.github.io/python/) · [The engine](https://github.com/dynamic-config-rs/dynamic-config) · [PyPI](https://pypi.org/project/dynamic-config-py/)

</div>

---

```sh
pip install dynamic-config-py                # dataclasses, and the whole engine
pip install "dynamic-config-py[pydantic]"    # + Pydantic models
pip install "dynamic-config-py[msgspec]"     # + msgspec Structs
pip install "dynamic-config-py[remote]"      # + the eight Rust stores
```

```python
from dataclasses import dataclass
from dynamic_config import DynamicConfig

@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432

config = DynamicConfig(Database, key="db").file("config.toml").env("APP_")
config.init()

config.current().host        # one attribute read, no lock
```

**The engine is compiled in.** Layering, precedence, discovery, profiles,
the watcher, the last-known-good cache and every diagnostic are the same
Rust that serves the Rust crate — what is Python here is the schema, the
API and the docstrings.

## Two distributions, one version

| Package | What | Where |
|---|---|---|
| [`dynamic-config-python`](dynamic-config-python) | the wheel: `dynamic-config-py` | [PyPI](https://pypi.org/project/dynamic-config-py/) |
| [`dynamic-config-python-remote`](dynamic-config-python-remote) | the eight stores, behind the `[remote]` extra | [PyPI](https://pypi.org/project/dynamic-config-py-remote/) |

They are built from one commit by one job and released together, always:
the extra resolves to a *pair*, and the remote wheel imports `Format` and
`RemoteSource` from the base one — a gap between them is a combination
nobody has tested.

**The name on PyPI is `dynamic-config-py`**, because `dynamic-config`
belongs to an unrelated package. The import is `dynamic_config`.

## The engine is a dependency, not a sibling

These crates name it with a caret (`dynamic-config = "0.6"`), so an engine
patch release reaches them with no release here. The wheels version on
their own schedule — a Rust-only release has nothing in it for a Python
user, and a Python-only fix should not drag ten crates behind it.

## Supported interpreters

| | |
|---|---|
| CPython | 3.9 – 3.14, one abi3 wheel per platform |
| free-threaded | 3.14t, its own wheel, tested with the GIL disabled |
| MSRV (to build from source) | 1.85 — 1.88 for the remote wheel |

Raising either floor is a breaking change.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md). `just check` needs a venv with
maturin; `just python-free-threaded /path/to/venv` is the second
interpreter.

## License

[MIT](LICENSE).
