"""`issue_operation()` and `claim_next()`, against a live store: the Operation that a request issues,
the feedback that its issuer gets after the claim and after the end, and the claim's commits.

The OGM is the real one, subclassed only to record each write it makes, so "one commit" is
counted at the seam that `mw.commit()` writes through. A claim is refused by the store's own SHACL
engine. Skipped when the TRIPLESTORE_* variables are absent (see conftest).
A test that needs a shape sets it with the store's `shapes_set`, which replaces every shape
of the dataset, and removes every shape again at its end.
"""

from __future__ import annotations

import contextlib
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest
import uvicorn
from kapps_ogm import OGM, Create, Update
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import (
    PreconditionFailedError,
    TripleStoreInterfaceError,
)
from rdflib.namespace import RDF

from _explicit import explicit_select
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import triplestore_for
from kapps_semantic_middleware.errors import NotificationUndelivered
from kapps_semantic_middleware.registration import (
    create_operation,
    mint_capability_iri,
    mint_operation_iri,
    mint_service_iri,
    mint_workflow_iri,
    register_service,
    register_workflow,
)
from kapps_semantic_middleware.vocabulary import CFC, OperationStatus, SVC

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

# A property an Operation's draft can carry: scenario 1 declares none of its own.
TORQUE_LIMIT = IRI("http://example.org/test#torqueLimit")
_TORQUE_LIMIT_DECLARATION = f"""
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{TORQUE_LIMIT}> a owl:DatatypeProperty ;
    rdfs:domain <{CFC.Operation}> ;
    rdfs:range xsd:string .
"""


def _status_shape(*allowed: str) -> str:
    """A shape that admits only the given values of an Operation's status."""
    values = " ".join(f'"{value}"' for value in allowed)
    return f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
<urn:test:StatusShape> a sh:NodeShape ;
    sh:targetClass <{CFC.Operation}> ;
    sh:property <urn:test:StatusValues> .
<urn:test:StatusValues> a sh:PropertyShape ;
    sh:path <{SVC.hasOperationStatus}> ;
    sh:in ( {values} ) .
"""


def hello_world() -> str:
    return "hello world"


class _RecordingOGM(OGM):
    """The real OGM, recording each write it makes: a commit as its changes, or a create."""

    def __init__(self, db) -> None:
        super().__init__(db=db)
        self.writes: list[tuple] = []

    def commit(self, **kwargs):  # type: ignore[override]
        result = super().commit(**kwargs)
        self.writes.append(("commit", kwargs.get("changes")))
        return result

    def create(self, **kwargs):  # type: ignore[override]
        result = super().create(**kwargs)
        self.writes.append(("create", kwargs.get("instance_iri")))
        return result


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _hello_resource(store, port: int) -> SemanticMiddleware:
    """The hello resource: the receiver, with its workflow and the built-in event trigger."""
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=_RecordingOGM(triplestore_for(store.dataset)),
        host="127.0.0.1",
        port=port,
        heartbeat_interval=None,
    )
    mw.workflow(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
    )(hello_world)
    return mw


def _planner(store) -> SemanticMiddleware:
    """The planner: the caller that dispatches. A test serves it only to hear of its Operations."""
    return SemanticMiddleware(
        mode="resource",
        resource_iri=seed.PLANNER_RESOURCE,
        service_class=seed.PLANNER_SERVICE_CLASS,
        ogm=_RecordingOGM(triplestore_for(store.dataset)),
        host="127.0.0.1",
        port=_free_port(),
        heartbeat_interval=None,
    )


@contextlib.contextmanager
def _served(mw: SemanticMiddleware) -> Iterator[None]:
    config = uvicorn.Config(mw.app, host="127.0.0.1", port=mw.port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("server did not start in time")
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=20)


def _seed(store) -> None:
    seed.seed_scenario1(store)
    store.graph_add(_TORQUE_LIMIT_DECLARATION, content_type="application/x-turtle")


def _register_receiver_at(store, address: str) -> None:
    """The hello resource's Service and Workflow at ``address``, as if it had started there."""
    ogm = OGM(db=store)
    service = mint_service_iri(seed.HELLO_RESOURCE, address)
    register_service(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=service,
        service_class=seed.HELLO_SERVICE_CLASS,
        address=address,
    )
    register_workflow(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=service,
        workflow_iri=mint_workflow_iri(service, "hello_world"),
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
        capability_iri=mint_capability_iri(seed.HELLO_RESOURCE, "hello_world"),
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        endpoint=f"{address}/workflows/hello_world/execute",
    )


def _values(db, subject: IRI, predicate: IRI) -> list[str]:
    return sorted(str(t[2]) for t in db.triples_get(sub=subject, pred=predicate))


def _explicit_statements(db) -> set:
    """Every explicit statement of the default graph, to compare before and after."""
    result = db.query(
        explicit_select(["?s", "?p", "?o"], "?s ?p ?o"),
        convert_bindings=True,
    )
    return {(str(b["s"]), str(b["p"]), str(b["o"])) for b in result["results"]["bindings"]}


