"""Pure-logic unit tests for SHACL shape generation from Python type hints.

These are the one part of the middleware with no graph interaction, so they run
without a live GraphDB (unlike the integration tests). See
src/kapps_semantic_middleware/shacl_interop/ and its ADR.
"""

from __future__ import annotations

from typing import Optional

from rdflib import URIRef
from rdflib.namespace import RDF

from kapps_semantic_middleware.shacl_interop import build_workflow_shape
from kapps_semantic_middleware.shacl_interop.shape_from_typehints import SH
from kapps_semantic_middleware.vocabulary import SVC

WF = "https://example.org/kapps-demo#HelloWorldWorkflow"


def _hello_world() -> None:
    return None


def _move(distance: float, fast: bool = False, note: Optional[str] = None) -> bool:
    return True


def test_zero_arg_none_return_is_minimal_shape():
    """A zero-arg, ``-> None`` workflow yields only NodeShape + targetClass."""
    g = build_workflow_shape(_hello_world, WF)
    shape = URIRef(WF + "Shape")
    assert (shape, RDF.type, SH.NodeShape) in g
    assert (shape, SH.targetClass, URIRef(WF)) in g
    # No precondition/outcome for the empty case.
    assert (shape, URIRef(str(SVC.precondition)), None) not in g
    assert (shape, URIRef(str(SVC.outcome)), None) not in g
    assert len(g) == 2


def test_targets_class_not_instance():
    g = build_workflow_shape(_hello_world, WF)
    # sh:targetClass points at the workflow class; there is no sh:targetNode.
    assert (None, SH.targetClass, URIRef(WF)) in g
    assert (None, SH.targetNode, None) not in g


def test_args_and_return_populate_precondition_and_outcome():
    move_cls = "https://example.org/kapps-demo#MoveWorkflow"
    g = build_workflow_shape(_move, move_cls)
    ttl = g.serialize(format="turtle")
    assert "svc:precondition" in ttl
    assert "svc:outcome" in ttl
    assert "xsd:double" in ttl  # distance: float
    assert "xsd:boolean" in ttl  # fast / return: bool
    assert "xsd:string" in ttl  # note: Optional[str]


def test_only_required_params_get_mincount():
    """Only params without a default and not Optional are required (sh:minCount 1)."""
    move_cls = "https://example.org/kapps-demo#MoveWorkflow"
    g = build_workflow_shape(_move, move_cls)
    ttl = g.serialize(format="turtle")
    # distance is the only required parameter; fast has a default, note is Optional.
    assert ttl.count("sh:minCount") == 1


def test_explicit_shape_iri_is_used():
    shape_iri = "https://example.org/kapps-demo#CustomShape"
    g = build_workflow_shape(_hello_world, WF, shape_iri=shape_iri)
    assert (URIRef(shape_iri), SH.targetClass, URIRef(WF)) in g
