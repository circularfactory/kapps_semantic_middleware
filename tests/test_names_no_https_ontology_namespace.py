"""No shipped file or test spells an ontology namespace with ``https://``.

Every ontology namespace of the project uses ``http://``, in step with the Core ontology: a
namespace is an identifier and not a link, and a single spelling keeps a stored graph, a shape and
a query from missing each other. The patterns are built from fragments, so this module's own text
names no banned literal.

**The match.** Two rules, over the same files as the old-name guard.

- A closed list of the project's namespace prefixes, ``NAMESPACE_PREFIXES``, spelled with the
  ``https`` scheme anywhere.
- Any ``https`` IRI inside a ``@prefix`` or ``PREFIX`` declaration.

**The exceptions, a named list and nothing else.**

- External vocabularies whose own namespace is ``https`` (the MOD vocabulary the Core copy cites).
- Web URLs and plain links: the documentation sites, the tracker, the code host, a store URL, a
  forwarded page URL. None is in the closed list, so none matches.
- Test data that must cover the ``https`` scheme of IRI syntax: none of it uses a listed prefix.
- The released sections of ``CHANGELOG.md``.
"""

from __future__ import annotations

import re

from tests._text_files import REPO_ROOT, text_files, unreleased_changelog_lines

# Built from fragments, so that no line of this shipped file holds a banned literal.
_SCHEME = "https" + ":" + "//"
_CRC = "www.sfb1574.kit.edu/ontologies/"
_CORE = "w3id.org/circularfactory/"

NAMESPACE_PREFIXES = (
    _CORE + "Core",
    _CORE + "Service",
    _CORE + "MES",
    _CORE + ".well-known",
    _CRC + "TransferUnit#",
    _CRC + "TransferUnitInstances#",
    _CRC + "CrcInterfaces#",
    _CRC + "Factory#",
    _CRC + "FactoryInstances#",
    "example.org/test#",
    "example.org/tui#",
    "example.org/tu#",
    "example.org/kapps-demo#",
    "example.org/other#",
    "example.org/shipped-stack#",
    "example.org/factory#",
    "example.org/ksm402#",
    "example.org/ksm468#",
    "example.org/owlim#",
    "example.org/shapes#",
    "example.org/line#",
    "example.org/line-instances#",
)

LISTED = re.compile(
    re.escape(_SCHEME)
    + "(?:"
    + "|".join(re.escape(p) for p in NAMESPACE_PREFIXES)
    + ")"
)
DECLARED = re.compile(
    r"^\s*(?:@prefix|PREFIX)\b[^\n]*" + re.escape(_SCHEME), re.IGNORECASE
)


def _is_offence(line: str) -> bool:
    return bool(LISTED.search(line) or DECLARED.search(line))


def _offences() -> list[str]:
    found = []
    for path in text_files():
        relative = path.relative_to(REPO_ROOT)
        lines = path.read_text(encoding="utf-8").splitlines()
        if relative.parts == ("CHANGELOG.md",):
            # Released sections are exempt; the Unreleased section is checked.
            lines = unreleased_changelog_lines()
        for number, line in enumerate(lines, 1):
            if _is_offence(line):
                found.append(f"{relative}:{number}: {line.strip()}")
    return found


def test_the_pattern_hits_a_listed_namespace_and_a_prefix_declaration() -> None:
    assert _is_offence("<" + _SCHEME + _CORE + "Core#Operation>")
    assert _is_offence('EX = "' + _SCHEME + "example.org/test#" + '"')
    assert _is_offence("@prefix ex: <" + _SCHEME + "elsewhere.org/ns#> .")
    assert _is_offence("PREFIX ex: <" + _SCHEME + "elsewhere.org/ns#>")


def test_the_pattern_misses_the_named_exceptions() -> None:
    assert not _is_offence("<" + _SCHEME + "w3id.org/mod#createdWith>")
    assert not _is_offence(_SCHEME + "store.example.org")
    assert not _is_offence("[c](" + _SCHEME + "example.org/)")
    assert not _is_offence(_SCHEME + "gitlab." + "kit.edu/kit/ifl")
    assert not _is_offence("<http" + "://" + _CORE + "Core#Operation>")


def test_no_shipped_file_or_test_spells_an_ontology_namespace_with_https() -> None:
    found = _offences()
    assert not found, "\n".join(found)
