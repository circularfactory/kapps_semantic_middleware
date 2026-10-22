"""The intake of an instance with a notification handler, offline.

The event trigger reads the notified Operation once, under the instance's scope, puts it into the
instance's FIFO and answers. One worker runs one Operation at a time: an Operation whose Capability
is one of the instance's own goes to the exposed workflow that realizes it, any other to the
notification handler. Both get the Operation object. An Operation that waited is read again before
it runs. The OGM is faked: it reads every Operation as one that implements the Capability the test
gave it, and it records and refuses every other use, so a write would be seen. Its store answers
the one query that the intake makes before a workflow runs, of the Operation's status and issuer,
as a `queued` Operation that another resource issued; it records that query and refuses a write.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest
from fastapi.testclient import TestClient
from kapps_ogm import Node
from kapps_ogm.utils.class_scope import ClassScope
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.registration import mint_capability_iri
from kapps_semantic_middleware.vocabulary import CFC, SVC, OperationStatus

MIDDLEWARE_LOGGER = "kapps_semantic_middleware.middleware"
RESOURCE = "http://example.org/test#Module1"
SERVICE_CLASS = "http://example.org/test#ModuleService"
CAPABILITY_CLASS = "http://example.org/test#TransportCapability"
WORKFLOW_CLASS = "http://example.org/test#TransportWorkflow"
TRANSPORT = IRI("http://example.org/test#Transport1")
SECOND = IRI("http://example.org/test#Transport2")
THIRD = IRI("http://example.org/test#Transport3")
OWN_CAPABILITY = mint_capability_iri(IRI(RESOURCE), "run_transport")
FOREIGN_CAPABILITY = IRI("http://example.org/test#Planner_capability_store")
HAS_BOX = IRI("http://example.org/test#hasBox")
HAS_DESTINATION = IRI("http://example.org/test#hasDestination")


class _StatusStore:
    """``ogm.db``: answers the query of an Operation's status and issuer, and records it."""

    def __init__(self) -> None:
        self.queries: list = []

    def query(self, sparql, convert_bindings=False, update=False):
        assert not update, f"the intake wrote: {sparql}"
        assert str(SVC.issuedBy) in sparql and str(SVC.hasOperationStatus) in sparql, sparql
        self.queries.append(sparql)
        row = {"type": IRI("http://example.org/test#Transport"), "status": OperationStatus.QUEUED}
        return {"results": {"bindings": [row]}}


class _FakeOGM:
    """An OGM that reads each Operation as implementing ``capabilities[iri]``, and refuses the
    rest. It records each read."""

    def __init__(self, capabilities: dict):
        self.capabilities = capabilities
        self.reads: list = []
        self.refused: list = []
        self.db = _StatusStore()

    def fetch(self, **kwargs):
        self.reads.append(kwargs)
        iri = IRI(kwargs["instance_iri"])
        capability = self.capabilities[iri]
        return Node(id=iri, data={IRI(CFC.implementsCapability): [Node(id=capability)]})

    def __getattr__(self, name):
        self.refused.append(name)
        raise AssertionError(f"the intake used the OGM's {name}")


class _FakeStore:
    """The store behind ``_FakeOGM.db``: it finds the resource's own Operations by status."""

    def __init__(self, by_status: dict):
        self.by_status = by_status
        self.queries: list = []

    def query(self, sparql, convert_bindings=False, update=False):
        assert not update, f"the startup wrote to the store: {sparql}"
        self.queries.append(sparql)
        if str(SVC.issuedBy) in sparql:  # the read of a status and an issuer, before a workflow
            return _StatusStore().query(sparql)
        found = [
            iri
            for status, iris in self.by_status.items()
            if f'"{status}"' in sparql
            for iri in iris
        ]
        return {"results": {"bindings": [{"op": str(iri)} for iri in found]}}


