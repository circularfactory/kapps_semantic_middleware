"""The intake end to end, against a live GraphDB.

A resource with a notification handler takes the Operations it is notified of through its intake:
the event trigger reads each one once, under the instance's scope, and one worker runs them one at
a time, in arrival order. An Operation whose Capability is one of the resource's own runs the
exposed workflow that realizes it, with the Operation object; any other reaches the handler. The
middleware writes nothing. At startup, the resource's own ``queued`` Operations run through the
intake. A peer reaches the intake with ``request()`` or ``notify()``, over the real REST route.
Each test waits until the worker has ended before it reads the graph. Skipped when GRAPHDB_* env
vars are absent (see conftest).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import pytest
import uvicorn
from kapps_ogm import OGM, Node, Update
from kapps_ogm.utils.class_scope import ClassScope
from kapps_triplestore_interface import IRI
from rdflib import Literal
from rdflib.namespace import RDF

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import graphdb_env_present, graphdb_for
from kapps_semantic_middleware.registration import (
    create_operation,
    find_possession_state,
    mint_capability_iri,
)
from kapps_semantic_middleware.vocabulary import CFC, MES, SVC, OperationStatus

requires_graphdb = pytest.mark.skipif(
    not graphdb_env_present(),
    reason="GRAPHDB_* environment variables not set; skipping live-GraphDB integration test",
)

# examples/ is not a package. Add it to the path. Reuse the scenario seed helper.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

PORT = 8988
MIDDLEWARE_LOGGER = "kapps_semantic_middleware.middleware"
OWN_CAPABILITY = mint_capability_iri(seed.HELLO_RESOURCE, "hello_operation")
TRANSPORT = IRI("http://example.org/test#Transport1")
WMS_CAPABILITY = IRI("http://example.org/test#WMS_capability_store")
# The receiver's own scope of an Operation: its status, beside the default part.
STATUS_SCOPE = ClassScope.from_property_chains([[SVC.operationStatus]])


def _receiver(
    graphdb,
    workflow: Callable[[Node], None],
    handler: Optional[Callable[[Node], None]],
) -> SemanticMiddleware:
    """The hello resource, whose exposed workflow takes the Operation, with a handler unless
    ``handler`` is None."""
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=PORT,
        heartbeat_interval=None,
    )

    def hello_operation(operation: Node) -> None:
        workflow(operation)

    mw.workflow(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
    )(hello_operation)
    if handler is not None:
        mw.on_notification(handler, class_scope=STATUS_SCOPE)
    return mw


def _planner(graphdb) -> SemanticMiddleware:
    """The planner, which requests and notifies; it is never served."""
    return SemanticMiddleware(
        mode="resource",
        resource_iri=seed.PLANNER_RESOURCE,
        service_class=seed.PLANNER_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=8989,
    )


def _request(planner: SemanticMiddleware) -> IRI:
    """The planner requests the hello Capability, and so notifies the hello resource."""
    with planner.request(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        operation_class=str(CFC.Operation),
    ) as op:
        pass
    return op.iri


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


def _wait_until(
    mw: SemanticMiddleware, done: Callable[[], bool], timeout: float = 20.0
) -> None:
    """Wait until ``done()`` holds and the intake's worker has ended."""
    deadline = time.time() + timeout
    while (not done() or mw._intake_working or mw._background_tasks) and time.time() < deadline:
        time.sleep(0.05)
    assert done()
    assert not mw._intake_working


def _explicit_statements(db) -> set:
    result = db.query(
        "SELECT ?s ?p ?o FROM <http://www.ontotext.com/explicit> WHERE { ?s ?p ?o }",
        convert_bindings=True,
    )
    return {(str(b["s"]), str(b["p"]), str(b["o"])) for b in result["results"]["bindings"]}


def _statuses(db, operation) -> list[str]:
    return [str(t[2]) for t in db.triples_get(sub=operation, pred=SVC.operationStatus)]


def _values(operation: Node, prop: IRI) -> list:
    return [getattr(value, "id", value) for value in (operation.data or {}).get(prop, [])]


