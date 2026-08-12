"""Shared seeding helpers for knowledge-graph initialization.

These functions load the published/general ontology modules (Core, svc:,
mes:) into named graphs, and clear the default graph. Both the example
scenarios and the multi-process factory demo use this library code, so it
lives here rather than in examples/.
"""

# ADR: 0030

from __future__ import annotations

from importlib import resources

from kapps_triplestore_interface import IRI

from kapps_semantic_middleware.vocabulary import CORE_ONTOLOGY, MES_ONTOLOGY, SVC_ONTOLOGY

# Named-graph IRIs for seeding — same terms as vocabulary.py's *_ONTOLOGY constants (ADR 0021),
# aliased here because "graph to load ontology X into" is this module's own concern.
CORE_GRAPH = CORE_ONTOLOGY
SERVICE_GRAPH = SVC_ONTOLOGY
MES_GRAPH = MES_ONTOLOGY


def _read_core_ontology() -> str:
    """Return the vendored cfc: Core ontology Turtle.

    A verbatim copy of the published Core (version 0.9.0,
    https://circularfactory.github.io/Core/latest/ontology.ttl). Core is external and
    superior: imported and specialized, never modified. It is vendored rather than fetched,
    so seeding stays reproducible offline and a scenario stays self-contained.
    """
    # ADR: 0012, examples 0001
    return (
        resources.files("kapps_semantic_middleware")
        .joinpath("ontology", "core.ttl")
        .read_text(encoding="utf-8")
    )


def _read_service_ontology() -> str:
    """Return the packaged svc: ontology Turtle."""
    return (
        resources.files("kapps_semantic_middleware")
        .joinpath("ontology", "service.ttl")
        .read_text(encoding="utf-8")
    )


def _read_mes_ontology() -> str:
    """Return the packaged mes: ontology Turtle (handover-ability vocabulary)."""
    return (
        resources.files("kapps_semantic_middleware")
        .joinpath("ontology", "mes.ttl")
        .read_text(encoding="utf-8")
    )


def clear_repository(db) -> None:
    """Clear the repository's default graph (authorized test repo).

    Always run this before seeding, so a scenario never accumulates residual
    triples from a previous run or a different scenario.
    """
    db.clear_graph()


def _graph_is_populated(db, graph_iri: IRI) -> bool:
    """Whether a named graph already holds any statement."""
    return bool(
        db.query(f"ASK {{ GRAPH <{graph_iri}> {{ ?s ?p ?o }} }}").get("boolean", False)
    )


def load_shared_ontologies(db, *, reload: bool = False) -> None:
    """Load the published/general ontology modules, one named graph per ontology.

    Every scenario needs Core, svc:, and mes:, and they never change during
    a run. Each gets its own named graph. It is not re-imported into the
    default graph on every seed. The clear_repository function clears only
    the default graph. So a seed wipes the scenario's own data, and leaves
    these standing. GraphDB reasons across all graphs, so a domain class
    still resolves its cfc: superclass from here.

    The operation is idempotent. The function skips a present module unless you set reload.
    """
    for graph, turtle in (
        (CORE_GRAPH, _read_core_ontology()),
        (SERVICE_GRAPH, _read_service_ontology()),
        (MES_GRAPH, _read_mes_ontology()),
    ):
        if reload:
            db.clear_graph(graph)
        elif _graph_is_populated(db, graph):
            continue
        db.import_statements(
            turtle, graph_iri=graph, content_type="application/x-turtle"
        )
