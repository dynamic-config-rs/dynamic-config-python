"""`status()`, `remote_status()` and the Prometheus exposition.

The Rust suite's `tests/telemetry.rs` pins these facts on that side; this
is the same set asked through the binding, plus the two questions only
the boundary raises: what a monotonic `Instant` becomes in Python, and
whether anything a document carries can reach a metric label.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from dynamic_config import (
    ConfigStatus,
    DynamicConfig,
    Exposition,
    Format,
    ParseError,
    RemoteError,
    RemoteSource,
)

PLANTED = "hunter2-planted-secret"
STORE_URL = "https://svcuser:hunter2@store.internal:8500/v1/kv/app"

# Every label name the exposition is allowed to emit. `config` is what
# `add`/`add_remote` spell, and the other two are the fixed enums.
ALLOWED_LABELS = {"config", "reason", "kind"}

# The lines are `name{a="b",c="d"} value` or `name value`.
SAMPLE = re.compile(r"^(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?:\{(?P<labels>[^}]*)\})? ")


@dataclass
class Database:
    host: str = "localhost"
    pool_size: int = 5


def write(path: str = "config.toml", pool_size: str = "8") -> None:
    """A loadable document, with a secret in it to look for later."""
    Path(path).write_text(
        f'[db]\nhost = "{PLANTED}"\npool_size = {pool_size}\n',
    )


def loaded(path: str = "config.toml") -> DynamicConfig[Database]:
    """A configuration with one install behind it."""
    write(path)
    config = DynamicConfig(Database, key="db").file(path)
    config.init()

    return config


def samples(body: str) -> list[tuple[str, dict[str, str]]]:
    """Every sample line as (family, labels), comments dropped."""
    found = []

    for line in body.splitlines():
        if line.startswith("#") or not line.strip():
            continue

        match = SAMPLE.match(line)
        assert match is not None, f"unparseable exposition line: {line!r}"

        raw = match.group("labels") or ""
        labels = {
            pair.split("=", 1)[0]: pair.split("=", 1)[1].strip('"')
            for pair in raw.split(",")
            if pair
        }
        found.append((match.group("name"), labels))

    return found


def families(body: str) -> set[str]:
    """The metric family names in a rendered body."""
    return {name for name, _ in samples(body)}


# ── ConfigStatus ───────────────────────────────────────────────────────


def test_a_configuration_that_has_never_loaded_reports_no_staleness(
    workspace: Path,
) -> None:
    write()
    config = DynamicConfig(Database, key="db").file("config.toml")

    status = config.status()

    assert status.generation == 0
    assert status.stale_for is None, (
        "zero would read as 'installed just now', which is the opposite"
    )
    assert status.last_reason is None
    assert status.last_failure is None
    assert status.is_healthy, "nothing has failed yet"


def test_an_install_moves_the_generation_and_names_its_reason(
    workspace: Path,
) -> None:
    config = loaded()

    first = config.status()
    assert first.generation == 1
    assert first.last_reason == "initial"

    config.reload()

    second = config.status()
    assert second.generation == 2
    assert second.last_reason == "manual"
    assert second.is_healthy


def test_a_refused_reload_counts_a_failure_and_keeps_the_generation(
    workspace: Path,
) -> None:
    config = loaded()
    Path("config.toml").write_text("[db\nhost = ")

    with pytest.raises(ParseError):
        config.reload()

    status = config.status()

    assert status.generation == 1, "the previous model is still serving"
    assert status.consecutive_failures == 1
    assert not status.is_healthy
    assert status.last_failure is not None
    assert status.last_failure.kind == "parse"


def test_a_later_success_clears_the_streak_and_keeps_the_failure_as_history(
    workspace: Path,
) -> None:
    config = loaded()
    Path("config.toml").write_text("[db\nhost = ")

    with pytest.raises(ParseError):
        config.reload()

    write(pool_size="16")
    config.reload()

    status = config.status()

    assert status.consecutive_failures == 0, "the streak is the health"
    assert status.is_healthy
    assert status.last_failure is not None, (
        "and the failure itself is history, which does not disappear"
    )
    assert status.last_failure.kind == "parse"


def test_when_is_seconds_since_rather_than_a_timestamp(workspace: Path) -> None:
    """The `Instant` question, asked directly.

    The engine records with a monotonic clock so an NTP step backwards
    cannot make a fresh configuration look stale, and a monotonic instant
    has no epoch to convert from. So `stale_for` grows, and there is no
    `loaded_at` to be wrong about.
    """
    config = loaded()

    first = config.status().stale_for
    assert first is not None
    time.sleep(0.05)
    second = config.status().stale_for
    assert second is not None

    assert second > first, "elapsed time is what a monotonic instant becomes"
    assert second >= 0.05

    assert not hasattr(config.status(), "loaded_at"), (
        "a wall-clock timestamp here would claim a precision the engine "
        "deliberately refused to claim"
    )
    assert isinstance(first, float)


def test_the_status_is_a_snapshot_and_not_a_live_view(workspace: Path) -> None:
    config = loaded()
    taken = config.status()

    config.reload()

    assert taken.generation == 1, "the object held is the moment it was taken"
    assert config.status().generation == 2


def test_a_status_is_frozen_so_a_handler_cannot_edit_what_it_reports(
    workspace: Path,
) -> None:
    status = loaded().status()

    # PyO3 spells a frozen class's refusal as "cannot assign to field". The
    # wording is matched rather than paraphrased, so a future PyO3 that
    # changed it fails here instead of quietly starting to accept the write.
    with pytest.raises(AttributeError, match="cannot assign to field"):
        status.generation = 99  # type: ignore[misc]


# ── RemoteStatus ───────────────────────────────────────────────────────


class Answering(RemoteSource):
    """A store that hands over a document."""

    def __init__(self, pool_size: int = 32) -> None:
        self.pool_size = pool_size

    def fetch(self) -> tuple[str, Format]:
        return f'{{"db": {{"pool_size": {self.pool_size}}}}}', Format.JSON

    def describe(self) -> str:
        return STORE_URL


class Refusing(RemoteSource):
    """A store that does not, and whose exception carries its URL."""

    def fetch(self) -> tuple[str, Format]:
        raise RuntimeError(f"{STORE_URL} refused the connection")

    def describe(self) -> str:
        return STORE_URL


def test_a_store_nobody_has_asked_is_not_reported_as_down(workspace: Path) -> None:
    config = loaded()
    config.remote(Answering())

    status = config.remote_status()

    assert status.fetches == 0
    assert status.reachable is None, (
        "installed and never asked is a third state, not down — a zero here "
        "is how a scrape at startup pages somebody"
    )
    assert status.stale_for is None


def test_a_configuration_with_no_store_at_all_still_answers(workspace: Path) -> None:
    status = loaded().remote_status()

    assert status.fetches == 0
    assert status.reachable is None
    assert status.last_failure is None


def test_a_fetch_reports_that_the_store_answered_and_how_long_it_took(
    workspace: Path,
) -> None:
    config = loaded()
    config.remote(Answering())
    config.refresh_remote()

    status = config.remote_status()

    assert status.fetches == 1
    assert status.reachable is True
    assert status.consecutive_failures == 0
    assert status.stale_for is not None
    assert status.last_fetch_duration is not None
    assert status.last_fetch_duration >= 0.0


def test_a_store_that_stops_answering_counts_the_failure_and_its_kind(
    workspace: Path,
) -> None:
    config = loaded()
    config.remote(Refusing())

    with pytest.raises(RemoteError):
        config.refresh_remote()

    status = config.remote_status()

    assert status.fetches == 0
    assert status.reachable is False
    assert status.consecutive_failures == 1
    assert status.last_failure is not None
    assert status.last_failure.kind == "remote", (
        "a store that may answer next time, as against auth, which will not"
    )


def test_asking_for_a_remote_status_does_not_fix_the_sources(
    workspace: Path,
) -> None:
    """`status()` builds the engine; this one deliberately does not."""
    write()
    config = DynamicConfig(Database, key="db")

    assert config.remote_status().fetches == 0

    config.file("config.toml")
    config.init()

    assert config.current().pool_size == 8


# ── The exposition ─────────────────────────────────────────────────────


def test_the_exposition_renders_the_six_families_a_status_has(
    workspace: Path,
) -> None:
    config = loaded()

    body = Exposition().add("db", config).render()

    assert families(body) == {
        "dynamic_config_installs_total",
        "dynamic_config_last_success_seconds",
        "dynamic_config_consecutive_failures",
        "dynamic_config_last_reload_info",
    }
    assert 'dynamic_config_installs_total{config="db"} 1' in body


def test_a_fact_that_does_not_exist_yet_is_absent_rather_than_zero(
    workspace: Path,
) -> None:
    write()
    config = DynamicConfig(Database, key="db").file("config.toml")

    body = Exposition().add("db", config).render()

    assert "dynamic_config_installs_total" in body
    assert "dynamic_config_last_success_seconds" not in body, (
        "a configuration that never installed has no staleness, and zero "
        "would read as 'installed just now'"
    )
    assert "dynamic_config_last_failure_seconds" not in body

    config.remote(Answering())
    remote_body = Exposition().add_remote("db", config).render()

    assert "dynamic_config_remote_up" not in remote_body, (
        "absent before the first fetch, on the same principle"
    )


def test_the_remote_half_renders_beside_the_config_half_under_one_name(
    workspace: Path,
) -> None:
    config = loaded()
    config.remote(Answering())
    config.refresh_remote()

    body = Exposition().add("db", config).add_remote("db", config).render()

    assert 'dynamic_config_remote_up{config="db"} 1' in body
    assert 'dynamic_config_remote_fetches_total{config="db"} 1' in body
    assert 'dynamic_config_installs_total{config="db"} 1' in body

    for _, labels in samples(body):
        assert labels["config"] == "db", "both halves join on the caller's name"


def test_labels_of_the_callers_choosing_carry_more_than_one_dimension(
    workspace: Path,
) -> None:
    config = loaded()

    body = (
        Exposition()
        .add_with({"application": "billing", "profile": "prod"}, config)
        .render()
    )

    assert 'application="billing"' in body
    assert 'profile="prod"' in body
    assert "config=" not in body, "add_with replaces the default label, not adds to it"


def test_two_configurations_are_two_series_in_one_body(workspace: Path) -> None:
    first = loaded("first.toml")
    second = loaded("second.toml")

    body = Exposition().add("first", first).add("second", second).render()
    names = {labels["config"] for _, labels in samples(body)}

    assert names == {"first", "second"}
    # One HELP and one TYPE per family, not one per series.
    assert body.count("# HELP dynamic_config_installs_total") == 1


def test_the_calls_chain_so_a_metrics_handler_is_one_expression(
    workspace: Path,
) -> None:
    config = loaded()

    body = Exposition().add("db", config).add_remote("db", config).render()

    assert body.startswith("# HELP ")


def test_an_exposition_repr_carries_neither_labels_nor_numbers(
    workspace: Path,
) -> None:
    exposition = Exposition().add("db", loaded())

    assert repr(exposition) == "<Exposition>"


# ── Redaction: what may not cross ──────────────────────────────────────


def test_no_configured_value_reaches_the_exposition(workspace: Path) -> None:
    """The document's values are in the model, and nowhere in the metrics."""
    config = loaded()
    config.remote(Answering())
    config.refresh_remote()
    config.reload()

    body = Exposition().add("db", config).add_remote("db", config).render()

    assert config.current().host == PLANTED, "the value did load"
    assert PLANTED not in body


