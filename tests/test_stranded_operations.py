"""The per-operation predicate of the sweep and the queue reconstruction, against an in-memory graph.

`find_stranded_operations` is one SPARQL query. These tests run that query, and the queries of the
sweep around it, on an rdflib graph, so that every case of the predicate is pinned without a store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from kapps_triplestore_interface import IRI
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

from kapps_semantic_middleware.registration import (
    _sweep_stale_services,
    deregister_service,
    find_stranded_operations,
)
from kapps_semantic_middleware.vocabulary import CFC, SVC, OperationStatus

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
THRESHOLD = 90.0
FRESH = NOW - timedelta(seconds=10)
STALE = NOW - timedelta(hours=1)

RESOURCE = IRI("http://example.org/test#Station")
MONITOR = IRI("http://example.org/test#Monitor")
CONTROLLER = IRI("http://example.org/test#Controller")
MONITOR_WORKFLOW = IRI("http://example.org/test#MonitorWorkflow")
CONTROLLER_WORKFLOW = IRI("http://example.org/test#ControllerWorkflow")
CAPABILITY = IRI("http://example.org/test#Drill")
OPERATION = IRI("http://example.org/test#Op1")
OTHER_OPERATION = IRI("http://example.org/test#Op2")


class _Db:
    """The ``ogm.db`` that answers a SELECT from an rdflib graph, in the store's JSON shape."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph

    def query(self, sparql: str, convert_bindings: bool = False) -> dict:
        result = self.graph.query(sparql)
        names = [str(v) for v in result.vars]
        rows = [
            {name: str(row[i]) for i, name in enumerate(names) if row[i] is not None}
            for row in result
        ]
        return {"results": {"bindings": rows}}

    def triples_get(self, *, pred: IRI, obj: IRI, named_graph: Optional[IRI] = None) -> list:
        return [
            (IRI(str(s)), pred, obj)
            for s in self.graph.subjects(URIRef(str(pred)), URIRef(str(obj)))
        ]


class _Ogm:
    """An OGM stand-in: the reads go to the graph, and a commit replaces the values it names."""

    def __init__(self) -> None:
        self.graph = Graph()
        self.db = _Db(self.graph)

    def commit(self, *, instance_iri: IRI, data: dict, named_graph: Optional[IRI] = None) -> None:
        for prop, values in data.items():
            self.graph.remove((URIRef(instance_iri), URIRef(prop), None))
            for value in values:
                self.graph.add((URIRef(instance_iri), URIRef(prop), Literal(value)))

    def add(self, subject: str, predicate: str, obj: Any) -> None:
        if not isinstance(obj, Literal):
            obj = URIRef(str(obj))
        self.graph.add((URIRef(str(subject)), URIRef(str(predicate)), obj))

    def service(self, iri: IRI, heartbeat: Optional[datetime], address: bool = True) -> None:
        self.add(iri, SVC.isServiceOf, RESOURCE)
        if address:
            self.add(iri, SVC.hasAddress, Literal("http://localhost:1"))
        if heartbeat is not None:
            self.add(iri, SVC.hasLastHeartbeat, Literal(heartbeat.isoformat(), datatype=XSD.dateTime))

    def realizes(self, workflow: IRI, service: IRI, capability: IRI = CAPABILITY) -> None:
        self.add(workflow, SVC.isWorkflowOf, service)
        self.add(capability, SVC.realizedByWorkflow, workflow)
        self.add(RESOURCE, CFC.hasCapability, capability)

    def operation(
        self,
        iri: IRI,
        status: str,
        executor: Optional[IRI] = None,
        capability: IRI = CAPABILITY,
    ) -> None:
        self.add(iri, CFC.implementsCapability, capability)
        self.add(iri, SVC.hasOperationStatus, Literal(status))
        if executor is not None:
            self.add(iri, SVC.executedByWorkflow, executor)

    def status(self, iri: IRI) -> str:
        return str(self.graph.value(URIRef(iri), URIRef(str(SVC.hasOperationStatus))))


def _stranded(ogm: _Ogm, *statuses: str, excluding: tuple = ()) -> list[IRI]:
    return find_stranded_operations(
        ogm,  # type: ignore[arg-type]
        RESOURCE,
        list(statuses) or [OperationStatus.QUEUED, OperationStatus.RUNNING],
        THRESHOLD,
        now=NOW,
        excluding=excluding,
    )


@pytest.fixture
def ogm() -> _Ogm:
    return _Ogm()


def test_a_queued_operation_whose_capability_a_live_service_realizes_is_not_stranded(ogm):
    ogm.service(CONTROLLER, FRESH)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.QUEUED)

    assert _stranded(ogm) == []


def test_a_queued_operation_with_no_live_realizing_service_is_stranded(ogm):
    ogm.service(CONTROLLER, STALE)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.QUEUED)

    assert _stranded(ogm) == [OPERATION]


