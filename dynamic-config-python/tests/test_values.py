"""A configuration with no schema class: `Values`.

The Python half of the crate's `Dynamic<Value>`. What it must do is
everything a typed configuration does — layers, profiles, the watcher,
the diagnostics — and what it must *not* do is claim knowledge it never
had: no field list means `check()` says so, and no declared secrets means
a redacting cache is refused rather than written unredacted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dynamic_config import BackendError, DynamicConfig, Values


def write(name: str, data: object) -> None:
    Path(name).write_text(json.dumps(data))


# ── Reading ────────────────────────────────────────────────────────────


def test_a_configuration_with_no_model_reads_by_path(workspace: Path) -> None:
    write("plugins.json", {"plugins": {"cache": {"ttl": 60}, "enabled": ["a"]}})

    config = DynamicConfig(Values, key="plugins").file("plugins.json")
    config.init()

    values = config.current()

    assert values["cache.ttl"] == 60
    assert values["cache"]["ttl"] == 60, "a step at a time works too"
    assert values.get("cache.backend", "memory") == "memory"
    assert values["enabled"] == ["a"]


def test_it_is_a_mapping(workspace: Path) -> None:
    write("plugins.json", {"plugins": {"cache": {"ttl": 60}, "enabled": ["a"]}})

    config = DynamicConfig(Values, key="plugins").file("plugins.json")
    config.init()

    values = config.current()

    assert len(values) == 2
    assert sorted(values) == ["cache", "enabled"]
    assert "cache" in values
    assert "cache.ttl" in values, "a dotted path is a lookup like any other"
    assert "nope" not in values
    assert dict(values) == {"cache": {"ttl": 60}, "enabled": ["a"]}
    assert values.to_dict() == dict(values)
    assert values.leaf_paths() == ["cache.ttl", "enabled"]


def test_a_missing_path_raises_and_a_default_does_not(workspace: Path) -> None:
    write("plugins.json", {"plugins": {"cache": {"ttl": 60}}})

    config = DynamicConfig(Values, key="plugins").file("plugins.json")
    config.init()

    values = config.current()

    with pytest.raises(KeyError):
        values["cache.nope"]

    assert values.get("cache.nope") is None
    assert values.get("cache.nope", 5) == 5


def test_the_repr_carries_keys_and_never_a_value(workspace: Path) -> None:
    write("plugins.json", {"plugins": {"token": "hunter2", "ttl": 60}})

    config = DynamicConfig(Values, key="plugins").file("plugins.json")
    config.init()

    rendered = repr(config.current())

    assert "hunter2" not in rendered, rendered
    assert "token" in rendered


# ── Everything else still works ────────────────────────────────────────


def test_the_layers_are_the_same_layers(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write("plugins.json", {"plugins": {"cache": {"ttl": 60}}})
    monkeypatch.setenv("APP_PLUGINS_CACHE__TTL", "90")

    config = DynamicConfig(Values, key="plugins").file("plugins.json").env("APP_")
    config.set_default("cache.backend", "memory")
    config.init()

    values = config.current()

    assert values["cache.ttl"] == 90, "the environment wins over the file"
    assert values["cache.backend"] == "memory", "the defaults layer is below it"


def test_whole_document_needs_no_model_either(workspace: Path) -> None:
    write("plugins.json", {"cache": {"ttl": 60}})

    config = DynamicConfig(Values, key="plugins").whole_document().file("plugins.json")
    config.init()

    assert config.current()["cache.ttl"] == 60


def test_the_diagnostics_answer(workspace: Path) -> None:
    write("plugins.json", {"plugins": {"cache": {"ttl": 60}}})

    config = DynamicConfig(Values, key="plugins").file("plugins.json")
    config.init()

    origin = config.source_of("cache.ttl")

    assert origin is not None
    assert config.is_set("cache.ttl")
    assert "cache.ttl" in config.snapshot().leaf_paths()
    assert "60" in str(config.explain("cache.ttl"))


def test_check_says_it_did_not_compare_field_names(workspace: Path) -> None:
    """The honest answer, and the reason `unknown_checked` exists.

    A configuration with no schema has no field list, so an empty
    ``unknown`` list would read as an all-clear it never earned.
    """
    write("plugins.json", {"plugins": {"anything": 1}})

    report = DynamicConfig(Values, key="plugins").file("plugins.json").check()

    assert report.unknown == ()
    assert not report.unknown_checked
    assert "not checked" in str(report)


def test_a_reload_publishes_a_new_mapping(workspace: Path) -> None:
    write("plugins.json", {"plugins": {"cache": {"ttl": 60}}})

    config = DynamicConfig(Values, key="plugins").file("plugins.json")
    config.init()

    write("plugins.json", {"plugins": {"cache": {"ttl": 90}}})
    config.reload()

    assert config.current()["cache.ttl"] == 90


# ── What it cannot claim ───────────────────────────────────────────────


def test_a_redacting_cache_is_refused_without_a_secret_list(workspace: Path) -> None:
    """No declaration, no redaction — and no cache that pretends otherwise."""
    write("plugins.json", {"plugins": {"token": "hunter2"}})

    config = (
        DynamicConfig(Values, key="plugins")
        .file("plugins.json")
        .cache("last.json", "redacted")
    )

    with pytest.raises(BackendError) as raised:
        config.init()

    assert "secret" in str(raised.value).lower(), raised.value
    assert "secrets=" in str(raised.value), "the refusal names the Python fix"
    assert not Path("last.json").exists(), "nothing was written"


def test_secrets_supplied_by_hand_make_the_cache_work(workspace: Path) -> None:
    write("plugins.json", {"plugins": {"token": "hunter2", "ttl": 60}})

    config = (
        DynamicConfig(Values, key="plugins", secrets=["token"])
        .file("plugins.json")
        .cache("last.json", "redacted")
    )
    config.init()

    written = Path("last.json").read_text()

    assert "hunter2" not in written, written
    assert "ttl" in written


def test_secrets_also_redact_explain(workspace: Path) -> None:
    write("plugins.json", {"plugins": {"token": "hunter2"}})

    config = DynamicConfig(Values, key="plugins", secrets=["token"]).file(
        "plugins.json"
    )
    config.init()

    rendered = str(config.explain("token"))

    assert "hunter2" not in rendered, rendered
    assert "***" in rendered


def test_something_that_is_not_a_schema_is_still_refused(workspace: Path) -> None:
    with pytest.raises(TypeError) as raised:
        DynamicConfig(dict, key="plugins")  # type: ignore[type-var]

    assert "Values" in str(raised.value), "the error names the way to say it"
