"""The shipped operation-status shape, against a live store: every Operation has exactly one status.

The session sets the shipped shapes (``conftest.store``), and the scenario seed sets them again.
The store's own SHACL engine refuses at commit an Operation without a status, one with two
statuses, one with a value that is not one of the four, and an Operation of a subclass without a
status; it admits an Operation with one valid status. A middleware commit that would leave two
statuses on an Operation is refused too, and the refusal shows in ``mw.refused_writes`` and in the
log. Skipped when the TRIPLESTORE_* variables are absent (see conftest).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from kapps_ogm import OGM, Update
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import ShaclValidationError
from rdflib import Literal
from rdflib.namespace import RDF

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.validation_report import validation_results
from kapps_semantic_middleware.vocabulary import CFC, SVC, SVC_NS, OperationStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

EX = "http://example.org/test#"
TRANSPORT = IRI(f"{EX}Transport")
STATUS_RULE = f"{SVC_NS}OperationStatusShape"

_TBOX = f"""
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<{TRANSPORT}> a owl:Class ; rdfs:subClassOf <{CFC.Operation}> .
"""


@pytest.fixture
def seeded(store):
    """The hello scenario, seeded with the shipped shapes, and a subclass of the Operation."""
    seed.seed_scenario1(store)
    store.graph_add(_TBOX, content_type="application/x-turtle")
    return store


def _refused_by_the_status_rule(store, triples: list, operation: IRI, cls: IRI) -> None:
    """``triples`` are refused, by the shipped status rule at ``operation``, and none is stored."""
    with pytest.raises(ShaclValidationError) as refusal:
        store.triples_add(triples)
    results = {(r.focus, r.shape) for r in validation_results(refusal.value.report)}
    assert (str(operation), STATUS_RULE) in results
    assert not store.triple_exists((operation, RDF.type, cls))


def test_an_operation_without_a_status_is_refused(seeded):
    operation = IRI(f"{EX}operation_without_status")
    _refused_by_the_status_rule(
        seeded, [(operation, RDF.type, CFC.Operation)], operation, CFC.Operation
    )


def test_an_operation_with_two_statuses_is_refused(seeded):
    operation = IRI(f"{EX}operation_with_two_statuses")
    _refused_by_the_status_rule(
        seeded,
        [
            (operation, RDF.type, CFC.Operation),
            (operation, SVC.hasOperationStatus, Literal(OperationStatus.QUEUED)),
            (operation, SVC.hasOperationStatus, Literal(OperationStatus.RUNNING)),
        ],
        operation,
        CFC.Operation,
    )


def test_an_operation_with_a_fifth_value_is_refused(seeded):
    operation = IRI(f"{EX}operation_paused")
    _refused_by_the_status_rule(
        seeded,
        [
            (operation, RDF.type, CFC.Operation),
            (operation, SVC.hasOperationStatus, Literal("paused")),
        ],
        operation,
        CFC.Operation,
    )


def test_an_operation_of_a_subclass_without_a_status_is_refused(seeded):
    operation = IRI(f"{EX}transport_without_status")
    _refused_by_the_status_rule(seeded, [(operation, RDF.type, TRANSPORT)], operation, TRANSPORT)


def test_an_operation_with_one_valid_status_is_admitted(seeded):
    operation = IRI(f"{EX}operation_queued")
    seeded.triples_add(
        [
            (operation, RDF.type, TRANSPORT),
            (operation, SVC.hasOperationStatus, Literal(OperationStatus.QUEUED)),
        ]
    )
    assert seeded.triple_exists((operation, SVC.hasOperationStatus, Literal("queued")))


def test_a_middleware_commit_that_leaves_two_statuses_is_refused_recorded_and_logged(
    seeded, caplog
):
    operation = IRI(f"{EX}operation_appended")
    seeded.triples_add(
        [
            (operation, RDF.type, CFC.Operation),
            (operation, SVC.hasOperationStatus, Literal(OperationStatus.QUEUED)),
        ]
    )
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=seeded),
        host="127.0.0.1",
        port=8993,
        heartbeat_interval=None,
    )
    # The append: the queued status stays, and running is written beside it.
    both = [
        Update(
            instance_iri=operation,
            data={str(SVC.hasOperationStatus): [OperationStatus.QUEUED, OperationStatus.RUNNING]},
        )
    ]

    with caplog.at_level(logging.INFO, logger="kapps_semantic_middleware"):
        with pytest.raises(ShaclValidationError):
            with mw.commit(operation, entry=both):
                pytest.fail("the body of a refused entry ran")

    (recorded,) = mw.refused_writes
    assert recorded.verb == "commit"
    assert recorded.rules == ("OperationStatusShape at operation_appended",)
    assert any(
        "OperationStatusShape at operation_appended" in record.getMessage()
        for record in caplog.records
    )
    statuses = [str(t[2]) for t in seeded.triples_get(sub=operation, pred=SVC.hasOperationStatus)]
    assert statuses == [OperationStatus.QUEUED]