def test_a_live_service_that_realizes_nothing_does_not_shield_a_queued_operation(ogm):
    ogm.service(MONITOR, FRESH)
    ogm.service(CONTROLLER, STALE)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.QUEUED)

    assert _stranded(ogm) == [OPERATION]


def test_a_service_without_an_address_is_not_live(ogm):
    ogm.service(CONTROLLER, FRESH, address=False)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.QUEUED)

    assert _stranded(ogm) == [OPERATION]


def test_a_running_operation_whose_executor_is_live_is_not_stranded(ogm):
    ogm.service(CONTROLLER, FRESH)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.RUNNING, executor=CONTROLLER_WORKFLOW)

    assert _stranded(ogm) == []


def test_a_running_operation_whose_executor_is_dead_is_stranded_despite_a_live_realizer(ogm):
    """The live sibling realizes the Capability but never runs an Operation that another claimed."""
    ogm.service(MONITOR, FRESH)
    ogm.realizes(MONITOR_WORKFLOW, MONITOR)
    ogm.service(CONTROLLER, STALE)
    ogm.add(CONTROLLER_WORKFLOW, SVC.isWorkflowOf, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.RUNNING, executor=CONTROLLER_WORKFLOW)

    assert _stranded(ogm) == [OPERATION]


def test_a_running_operation_with_no_recorded_executor_is_stranded(ogm):
    """A claim through a generic commit entry records no executor, so the sweep fails it as before."""
    ogm.service(CONTROLLER, FRESH)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.RUNNING)

    assert _stranded(ogm) == [OPERATION]


def test_the_excluded_service_never_counts_as_live(ogm):
    """A restarting instance names its own Service: its heartbeat may be fresh from before the crash."""
    ogm.service(CONTROLLER, FRESH)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.RUNNING, executor=CONTROLLER_WORKFLOW)

    assert _stranded(ogm, OperationStatus.RUNNING, excluding=(CONTROLLER,)) == [OPERATION]


def test_the_reconstruction_leaves_another_services_running_operation_alone(ogm):
    """A restarting monitor fails its own orphan but not the live controller's running Operation."""
    ogm.service(MONITOR, FRESH)  # fresh from before the crash
    ogm.add(MONITOR_WORKFLOW, SVC.isWorkflowOf, MONITOR)
    ogm.service(CONTROLLER, FRESH)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.RUNNING, executor=MONITOR_WORKFLOW)
    ogm.operation(OTHER_OPERATION, OperationStatus.RUNNING, executor=CONTROLLER_WORKFLOW)

    assert _stranded(ogm, OperationStatus.RUNNING, excluding=(MONITOR,)) == [OPERATION]


def test_the_status_filter_limits_the_operations_read(ogm):
    ogm.service(CONTROLLER, STALE)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.QUEUED)
    ogm.operation(OTHER_OPERATION, OperationStatus.DONE)

    assert _stranded(ogm) == [OPERATION]


def test_a_stale_service_does_not_drag_down_the_operations_of_its_live_sibling(ogm):
    """The sweep of a dead monitor fails nothing that the live controller still runs or realizes."""
    ogm.service(MONITOR, STALE)
    ogm.add(MONITOR_WORKFLOW, SVC.isWorkflowOf, MONITOR)
    ogm.service(CONTROLLER, FRESH)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.QUEUED)
    ogm.operation(OTHER_OPERATION, OperationStatus.RUNNING, executor=CONTROLLER_WORKFLOW)
    failed: list[IRI] = []

    swept = _sweep_stale_services(
        ogm,  # type: ignore[arg-type]
        THRESHOLD,
        now=NOW,
        failed=failed.append,
    )

    assert swept == [MONITOR]
    assert failed == []
    assert ogm.status(OPERATION) == OperationStatus.QUEUED
    assert ogm.status(OTHER_OPERATION) == OperationStatus.RUNNING


def test_the_sweep_fails_what_the_stale_service_alone_could_finish(ogm):
    ogm.service(MONITOR, FRESH)
    ogm.add(MONITOR_WORKFLOW, SVC.isWorkflowOf, MONITOR)
    ogm.service(CONTROLLER, STALE)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.QUEUED)
    ogm.operation(OTHER_OPERATION, OperationStatus.RUNNING, executor=CONTROLLER_WORKFLOW)
    failed: list[IRI] = []

    swept = _sweep_stale_services(
        ogm,  # type: ignore[arg-type]
        THRESHOLD,
        now=NOW,
        failed=failed.append,
    )

    assert swept == [CONTROLLER]
    assert sorted(failed) == sorted([OPERATION, OTHER_OPERATION])
    assert ogm.status(OPERATION) == OperationStatus.FAILED
    assert ogm.status(OTHER_OPERATION) == OperationStatus.FAILED


# The second pass: a Resource whose Services all stopped cleanly (no address) is found by its heartbeat.