# --------------------------------------------------------------------------- #
# issue_operation()
# --------------------------------------------------------------------------- #


def test_a_dispatch_creates_its_operation_in_one_commit_and_its_receiver_queues_it(store):
    _seed(store)
    receiver = _hello_resource(store, _free_port())
    with _served(receiver):
        planner = _planner(store)
        with planner.issue_operation(
            capability_class=seed.HELLO_CAPABILITY_CLASS, operation_class=str(CFC.Operation)
        ) as op:
            op.data[str(TORQUE_LIMIT)] = ["4.2 Nm"]

        # The planner wrote once: one commit of one Create, which the receiver could fetch.
        assert [kind for kind, _ in planner.ogm.writes] == ["commit"]
        (creation,) = planner.ogm.writes[0][1]
        assert isinstance(creation, Create) and creation.instance_iri == op.iri
        assert op.iri in receiver._operation_queue

    assert store.triple_exists((op.iri, RDF.type, CFC.Operation))
    assert store.triple_exists(
        (op.iri, CFC.implementsCapability, mint_capability_iri(seed.HELLO_RESOURCE, "hello_world"))
    )
    assert _values(store, op.iri, SVC.hasOperationStatus) == [OperationStatus.QUEUED]
    assert _values(store, op.iri, TORQUE_LIMIT) == ["4.2 Nm"]
    # The issuer, the planner's Resource, written in the same commit.
    assert op.issuer == IRI(seed.PLANNER_RESOURCE)
    assert _values(store, op.iri, SVC.issuedBy) == [str(seed.PLANNER_RESOURCE)]


def test_a_failed_notification_leaves_the_operation_queued_and_raises_notification_undelivered(
    store,
):
    _seed(store)
    # A reachable-looking receiver: its address is a port where nothing listens.
    _register_receiver_at(store, f"http://127.0.0.1:{_free_port()}")
    planner = _planner(store)
    before = _explicit_statements(store)

    with pytest.raises(NotificationUndelivered) as raised:
        with planner.issue_operation(
            capability_class=seed.HELLO_CAPABILITY_CLASS, operation_class=str(CFC.Operation)
        ) as op:
            op.data[str(TORQUE_LIMIT)] = ["4.2 Nm"]

    assert isinstance(raised.value.__cause__, httpx.ConnectError)
    # The request's one commit, and nothing undone: the Operation stays, queued, the issuer's.
    assert [kind for kind, _ in planner.ogm.writes] == ["commit"]
    assert _values(store, op.iri, SVC.hasOperationStatus) == [OperationStatus.QUEUED]
    assert _values(store, op.iri, SVC.issuedBy) == [str(seed.PLANNER_RESOURCE)]
    assert _values(store, op.iri, TORQUE_LIMIT) == ["4.2 Nm"]
    added = _explicit_statements(store) - before
    assert added and all(subject == str(op.iri) for subject, _, _ in added)
    assert before <= _explicit_statements(store)


def test_a_dispatch_that_names_an_existing_operation_is_refused_and_leaves_it_untouched(store):
    """A refused creation notifies nobody and writes nothing: the Operation that the store
    already holds stays as it was."""
    _seed(store)
    _register_receiver_at(store, f"http://127.0.0.1:{_free_port()}")
    existing = mint_operation_iri(CFC.Operation)
    create_operation(
        OGM(db=store),
        operation_iri=existing,
        operation_class=CFC.Operation,
        capability_iri=mint_capability_iri(seed.HELLO_RESOURCE, "hello_world"),
        data={str(TORQUE_LIMIT): ["1.0 Nm"]},
    )
    before = _explicit_statements(store)

    with pytest.raises(PreconditionFailedError):
        with _planner(store).issue_operation(
            capability_class=seed.HELLO_CAPABILITY_CLASS,
            operation_class=str(CFC.Operation),
            operation_iri=str(existing),
        ) as op:
            op.data[str(TORQUE_LIMIT)] = ["4.2 Nm"]

    assert _explicit_statements(store) == before


def _claim_with_commit(mw: SemanticMiddleware, operation: IRI) -> None:
    """A claim and its end through ``mw.commit()``, as a participant without ``claim_next()``
    makes them."""

    def status(value: str) -> list:
        return [Update(instance_iri=operation, data={str(SVC.hasOperationStatus): [value]})]

    with mw.commit(entry=status(OperationStatus.RUNNING), exit=status(OperationStatus.DONE)):
        pass


def _claim_with_claim_next(mw: SemanticMiddleware, operation: IRI) -> None:
    with mw.claim_next() as claimed:
        assert claimed.iri == operation
        claimed.result = hello_world()


