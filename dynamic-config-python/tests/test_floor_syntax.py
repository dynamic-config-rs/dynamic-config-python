"""This suite and the examples must be readable by the oldest interpreter.

The package supports Python 3.9, and 3.9 is a row in CI — so a mistake
here *is* caught, eventually, by a job that installs an interpreter this
machine may not have. That is a slow way to find out, and the mistake is
not obvious while you are making it: `from __future__ import annotations`
makes every annotation a string, so `list[str] | None` in a model body
looks fine and compiles fine on any version. It fails at *class creation*
on 3.9, because Pydantic evaluates a model's annotations there and PEP 604
unions do not evaluate before 3.10.

This is that check, on whatever interpreter you have: it parses the suite
and the examples rather than running them, so it finds the syntax without
needing the version that would reject it.

**Where the rule applies, and where it does not.** Only class bodies whose
annotations something *evaluates*:

- a Pydantic model, a `BaseSettings` class, a msgspec `Struct` — Pydantic
  and msgspec resolve annotations when the class is created;
- a `dataclasses.dataclass`, because this package's own dataclass adapter
  resolves them with `typing.get_type_hints`, and every dataclass in the
  suite and the examples exists to be handed to a configuration.

A function signature, a local variable and a plain class's attributes are
*not* evaluated with the future import in place, so `Service | None` in a
reload hook's signature is correct and idiomatic and stays. The package's
own dataclasses (`Report`, `ConfigStatus`) are exempt for the same reason:
nothing resolves their hints, and `dataclasses.fields()` never does.
"""

from __future__ import annotations

import ast
from pathlib import Path

MODEL_BASES = frozenset({"BaseModel", "BaseSettings", "Struct", "RootModel"})

#: The oldest interpreter this package supports, as `ast.parse` takes it.
FLOOR = (3, 9)

#: What this rule covers: both wheels' suites and this one's examples —
#: the code a reader copies, and the code the 3.9 rows run. The remote
#: wheel is included from here rather than given a second copy of this
#: file, the same way one crate's test asserts about its siblings' copied
#: `tls.rs`: one rule, one place it is written down.
ROOTS = (
    "dynamic-config-python/tests",
    "dynamic-config-python/examples",
    "dynamic-config-python-remote/tests",
)


def _covered() -> list[Path]:
    """Every file the rule applies to, skipping a root that is not there.

    A packaged sdist carries one wheel's tree and not its sibling's, and a
    rule that cannot find its subject should say nothing rather than fail.
    """
    workspace = Path(__file__).resolve().parents[2]

    return [
        path
        for root in ROOTS
        if (workspace / root).is_dir()
        for path in sorted((workspace / root).rglob("*.py"))
    ]


def _is_evaluated(node: ast.ClassDef) -> bool:
    """Whether something will resolve this class's annotations."""
    for base in node.bases:
        if ast.unparse(base).split(".")[-1] in MODEL_BASES:
            return True

    return any("dataclass" in ast.unparse(d) for d in node.decorator_list)


def _offences(path: Path) -> list[str]:
    """Every PEP 604 union in an evaluated class body in ``path``."""
    found = []

    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ClassDef) or not _is_evaluated(node):
            continue

        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign):
                continue

            annotation = ast.unparse(statement.annotation)

            if "|" in annotation:
                target = ast.unparse(statement.target)
                found.append(
                    f"{path}:{statement.lineno}: {node.name}.{target} is "
                    f"`{annotation}` — write `Optional[...]` or `Union[...]`, "
                    "which 3.9 can evaluate"
                )

    return found


def test_the_suite_and_the_examples_parse_as_the_floor_does() -> None:
    """Syntax, rather than semantics.

    A `match` statement, `except*`, a parenthesised context manager —
    anything newer than the floor fails here rather than on the one CI row
    that installs 3.9.

    `ast.parse(feature_version=...)` is best-effort by design: it knows the
    grammar changes, not every library. It catches the whole class of
    mistake this file exists for, which is writing 3.12 in a package whose
    users are on 3.9.
    """
    refused = []

    for path in _covered():
        try:
            ast.parse(path.read_text(encoding="utf-8"), feature_version=FLOOR)
        except SyntaxError as error:  # pragma: no cover - the failure path
            refused.append(f"{path}:{error.lineno}: {error.msg}")

    assert not refused, "\n".join(refused)


def test_no_pep604_where_annotations_are_evaluated() -> None:
    offences = [offence for path in _covered() for offence in _offences(path)]

    assert not offences, "\n".join(offences)