class _StartupOGM(_FakeOGM):
    """A ``_FakeOGM`` whose store holds the resource's own Operations by status."""

    def __init__(self, capabilities: dict, by_status: dict):
        super().__init__(capabilities)
        self.db = _FakeStore(by_status)


def _module(ogm: _FakeOGM) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=RESOURCE,
        service_class=SERVICE_CLASS,
        ogm=ogm,
        heartbeat_interval=None,
    )


async def _settle(mw: SemanticMiddleware) -> None:
    """Wait until every background task that the notifications started has ended."""
    while mw._background_tasks:
        await asyncio.wait(set(mw._background_tasks))


def test_an_operation_of_an_own_capability_runs_its_workflow_with_the_operation_object():
    ogm = _FakeOGM({TRANSPORT: OWN_CAPABILITY})
    mw = _module(ogm)
    ran: list = []
    handled: list = []

    @mw.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    def run_transport(transport: Node) -> None:
        ran.append(transport)

    mw.on_notification(handled.append)

    async def deliver():
        answer = await mw._handle_event_trigger(TRANSPORT)
        await _settle(mw)
        return answer

    answer = asyncio.run(deliver())

    assert answer == {"operation": str(TRANSPORT)}
    assert [operation.id for operation in ran] == [TRANSPORT]
    assert handled == []
    assert len(ogm.reads) == 1
    assert ogm.refused == []


def test_an_operation_of_no_own_capability_reaches_the_handler_with_the_operation_object():
    ogm = _FakeOGM({TRANSPORT: FOREIGN_CAPABILITY})
    mw = _module(ogm)
    ran: list = []
    handled: list = []

    @mw.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    def run_transport(transport: Node) -> None:
        ran.append(transport)

    mw.on_notification(handled.append)

    async def deliver():
        answer = await mw._handle_event_trigger(TRANSPORT)
        await _settle(mw)
        return answer

    answer = asyncio.run(deliver())

    assert answer == {"operation": str(TRANSPORT)}
    assert ran == []
    assert [operation.id for operation in handled] == [TRANSPORT]
    assert len(ogm.reads) == 1
    assert ogm.refused == []


def test_the_operation_is_read_and_materialized_under_the_instances_scope_united_with_the_default():
    ogm = _FakeOGM({TRANSPORT: FOREIGN_CAPABILITY})
    mw = _module(ogm)
    transport_scope = ClassScope.from_property_chains([[HAS_BOX], [HAS_DESTINATION]])
    mw.on_notification(lambda transport: None, class_scope=transport_scope)

    async def deliver():
        await mw._handle_event_trigger(TRANSPORT)
        await _settle(mw)

    asyncio.run(deliver())

    [read] = ogm.reads
    assert read["instance_iri"] == TRANSPORT
    assert read["materialize"] is True
    assert sorted(read["class_scope"].to_property_chains()) == sorted(
        [
            [CFC.implementsCapability, SVC.realizedByWorkflow],
            [HAS_BOX],
            [HAS_DESTINATION],
        ]
    )


def test_without_a_scope_of_its_own_the_read_is_the_capability_and_its_workflow():
    ogm = _FakeOGM({TRANSPORT: FOREIGN_CAPABILITY})
    mw = _module(ogm)
    mw.on_notification(lambda transport: None)

    async def deliver():
        await mw._handle_event_trigger(TRANSPORT)
        await _settle(mw)

    asyncio.run(deliver())

    [read] = ogm.reads
    assert read["class_scope"].to_property_chains() == [
        [CFC.implementsCapability, SVC.realizedByWorkflow]
    ]