def _unserved(ogm: _Ogm) -> list[IRI]:
    failed: list[IRI] = []
    _sweep_stale_services(ogm, THRESHOLD, now=NOW, failed=failed.append)  # type: ignore[arg-type]
    return failed


def _stopped_resource_with_work(ogm: _Ogm, heartbeat: datetime) -> None:
    ogm.service(CONTROLLER, heartbeat, address=False)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.QUEUED)
    ogm.operation(OTHER_OPERATION, OperationStatus.RUNNING, executor=CONTROLLER_WORKFLOW)


def test_the_sweep_fails_the_work_of_a_cleanly_stopped_resource_once_its_heartbeat_is_old(ogm):
    _stopped_resource_with_work(ogm, STALE)

    failed = _unserved(ogm)

    assert sorted(failed) == sorted([OPERATION, OTHER_OPERATION])
    assert ogm.status(OPERATION) == OperationStatus.FAILED
    assert ogm.status(OTHER_OPERATION) == OperationStatus.FAILED
    reason = str(ogm.graph.value(URIRef(OPERATION), URIRef(str(SVC.hasExecutionResult))))
    assert "no live service of the resource will finish it" in reason


def test_the_sweep_fails_nothing_of_a_stopped_resource_whose_heartbeat_is_inside_the_threshold(ogm):
    _stopped_resource_with_work(ogm, FRESH)

    failed = _unserved(ogm)

    assert failed == []
    assert ogm.status(OPERATION) == OperationStatus.QUEUED


def test_the_sweep_fails_nothing_of_a_restarted_service(ogm):
    ogm.service(CONTROLLER, FRESH)  # address and a fresh heartbeat again
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.QUEUED)

    failed = _unserved(ogm)

    assert failed == []


def test_the_new_pass_leaves_a_resource_with_a_live_sibling_alone(ogm):
    ogm.service(CONTROLLER, STALE, address=False)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.service(MONITOR, FRESH)
    ogm.operation(OPERATION, OperationStatus.QUEUED)
    ogm.operation(OTHER_OPERATION, OperationStatus.RUNNING)  # no recorded executor

    failed = _unserved(ogm)

    assert failed == []
    assert ogm.status(OTHER_OPERATION) == OperationStatus.RUNNING


def test_the_newest_heartbeat_of_two_stopped_services_counts(ogm):
    _stopped_resource_with_work(ogm, STALE)
    ogm.service(MONITOR, FRESH, address=False)

    failed = _unserved(ogm)

    assert failed == []
    assert ogm.status(OPERATION) == OperationStatus.QUEUED


def test_a_store_error_on_one_resource_is_logged_and_the_pass_goes_on(ogm, caplog, monkeypatch):
    other_resource = IRI("http://example.org/test#Other")
    other_service = IRI("http://example.org/test#OtherService")
    other_capability = IRI("http://example.org/test#Other_Cap")
    other_operation = IRI("http://example.org/test#OtherOp")
    _stopped_resource_with_work(ogm, STALE)
    ogm.add(other_service, SVC.isServiceOf, other_resource)
    ogm.add(other_service, SVC.hasLastHeartbeat, Literal(STALE.isoformat(), datatype=XSD.dateTime))
    ogm.add(other_resource, CFC.hasCapability, other_capability)
    ogm.operation(other_operation, OperationStatus.QUEUED, capability=other_capability)

    from kapps_semantic_middleware import registration

    real = registration.find_stranded_operations

    def flaky(ogm_, resource_iri, *args, **kwargs):
        if resource_iri == RESOURCE:
            raise registration.TripleStoreUnavailableError("store down")
        return real(ogm_, resource_iri, *args, **kwargs)

    monkeypatch.setattr(registration, "find_stranded_operations", flaky)
    failed: list[IRI] = []

    with caplog.at_level("WARNING"):
        _sweep_stale_services(ogm, THRESHOLD, now=NOW, failed=failed.append)  # type: ignore[arg-type]

    assert failed == [other_operation]
    assert ogm.status(OPERATION) == OperationStatus.QUEUED
    warnings = [r.getMessage() for r in caplog.records if str(RESOURCE) in r.getMessage()]
    assert len(warnings) == 1


def test_a_real_clean_stop_leaves_the_heartbeat_that_the_second_pass_reads(ogm):
    """Deregistration removes the address and keeps the heartbeat, so the stopped Resource is found."""
    ogm.service(CONTROLLER, STALE)
    ogm.realizes(CONTROLLER_WORKFLOW, CONTROLLER)
    ogm.operation(OPERATION, OperationStatus.QUEUED)
    ogm.operation(OTHER_OPERATION, OperationStatus.RUNNING, executor=CONTROLLER_WORKFLOW)
    deregister_service(ogm, CONTROLLER)  # type: ignore[arg-type]

    failed = _unserved(ogm)

    assert sorted(failed) == sorted([OPERATION, OTHER_OPERATION])
