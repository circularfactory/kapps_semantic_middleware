"""No query text in KSM's Python source names a store's explicit or implicit pseudo-graph.

One store answers "asserted statements only" through two pseudo-graphs of its own, written into a
query as ``FROM <...>``. Another store reads such an IRI as an empty named graph, so the query returns
nothing and raises no error. KSM states the scope through the interface's query builder,
``SPARQLQuery(..., include_implicit=False)``, which writes the form its store understands.

This reads every string literal of the Python source under ``src`` and ``tests``, except
docstrings (prose), and fails when one names a pseudo-graph IRI. The IRIs are assembled below,
so that this module does not name them itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOTS = (REPO_ROOT / "src", REPO_ROOT / "tests")
PSEUDO_GRAPHS = tuple(
    "http://www.onto" + "text.com/" + name for name in ("explicit", "implicit")
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The ids of the string nodes that are docstrings."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _offences() -> list[str]:
    found: list[str] = []
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path == Path(__file__).resolve():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            docstrings = _docstring_nodes(tree)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and any(iri in node.value for iri in PSEUDO_GRAPHS)
                ):
                    found.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    return found


def test_no_query_text_names_a_pseudo_graph():
    offences = _offences()
    assert not offences, (
        "These string literals name a store's explicit or implicit pseudo-graph. State the scope "
        "with SPARQLQuery(..., include_implicit=False) instead:\n" + "\n".join(offences)
    )
