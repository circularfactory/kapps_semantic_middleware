"""The library's shipped files name no consumer, and no history outside the library.

The middleware is generic. An example that speaks of another project's machines, resources or
operations makes a reader learn that project's vocabulary first. This module scans every file
that ships, offline: the package, the demonstration, the examples, the mechanics pages, the
docs site, the tests, the root documents, and the changelog's section of unreleased changes.

**The terms.** Each is matched in any case, also inside a name in code or an IRI, so a constant,
a class or an IRI built on a term fails as the word does. A term that a line break splits, in
wrapped prose, a comment or a quote, fails on the line where it starts.

- The consumer project's name, and its short form.
- The scenarios it was built for: the phrase it uses for them, and their two numbered short
  names.
- Its machine (the flexible belt module), and its resources: the system that keeps its stock,
  by its full name and by its abbreviation.
- Its operations: moving a box, passing a box on, the fastener that its station drives in and
  out (with every inflection), and the grinding tool.
- History outside the library: a sentence about what comes after the article the library was
  written for, and a section of that article cited by number.

Each term is built from fragments below, so that no term stands in this file as a word.

**What is not banned.** The demonstration's own vocabulary: its transfer units, their belts and
their boxes. The ordinary words that a consumer also uses, such as a detector or a learner.

**What is exempt.** The decision records, each file in a directory ``adr`` inside a directory
``docs``: they do not ship. This module, which builds the terms. The changelog's released
sections, which record each release as it was.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

SCANNED = (
    "src",
    "demo",
    "examples",
    "docs/mechanics",
    "docs/site",
    "tests",
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTEXT-MAP.md",
    "pyproject.toml",
    ".env.example",
)
CHANGELOG = "CHANGELOG.md"
UNRELEASED = "## " + "Unreleased"
SELF = Path(__file__).resolve()

_SEP = r"[ _-]?"
_NOT_A_LETTER_BEFORE = r"(?<![a-z])"
# A capital after a lower-case letter or a digit starts a new word in a camel-case name.
_CAMEL_CASE_U = r"(?-i:(?<=[a-z0-9])U)"
_TERMS = (
    "j" + "ms",
    _NOT_A_LETTER_BEFORE + "use" + _SEP + "case",
    _CAMEL_CASE_U + "se" + _SEP + "case",
    "u" + "c[12]" + r"(?![0-9])",
    "flex" + _SEP + "con" + "veyor",
    "w" + "ms",
    "ware" + "house" + _SEP + "management",
    "pass" + _SEP + "box",
    "box" + _SEP + "trans" + "port",
    "sc" + "rew",
    "angle" + _SEP + "grinder",
    "after the " + "pa" + "per",
    "pa" + r"per\W{0,3}(?:§|sec(?:tion|\.)?\s*\d)",
)
CONSUMER = re.compile("|".join(_TERMS), re.IGNORECASE)


def _text(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    if path.name == CHANGELOG:
        before, found, rest = text.partition(f"\n{UNRELEASED}\n")
        if not found:
            return ""
        released = re.search(r"^## ", rest, re.MULTILINE)
        # Blank lines stand in for the text before the section, so a hit keeps its line number.
        return "\n" * (before.count("\n") + 2) + (
            rest[: released.start()] if released else rest
        )
    return text


def _files(root: Path = REPO_ROOT) -> Iterator[Path]:
    for entry in (*SCANNED, CHANGELOG):
        top = root / entry
        if not top.exists():
            continue
        paths = [top] if top.is_file() else sorted(top.rglob("*"))
        for path in paths:
            if not path.is_file() or path.resolve() == SELF:
                continue
            relative = path.relative_to(root).parts
            if "__pycache__" in relative or ("adr" in relative and "docs" in relative):
                continue
            yield path


# What a wrapped line of a comment, a docstring or a Markdown quote or list starts with.
_CONTINUATION = re.compile(r"^\s*(?:(?:#+|>|\*|//|--)\s*)?")


def _wraps_a_term(line: str, following: str) -> bool:
    """Whether a term starts on ``line`` and ends on the line ``following`` it."""
    head = line.rstrip()
    joined = head + " " + _CONTINUATION.sub("", following, count=1)
    return any(
        match.start() < len(head) < match.end() for match in CONSUMER.finditer(joined)
    )


def _hits(root: Path = REPO_ROOT) -> list[str]:
    found = []
    for path in _files(root):
        text = _text(path)
        if not text:
            continue
        lines = text.splitlines()
        for number, line in enumerate(lines, 1):
            following = lines[number] if number < len(lines) else ""
            if CONSUMER.search(line) or _wraps_a_term(line, following):
                found.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    return found


def test_the_scan_reaches_every_shipped_area() -> None:
    names = {path.relative_to(REPO_ROOT).as_posix() for path in _files()}
    assert {
        "src/kapps_semantic_middleware/middleware.py",
        "src/kapps_semantic_middleware/CONTEXT.md",
        "demo/transferunits/README.md",
        "demo/transferunits/templates/station_board.html",
        "examples/seed.py",
        "docs/mechanics/05-operation-coordination.md",
        "docs/site/concepts.md",
        "docs/site/scenarios/factory.md",
        "tests/test_issued_operation.py",
        "tests/reports/skip-reserve.nq",
        "README.md",
        "AGENTS.md",
        "CONTEXT-MAP.md",
        "CHANGELOG.md",
    } <= names
    assert "tests/test_names_no_consumer.py" not in names


def test_no_shipped_file_names_a_consumer() -> None:
    found = _hits()
    assert not found, "\n".join(found)


_PLANTED = (
    "the " + "W" + "MS",
    "a " + "sc" + "rewer",
    "UN" + "SC" + "REWING_CAPABILITY",
    "Flex" + "Conveyor" + "Module",
    "http://example.org/" + "j" + "ms#Module1",
    "the " + "u" + "se case",
    "U" + "C2 monitor",
    "box" + "_transport",
    "Pass" + "Box",
    "after the " + "pa" + "per",
    "(pa" + "per §4.4.2)",
    "the pa" + "per, Section 5.2",
    "pa" + "per Sec. 4",
    "an angle " + "grinder",
    "Warehouse" + "ManagementSystem1",
    "W" + "MSCapability",
    "Planner" + "W" + "msState",
    "TestU" + "C2Monitor",
    "handle" + "Pass" + "Box",
    "my" + "U" + "seCase",
    "foo" + "J" + "MS",
)


@pytest.mark.parametrize(
    "area",
    [
        "src/pkg/module.py",
        "demo/unit/README.md",
        "examples/seed.ttl",
        "docs/site/guide/page.md",
        "docs/mechanics/page.md",
        "tests/test_x.py",
        "tests/reports/report.nq",
        "README.md",
    ],
)
def test_a_planted_term_fails_the_scan_in_each_area(tmp_path: Path, area: str) -> None:
    planted = tmp_path / area
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("\n".join(_PLANTED) + "\n", encoding="utf-8")
    assert len(_hits(tmp_path)) == len(_PLANTED)


@pytest.mark.parametrize(
    "text",
    [
        "a sentence about what comes after the\n" + "pa" + "per is gone",
        "# belongs here when a " + "use\n# case needs it",
        "> the ware" + "house\n> management system",
        "- a " + "pass\n  box",
    ],
)
def test_a_term_wrapped_over_two_lines_fails_the_scan(tmp_path: Path, text: str) -> None:
    (tmp_path / "README.md").write_text(text + "\n", encoding="utf-8")
    assert _hits(tmp_path) == [f"README.md:1: {text.splitlines()[0]}"]


def test_the_changelog_is_scanned_in_its_unreleased_section_only(
    tmp_path: Path,
) -> None:
    term = "the " + "W" + "MS"
    (tmp_path / CHANGELOG).write_text(
        f"# Changelog\n\n{UNRELEASED}\n\n- {term} now\n\n## 0.1.0\n\n- {term} then\n",
        encoding="utf-8",
    )
    assert _hits(tmp_path) == [f"{CHANGELOG}:5: - {term} now"]
    (tmp_path / CHANGELOG).write_text(
        f"# Changelog\n\n## 0.1.0\n\n- {term} then\n", encoding="utf-8"
    )
    assert _hits(tmp_path) == []


def test_the_demonstration_vocabulary_and_ordinary_words_pass() -> None:
    for text in (
        "tu:hasConveyorBelt",
        "ConveyorBelt1_left",
        "a conveyor belt moves the box",
        "TransferUnit3",
        "a transport of a box",
        "the detector's snapshot, a learner",
        "var(--paper)",
        "a paper second",
        "Line monitor",
        "jsonschema",
        "a reuse case",
        "reuseCase",
        "ReuseCase",
        "because",
    ):
        assert not CONSUMER.search(text), text