@requires_graphdb
def test_a_requested_operation_of_an_own_capability_runs_its_workflow_once_and_writes_nothing(
    graphdb,
):
    """The workflow gets the Operation object, read under the receiver's scope; the handler
    gets nothing, the Operation stays ``queued`` and the graph does not change."""
    db = graphdb
    seed.seed_scenario1(db)
    ran: list = []
    handled: list = []
    seen: list = []

    def workflow(operation: Node) -> None:
        seen.append(_explicit_statements(db))
        ran.append(operation)

    receiver = _receiver(graphdb, workflow, handled.append)
    server, thread = _start_server(receiver)
    try:
        op_iri = _request(_planner(graphdb))
        _wait_until(receiver, lambda: bool(ran))
        time.sleep(0.5)  # a second run would have started by now

        [operation] = ran
        assert operation.id == op_iri
        assert _values(operation, CFC.implementsCapability) == [OWN_CAPABILITY]
        assert _values(operation, SVC.operationStatus) == [OperationStatus.QUEUED]
        assert operation.instance is not None  # materialized under the scope
        assert handled == []
        assert _explicit_statements(db) == seen[0]
        assert _statuses(db, op_iri) == [OperationStatus.QUEUED]
        assert not db.triples_get(sub=op_iri, pred=SVC.executedByWorkflow)
        assert receiver._operation_queue == []
    finally:
        _stop_server(server, thread)


@requires_graphdb
def test_two_operations_that_arrive_while_the_first_runs_run_one_after_the_other(graphdb):
    """Three requests: the second and third arrive while the first runs, and run after it, in
    arrival order, one at a time."""
    db = graphdb
    seed.seed_scenario1(db)
    release = threading.Event()
    first_started = threading.Event()
    lock = threading.Lock()
    running: list = []
    overlaps: list = []
    order: list = []

    def workflow(operation: Node) -> None:
        with lock:
            overlaps.append(len(running))
            running.append(operation.id)
        if not first_started.is_set():
            first_started.set()
            assert release.wait(timeout=30), "the test never released the first Operation"
        with lock:
            order.append(operation.id)
            running.remove(operation.id)

    receiver = _receiver(graphdb, workflow, lambda operation: None)
    server, thread = _start_server(receiver)
    try:
        planner = _planner(graphdb)
        first = _request(planner)
        assert first_started.wait(timeout=20)
        second = _request(planner)
        third = _request(planner)
        ran_before_release = list(order)
        release.set()
        _wait_until(receiver, lambda: len(order) == 3)

        assert ran_before_release == []
        assert order == [first, second, third]
        assert overlaps == [0, 0, 0]
    finally:
        release.set()
        _stop_server(server, thread)


@requires_graphdb
def test_a_notification_of_an_operation_of_no_own_capability_reaches_the_handler(graphdb):
    """The up direction: a peer notifies the resource that a transport ended. The transport
    implements another resource's Capability, so the handler gets the object; no workflow runs,
    and the graph does not change."""
    db = graphdb
    seed.seed_scenario1(db)
    db.triple_add((WMS_CAPABILITY, RDF.type, CFC.Capability))
    db.triple_add((TRANSPORT, RDF.type, CFC.Operation))
    db.triple_add((TRANSPORT, CFC.implementsCapability, WMS_CAPABILITY))
    db.triple_add((TRANSPORT, SVC.operationStatus, Literal(OperationStatus.DONE)))
    ran: list = []
    handled: list = []
    receiver = _receiver(graphdb, ran.append, handled.append)
    server, thread = _start_server(receiver)
    try:
        before = _explicit_statements(db)

        _planner(graphdb).notify(operation=TRANSPORT, resource=seed.HELLO_RESOURCE)

        _wait_until(receiver, lambda: bool(handled))
        [operation] = handled
        assert operation.id == TRANSPORT
        assert _values(operation, CFC.implementsCapability) == [WMS_CAPABILITY]
        assert _values(operation, SVC.operationStatus) == [OperationStatus.DONE]
        assert ran == []
        assert _explicit_statements(db) == before
    finally:
        _stop_server(server, thread)


@requires_graphdb
def test_at_startup_the_resources_own_queued_operation_runs_through_the_intake(graphdb):
    """An Operation requested while the resource took no notifications runs its workflow when
    the resource starts with a handler, and stays out of the queue."""
    db = graphdb
    seed.seed_scenario1(db)
    first = _receiver(graphdb, lambda operation: None, None)  # it queues the request
    server, thread = _start_server(first)
    try:
        op_iri = _request(_planner(graphdb))
        assert op_iri in first._operation_queue
    finally:
        _stop_server(server, thread)

    ran: list = []
    restarted = _receiver(graphdb, ran.append, lambda operation: None)
    server, thread = _start_server(restarted)
    try:
        _wait_until(restarted, lambda: bool(ran))
        assert [operation.id for operation in ran] == [op_iri]
        assert _values(ran[0], SVC.operationStatus) == [OperationStatus.QUEUED]
        assert restarted._operation_queue == []
    finally:
        _stop_server(server, thread)


