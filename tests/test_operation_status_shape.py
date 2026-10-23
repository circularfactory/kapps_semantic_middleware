"""The shipped operation-status shape, read offline.

The middleware ships a SHACL shapes file beside its service ontology. It holds one rule: every
Operation has exactly one status, one of the four values of ``OperationStatus``. The middleware
has no SHACL validator of its own, so these tests read the shape as RDF and check what it says;
the store's refusals are checked live (``test_operation_status_shape_integration.py``).

The seeding helper that sets the shape on a store is checked here against a fake store.
"""

from __future__ import annotations

import logging
from importlib import resources

import pytest
from kapps_triplestore_interface import TripleStoreNotSupportedError
from rdflib import Graph, Literal
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SH

from kapps_semantic_middleware.seeding import read_service_shapes, set_service_shapes
from kapps_semantic_middleware.vocabulary import CFC, SVC, OperationStatus


def _shapes() -> Graph:
    return Graph().parse(data=read_service_shapes(), format="turtle")


def test_the_shapes_file_holds_one_node_shape_targeting_the_core_operation():
    graph = _shapes()
    [node_shape] = list(graph.subjects(RDF.type, SH.NodeShape))
    assert list(graph.objects(node_shape, SH.targetClass)) == [CFC.Operation]


def test_the_status_has_exactly_one_value_of_the_four():
    graph = _shapes()
    [node_shape] = list(graph.subjects(RDF.type, SH.NodeShape))
    [property_shape] = list(graph.objects(node_shape, SH.property))
    assert list(graph.subjects(RDF.type, SH.PropertyShape)) == [property_shape]
    assert list(graph.objects(property_shape, SH.path)) == [SVC.hasOperationStatus]
    assert list(graph.objects(property_shape, SH.minCount)) == [Literal(1)]
    assert list(graph.objects(property_shape, SH.maxCount)) == [Literal(1)]
    [values] = list(graph.objects(property_shape, SH["in"]))
    assert {str(value) for value in Collection(graph, values)} == set(OperationStatus.ALL)
    assert len(list(Collection(graph, values))) == 4


def test_the_shapes_file_holds_exactly_that_rule():
    """No datatype, no closed shape, no functional-property flag: the rule and nothing else."""
    graph = _shapes()
    predicates = {p for _, p, _ in graph}
    assert predicates <= {
        RDF.type, RDF.first, RDF.rest,
        SH.targetClass, SH.property, SH.path, SH.minCount, SH.maxCount, SH["in"],
    }
    assert (None, RDF.type, OWL.FunctionalProperty) not in graph


def test_the_shapes_file_has_no_labelled_blank_node():
    """A test joins its own shapes with the shipped ones; a labelled blank node could merge."""
    assert "_:" not in read_service_shapes()


def test_the_service_ontology_states_the_rule_on_the_status():
    ontology = Graph().parse(
        data=resources.files("kapps_semantic_middleware")
        .joinpath("ontology", "service.ttl")
        .read_text(encoding="utf-8"),
        format="turtle",
    )
    [comment] = [str(c) for c in ontology.objects(SVC.hasOperationStatus, RDFS.comment)]
    assert "exactly one" in comment
    assert "in the commit that creates the Operation" in comment
    assert "replaced atomically" in comment
    assert (SVC.hasOperationStatus, RDF.type, OWL.FunctionalProperty) not in ontology


class _Store:
    """The part of a store that ``set_service_shapes`` uses."""

    def __init__(self, supported: bool = True):
        self.supported = supported
        self.dataset = "demo"
        self.sent: list[tuple[str, str]] = []

    def shapes_set(self, data: str, content_type: str) -> None:
        if not self.supported:
            raise TripleStoreNotSupportedError("the dataset has no SHACL validation")
        self.sent.append((data, content_type))


def test_the_seed_helper_sets_the_shipped_shapes():
    store = _Store()
    assert set_service_shapes(store) is True
    assert store.sent == [(read_service_shapes(), "text/turtle")]


def test_a_dataset_without_shacl_gets_one_readable_line_and_the_seed_goes_on(caplog):
    store = _Store(supported=False)
    with caplog.at_level(logging.WARNING, logger="kapps_semantic_middleware.seeding"):
        assert set_service_shapes(store) is False
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "\n" not in message
    assert "'demo'" in message
    assert "not checked" in message


def test_a_store_that_does_not_conform_is_not_swallowed():
    """Only the not-supported refusal is forgiven; anything else raises."""

    class _Refusing(_Store):
        def shapes_set(self, data, content_type):
            raise RuntimeError("the existing data does not conform")

    with pytest.raises(RuntimeError):
        set_service_shapes(_Refusing())
