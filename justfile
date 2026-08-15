# Everything CI runs, in the order that fails fastest.
#
# Both wheels need a virtual environment with maturin in it; `python` and
# `python-remote` are the two gates, and the second needs the first —
# `dynamic_config_remote` imports `Format` and `RemoteSource` from the base
# package, so a stale base install fails as `no attribute 'EtcdStore'`, a
# long way from the cause.

default: check

# The whole gate, locally. Needs an active venv with maturin.
check: fmt lint python python-remote

# Formatting, as CI checks it.
fmt:
    cargo fmt --all -- --check

# Clippy with warnings denied. `--lib` on purpose: an extension module
# links no libpython, so there is no test target for `--all-targets` to
# build.
lint:
    cargo clippy --workspace --lib --all-features -- -D warnings

# The base wheel: build it into the active venv, then its whole suite.
python:
    CARGO_TARGET_DIR=target/python maturin develop -m dynamic-config-python/Cargo.toml
    cd dynamic-config-python && python -m pytest tests -q
    cd dynamic-config-python && mypy --strict python/dynamic_config/ tests/typing/
    cd dynamic-config-python && ruff check .
    cd dynamic-config-python && ruff format --check .
    cd dynamic-config-python && for example in examples/[0-9]*.py; do echo "→ $example"; python "$example" > /dev/null || exit 1; done

# The opt-in remote wheel: the same gate, pointed at the other directory.
# Needs `just python` to have run — its tests import the base package.
python-remote:
    CARGO_TARGET_DIR=target/python maturin develop -m dynamic-config-python-remote/Cargo.toml
    cd dynamic-config-python-remote && python -m pytest tests -q
    cd dynamic-config-python-remote && mypy --strict python/dynamic_config_remote/
    cd dynamic-config-python-remote && ruff check .
    cd dynamic-config-python-remote && ruff format --check .

# The free-threaded build is a second interpreter and needs its own venv:
#
#   uv venv --python 3.14t /tmp/ft
#   VIRTUAL_ENV=/tmp/ft uv pip install maturin pytest pytest-asyncio pydantic
#   just python-free-threaded /tmp/ft
python-free-threaded VENV:
    {{VENV}}/bin/python -c "import sysconfig; assert sysconfig.get_config_var('Py_GIL_DISABLED'), 'not a free-threaded interpreter'"
    cd dynamic-config-python && VIRTUAL_ENV={{VENV}} CARGO_TARGET_DIR=../target/python-free-threaded {{VENV}}/bin/maturin develop --no-default-features
    {{VENV}}/bin/python -c "import sys, dynamic_config; assert not sys._is_gil_enabled(), 'importing dynamic_config re-enabled the GIL'"
    cd dynamic-config-python && {{VENV}}/bin/python -m pytest tests -q
    cd dynamic-config-python && for i in $(seq 1 10); do echo "→ iteration $i"; {{VENV}}/bin/python -m pytest tests/test_threading.py tests/test_shutdown.py tests/test_free_threaded.py -q || exit 1; done

# This repository's book. The docs site builds it alongside the other
# three and publishes all four together; this is the same build, alone.
# Needs mdbook (`cargo install mdbook`).
book:
    mdbook build book
    test -f book/book/index.html

# Advisories, licences and registries — including what the wheels' extras
# resolve to, which cargo-deny has never heard of.
audit:
    cargo deny check
    python scripts/resolve-python-audit.py
