"""Notifications end to end, against a live store.

A peer notifies, over REST, a resource that registered a notification handler. For an Operation
that matches none of the resource's workflows, such as one that the production system level
assigned to a Station and addressed to no Capability (UC2's transport), the handler gets the
Operation object in the background, the trigger answers before the handler ends, the graph is
left as it was, and the Operation does not enter the in-memory queue. Startup still fails such a
resource's orphaned ``running`` Operations. ``notify`` crosses the same route from one middleware
to another and writes nothing. Each test waits until the handler's background task has ended
before it reads the graph. An Operation of the resource's own Capability, and the startup of
its ``queued`` ones, are the intake's (``test_intake_integration.py``). Skipped when TRIPLESTORE_*
env vars are absent (see conftest).
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from rdflib import Literal
from rdflib.namespace import RDF

from kapps_ogm import OGM
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import triplestore_env_present, triplestore_for
from kapps_semantic_middleware.registration import (
    build_event_trigger_url,
    set_operation_status,
)
from kapps_semantic_middleware.vocabulary import CFC, OperationStatus, SVC

requires_store = pytest.mark.skipif(
    not triplestore_env_present(),
    reason="TRIPLESTORE_* environment variables not set; skipping live-store integration test",
)

# examples/ is not a package. Add it to the path. Reuse the scenario seed helper.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

PORT = 8992
TRANSPORT = "http://example.org/test#Transport1"


def hello_world() -> str:
    return "hello world"


def _hello_resource(
    store, heartbeat_interval: float | None = 30.0
) -> SemanticMiddleware:
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=triplestore_for(store.dataset)),
        host="127.0.0.1",
        port=PORT,
        heartbeat_interval=heartbeat_interval,
    )
    mw.workflow(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
    )(hello_world)
    return mw


def _start_server(mw: SemanticMiddleware) -> tuple[uvicorn.Server, threading.Thread]:
    config = uvicorn.Config(mw.app, host="127.0.0.1", port=mw.port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    t0 = time.time()
    while not server.started and time.time() - t0 < 30:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("server did not start in time")
    return server, thread


def _stop_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=20)
    time.sleep(0.5)


def _seed_transport(db, status: str) -> None:
    """UC2's transport: an Operation with a status, addressed to no Capability."""
    db.triple_add((TRANSPORT, RDF.type, CFC.Operation))
    db.triple_add((TRANSPORT, SVC.operationStatus, Literal(status)))


def _statuses(db, operation) -> list[str]:
    return [str(t[2]) for t in db.triples_get(sub=operation, pred=SVC.operationStatus)]


def _wait_until_handled(
    mw: SemanticMiddleware, received: list, timeout: float = 10.0
) -> None:
    """Wait until the handler got one notification and every background task has ended.

    The handler's task ends only after everything the middleware runs around the handler, so
    a test that reads the graph afterwards also sees anything written after the handler.
    """
    deadline = time.time() + timeout
    while (not received or mw._background_tasks) and time.time() < deadline:
        time.sleep(0.05)
    assert len(received) == 1
    assert not mw._background_tasks


@requires_store
def test_a_notification_over_rest_reaches_the_handler_with_no_lifecycle(store):
    """The handler gets the IRI after the trigger answered, and the notification writes nothing."""
    db = store
    seed.seed_scenario1(db)
    _seed_transport(db, OperationStatus.RUNNING)

    mw = _hello_resource(store)
    release = threading.Event()
    received: list = []

    def handler(operation):
        assert release.wait(timeout=20), "the test never released the handler"
        received.append(operation.id)

    mw.on_notification(handler)
    server, thread = _start_server(mw)
    try:
        response = httpx.post(
            build_event_trigger_url(mw.address),
            json={"operation_iri": TRANSPORT},
            timeout=10,
        )
        # The answer arrived while the handler still blocks, and it claims no status.
        assert response.status_code == 200
        assert response.json() == {"operation": TRANSPORT}
        assert received == []

        release.set()
        _wait_until_handled(mw, received)
        assert received == [TRANSPORT]

        # No lifecycle: the status is the one seeded, no provenance, and no queue entry.
        assert _statuses(db, TRANSPORT) == [OperationStatus.RUNNING]
        assert not db.triples_get(sub=TRANSPORT, pred=SVC.executedByWorkflow)
        assert TRANSPORT not in mw._operation_queue
    finally:
        release.set()
        _stop_server(server, thread)


def _request_hello(store):
    """A planner issues an Operation for the hello Capability, and so notifies the hello resource."""
    planner = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.PLANNER_RESOURCE,
        service_class=seed.PLANNER_SERVICE_CLASS,
        ogm=OGM(db=triplestore_for(store.dataset)),
        host="127.0.0.1",
        port=8996,
    )
    with planner.issue_operation(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        operation_class=str(CFC.Operation),
    ) as op:
        pass
    return op.iri


@requires_store
def test_startup_still_fails_an_orphaned_running_operation_on_a_resource_with_a_handler(
    store,
):
    """Crash recovery stands: an own `running` Operation left by a crash becomes `failed`."""
    db = store
    seed.seed_scenario1(db)
    server, thread = _start_server(_hello_resource(store))
    try:
        op_iri = _request_hello(store)
    finally:
        _stop_server(server, thread)
    # The handler's own commit took it `running`, and then the resource crashed.
    set_operation_status(
        OGM(db=db), operation_iri=op_iri, status=OperationStatus.RUNNING
    )

    restarted = _hello_resource(store)
    restarted.on_notification(lambda operation_iri: None)
    asyncio.run(restarted._reconstruct_queue())

    assert _statuses(db, op_iri) == [OperationStatus.FAILED]
    assert op_iri not in restarted._operation_queue


def _statement_count(db) -> int:
    result = db.query(
        "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }", convert_bindings=True
    )
    return int(result["results"]["bindings"][0]["n"])


@requires_store
def test_notify_reaches_the_handler_of_another_middleware_and_writes_nothing(store):
    """One middleware notifies another over the real REST route; the graph does not change."""
    db = store
    seed.seed_scenario1(db)
    _seed_transport(db, OperationStatus.RUNNING)
    receiver = _hello_resource(store, heartbeat_interval=None)
    received: list = []
    receiver.on_notification(lambda operation: received.append(operation.id))
    server, thread = _start_server(receiver)
    try:
        sender = SemanticMiddleware(
            mode="resource",
            resource_iri=seed.PLANNER_RESOURCE,
            service_class=seed.PLANNER_SERVICE_CLASS,
            ogm=OGM(db=triplestore_for(store.dataset)),
            host="127.0.0.1",
            port=8996,
        )
        before = _statement_count(db)

        sender.notify(operation=TRANSPORT, resource=seed.HELLO_RESOURCE)

        _wait_until_handled(receiver, received)
        assert received == [TRANSPORT]
        assert _statement_count(db) == before
        assert TRANSPORT not in receiver._operation_queue
    finally:
        _stop_server(server, thread)
