"""Event-trigger coordination end-to-end integration test (#13) against a live GraphDB.

Prove the decentralized event trigger over REST (ADR 0009). Resource A *dispatches*
an Operation to resource B. Create it in the graph (status ``queued``). Trigger B
event-trigger endpoint over HTTP. B enqueues it. Leave it ``queued``. Also cover
the atomic revert (ADR 0010). A dispatch trigger cannot be delivered. Remove the
created Operation. Skip when GRAPHDB_* env vars are absent (see conftest).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from rdflib.namespace import RDF

from kapps_ogm import OGM
from kapps_ogm.utils.class_scope import ClassScope
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import graphdb_env_present, graphdb_for
from kapps_semantic_middleware.registration import (
    OperationQueueEmpty,
    mint_capability_iri,
    mint_service_iri,
    mint_workflow_iri,
    register_service,
    register_workflow,
)
from kapps_semantic_middleware.vocabulary import CFC, OperationStatus, SVC

requires_graphdb = pytest.mark.skipif(
    not graphdb_env_present(),
    reason="GRAPHDB_* environment variables not set; skipping live-GraphDB integration test",
)

# examples/ is not a package. Add it to the path. Reuse the scenario seed helper.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402
from handlers import door_open, door_status, reset_door  # noqa: E402

B_PORT = 8995


def hello_world() -> str:
    """B domain workflow. The same trivial greeting as scenario 1."""
    return "hello world"


def _start_server(mw: SemanticMiddleware, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    config = uvicorn.Config(mw.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    t0 = time.time()
    while not server.started and time.time() - t0 < 30:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("server did not start in time")
    return server, thread


@requires_graphdb
def test_event_trigger_dispatch_enqueues_over_rest(graphdb):
    """A dispatches -> B event trigger over REST -> B enqueues -> Operation queued in the graph."""
    db = graphdb
    seed.seed_scenario1(db)

    cap_instance = mint_capability_iri(seed.HELLO_RESOURCE, "hello_world")

    # B: the hello resource. Expose helloworld + the built-in event trigger over REST.
    mw_b = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=B_PORT,
    )
    mw_b.workflow(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
    )(hello_world)

    server, thread = _start_server(mw_b, B_PORT)
    try:
        assert db.triples_get(sub=mw_b.service_iri, pred=SVC.address)

        # A: the planner. Dispatch an Operation for B capability (no hardcoded peer).
        mw_a = SemanticMiddleware(
            mode="resource",
            resource_iri=seed.PLANNER_RESOURCE,
            service_class=seed.PLANNER_SERVICE_CLASS,
            ogm=OGM(db=graphdb_for(graphdb.repository)),
            host="127.0.0.1",
            port=8996,
        )

        with mw_a.request(
            capability_class=seed.HELLO_CAPABILITY_CLASS,
            operation_class=str(CFC.Operation),
        ) as op:
            pass  # helloworld takes no arguments. Nothing to populate on the draft
        op_iri = op.iri

        # The dispatch created the Operation queued in the graph. Addressed via its Capability.
        assert db.triple_exists((op_iri, RDF.type, CFC.Operation))
        assert db.triple_exists((op_iri, CFC.implementsCapability, cap_instance))
        status = list(db.triples_get(sub=op_iri, pred=SVC.operationStatus))
        assert status and str(status[0][2]) == OperationStatus.QUEUED

        # The event trigger reached B over REST. B enqueued it (receiver queue state).
        assert op_iri in mw_b._operation_queue
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        time.sleep(0.5)


@requires_graphdb
def test_event_trigger_revert_on_undeliverable(graphdb):
    """A dispatch event trigger cannot be delivered. Revert the created Operation (ADR 0010)."""
    db = graphdb
    seed.seed_scenario1(db)

    # Seed a reachable-looking receiver. Address points at a dead port (no server is
    # started). Resolution succeeds. The event-trigger POST fails to connect.
    dead_address = "http://127.0.0.1:9"  # nothing listens on port 9
    dead_service = mint_service_iri(seed.HELLO_RESOURCE, dead_address)
    cap_instance = mint_capability_iri(seed.HELLO_RESOURCE, "hello_world")
    ogm = OGM(db=db)
    register_service(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=dead_service,
        service_class=seed.HELLO_SERVICE_CLASS,
        address=dead_address,
    )
    register_workflow(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=dead_service,
        workflow_iri=mint_workflow_iri(dead_service, "hello_world"),
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
        capability_iri=cap_instance,
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        endpoint="http://127.0.0.1:9/workflows/hello_world/execute",
    )

    mw_a = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.PLANNER_RESOURCE,
        service_class=seed.PLANNER_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=8997,
    )

    with pytest.raises(Exception):
        with mw_a.request(
            capability_class=seed.HELLO_CAPABILITY_CLASS,
            operation_class=str(CFC.Operation),
        ) as op:
            pass
    op_iri = op.iri

    # The Operation reverted. No type remains. No capability link remains. No status remains.
    assert not db.triple_exists((op_iri, RDF.type, CFC.Operation))
    assert not db.triple_exists((op_iri, CFC.implementsCapability, cap_instance))
    assert not db.triples_get(sub=op_iri, pred=SVC.operationStatus)


# --------------------------------------------------------------------------- #
# Pull-and-run (#14): claim the next queued Operation. Run the work. Record the
# terminal status + provenance atomically (ADR 0009 / 0010).
# --------------------------------------------------------------------------- #


def _hello_resource(graphdb, port: int) -> SemanticMiddleware:
    """A hello-resource middleware. Expose the helloworld workflow + the event trigger."""
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=port,
    )
    mw.workflow(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
    )(hello_world)
    return mw


def _dispatch_hello_op(graphdb) -> str:
    """Dispatch one hello-capability Operation from a planner middleware. Return its IRI."""
    mw_a = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.PLANNER_RESOURCE,
        service_class=seed.PLANNER_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=8996,
    )
    with mw_a.request(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        operation_class=str(CFC.Operation),
    ) as op:
        pass  # helloworld takes no arguments
    return op.iri


@requires_graphdb
def test_pull_and_run_completes_operation(graphdb):
    """Full loop: A dispatches -> B enqueues -> B pull-and-runs helloworld -> Operation done + provenance."""
    db = graphdb
    seed.seed_scenario1(db)
    mw_b = _hello_resource(graphdb, B_PORT)
    # Per-instance since ADR 0022. Derived from the instance. Not from the resource.
    wf_instance = mint_workflow_iri(mw_b.service_iri, "hello_world")
    server, thread = _start_server(mw_b, B_PORT)
    try:
        op_iri = _dispatch_hello_op(graphdb)  # queued on B via the REST event trigger

        # B pulls the queued Operation. Run the work under a domain-supplied ClassScope.
        scope = ClassScope.from_property_chains([[SVC.operationStatus]])
        with mw_b.claim_next(scope) as claimed:
            assert claimed.iri == op_iri
            assert claimed.operation is not None  # re-fetched under the scope
            claimed.result = hello_world()

        # Terminal transition: done + provenance folded in (ADR 0009).
        status = list(db.triples_get(sub=op_iri, pred=SVC.operationStatus))
        assert status and str(status[0][2]) == OperationStatus.DONE
        assert db.triple_exists((op_iri, SVC.executedByWorkflow, wf_instance))
        assert db.triples_get(sub=op_iri, pred=SVC.executionTimestamp)
        result = list(db.triples_get(sub=op_iri, pred=SVC.executionResult))
        assert result and str(result[0][2]) == "hello world"
        # A successful Operation carries no failure snapshot (svc:failureState is failure-only).
        assert not list(db.triples_get(sub=op_iri, pred=SVC.failureState))
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        time.sleep(0.5)


@requires_graphdb
def test_pull_and_run_failure_marks_failed(graphdb):
    """A body exception marks the Operation `failed`. Error message + provenance (ADR 0009)."""
    db = graphdb
    seed.seed_scenario1(db)
    mw_b = _hello_resource(graphdb, B_PORT)
    # Per-instance since ADR 0022. Derived from the instance. Not from the resource.
    wf_instance = mint_workflow_iri(mw_b.service_iri, "hello_world")
    server, thread = _start_server(mw_b, B_PORT)
    try:
        op_iri = _dispatch_hello_op(graphdb)

        with pytest.raises(RuntimeError, match="boom"):
            with mw_b.claim_next():
                raise RuntimeError("boom")

        status = list(db.triples_get(sub=op_iri, pred=SVC.operationStatus))
        assert status and str(status[0][2]) == OperationStatus.FAILED
        assert db.triple_exists((op_iri, SVC.executedByWorkflow, wf_instance))
        result = list(db.triples_get(sub=op_iri, pred=SVC.executionResult))
        assert result and "boom" in str(result[0][2])
        # The resource datamodel is snapshotted into svc:failureState. Atomically with `failed`.
        failure = list(db.triples_get(sub=op_iri, pred=SVC.failureState))
        assert failure, "svc:failureState should capture the resource datamodel on failure"
        assert json.loads(str(failure[0][2]))["id"] == str(seed.HELLO_RESOURCE)
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        time.sleep(0.5)


@requires_graphdb
def test_claim_next_empty_queue_raises(graphdb):
    """claim_next with nothing queued raises OperationQueueEmpty (nothing to pull)."""
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=graphdb),
        host="127.0.0.1",
        port=8998,
    )
    with pytest.raises(OperationQueueEmpty):
        with mw.claim_next():
            pass


# --------------------------------------------------------------------------- #
# Domain callback on enqueue (#15): a registered callback fires when an Operation
# is enqueued and drives a background pull-and-run (ADR 0009).
# --------------------------------------------------------------------------- #


def _wait_for_status(db, op_iri, status: str, timeout: float = 15.0) -> None:
    """Poll the graph until `op_iri` reaches `status` (the callback runs in the background)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        triples = list(db.triples_get(sub=op_iri, pred=SVC.operationStatus))
        if triples and str(triples[0][2]) == status:
            return
        time.sleep(0.1)
    raise AssertionError(f"operation {op_iri} did not reach status {status!r} within {timeout}s")


