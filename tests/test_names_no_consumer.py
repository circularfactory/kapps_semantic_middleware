"""The library's docstrings, comments and shipped documents name no consumer.

The middleware is generic. An example that speaks of a conveyor, a WMS or a use case makes a reader
learn another project's vocabulary first. This module scans the package, the consumption rules
file `AGENTS.md`, the mechanics pages and the published guide and reference pages that
mirror them, offline.

**The terms.** Each is matched in any case, as a word of its own: ``conveyor`` does not match the
identifiers of the demonstration's vocabulary, such as ``tu:hasConveyorBelt``.

- ``jms_kapps``, ``FlexConveyor``: the consumer and its machine.
- ``conveyor``, ``WMS``: the consumer's resources.
- ``pass_box``, ``screw``, ``unscrew``, and their inflections: the consumer's operations.
- ``UC1``, ``UC2``: the consumer's use cases.

**What is exempt.** The decision records, each file in a directory ``adr`` inside a directory
``docs``: they do not ship. This module, which names the terms.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCANNED = (
    "src",
    "docs/mechanics",
    "docs/site/guide",
    "docs/site/reference",
    "AGENTS.md",
)
SUFFIXES = {".py", ".md", ".ttl"}

CONSUMER = re.compile(
    r"jms_kapps|FlexConveyor|(?<![A-Za-z_])(?:conveyors?|WMS|pass_box|(?:un)?screw(?:s|ed|ing)?|UC[12])(?![A-Za-z_])",
    re.IGNORECASE,
)


def _files() -> Iterator[Path]:
    for entry in SCANNED:
        root = REPO_ROOT / entry
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in paths:
            if path.suffix not in SUFFIXES or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(REPO_ROOT).parts
            if "adr" in relative and "docs" in relative:
                continue
            yield path


def test_the_scan_reaches_the_package_and_the_documents() -> None:
    names = {path.name for path in _files()}
    assert {"middleware.py", "AGENTS.md", "05-operation-coordination.md"} <= names


def test_no_docstring_comment_or_shipped_document_names_a_consumer() -> None:
    found = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in _files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if CONSUMER.search(line)
    ]
    assert not found, "\n".join(found)


def test_the_pattern_matches_each_consumer_term_and_no_vocabulary_identifier() -> None:
    for term in (
        "jms_kapps",
        "FlexConveyor",
        "a conveyor module",
        "the WMS",
        "pass_box",
        "to screw",
        "unscrew",
        "screwing",
        "UC1",
        "UC2 monitor",
    ):
        assert CONSUMER.search(term), term
    for identifier in (
        "tu:hasConveyorBelt",
        "ConveyorBelt1_left",
        "EX.hasScrew",
        "conveyor_speed",
    ):
        assert not CONSUMER.search(identifier), identifier