@requires_graphdb
def test_startup_warns_about_a_resource_with_neither_a_handler_nor_a_callback(graphdb, caplog):
    db = graphdb
    seed.seed_scenario1(db)
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=PORT,
        heartbeat_interval=None,
    )

    with caplog.at_level(logging.WARNING, logger=MIDDLEWARE_LOGGER):
        asyncio.run(mw._reconstruct_queue())

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "on_notification" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert str(seed.HELLO_RESOURCE) in warnings[0]


# --------------------------------------------------------------------------- #
# UC2's shape: a transport passed from module to module, and its end told to the WMS.
# --------------------------------------------------------------------------- #

WMS = seed.PLANNER_RESOURCE
HOP_TRANSPORT = IRI(f"{seed.DEMO_NS}transport_hop")


def _transport_capability(resource: IRI) -> IRI:
    return mint_capability_iri(resource, "box_transport")


def _conveyor_module(
    graphdb, resource: IRI, port: int, step: Callable[[SemanticMiddleware, IRI], None], log: list
) -> SemanticMiddleware:
    """A FlexConveyor module: its exposed transport workflow takes the transport object and runs
    ``step``; its handler records what it gets."""
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=resource,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=port,
        heartbeat_interval=None,
    )

    def box_transport(transport: Node) -> None:
        log.append((resource, "workflow", transport.id))
        step(mw, transport.id)

    mw.workflow(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
    )(box_transport)
    mw.on_notification(lambda operation: log.append((resource, "handler", operation.id)))
    return mw


@requires_graphdb
def test_a_transport_passes_from_module_to_module_and_its_end_reaches_the_wms(graphdb):
    """The WMS notifies the origin; each module's intake runs its transport workflow; the origin
    hands the box over with the transport's reassignment in the entry and notifies the next one;
    the destination completes the transport in its handover and notifies the WMS, whose handler
    gets it."""
    db = graphdb
    seed.seed_handover(db, OGM(db=db))
    seed.load_scenario1_ontologies(db)
    seed.create_resource(db, WMS, seed.PLANNER_RESOURCE_CLASS)
    db.triple_add((WMS, MES.hasHandoverAbility, MES.Retrieve))
    origin, destination = seed.HANDOVER_SOURCE, seed.HANDOVER_DEST
    log: list = []
    ended: list = []

    def pass_on(mw: SemanticMiddleware, transport: IRI) -> None:
        moved_on = Update(
            instance_iri=transport,
            data={
                str(CFC.executedOn): [{"id": str(destination)}],
                str(CFC.implementsCapability): [{"id": str(_transport_capability(destination))}],
            },
        )
        with mw.handover(
            mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=destination, entry=[moved_on]
        ):
            pass
        mw.notify(operation=transport, resource=destination)

    def deliver(mw: SemanticMiddleware, transport: IRI) -> None:
        with mw.handover(
            mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=WMS, operation=transport
        ):
            pass
        mw.notify(operation=transport, resource=WMS)

    wms = SemanticMiddleware(
        mode="resource",
        resource_iri=WMS,
        service_class=seed.PLANNER_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=8986,
        heartbeat_interval=None,
    )
    wms.on_notification(ended.append, class_scope=STATUS_SCOPE)
    servers = [
        _start_server(_conveyor_module(graphdb, origin, 8984, pass_on, log)),
        _start_server(_conveyor_module(graphdb, destination, 8985, deliver, log)),
        _start_server(wms),
    ]
    try:
        create_operation(
            OGM(db=db),
            operation_iri=HOP_TRANSPORT,
            operation_class=CFC.Operation,
            capability_iri=_transport_capability(origin),
            status=OperationStatus.RUNNING,
        )

        wms.notify(operation=HOP_TRANSPORT, resource=origin)

        _wait_until(wms, lambda: bool(ended), timeout=60)
        assert log == [
            (origin, "workflow", HOP_TRANSPORT),
            (destination, "workflow", HOP_TRANSPORT),
        ]
        [transport] = ended
        assert transport.id == HOP_TRANSPORT
        assert _values(transport, SVC.operationStatus) == [OperationStatus.DONE]
        assert _values(transport, CFC.implementsCapability) == [_transport_capability(destination)]
        assert [str(t[2]) for t in db.triples_get(sub=HOP_TRANSPORT, pred=CFC.executedOn)] == [
            str(destination)
        ]
        assert find_possession_state(OGM(db=db), seed.HANDOVER_BOX, WMS) is not None
    finally:
        for server, thread in servers:
            _stop_server(server, thread)