@requires_graphdb
def test_callback_runs_operation_on_enqueue(graphdb):
    """A registered callback fires on enqueue. Drive a pull-and-run to done + provenance (#15).

    The no-callback path exists. An Operation left `queued`. Covered by
    test_event_trigger_dispatch_enqueues_over_rest. Dispatch without a callback.
    """
    db = graphdb
    seed.seed_scenario1(db)
    mw_b = _hello_resource(graphdb, B_PORT)
    # Per-instance since ADR 0022. Derived from the instance. Not from the resource.
    wf_instance = mint_workflow_iri(mw_b.service_iri, "hello_world")
    mw_b.register_callback(lambda operation: hello_world())  # domain work function
    server, thread = _start_server(mw_b, B_PORT)
    try:
        op_iri = _dispatch_hello_op(graphdb)  # event trigger enqueues + fires the callback

        # The callback runs in the background. Wait for the terminal transition.
        _wait_for_status(db, op_iri, OperationStatus.DONE)
        assert db.triple_exists((op_iri, SVC.executedByWorkflow, wf_instance))
        result = list(db.triples_get(sub=op_iri, pred=SVC.executionResult))
        assert result and str(result[0][2]) == "hello world"
        # The callback drained the queue (no manual claim_next needed).
        assert op_iri not in mw_b._operation_queue
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        time.sleep(0.5)


