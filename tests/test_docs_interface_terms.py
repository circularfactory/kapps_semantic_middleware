"""The guide's table of interface terms and the ``INF`` vocabulary class name the same terms."""

import re
from pathlib import Path

from kapps_semantic_middleware.vocabulary import INF, INF_NS

_ROOT = Path(__file__).parent.parent
_GUIDE = _ROOT / "docs" / "site" / "guide" / "08-provisioning-and-seeding.md"
_REFERENCE = _ROOT / "docs" / "site" / "reference" / "vocabulary.md"
_HEADING = "## The Interface Terms a Deployment Must Hold"


def _section() -> str:
    text = _GUIDE.read_text(encoding="utf-8")
    start = text.index(_HEADING)
    end = text.find("\n## ", start + 1)
    if end == -1:
        end = len(text)
    return text[start:end]


def _class_terms() -> set[str]:
    return {
        name
        for name, value in vars(INF).items()
        if not name.startswith("_") and str(value).startswith(INF_NS)
    }


def _table_terms() -> list[str]:
    return re.findall(r"^\| `inf:(\w+)` \|", _section(), flags=re.MULTILINE)


def test_the_table_names_each_term_of_the_inf_class_once() -> None:
    names = _table_terms()
    assert len(names) == len(set(names)), "a term appears twice in the table"
    assert set(names) == _class_terms()


def test_the_section_gives_the_namespace_iri_in_full() -> None:
    assert INF_NS in _section()


def test_the_vocabulary_reference_links_to_the_section() -> None:
    link = "<interface-terms-a-deployment-must-hold>"
    assert link in _REFERENCE.read_text(encoding="utf-8")


def test_the_text_names_no_ticket_number() -> None:
    assert not re.search(r"\b[A-Z]{2,4}[ -]\d+\b", _section())