def test_two_operations_that_arrive_while_the_first_runs_run_one_after_the_other_in_order():
    """The trigger answers each at once; the worker runs one at a time, and reads each that
    waited again before it runs."""
    ogm = _FakeOGM({iri: OWN_CAPABILITY for iri in (TRANSPORT, SECOND, THIRD)})
    mw = _module(ogm)
    release = threading.Event()
    started = threading.Event()
    lock = threading.Lock()
    running: list = []
    overlaps: list = []
    order: list = []

    @mw.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    def run_transport(transport: Node) -> None:
        with lock:
            overlaps.append(len(running))
            running.append(transport.id)
        started.set()
        assert release.wait(timeout=10), "the test never released the first Operation"
        with lock:
            order.append(transport.id)
            running.remove(transport.id)

    mw.on_notification(lambda operation: None)

    async def deliver():
        answers = [await mw._handle_event_trigger(TRANSPORT)]
        while not started.is_set():
            await asyncio.sleep(0.01)
        answers.append(await mw._handle_event_trigger(SECOND))
        answers.append(await mw._handle_event_trigger(THIRD))
        ran_before_release = list(order)
        release.set()
        await _settle(mw)
        return answers, ran_before_release

    answers, ran_before_release = asyncio.run(deliver())

    assert answers == [{"operation": str(iri)} for iri in (TRANSPORT, SECOND, THIRD)]
    assert ran_before_release == []
    assert order == [TRANSPORT, SECOND, THIRD]
    assert overlaps == [0, 0, 0]
    reads = [read["instance_iri"] for read in ogm.reads]
    assert reads.count(TRANSPORT) == 1
    assert reads.count(SECOND) == 2
    assert reads.count(THIRD) == 2


def test_an_exception_of_the_workflow_is_logged_writes_nothing_and_the_next_one_runs(caplog):
    ogm = _FakeOGM({TRANSPORT: OWN_CAPABILITY, SECOND: OWN_CAPABILITY})
    mw = _module(ogm)
    ran: list = []

    @mw.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    def run_transport(transport: Node) -> None:
        ran.append(transport.id)
        if transport.id == TRANSPORT:
            raise RuntimeError("belt jammed")

    mw.on_notification(lambda operation: None)

    async def deliver():
        await mw._handle_event_trigger(TRANSPORT)
        await mw._handle_event_trigger(SECOND)
        await _settle(mw)

    with caplog.at_level(logging.ERROR, logger=MIDDLEWARE_LOGGER):
        asyncio.run(deliver())

    assert ran == [TRANSPORT, SECOND]
    assert "belt jammed" in caplog.text
    assert "run_transport" in caplog.text
    assert str(TRANSPORT) in caplog.text
    assert ogm.refused == []


def test_a_coroutine_workflow_runs_on_the_event_loop():
    ogm = _FakeOGM({TRANSPORT: OWN_CAPABILITY})
    mw = _module(ogm)
    threads: list = []

    @mw.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    async def run_transport(transport: Node) -> None:
        threads.append(threading.get_ident())

    mw.on_notification(lambda operation: None)

    async def deliver():
        await mw._handle_event_trigger(TRANSPORT)
        await _settle(mw)
        return threading.get_ident()

    loop_thread = asyncio.run(deliver())

    assert threads == [loop_thread]


def test_an_operation_that_cannot_be_read_again_is_logged_and_the_next_one_runs(caplog):
    ogm = _FakeOGM({iri: OWN_CAPABILITY for iri in (TRANSPORT, SECOND, THIRD)})
    mw = _module(ogm)
    release = threading.Event()
    ran: list = []

    @mw.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    def run_transport(transport: Node) -> None:
        if transport.id == TRANSPORT:
            assert release.wait(timeout=10), "the test never released the first Operation"
        ran.append(transport.id)

    mw.on_notification(lambda operation: None)

    async def deliver():
        await mw._handle_event_trigger(TRANSPORT)
        await mw._handle_event_trigger(SECOND)
        await mw._handle_event_trigger(THIRD)
        del ogm.capabilities[SECOND]  # gone from the graph while it waited
        release.set()
        await _settle(mw)

    with caplog.at_level(logging.ERROR, logger=MIDDLEWARE_LOGGER):
        asyncio.run(deliver())

    assert ran == [TRANSPORT, THIRD]
    assert str(SECOND) in caplog.text