@pytest.mark.parametrize("claim", [_claim_with_claim_next, _claim_with_commit])
def test_the_issuer_hears_of_the_claim_and_of_the_end_and_its_handle_wakes(store, claim):
    """The planner is served, without a notification handler. The receiver claims the Operation
    and ends it, and its middleware notifies the planner after each commit. The planner's handle
    wakes, and the feedback is no work for the planner: its queue stays empty."""
    _seed(store)
    receiver = _hello_resource(store, _free_port())
    planner = _planner(store)
    with _served(receiver), _served(planner):
        with planner.issue_operation(
            capability_class=seed.HELLO_CAPABILITY_CLASS, operation_class=str(CFC.Operation)
        ) as op:
            pass
        assert op.wait_claimed(timeout=0.5) is False  # queued: nobody claimed it yet

        threading.Thread(target=claim, args=(receiver, op.iri), daemon=True).start()
        started = time.monotonic()
        assert op.wait_claimed(timeout=30) is True
        assert op.wait_ended(timeout=30) == OperationStatus.DONE
        assert time.monotonic() - started < 25  # woken, not timed out

        assert planner._operation_queue == []
    assert _values(store, op.iri, SVC.hasOperationStatus) == [OperationStatus.DONE]


# --------------------------------------------------------------------------- #
# claim_next()
# --------------------------------------------------------------------------- #


def _queued_operation(store) -> tuple[SemanticMiddleware, IRI]:
    """The hello resource registered at its address, and one queued Operation in its queue."""
    _seed(store)
    port = _free_port()
    _register_receiver_at(store, f"http://127.0.0.1:{port}")
    operation = mint_operation_iri(CFC.Operation)
    create_operation(
        OGM(db=store),
        operation_iri=operation,
        operation_class=CFC.Operation,
        capability_iri=mint_capability_iri(seed.HELLO_RESOURCE, "hello_world"),
    )
    mw = _hello_resource(store, port)
    mw._operation_queue.append(operation)  # as its event trigger does
    return mw, operation


def test_a_claim_is_one_commit_and_done_with_its_provenance_is_one_more(store):
    mw, operation = _queued_operation(store)
    with mw.claim_next() as claimed:
        assert _values(store, operation, SVC.hasOperationStatus) == [OperationStatus.RUNNING]
        claimed.result = hello_world()

    assert [kind for kind, _ in mw.ogm.writes] == ["commit", "commit"]
    assert _values(store, operation, SVC.hasOperationStatus) == [OperationStatus.DONE]
    workflow = mint_workflow_iri(mw.service_iri, "hello_world")
    assert store.triple_exists((operation, SVC.executedByWorkflow, workflow))
    assert store.triples_get(sub=operation, pred=SVC.executionTimestamp)
    assert _values(store, operation, SVC.executionResult) == ["hello world"]
    assert _values(store, operation, SVC.failureState) == []


def test_a_failed_body_is_one_commit_of_failed_with_its_provenance_and_the_datamodel(store):
    mw, operation = _queued_operation(store)
    with pytest.raises(RuntimeError, match="boom"):
        with mw.claim_next():
            raise RuntimeError("boom")

    assert [kind for kind, _ in mw.ogm.writes] == ["commit", "commit"]
    assert _values(store, operation, SVC.hasOperationStatus) == [OperationStatus.FAILED]
    workflow = mint_workflow_iri(mw.service_iri, "hello_world")
    assert store.triple_exists((operation, SVC.executedByWorkflow, workflow))
    assert _values(store, operation, SVC.executionResult) == ["boom"]
    (failure_state,) = _values(store, operation, SVC.failureState)
    assert json.loads(failure_state)["id"] == str(seed.HELLO_RESOURCE)


def test_a_claim_that_the_store_refuses_runs_no_body(store):
    mw, operation = _queued_operation(store)
    ran = []
    # The shape admits every status but running.
    store.shapes_set(_status_shape("queued", "done", "failed"), "text/turtle")
    try:
        with pytest.raises(TripleStoreInterfaceError, match="ValidationReport"):
            with mw.claim_next():
                ran.append(True)
    finally:
        store.shapes_set("", "text/turtle")

    assert ran == []
    assert mw.ogm.writes == []
    assert _values(store, operation, SVC.hasOperationStatus) == [OperationStatus.QUEUED]


def test_a_done_that_the_store_refuses_records_failed_with_its_provenance_and_raises(store):
    mw, operation = _queued_operation(store)
    # The shape admits every status but done.
    store.shapes_set(_status_shape("queued", "running", "failed"), "text/turtle")
    try:
        with pytest.raises(TripleStoreInterfaceError, match="ValidationReport") as refused:
            with mw.claim_next() as claimed:
                claimed.result = hello_world()
    finally:
        store.shapes_set("", "text/turtle")

    # Two writes reached the store: the claim, and failed in place of the refused done.
    assert [kind for kind, _ in mw.ogm.writes] == ["commit", "commit"]
    assert _values(store, operation, SVC.hasOperationStatus) == [OperationStatus.FAILED]
    workflow = mint_workflow_iri(mw.service_iri, "hello_world")
    assert store.triple_exists((operation, SVC.executedByWorkflow, workflow))
    assert store.triples_get(sub=operation, pred=SVC.executionTimestamp)
    assert _values(store, operation, SVC.executionResult) == [str(refused.value)]
    (failure_state,) = _values(store, operation, SVC.failureState)
    assert json.loads(failure_state)["id"] == str(seed.HELLO_RESOURCE)
