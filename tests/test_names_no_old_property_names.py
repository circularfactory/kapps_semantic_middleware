"""No shipped file or test names a service-ontology property by its old noun name.

The service ontology names every property as a predicate with a verb, so that a triple reads as
a sentence: ``svc:hasOperationStatus``, ``svc:hasAddress``, and so on, with no alias and no
equivalence for the old noun names. A file that still spells an old name would write or read a
property the ontology no longer declares.

**The nine old names.** The status property and the eight properties of the second rename.
Four of the eight are plain words in prose (the precondition, the outcome, the address, the
endpoint), so a bare match would flag every sentence. Those four are banned where they name the
property: after the ``svc:`` prefix, after ``SVC.``, after the namespace's ``Service#``, as an
``rdfs:label`` and as the argument of ``IRI("...")``. The other five are unambiguous and are
banned as bare words.

**The match.** Whole word, case-sensitively: the enum ``OperationStatus`` and the new names do
not match. The patterns are built from fragments, so this module's own text names no banned
literal.

**What is scanned.** Every file that decodes as UTF-8 in the package, the docs, the examples, the
demos, the scripts, the release data, the tests and at the top level, but the lock file.

**The exceptions, a named list and nothing else.**

- The released sections of ``CHANGELOG.md``.
- The lines of the Unreleased section that announce the renames.
- Any line that carries the fixed marker ``RENAMED_MARKER``.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests._text_files import REPO_ROOT, text_files, unreleased_changelog_lines

# Built from fragments, so that no line of this shipped file holds a date.
RENAMED_MARKER = "(renamed on " + "2026" + "-10-" + "22 from"

# Names that are not plain words: banned as bare words.
_UNAMBIGUOUS = [
    "operation" + "Status",
    "last" + "Heartbeat",
    "execution" + "Timestamp",
    "execution" + "Result",
    "failure" + "State",
]
# Plain words: banned where they name the property.
_AMBIGUOUS = ["pre" + "condition", "out" + "come", "add" + "ress", "end" + "point"]

_BARE = r"\b(?:" + "|".join(_UNAMBIGUOUS) + r")\b"
_ALT = "|".join(_AMBIGUOUS)
_QUALIFIED = (
    r"(?:\bsvc:|\bSVC\.|\bService#)(?:" + _ALT + r")\b"
    r"|rdfs:label\s+\"(?:" + _ALT + r")\""
    r"|\bIRI\(\s*\"(?:" + _ALT + r")\""
)
OLD_NAME = re.compile(_BARE + "|" + _QUALIFIED)


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


def test_the_pattern_catches_every_old_name_where_it_names_the_property() -> None:
    for name in _UNAMBIGUOUS:
        assert OLD_NAME.search(f"a {name} b"), name
        assert OLD_NAME.search(f"svc:{name}"), name
    for name in _AMBIGUOUS:
        assert OLD_NAME.search(f"svc:{name} ?x"), name
        assert OLD_NAME.search(f"pred=SVC.{name}"), name
        assert OLD_NAME.search(f"<http://w3id.org/circularfactory/Service#{name}>"), name
        assert OLD_NAME.search(f'rdfs:label "{name}"'), name
        assert OLD_NAME.search(f'x = IRI("{name}", base=SVC_NS)'), name


def test_the_pattern_leaves_the_plain_words_and_the_new_names_alone() -> None:
    for name in _AMBIGUOUS:
        assert not OLD_NAME.search(f"the {name} of the workflow")
        has = "has" + name[0].upper() + name[1:]
        assert not OLD_NAME.search(f"svc:{has} and SVC.{has}")
    for name in _UNAMBIGUOUS:
        has = "has" + name[0].upper() + name[1:]
        assert not OLD_NAME.search(f"svc:{has}")


def test_no_shipped_file_or_test_names_an_old_property() -> None:
    found = _offences()
    assert not found, "\n".join(found)


def test_the_unreleased_changelog_names_the_old_properties_only_in_the_announcing_lines() -> (
    None
):
    lines = unreleased_changelog_lines()
    naming = [line for line in lines if OLD_NAME.search(line)]
    assert len(naming) <= 3, naming
    assert not naming or "hasOperationStatus" in "\n".join(lines)
    assert not naming or "hasPrecondition" in "\n".join(lines)
