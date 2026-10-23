"""The shapes a live test sets: the middleware's shipped shapes, joined with the test's own.

The session sets the shipped shapes once (``conftest.store``). The store's ``shapes_set``
replaces every shape of the dataset, so a test that needs a shape of its own sends it joined
with the shipped ones, and restores the shipped ones at its end, never an empty document.
"""

from __future__ import annotations

from rdflib import Graph

from kapps_semantic_middleware.seeding import read_service_shapes


def with_shipped_shapes(own: str) -> str:
    """The shipped shapes and ``own`` as one Turtle document.

    Merged as RDF, not joined as text, so a labelled blank node of one document cannot become
    a node of the other.
    """
    graph = Graph()
    graph.parse(data=read_service_shapes(), format="turtle")
    graph.parse(data=own, format="turtle")
    return graph.serialize(format="turtle")


def restore_shipped_shapes(store) -> None:
    """Set the shipped shapes alone again, as the session set them."""
    store.shapes_set(read_service_shapes(), "text/turtle")