def test_an_operation_that_reaches_the_intake_without_a_handler_is_logged_not_dropped(caplog):
    """The intake runs only on an instance with a handler. Should an Operation of no own
    Capability ever reach it on one without, the broken rule is logged, not passed over."""
    ogm = _FakeOGM({TRANSPORT: FOREIGN_CAPABILITY})
    mw = _module(ogm)

    async def deliver():
        mw._take_in(TRANSPORT)
        await _settle(mw)

    with caplog.at_level(logging.ERROR, logger=MIDDLEWARE_LOGGER):
        asyncio.run(deliver())

    assert str(TRANSPORT) in caplog.text
    assert "without a notification handler" in caplog.text
    assert ogm.refused == []


def test_at_startup_the_instances_own_queued_operations_run_through_the_intake():
    """Each is read before it runs, in the order the store returned them, one at a time."""
    ogm = _StartupOGM(
        {TRANSPORT: OWN_CAPABILITY, SECOND: FOREIGN_CAPABILITY},
        {OperationStatus.QUEUED: [TRANSPORT, SECOND]},
    )
    mw = _module(ogm)
    ran: list = []
    handled: list = []

    @mw.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    def run_transport(transport: Node) -> None:
        ran.append(transport.id)

    mw.on_notification(lambda operation: handled.append(operation.id))

    async def start():
        await mw._reconstruct_queue()
        await _settle(mw)

    asyncio.run(start())

    assert ran == [TRANSPORT]
    assert handled == [SECOND]
    assert [read["instance_iri"] for read in ogm.reads] == [TRANSPORT, SECOND]
    assert mw._operation_queue == []
    assert ogm.refused == []


def _startup_warnings(mw: SemanticMiddleware, caplog) -> list:
    with caplog.at_level(logging.WARNING, logger=MIDDLEWARE_LOGGER):
        asyncio.run(mw._reconstruct_queue())
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == MIDDLEWARE_LOGGER
    ]


def test_startup_warns_about_an_instance_with_neither_a_handler_nor_a_callback(caplog):
    mw = _module(_StartupOGM({}, {}))

    [warning] = _startup_warnings(mw, caplog)

    message = warning.getMessage()
    assert RESOURCE in message
    assert "on_notification" in message and "register_callback" in message


def test_startup_does_not_warn_about_an_instance_with_a_handler(caplog):
    mw = _module(_StartupOGM({}, {}))
    mw.on_notification(lambda operation: None)

    assert _startup_warnings(mw, caplog) == []


def test_startup_does_not_warn_about_an_instance_with_a_callback(caplog):
    mw = _module(_StartupOGM({}, {}))
    mw.register_callback(lambda operation: None)

    assert _startup_warnings(mw, caplog) == []


def test_a_workflow_that_takes_the_operation_as_a_node_registers_as_an_exposed_workflow():
    """A parameter typed ``kapps_ogm.Node`` needs nothing more: the REST route is mounted. The
    framework takes a one-parameter workflow's argument as an optional query parameter ``arg``,
    not as a body, so a call passes ``None`` and the framework's type check refuses it: the call
    is answered with an HTTP 500 and the function does not run."""
    mw = _module(_FakeOGM({}))
    ran: list = []

    @mw.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    def run_transport(transport: Node) -> None:
        ran.append(transport)

    route = mw.app.openapi()["paths"]["/workflows/run_transport/execute"]["post"]
    assert route.get("requestBody") is None
    assert [parameter["in"] for parameter in route["parameters"]] == ["query"]
    assert run_transport.__name__ == "run_transport"  # the decorator returns the function

    client = TestClient(mw.app, raise_server_exceptions=False)
    answer = client.post("/workflows/run_transport/execute", json={"id": str(TRANSPORT)})
    assert answer.status_code == 500
    with pytest.raises(Exception, match=r'"transport" \(None\)'):
        TestClient(mw.app).post("/workflows/run_transport/execute", json={"id": str(TRANSPORT)})
    assert ran == []
