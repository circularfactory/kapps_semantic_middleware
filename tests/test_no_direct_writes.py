"""No code of the middleware calls a mutation operation of the store's access module, but seeding.

Every knowledge-graph write of the middleware goes through the OGM (`OGM.create`,
`OGM.commit`), which checks it against the ontology and sends it as one update. Only the seeding
module writes through the access module directly: it provisions a dataset (clears it and loads
the shared ontologies) before any service runs.

This parses every module under the package's source with `ast` and collects each call of an
attribute whose name is a mutation operation of the store's contract
(`kapps_triplestore_interface.TripleStore`), and each `query(...)` that may send a SPARQL update:
`update=` or a second positional argument with any value but a literal False, and, to be safe, a
`*args` or `**kwargs` argument. Every public member of the contract is classified as a read, a
mutation or `query`, so a new operation of the contract fails here until it is classified.

The scan covers attribute calls only (`<receiver>.<operation>(...)`). A call through `getattr`,
or through an alias of the bound method, is not seen. It reads the source only, without a store.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from kapps_triplestore_interface import TripleStore

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "kapps_semantic_middleware"

# The one module that may write through the access module: it provisions a dataset before any
# service runs.
SEEDING = {Path("seeding.py")}

# The operations of the store's contract that change the dataset.
MUTATIONS = {
    "triple_add",
    "triple_delete",
    "triples_add",
    "triples_delete",
    "triples_update",
    "graph_add",
    "graph_clear",
    "prefix_set",
    "prefix_delete",
    "shapes_set",
}

# The operations of the store's contract that only read, or close the client.
READS = {
    "close",
    "dataset",
    "graph_get",
    "iri_exists",
    "is_subclass",
    "new_iri",
    "owl_get_classes_of_individual",
    "prefixes_get",
    "triple_exists",
    "triples_get",
    # Checks content against shapes and answers; it changes no statement.
    "validate",
}

# Reads, or writes when it is given `update`.
QUERY = "query"


def _is_false(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and value.value is False


def _sends_an_update(call: ast.Call) -> bool:
    """Whether `call` may send a SPARQL update: `update=`, or a second positional argument, with
    any value but a literal False, or a `*` or `**` argument that may carry it."""
    if any(isinstance(arg, ast.Starred) for arg in call.args):
        return True
    if len(call.args) >= 2 and not _is_false(call.args[1]):
        return True
    return any(
        keyword.arg is None or (keyword.arg == "update" and not _is_false(keyword.value))
        for keyword in call.keywords
    )


def direct_writes(tree: ast.AST) -> list[str]:
    """Every call of a mutation operation in `tree`, as `<line>: <method>`."""
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        method = node.func.attr
        if method in MUTATIONS or (method == QUERY and _sends_an_update(node)):
            found.append(f"{node.lineno}: {method}")
    return sorted(found, key=lambda entry: int(entry.split(":")[0]))


def _modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_every_operation_of_the_store_contract_is_classified():
    """The names follow the contract: a new, renamed or removed operation fails here until it is
    classified as a read or a mutation."""
    contract = {name for name in dir(TripleStore) if not name.startswith("_")}
    assert not MUTATIONS & READS
    # `validate` need not be a member: the pinned release of the interface may still lack it.
    assert contract - MUTATIONS - READS - {QUERY} == set()
    assert (MUTATIONS | READS | {QUERY}) - contract - {"validate"} == set()


def test_the_source_is_found():
    modules = {path.relative_to(PACKAGE) for path in _modules()}
    assert {Path("registration.py"), Path("middleware.py"), *SEEDING} <= modules


@pytest.mark.parametrize("path", _modules(), ids=lambda path: str(path.relative_to(PACKAGE)))
def test_no_module_writes_past_the_ogm(path):
    """Every graph write of a service goes through the OGM. Only the seeding module, which
    provisions a dataset before any service runs, writes directly."""
    if path.relative_to(PACKAGE) in SEEDING:
        pytest.skip("seeding provisions a dataset before any service runs")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert direct_writes(tree) == []


def test_a_planted_mutation_outside_seeding_is_found():
    """The scan proves itself on a module that writes past the OGM, parsed as a module of the
    package is."""
    planted = "def link(ogm, s, p, o):\n    ogm.db.triple_add((s, p, o))\n"
    assert direct_writes(ast.parse(planted, filename="planted.py")) == ["2: triple_add"]


def test_seeding_is_where_the_direct_writes_are():
    """The allowlist is not stale: seeding does write through the access module."""
    tree = ast.parse((PACKAGE / "seeding.py").read_text(encoding="utf-8"))
    assert direct_writes(tree) != []


@pytest.mark.parametrize(
    ("code", "found"),
    [
        ("ogm.db.triple_add((s, p, o), named_graph=g)", ["1: triple_add"]),
        ("store.graph_clear()", ["1: graph_clear"]),
        ("self._db.triples_update(old_triples=a, new_triples=b)", ["1: triples_update"]),
        ("db.shapes_set('', 'text/turtle')", ["1: shapes_set"]),
        ("ogm.commit(instance_iri=x, data={})", []),
        ("ogm.create(class_iri=c, data={})", []),
        ("ogm.db.triples_get(sub=s)", []),
        ("db.query('SELECT ?s WHERE { ?s ?p ?o }')", []),
        ("db.query('DELETE WHERE { ?s ?p ?o }', update=True)", ["1: query"]),
        ("db.query(text, update=flag)", ["1: query"]),
        ("db.query('ASK {}', update=False)", []),
        ("db.query(text, True)", ["1: query"]),
        ("db.query(text, False)", []),
        ("db.query(text, False, True)", []),
        ("db.query(text, **options)", ["1: query"]),
        ("db.query(*arguments)", ["1: query"]),
        ("db.query(text, convert_bindings=True)", []),
    ],
)
def test_a_direct_write_is_told_from_an_ogm_write_and_a_read(code, found):
    assert direct_writes(ast.parse(code)) == found