# --------------------------------------------------------------------------- #
# Two-instance REST event-trigger proof (#16): A dispatches to B over real HTTP.
# B enqueues (queued). Pull-and-runs a STATEFUL workflow (done). The door state
# change is B observable side effect.
# --------------------------------------------------------------------------- #


@requires_graphdb
def test_two_instance_rest_event_trigger_full_lifecycle(graphdb):
    """A (mobile robot) dispatches an open Operation to B (door) over REST. B enqueues it
    (queued). Pull-and-runs door_open (done). Assert the full status lifecycle across
    two instances and B observable side effect (the door actually opens)."""
    db = graphdb
    reset_door()
    seed.seed_scenario2(db)
    # B: the door resource. Expose door_open + the built-in event trigger over REST.
    mw_b = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.DOOR_RESOURCE,
        service_class=seed.DOOR_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=B_PORT,
    )
    mw_b.workflow(
        capability_class=seed.DOOR_OPEN_CAPABILITY_CLASS,
        workflow_class=seed.DOOR_OPEN_WORKFLOW_CLASS,
    )(door_open)
    # Per-instance since ADR 0022. Derived from the instance. Not from the resource.
    open_wf = mint_workflow_iri(mw_b.service_iri, "door_open")

    server, thread = _start_server(mw_b, B_PORT)
    try:
        # A: the mobile robot. Dispatch an open Operation to the door. Peer resolved
        # purely through the graph. Event trigger fired over HTTP.
        mw_a = SemanticMiddleware(
            mode="resource",
            resource_iri=seed.MOBILE_ROBOT,
            service_class=seed.MOBILE_ROBOT_SERVICE_CLASS,
            ogm=OGM(db=graphdb_for(graphdb.repository)),
            host="127.0.0.1",
            port=8996,
        )
        with mw_a.request(
            capability_class=seed.DOOR_OPEN_CAPABILITY_CLASS,
            operation_class=str(CFC.Operation),
        ) as op:
            pass
        op_iri = op.iri

        # The REST event trigger reached B. B enqueued it. The Operation is `queued`.
        # The door has not moved yet.
        status = list(db.triples_get(sub=op_iri, pred=SVC.operationStatus))
        assert status and str(status[0][2]) == OperationStatus.QUEUED
        assert op_iri in mw_b._operation_queue
        assert door_status() == "closed"

        # B domain pulls and runs it. The door opens (observable side effect). Op -> done.
        with mw_b.claim_next() as claimed:
            assert claimed.iri == op_iri
            claimed.result = door_open()

        assert door_status() == "opened"  # B observable state change
        status = list(db.triples_get(sub=op_iri, pred=SVC.operationStatus))
        assert status and str(status[0][2]) == OperationStatus.DONE
        assert db.triple_exists((op_iri, SVC.executedByWorkflow, open_wf))
        result = list(db.triples_get(sub=op_iri, pred=SVC.executionResult))
        assert result and str(result[0][2]) == "opened"
    finally:
        reset_door()
        server.should_exit = True
        thread.join(timeout=20)
        time.sleep(0.5)
