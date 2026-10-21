"""No code of the package, the demo or the examples deletes an individual.

An individual is never deleted; its properties are retracted. A deleted individual leaves
statements in other nodes that still point at it, and a record of what was true about it is lost.
Retracting a property keeps the node, so its history and the references to it stay valid.

This keeps the middleware from calling the OGM's deletion, public or private. The test parses
every Python module under the package's source, the demo and the examples with `ast` and fails on
a call of `delete` or `_delete`, whether it is called on an object (`ogm.delete(...)`) or by name.
It reads the source only, without a store. A call through `getattr` is not seen.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = [ROOT / "src" / "kapps_semantic_middleware", ROOT / "demo", ROOT / "examples"]

FORBIDDEN = {"delete", "_delete"}


def _deletion_calls(source: str) -> list[int]:
    """The line of every call of a forbidden name in the module's source."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in FORBIDDEN:
            lines.append(node.lineno)
    return lines


def test_the_scan_finds_a_planted_call() -> None:
    assert _deletion_calls("ogm.delete(instance)\n") == [1]
    assert _deletion_calls("def f(ogm):\n    ogm._delete(instance)\n") == [2]
    assert _deletion_calls("delete(instance)\n") == [1]


def test_the_scan_ignores_other_names() -> None:
    assert _deletion_calls("ogm.commit(change)\nretract(prop)\nx = ogm.deleted\n") == []


def test_nothing_deletes_an_individual() -> None:
    found = []
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            found += [f"{path.relative_to(ROOT)}:{n}" for n in _deletion_calls(path.read_text())]
    assert not found, f"an individual is never deleted, its properties are retracted: {found}"