def test_a_store_url_never_becomes_a_metric_label(workspace: Path) -> None:
    """The one string a source can produce for itself is a URL with a password in it."""
    config = loaded()
    config.remote(Refusing())

    with pytest.raises(RemoteError):
        config.refresh_remote()

    body = Exposition().add("db", config).add_remote("db", config).render()

    assert config.remote_description == STORE_URL, "the store did name itself"
    assert "hunter2" not in body
    assert "store.internal" not in body
    assert "svcuser" not in body


def test_a_store_url_never_reaches_a_remote_status_either(workspace: Path) -> None:
    config = loaded()
    config.remote(Refusing())

    with pytest.raises(RemoteError):
        config.refresh_remote()

    rendered = repr(config.remote_status())

    assert "hunter2" not in rendered
    assert "store.internal" not in rendered
    assert config.remote_status().last_failure is not None


def test_a_failure_carries_a_category_and_never_the_message(workspace: Path) -> None:
    """A store's exception routinely carries the URL it called."""
    config = loaded()
    config.remote(Refusing())

    with pytest.raises(RemoteError):
        config.refresh_remote()

    failure = config.remote_status().last_failure
    assert failure is not None

    assert failure.kind == "remote"
    assert not hasattr(failure, "message"), (
        "a struct that stores free text is one careless construction away "
        "from putting a value in every log line that prints a status"
    )
    assert "refused the connection" not in repr(failure)


