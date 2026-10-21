"""The core class and registration name no possession term: possession is an extension.

The handover, its preconditions and the possession helpers live in
`kapps_semantic_middleware.possession`, which is built on the middleware's public surface
(`mw.commit()`, `mw.ogm`, `mw.resource`, `mw.mode`). `middleware.py` and `registration.py` neither
import it nor speak of possession or of the `mes:` handover abilities, outside comments. A
docstring counts: it is part of what the module says.

Two deprecated aliases are exempt: the method `SemanticMiddleware.handover`, which names the
extension's handover and imports it when it is called, and the `__getattr__` of `registration`,
which resolves the old names of the moved helpers and errors and imports their new home on use.
The exemption covers these two functions and nothing else, and it goes with the aliases.

This reads the sources only, without a store.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
import tokenize
import warnings
from pathlib import Path

import pytest

from kapps_semantic_middleware import SemanticMiddleware, errors, possession, registration

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "kapps_semantic_middleware"
CORE = ["middleware.py", "registration.py"]
POSSESSION = "kapps_semantic_middleware.possession"
# The deprecated aliases, by module and qualified name: the only exemptions.
ALIASES = [
    ("middleware.py", "SemanticMiddleware.handover"),
    ("registration.py", "__getattr__"),
]

# A possession term: any form of "possess" (possession, possessor, PossessionSwitchConflict), in
# any case, the `MES` namespace of the handover abilities, and a `mes:` IRI.
TERMS = (re.compile(r"possess", re.IGNORECASE), re.compile(r"\bMES\b|\bmes:"))


def _without_comments(source: str) -> str:
    """``source`` with every comment blanked, line numbers kept."""
    lines = source.splitlines(keepends=True)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            (row, start), (_, end) = token.start, token.end
            line = lines[row - 1]
            lines[row - 1] = line[:start] + " " * (end - start) + line[end:]
    return "".join(lines)


def _aliases(name: str, tree: ast.Module) -> list[ast.FunctionDef]:
    """The function nodes of the deprecated aliases in the module ``name``."""
    found = []
    for module, qualname in ALIASES:
        if module != name:
            continue
        *owners, function = qualname.split(".")
        body = tree.body
        for owner in owners:
            body = next(
                (n.body for n in body if isinstance(n, ast.ClassDef) and n.name == owner), []
            )
        found += [n for n in body if isinstance(n, ast.FunctionDef) and n.name == function]
    return found


def _without_alias(name: str, source: str) -> str:
    """``source`` with the lines of its deprecated aliases blanked, decorators included."""
    lines = source.splitlines(keepends=True)
    for alias in _aliases(name, ast.parse(source)):
        first = min([alias.lineno, *(d.lineno for d in alias.decorator_list)])
        for row in range(first - 1, alias.end_lineno or alias.lineno):
            lines[row] = "\n"
    return "".join(lines)


def _terms(source: str) -> list[str]:
    """Each line of ``source`` that names a possession term outside a comment."""
    return [
        f"{number}: {line.strip()}"
        for number, line in enumerate(_without_comments(source).splitlines(), 1)
        if any(term.search(line) for term in TERMS)
    ]


@pytest.mark.parametrize("name", CORE)
def test_the_module_names_no_possession_term_outside_comments(name):
    source = (PACKAGE / name).read_text(encoding="utf-8")
    assert _terms(_without_alias(name, source)) == []


@pytest.mark.parametrize("name", CORE)
def test_the_module_imports_nothing_of_the_possession_extension(name):
    tree = ast.parse(_without_alias(name, (PACKAGE / name).read_text(encoding="utf-8")))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = node.module or ""
            imported |= {source} | {f"{source}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
    assert sorted(name for name in imported if name.startswith(POSSESSION)) == []


@pytest.mark.parametrize("module, qualname", ALIASES)
def test_the_exemption_finds_each_alias(module, qualname):
    """An exemption that matches nothing would let the alias's lines through as they are."""
    source = (PACKAGE / module).read_text(encoding="utf-8")
    assert [a.name for a in _aliases(module, ast.parse(source))] == [qualname.split(".")[-1]]
    assert _terms(source) != []


def test_the_alias_warns_once_and_hands_its_arguments_to_the_extension(monkeypatch):
    """``SemanticMiddleware.handover`` is ``possession.handover`` for this instance."""
    calls = []
    returned = object()

    def successor(mw, **kwargs):
        calls.append((mw, kwargs))
        return returned

    monkeypatch.setattr(possession, "handover", successor)
    mw = object.__new__(SemanticMiddleware)
    arguments = dict(
        mode="mode",
        workpiece="workpiece",
        counterpart="counterpart",
        workpiece_update={"p": ["v"]},
        operation="op",
        entry=["change"],
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert SemanticMiddleware.handover(mw, **arguments) is returned

    assert [w.category for w in caught] == [DeprecationWarning]
    assert "kapps_semantic_middleware.possession.handover" in str(caught[0].message)
    assert caught[0].filename == __file__
    assert calls == [(mw, arguments)]


# The old names of the moved helpers and errors, and where each lives now.
MOVED = {
    "create_possession": possession,
    "switch_possession": possession,
    "find_possession_state": possession,
    "counterpart_has_complementary_ability": possession,
    "mint_possession_state_iri": possession,
    "HandoverPreconditionError": errors,
    "PossessionSwitchConflict": errors,
}


@pytest.mark.parametrize("name", sorted(MOVED))
def test_an_old_name_of_registration_warns_once_and_is_the_moved_object(name):
    home = MOVED[name]
    assert name not in vars(registration)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert getattr(registration, name) is getattr(home, name)

    assert [w.category for w in caught] == [DeprecationWarning]
    assert f"{home.__name__}.{name}" in str(caught[0].message)
    assert caught[0].filename == __file__


def test_an_import_of_an_old_name_warns_at_the_importing_line():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from kapps_semantic_middleware.registration import create_possession

    assert create_possession is possession.create_possession
    assert [w.category for w in caught] == [DeprecationWarning]
    assert caught[0].filename == __file__


def test_registration_has_no_other_name():
    with pytest.raises(AttributeError, match="no attribute 'handover'"):
        registration.handover  # noqa: B018


def test_importing_registration_does_not_import_the_extension():
    """The alias imports the extension on use; the import of the core alone does not."""
    code = (
        "import sys, kapps_semantic_middleware.registration; "
        f"print({POSSESSION!r} in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


# The check itself, on sources written for it.


def test_the_check_skips_a_comment_and_finds_a_docstring():
    assert _terms("x = 1  # the possession extension\n") == []
    assert _terms('"""The possession state."""\n') != []
    assert _terms("from kapps_semantic_middleware.vocabulary import MES\n") != []
    assert _terms("Q = 'ASK { ?r mes:hasHandoverAbility ?a }'\n") != []
