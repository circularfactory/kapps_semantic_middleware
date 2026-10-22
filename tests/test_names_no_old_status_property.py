"""No shipped file or test names the status property by its old local name.

The service ontology's status property is named as a predicate with a verb, so that a triple
reads as a sentence: ``svc:hasOperationStatus``, with no alias and no equivalence for its old
noun name. A file that still spells the
old name would write or read a property the ontology no longer declares.

**The match.** The old local name as a whole word, case-sensitively: the enum ``OperationStatus``
and the new name do not match. The pattern is built from fragments, so this module's own text
names no banned literal.

**What is scanned.** Every file that decodes as UTF-8 in the package, the docs, the examples, the
demos, the scripts, the release data, the tests and at the top level, but the lock file.

**The exceptions, a named list and nothing else.**

- The released sections of ``CHANGELOG.md``.
- The one line of the Unreleased section that announces the rename.
- Any line that carries the fixed marker ``RENAMED_MARKER``.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests._text_files import REPO_ROOT, text_files, unreleased_changelog_lines

# Built from fragments, so that no line of this shipped file holds a date.
RENAMED_MARKER = "(renamed on " + "2026" + "-10-" + "22 from"

OLD_NAME = re.compile(r"\b" + "operation" + "Status" + r"\b")


def _offences() -> list[str]:
    found = []
    for path in text_files():
        relative = path.relative_to(REPO_ROOT)
        if relative.name == "CHANGELOG.md" and len(relative.parts) == 1:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if OLD_NAME.search(line) and RENAMED_MARKER not in line:
                found.append(f"{relative}:{number}: {line.strip()}")
    return found


def test_the_scan_reaches_the_package_the_tests_and_the_ontology() -> None:
    names = {path.name for path in text_files()}
    assert {
        "service.ttl",
        "middleware.py",
        "vocabulary.py",
        "AGENTS.md",
        Path(__file__).name,
    } <= names


def test_the_pattern_is_a_whole_word_and_case_sensitive() -> None:
    assert OLD_NAME.search("svc:" + "operation" + "Status")
    assert not OLD_NAME.search("svc:hasOperationStatus")
    assert not OLD_NAME.search("OperationStatus.DONE")


def test_no_shipped_file_or_test_names_the_old_status_property() -> None:
    found = _offences()
    assert not found, "\n".join(found)


def test_the_unreleased_changelog_names_the_old_property_at_most_in_the_announcing_line() -> (
    None
):
    naming = [line for line in unreleased_changelog_lines() if OLD_NAME.search(line)]
    assert len(naming) <= 1, naming
    assert not naming or "hasOperationStatus" in "\n".join(unreleased_changelog_lines())
