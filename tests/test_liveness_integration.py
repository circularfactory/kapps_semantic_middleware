"""Phase 4 liveness integration tests against a live store.

Deterministic (no waiting on real intervals): a heartbeat is written and refreshed
idempotently, and a watchdog sweep deregisters a stale service while leaving a
fresh one reachable. The sweep and the startup reconstruction decide per Operation: an
queued Operation is failed only when no live Service of its resource realizes its Capability, a
running one when the Service that claimed it (`svc:executedByWorkflow`) is not live.
Heartbeats are written with explicit timestamps (fresh: now; stale: an hour ago), so a slow
store cannot move a service across the staleness threshold. Skipped when TRIPLESTORE_* env
vars are absent.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kapps_ogm import OGM
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import triplestore_env_present, triplestore_for
from kapps_semantic_middleware.registration import (
    create_operation,
    mint_capability_iri,
    mint_operation_iri,
    mint_service_iri,
    mint_workflow_iri,
    register_service,
    register_workflow,
    update_heartbeat,
)
from kapps_semantic_middleware.vocabulary import CFC, SVC, OperationStatus

requires_store = pytest.mark.skipif(
    not triplestore_env_present(),
    reason="TRIPLESTORE_* environment variables not set; skipping live-store integration test",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402


@requires_store
def test_heartbeat_write_and_refresh(store):
    db = store
    seed.seed_scenario1(db)

    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=triplestore_for(store.dataset)),
        host="127.0.0.1",
        port=8987,
    )
    # Per-instance, so it is read off the instance rather than reconstructed.
    service_iri = mw.service_iri
    asyncio.run(mw._register_service())

    asyncio.run(mw.emit_heartbeat())
    hb1 = db.triples_get(sub=service_iri, pred=SVC.hasLastHeartbeat)
    assert len(hb1) == 1, "exactly one heartbeat after first emit"

    asyncio.run(mw.emit_heartbeat())
    hb2 = db.triples_get(sub=service_iri, pred=SVC.hasLastHeartbeat)
    assert len(hb2) == 1, "heartbeat is replaced, not accumulated"


@requires_store
def test_watchdog_sweeps_only_stale_service(store):
    db = store
    seed.seed_scenario1(db)

    ogm = OGM(db=db)
    fresh_service = mint_service_iri(seed.HELLO_RESOURCE, "http://127.0.0.1:8001")
    stale_service = mint_service_iri(seed.PLANNER_RESOURCE, "http://127.0.0.1:8002")

    # A fresh, reachable service (heartbeat now).
    register_service(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=fresh_service,
        service_class=seed.HELLO_SERVICE_CLASS,
        address="http://127.0.0.1:8001",
    )
    update_heartbeat(ogm, fresh_service)

    # A reachable but silent service (heartbeat far in the past → stale).
    register_service(
        ogm,
        resource_iri=seed.PLANNER_RESOURCE,
        service_iri=stale_service,
        service_class=seed.PLANNER_SERVICE_CLASS,
        address="http://127.0.0.1:8002",
    )
    update_heartbeat(ogm, stale_service, timestamp=datetime.now(timezone.utc) - timedelta(hours=1))

    watchdog = SemanticMiddleware(
        mode="watchdog",
        ogm=OGM(db=triplestore_for(store.dataset)),
        staleness_threshold=60.0,
    )
    swept = asyncio.run(watchdog.sweep())

    assert str(stale_service) in swept
    assert str(fresh_service) not in swept
    # The stale service lost its reachability; the fresh one kept it.
    assert not db.triples_get(sub=stale_service, pred=SVC.hasAddress)
    assert db.triples_get(sub=fresh_service, pred=SVC.hasAddress)
    # The stale service's individual is preserved (only reachability removed).
    from rdflib.namespace import RDF

    assert db.triple_exists((stale_service, RDF.type, seed.PLANNER_SERVICE_CLASS))


# --------------------------------------------------------------------------- #
# Per-Operation decision: a stale Service with a sibling on the same resource.
# --------------------------------------------------------------------------- #

HELLO_CAPABILITY = mint_capability_iri(seed.HELLO_RESOURCE, "hello_world")


def _hello_service(ogm, address: str, *, realizes: bool, heartbeat_age: timedelta | None):
    """Register a Service of the hello resource at ``address``, optionally with the hello_world
    Workflow that realizes ``HELLO_CAPABILITY``, and give it a heartbeat ``heartbeat_age`` old
    (None: only the one that the registration writes). Returns the Service IRI."""
    service = mint_service_iri(seed.HELLO_RESOURCE, address)
    register_service(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=service,
        service_class=seed.HELLO_SERVICE_CLASS,
        address=address,
    )
    if realizes:
        register_workflow(
            ogm,
            resource_iri=seed.HELLO_RESOURCE,
            service_iri=service,
            workflow_iri=mint_workflow_iri(service, "hello_world"),
            workflow_class=seed.HELLO_WORKFLOW_CLASS,
            capability_iri=HELLO_CAPABILITY,
            capability_class=seed.HELLO_CAPABILITY_CLASS,
            endpoint=f"{address}/workflows/hello_world/execute",
        )
    if heartbeat_age is not None:
        update_heartbeat(ogm, service, timestamp=datetime.now(timezone.utc) - heartbeat_age)
    return service


def _hello_operation(ogm, status: str, executor_service=None):
    """An Operation on the hello resource's Capability, issued by the planner resource. With
    ``executor_service``, it records that Service's hello_world Workflow as its executor, as a
    claim by that Service does."""
    op_iri = mint_operation_iri(CFC.Operation)
    data: dict = {str(SVC.issuedBy): [{"id": str(seed.PLANNER_RESOURCE)}]}
    if executor_service is not None:
        executor = mint_workflow_iri(executor_service, "hello_world")
        data[str(SVC.executedByWorkflow)] = [{"id": str(executor)}]
    create_operation(
        ogm,
        operation_iri=op_iri,
        operation_class=CFC.Operation,
        capability_iri=HELLO_CAPABILITY,
        status=status,
        data=data,
    )
    return op_iri


def _status(db, op_iri) -> str | None:
    triples = list(db.triples_get(sub=op_iri, pred=SVC.hasOperationStatus))
    return str(triples[0][2]) if triples else None


def _sweep_recording_notifications(store) -> tuple[list[str], list]:
    """Run one watchdog sweep. Return the swept Services and the (operation, issuer) pairs the
    watchdog notified; the delivery itself is recorded rather than posted."""
    watchdog = SemanticMiddleware(
        mode="watchdog",
        ogm=OGM(db=triplestore_for(store.dataset)),
        staleness_threshold=60.0,
    )
    notified: list = []
    watchdog._notify = lambda op_iri, issuer, timeout=10.0: notified.append(  # type: ignore[method-assign]
        (op_iri, issuer)
    )
    swept = asyncio.run(watchdog.sweep())
    assert watchdog._flush_notifications(timeout=10)
    return swept, notified


HOUR = timedelta(hours=1)
NOW = timedelta(0)


@requires_store
def test_a_live_sibling_that_realizes_the_capability_keeps_the_operation(store):
    """(a) The stale Service is deregistered; its resource's queued Operation stays queued,
    because a live sibling on the same resource realizes its Capability."""
    db = store
    seed.seed_scenario1(db)
    ogm = OGM(db=db)
    dead = _hello_service(ogm, "http://127.0.0.1:8011", realizes=True, heartbeat_age=HOUR)
    live = _hello_service(ogm, "http://127.0.0.1:8012", realizes=True, heartbeat_age=NOW)
    op_iri = _hello_operation(ogm, OperationStatus.QUEUED)

    swept, notified = _sweep_recording_notifications(store)

    assert str(dead) in swept and str(live) not in swept
    assert not db.triples_get(sub=dead, pred=SVC.hasAddress)
    assert db.triples_get(sub=live, pred=SVC.hasAddress)
    assert _status(db, op_iri) == OperationStatus.QUEUED
    assert notified == []


@requires_store
def test_a_live_sibling_that_does_not_realize_the_capability_does_not_shield_it(store):
    """(b) A live monitor that realizes no Workflow does not keep a dead controller's queued and
    running Operations: they are failed, and their issuer is notified."""
    db = store
    seed.seed_scenario1(db)
    ogm = OGM(db=db)
    controller = _hello_service(ogm, "http://127.0.0.1:8013", realizes=True, heartbeat_age=HOUR)
    monitor = _hello_service(ogm, "http://127.0.0.1:8014", realizes=False, heartbeat_age=NOW)
    queued_op = _hello_operation(ogm, OperationStatus.QUEUED)
    running_op = _hello_operation(ogm, OperationStatus.RUNNING, executor_service=controller)

    swept, notified = _sweep_recording_notifications(store)

    assert str(controller) in swept and str(monitor) not in swept
    assert not db.triples_get(sub=controller, pred=SVC.hasAddress)
    assert db.triples_get(sub=monitor, pred=SVC.hasAddress)
    assert _status(db, queued_op) == OperationStatus.FAILED
    assert _status(db, running_op) == OperationStatus.FAILED
    assert sorted(notified) == sorted(
        [(queued_op, seed.PLANNER_RESOURCE), (running_op, seed.PLANNER_RESOURCE)]
    )


def _hello_instance(store, port: int) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=triplestore_for(store.dataset)),
        host="127.0.0.1",
        port=port,
    )


@requires_store
def test_a_dead_claimer_fails_its_running_operation_despite_a_live_realizer(store):
    """(ii) Controller A claims an Operation and dies; controller B, registered later, is live
    and the graph's realizer of the Capability. B would never run A's running Operation, so the
    sweep fails it and notifies the issuer; the queued Operation stays for B."""
    db = store
    seed.seed_scenario1(db)
    ogm = OGM(db=db)
    a = _hello_service(ogm, "http://127.0.0.1:8021", realizes=True, heartbeat_age=NOW)
    b = _hello_service(ogm, "http://127.0.0.1:8022", realizes=True, heartbeat_age=NOW)
    claimed_op = _hello_operation(ogm, OperationStatus.QUEUED)
    queued_op = _hello_operation(ogm, OperationStatus.QUEUED)

    controller_a = _hello_instance(store, 8021)

    @controller_a.workflow(
        capability_class=seed.HELLO_CAPABILITY_CLASS, workflow_class=seed.HELLO_WORKFLOW_CLASS
    )
    def hello_world() -> str:
        """Say hello."""
        return "hello"

    controller_a._operation_queue.append(claimed_op)
    claim = controller_a.claim_next()
    claim.__enter__()  # A claims, and dies in the body: nothing more is written
    try:
        assert _status(db, claimed_op) == OperationStatus.RUNNING
        executors = [str(t[2]) for t in db.triples_get(sub=claimed_op, pred=SVC.executedByWorkflow)]
        assert executors == [str(mint_workflow_iri(a, "hello_world"))]

        update_heartbeat(ogm, a, timestamp=datetime.now(timezone.utc) - HOUR)
        swept, notified = _sweep_recording_notifications(store)
    finally:
        claim.gen.close()  # the exit of a crash: the commit writes nothing on GeneratorExit

    assert str(a) in swept and str(b) not in swept
    assert not db.triples_get(sub=a, pred=SVC.hasAddress)
    assert db.triples_get(sub=b, pred=SVC.hasAddress)
    assert _status(db, claimed_op) == OperationStatus.FAILED
    assert _status(db, queued_op) == OperationStatus.QUEUED
    assert notified == [(claimed_op, seed.PLANNER_RESOURCE)]


@requires_store
def test_the_reconstruction_leaves_a_running_operation_to_its_live_claimer(store):
    """(c) A restarting instance does not fail a running Operation of its resource that a live
    sibling claimed: the reconstruction leaves it running, and does not queue it."""
    db = store
    seed.seed_scenario1(db)
    ogm = OGM(db=db)
    # The restarting instance's own Service, from before its crash.
    _hello_service(ogm, "http://127.0.0.1:8015", realizes=True, heartbeat_age=NOW)
    sibling = _hello_service(ogm, "http://127.0.0.1:8016", realizes=True, heartbeat_age=NOW)
    op_iri = _hello_operation(ogm, OperationStatus.RUNNING, executor_service=sibling)

    mw = _hello_instance(store, 8015)
    asyncio.run(mw._reconstruct_queue())

    assert _status(db, op_iri) == OperationStatus.RUNNING
    assert op_iri not in mw._operation_queue


@requires_store
def test_the_reconstruction_does_not_count_its_own_fresh_heartbeat(store):
    """A restarting instance whose own heartbeat is still fresh from before the crash fails the
    running Operation it claimed then: its own Service never counts as live."""
    db = store
    seed.seed_scenario1(db)
    ogm = OGM(db=db)
    own = _hello_service(ogm, "http://127.0.0.1:8017", realizes=True, heartbeat_age=NOW)
    op_iri = _hello_operation(ogm, OperationStatus.RUNNING, executor_service=own)

    mw = _hello_instance(store, 8017)
    asyncio.run(mw._reconstruct_queue())

    assert _status(db, op_iri) == OperationStatus.FAILED
    assert op_iri not in mw._operation_queue