def test_only_the_fixed_enums_and_the_callers_own_name_become_labels(
    workspace: Path,
) -> None:
    """The structural half: no key path can become a label, today or later.

    A `Failure` carries a dotted key path, which is a detail nobody asked
    to publish *and* unbounded label cardinality. The exposition renders
    the failure's category instead — so the label names are a closed set
    and every value is either the caller's string or a fixed enum's name.
    """
    config = loaded()
    Path("config.toml").write_text("[db\nhost = ")

    with pytest.raises(ParseError):
        config.reload()

    config.remote(Refusing())

    with pytest.raises(RemoteError):
        config.refresh_remote()

    body = Exposition().add("db", config).add_remote("db", config).render()
    seen = samples(body)

    assert seen, "the body has samples to check"

    for name, labels in seen:
        assert set(labels) <= ALLOWED_LABELS, f"{name} grew a label"

        if "reason" in labels:
            assert labels["reason"] in {
                "initial",
                "manual",
                "file_changed",
                "remote",
                "replaced",
            }

        if "kind" in labels:
            assert labels["kind"] in {
                "io",
                "parse",
                "missing",
                "type",
                "env",
                "invalid",
                "remote",
                "auth",
                "decrypt",
                "backend",
            }


def test_a_key_name_from_the_document_is_not_in_the_body(workspace: Path) -> None:
    config = loaded()
    Path("config.toml").write_text("[db\nhost = ")

    with pytest.raises(ParseError):
        config.reload()

    body = Exposition().add("db", config).render()

    assert "pool_size" not in body
    assert "host" not in body


# ── Typing ─────────────────────────────────────────────────────────────


def test_a_status_is_the_declared_type_and_not_a_dict(workspace: Path) -> None:
    """The stub says these are objects; the module has to agree."""
    config = loaded()
    status: ConfigStatus = config.status()

    assert isinstance(status, ConfigStatus)
    assert isinstance(status.generation, int)
    assert not isinstance(status, dict)

    raw: Any = config._core.status()
    assert isinstance(raw, dict), "the boundary hands over a dict; the facade types it"
