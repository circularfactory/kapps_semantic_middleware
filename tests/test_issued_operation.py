"""The issuer of an Operation, offline: `issue_operation()` records it, the claimer's middleware tells it
when the Operation is claimed and when it ends, and its handle waits for that.

`issue_operation()` is built on `mw.create()` and on the delivery of `mw.notify()`. Its commit records the
calling instance's Resource as the Operation's issuer (`svc:issuedBy`). A failed notification
leaves the Operation `queued` and raises `NotificationUndelivered`.

After a commit that the store admitted and that moves an Operation with an issuer to `running`,
`done` or `failed`, the middleware that made the commit notifies the issuer, whichever function
made the commit. The issuer's middleware wakes the Operation's handle (`IssuedOperation`), which
reads the status from the graph.

The store is a small fake that answers the reads the middleware makes by what they ask, and logs
every read, commit and fetch in order, so a test counts the store round trips. A receiver is a
real HTTP server on the loopback interface, or a real middleware app served without its startup.
The live counterpart is `test_request_claim_integration.py`.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Iterator

import httpx
import pytest
import uvicorn
from kapps_ogm import Create, Node, Update
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import ShaclValidationError

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.errors import NotificationUndelivered, OperationQueueEmpty
from kapps_semantic_middleware.middleware import IssuedOperation
from kapps_semantic_middleware.possession import handover
from kapps_semantic_middleware.registration import mint_capability_iri, mint_workflow_iri
from kapps_semantic_middleware.vocabulary import CFC, MES, SVC, OperationStatus

MIDDLEWARE_LOGGER = "kapps_semantic_middleware.middleware"
EX = "http://example.org/test#"
PLANNER = IRI(f"{EX}planner")
DRILLER = IRI(f"{EX}driller")
SERVICE_CLASS = f"{EX}ParticipantService"
OPERATION_CLASS = IRI(f"{EX}Drilling")
CAPABILITY_CLASS = IRI(f"{EX}DrillingCapability")
WORKFLOW_CLASS = IRI(f"{EX}DrillingWorkflow")
CAPABILITY = mint_capability_iri(DRILLER, "drill")
WORKFLOW = IRI(f"{EX}driller_service_workflow_drill")
TORQUE = IRI(f"{EX}torque")
OPERATION = IRI(f"{EX}Drilling_1")

_IRI = re.compile(r"<([^>]+)>")


class _Graph:
    """The store, as far as these tests need it: the status and the issuer of each Operation, the
    live Services of each Resource, and the Service that discovery finds. It answers each read
    by what the read asks, and logs every read, commit and fetch in order."""

    def __init__(self) -> None:
        self.status: dict[IRI, str] = {}
        self.issuer: dict[IRI, IRI] = {}
        self.live: dict[IRI, list[tuple[IRI, str]]] = {}
        self.target: tuple[IRI, IRI, str] | None = None
        self.log: list = []

    # ``ogm.db``
    def query(self, sparql, convert_bindings=False, update=False):
        assert not update, f"a direct write: {sparql}"
        self.log.append(("read", sparql))
        return {"results": {"bindings": self._rows(sparql)}}

    def _rows(self, sparql: str) -> list[dict]:
        named = [IRI(iri) for iri in _IRI.findall(sparql)]
        if str(SVC.isWorkflowOf) in sparql:  # discovery
            cap, svc, addr = self.target  # type: ignore[misc]
            return [{"cap": cap, "svc": svc, "addr": addr}]
        if str(SVC.realizedByWorkflow) in sparql:  # the claim's read
            (op,) = {iri for iri in named if iri in self.status}
            row: dict = {"wf": WORKFLOW}
            if op in self.issuer:
                row["issuer"] = self.issuer[op]
            return [row]
        if str(SVC.isServiceOf) in sparql:  # the live Services of a Resource
            (resource,) = {iri for iri in named if iri in self.live or iri in (PLANNER, DRILLER)}
            return [{"svc": svc, "addr": addr} for svc, addr in self.live.get(resource, [])]
        if "?type" in sparql:  # a notified Operation, read by an instance without a handler
            (op,) = {iri for iri in named if iri in self.status}
            row = {"type": OPERATION_CLASS, "status": self.status[op]}
            if op in self.issuer:
                row["issuer"] = self.issuer[op]
            return [row]
        if str(SVC.issuedBy) in sparql:  # the issuers of Operations
            return [
                {"op": iri, "issuer": self.issuer[iri]} for iri in named if iri in self.issuer
            ]
        if str(SVC.hasOperationStatus) in sparql:  # the status of one Operation
            (op,) = {iri for iri in named if iri in self.status}
            return [{"status": self.status[op]}]
        raise AssertionError(f"an unexpected read: {sparql}")

    def triples_get(self, **pattern):
        """No Workflow or StateProperty of a deregistered Service holds an endpoint here."""
        self.log.append(("read", pattern))
        return []


class _OGM:
    """Applies each admitted commit to the graph; a fetch reads an Operation as implementing the
    driller's Capability."""

    def __init__(self, graph: _Graph) -> None:
        self.db = graph

    def commit(self, *, changes=None, instance_iri=None, data=None, named_graph=None):
        if changes is None:  # the one-instance form, as the registration's writes use it
            changes = [Update(instance_iri=instance_iri, data=data)]
        changes = list(changes)
        for change in changes:
            iri = IRI(change.instance_iri)
            data = {str(key): values for key, values in change.data.items()}
            if str(SVC.hasOperationStatus) in data:
                self.db.status[iri] = data[str(SVC.hasOperationStatus)][0]
            if str(SVC.issuedBy) in data:
                self.db.issuer[iri] = IRI(data[str(SVC.issuedBy)][0]["id"])
        self.db.log.append(("commit", changes))
        return [SimpleNamespace(id=change.instance_iri) for change in changes]

    def fetch(self, *, instance_iri, class_scope=None, as_reference=False, materialize=False):
        self.db.log.append(("fetch", IRI(instance_iri)))
        return Node(
            id=IRI(instance_iri), data={IRI(CFC.implementsCapability): [Node(id=CAPABILITY)]}
        )


def _middleware(resource: IRI, graph: _Graph, port: int | None = None) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=str(resource),
        service_class=SERVICE_CLASS,
        ogm=_OGM(graph),
        port=port or _free_port(),
        heartbeat_interval=None,
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def _trigger(
    graph: _Graph,
    notified: list,
    status: int = 200,
    hang: float = 0.0,
    gate: threading.Event | None = None,
    arrivals: list | None = None,
) -> Iterator[str]:
    """An event trigger on the loopback interface. Each notification goes into ``notified`` as
    ``(operation IRI, its status in the graph when it arrives)``, and its arrival time into
    ``arrivals``. It is answered with ``status`` after ``hang`` seconds, and only once ``gate``
    is set, if one is given. Yields the address."""

    class EventTrigger(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            operation = IRI(body["operation_iri"])
            notified.append((operation, graph.status.get(operation)))
            if arrivals is not None:
                arrivals.append(time.monotonic())
            time.sleep(hang)
            if gate is not None:
                gate.wait(30)
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args) -> None:  # keep the test output quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), EventTrigger)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextlib.contextmanager
def _served(mw: SemanticMiddleware) -> Iterator[None]:
    """``mw``'s app on its port, without its startup."""
    config = uvicorn.Config(
        mw.app, host="127.0.0.1", port=mw.port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.02)


def _data(change) -> dict:
    return {str(key): value for key, value in change.data.items()}


def _kinds(graph: _Graph) -> list[str]:
    return [kind for kind, *_ in graph.log]


def _status(operation: IRI, status: str) -> list:
    return [Update(instance_iri=operation, data={str(SVC.hasOperationStatus): [status]})]


def _issued(graph: _Graph, issuer_address: str | None) -> None:
    """OPERATION, ``queued``, issued by the planner, which is live at ``issuer_address``."""
    graph.status[OPERATION] = OperationStatus.QUEUED
    graph.issuer[OPERATION] = PLANNER
    if issuer_address is not None:
        graph.live[PLANNER] = [(IRI(f"{EX}planner_service"), issuer_address)]


def _queued(graph: _Graph) -> SemanticMiddleware:
    """The driller, with OPERATION in its in-memory queue, as after its event trigger."""
    driller = _middleware(DRILLER, graph)
    driller._operation_queue.append(OPERATION)
    return driller


# --------------------------------------------------------------------------- #
# issue_operation(): create, then notify, and the issuer recorded.
# --------------------------------------------------------------------------- #


def test_a_request_records_its_issuer_in_the_commit_that_creates_the_operation():
    graph = _Graph()
    notified: list = []
    with _trigger(graph, notified) as address:
        graph.target = (CAPABILITY, IRI(f"{EX}driller_service"), address)
        with _middleware(PLANNER, graph).issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ) as op:
            op.data[str(TORQUE)] = ["4.2 Nm"]

    assert isinstance(op, IssuedOperation)
    assert op.issuer == PLANNER
    # One read finds the receiver, one commit creates the Operation, then the receiver is
    # notified of it, queued.
    assert _kinds(graph) == ["read", "commit"]
    (creation,) = graph.log[1][1]
    assert isinstance(creation, Create) and creation.instance_iri == op.iri
    assert _data(creation) == {
        str(CFC.implementsCapability): [{"id": str(CAPABILITY)}],
        str(SVC.hasOperationStatus): ["queued"],
        str(SVC.issuedBy): [{"id": str(PLANNER)}],
        str(TORQUE): ["4.2 Nm"],
    }
    assert SVC.issuedBy in set(creation.class_scope)
    assert notified == [(op.iri, "queued")]


def test_a_failed_notification_leaves_the_operation_queued_and_raises_notification_undelivered():
    graph = _Graph()
    notified: list = []
    with _trigger(graph, notified, status=503) as address:
        graph.target = (CAPABILITY, IRI(f"{EX}driller_service"), address)
        with pytest.raises(NotificationUndelivered, match="did not take") as raised:
            with _middleware(PLANNER, graph).issue_operation(
                capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
            ) as op:
                pass

    assert isinstance(raised.value.__cause__, httpx.HTTPStatusError)
    # Nothing reverts it: the Operation stays as the request's commit wrote it.
    assert _kinds(graph) == ["read", "commit"]
    assert graph.status[op.iri] == OperationStatus.QUEUED
    assert graph.issuer[op.iri] == PLANNER


@pytest.mark.parametrize(
    "address, cause",
    [
        ("http://[::1", httpx.InvalidURL),
        (f"http://{'a' * 64}.org:9", UnicodeError),  # a host label longer than 63 characters
    ],
    ids=["invalid-url", "label-too-long"],
)
def test_a_malformed_address_leaves_the_operation_queued_and_raises_notification_undelivered(
    address, cause
):
    graph = _Graph()
    graph.target = (CAPABILITY, IRI(f"{EX}driller_service"), address)
    with pytest.raises(NotificationUndelivered, match="did not take") as raised:
        with _middleware(PLANNER, graph).issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ) as op:
            pass

    assert isinstance(raised.value.__cause__, cause)
    # Nothing reverts it: the Operation stays as the request's commit wrote it.
    assert _kinds(graph) == ["read", "commit"]
    assert graph.status[op.iri] == OperationStatus.QUEUED
    assert graph.issuer[op.iri] == PLANNER


def test_a_draft_that_sets_the_issuer_is_refused_before_anything_is_written():
    graph = _Graph()
    graph.target = (CAPABILITY, IRI(f"{EX}driller_service"), "http://127.0.0.1:9")
    with pytest.raises(ValueError, match="issuedBy"):
        with _middleware(PLANNER, graph).issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ) as op:
            op.data[str(SVC.issuedBy)] = [{"id": str(DRILLER)}]

    assert _kinds(graph) == ["read"]


# --------------------------------------------------------------------------- #
# op.update(): a change of an existing instance in the Operation's commit.
# --------------------------------------------------------------------------- #

PART = IRI(f"{EX}part_1")
HAS_OPERATION = IRI(f"{EX}hasOperation")
PART_CLASS = IRI(f"{EX}Part")


def _target(graph: _Graph, address: str = "http://127.0.0.1:9") -> None:
    graph.target = (CAPABILITY, IRI(f"{EX}driller_service"), address)


def _commits(graph: _Graph) -> list[list]:
    return [changes for kind, changes in graph.log if kind == "commit"]


def test_an_update_lands_in_the_commit_that_creates_the_operation_before_the_notification():
    graph = _Graph()
    notified: list = []
    with _trigger(graph, notified) as address:
        _target(graph, address)
        with _middleware(PLANNER, graph).issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ) as op:
            op.update(PART, {str(HAS_OPERATION): [op.iri]})

    assert _kinds(graph) == ["read", "commit"]
    creation, update = graph.log[1][1]
    assert isinstance(creation, Create) and creation.instance_iri == op.iri
    assert _data(creation)[str(SVC.issuedBy)] == [{"id": str(PLANNER)}]
    assert isinstance(update, Update) and update.instance_iri == PART
    assert _data(update) == {str(HAS_OPERATION): [{"id": str(op.iri)}]}  # a reference
    assert notified == [(op.iri, "queued")]


def test_two_updates_of_one_instance_merge_and_the_later_value_wins():
    graph = _Graph()
    _target(graph)
    with pytest.raises(NotificationUndelivered):
        with _middleware(PLANNER, graph).issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ) as op:
            op.update(PART, {str(HAS_OPERATION): [op.iri], str(TORQUE): ["1 Nm"]})
            op.update(str(PART), {str(TORQUE): ["2 Nm"]})

    (commit,) = _commits(graph)
    _, update = commit
    assert _data(update) == {
        str(HAS_OPERATION): [{"id": str(op.iri)}],
        str(TORQUE): ["2 Nm"],
    }


def test_an_update_of_an_instance_created_in_the_block_folds_into_its_create():
    graph = _Graph()
    _target(graph)
    planner = _middleware(PLANNER, graph)
    with pytest.raises(NotificationUndelivered):
        with planner.issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ) as op:
            # The update comes first in the block, but stages after the nested creation.
            with planner.create(PART_CLASS, {}, instance_iri=str(PART)) as part:
                part.update({str(TORQUE): ["1 Nm"]})
            op.update(PART, {str(TORQUE): ["2 Nm"], str(HAS_OPERATION): [op.iri]})

    (commit,) = _commits(graph)
    assert [type(change) for change in commit] == [Create, Create]
    folded = commit[1]
    assert folded.instance_iri == PART
    assert _data(folded) == {
        str(TORQUE): ["2 Nm"],
        str(HAS_OPERATION): [{"id": str(op.iri)}],
    }
    # The Create names every property it carries, so kapps_ogm drops none of them.
    assert {TORQUE, HAS_OPERATION} <= set(folded.class_scope)


def test_an_update_inside_another_commit_lands_in_the_operations_own_commit():
    graph = _Graph()
    _target(graph)
    planner = _middleware(PLANNER, graph)
    other = IRI(f"{EX}other")
    with pytest.raises(NotificationUndelivered):
        with planner.commit(exit=[Update(instance_iri=other, data={str(TORQUE): ["3 Nm"]})]):
            with planner.issue_operation(
                capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
            ) as op:
                op.update(PART, {str(HAS_OPERATION): [op.iri]})

    (own,) = _commits(graph)  # the notification failed, so the outer commit never exits clean
    assert [change.instance_iri for change in own] == [op.iri, PART]


def test_an_outer_commit_exits_on_its_own_after_the_operations_commit_and_its_notification():
    graph = _Graph()
    notified: list = []
    other = IRI(f"{EX}other")
    with _trigger(graph, notified) as address:
        _target(graph, address)
        planner = _middleware(PLANNER, graph)
        outer = [Update(instance_iri=other, data={str(TORQUE): ["3 Nm"]})]
        with planner.commit(exit=outer):
            with planner.issue_operation(
                capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
            ) as op:
                op.update(PART, {str(HAS_OPERATION): [op.iri]})
            assert notified == [(op.iri, "queued")]  # before the outer commit exits

    own, theirs = _commits(graph)
    assert [change.instance_iri for change in own] == [op.iri, PART]
    assert [change.instance_iri for change in theirs] == [other]


def test_an_update_that_names_the_operation_raises_before_any_write():
    graph = _Graph()
    _target(graph)
    with pytest.raises(ValueError, match=r"op\.data"):
        with _middleware(PLANNER, graph).issue_operation(
            capability_class=CAPABILITY_CLASS,
            operation_class=OPERATION_CLASS,
            operation_iri=str(OPERATION),
        ) as op:
            # The Operation's IRI as a plain str, not the IRI that op.iri is: one spelling.
            op.update(f"{EX}Drilling_1", {str(TORQUE): ["1 Nm"]})

    assert _kinds(graph) == ["read"]


def test_values_that_are_not_a_list_raise_before_any_write():
    graph = _Graph()
    _target(graph)
    with pytest.raises(ValueError, match="list"):
        with _middleware(PLANNER, graph).issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ) as op:
            op.update(PART, {str(HAS_OPERATION): op.iri})

    assert _kinds(graph) == ["read"]


def test_an_error_in_the_block_drops_the_updates_and_a_later_update_raises():
    graph = _Graph()
    _target(graph)
    with pytest.raises(KeyError):
        with _middleware(PLANNER, graph).issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ) as op:
            op.update(PART, {str(HAS_OPERATION): [op.iri]})
            raise KeyError("domain work failed")

    assert _kinds(graph) == ["read"]
    with pytest.raises(RuntimeError, match="block"):
        op.update(PART, {str(HAS_OPERATION): []})


def test_an_update_after_the_block_raises():
    graph = _Graph()
    notified: list = []
    with _trigger(graph, notified) as address:
        _target(graph, address)
        with _middleware(PLANNER, graph).issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ) as op:
            pass

    with pytest.raises(RuntimeError, match="block"):
        op.update(PART, {str(HAS_OPERATION): [op.iri]})
    assert len(_commits(graph)) == 1


def test_a_refused_commit_writes_nothing_and_notifies_nobody():
    graph = _Graph()
    notified: list = []
    planner = _middleware(PLANNER, graph)
    refusal = ShaclValidationError("refused", report="the report")

    def refuse(**kwargs):
        raise refusal

    planner.ogm.commit = refuse  # type: ignore[method-assign]
    with _trigger(graph, notified) as address:
        _target(graph, address)
        with pytest.raises(ShaclValidationError):
            with planner.issue_operation(
                capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
            ) as op:
                op.update(PART, {str(HAS_OPERATION): [op.iri]})

    assert _kinds(graph) == ["read"]
    assert graph.status == {}
    assert notified == []


def test_a_failed_notification_leaves_the_update_in_the_store():
    graph = _Graph()
    notified: list = []
    with _trigger(graph, notified, status=503) as address:
        _target(graph, address)
        with pytest.raises(NotificationUndelivered):
            with _middleware(PLANNER, graph).issue_operation(
                capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
            ) as op:
                op.update(PART, {str(HAS_OPERATION): [op.iri]})

    (commit,) = _commits(graph)
    assert [change.instance_iri for change in commit] == [op.iri, PART]
    assert graph.status[op.iri] == OperationStatus.QUEUED


def test_an_update_that_moves_another_operation_notifies_its_issuer():
    graph = _Graph()
    receiver: list = []
    feedback: list = []
    with _trigger(graph, receiver) as address, _trigger(graph, feedback) as issuer_address:
        _target(graph, address)
        _issued(graph, issuer_address)  # OPERATION, queued, issued by the planner
        driller = _middleware(DRILLER, graph)
        with driller.issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ) as op:
            op.update(OPERATION, {str(SVC.hasOperationStatus): [OperationStatus.DONE]})
        _wait_for(lambda: feedback)

    assert receiver == [(op.iri, "queued")]
    assert feedback == [(OPERATION, "done")]


# --------------------------------------------------------------------------- #
# The claimer's middleware tells the issuer, after the claim and after the end.
# --------------------------------------------------------------------------- #


def test_the_claimer_notifies_the_issuer_after_the_claim_and_after_done():
    graph = _Graph()
    notified: list = []
    with _trigger(graph, notified) as address:
        _issued(graph, address)
        driller = _queued(graph)
        with driller.claim_next() as claimed:
            assert driller._flush_notifications(timeout=5)
            assert notified == [(OPERATION, "running")]
            claimed.result = "4 holes done"
        assert driller._flush_notifications(timeout=5)

    assert notified == [(OPERATION, "running"), (OPERATION, "done")]


def test_the_claim_reads_the_issuer_in_the_read_that_it_makes_anyway():
    """Store round trips of a claim with an issuer: the claim's one read, which finds the
    issuer too, the claim, the fetch for the body, and done; and in the background one read of
    the issuer's live Service for each notification."""
    graph = _Graph()
    notified: list = []
    with _trigger(graph, notified) as address:
        _issued(graph, address)
        driller = _queued(graph)
        with driller.claim_next() as claimed:
            assert driller._flush_notifications(timeout=5)
            claimed.result = "4 holes done"
        assert driller._flush_notifications(timeout=5)

    kinds = _kinds(graph)
    assert kinds[:2] == ["read", "commit"]  # the issuer comes with the claim's own read
    assert collections.Counter(kinds) == {"read": 3, "commit": 2, "fetch": 1}


def test_a_failed_body_notifies_the_issuer_after_the_claim_and_after_failed():
    graph = _Graph()
    notified: list = []
    with _trigger(graph, notified) as address:
        _issued(graph, address)
        driller = _queued(graph)
        with pytest.raises(RuntimeError, match="gripper jammed"):
            with driller.claim_next():
                assert driller._flush_notifications(timeout=5)
                raise RuntimeError("gripper jammed")
        assert driller._flush_notifications(timeout=5)

    assert notified == [(OPERATION, "running"), (OPERATION, "failed")]


def test_any_commit_that_moves_an_operation_notifies_its_issuer():
    """A participant that claims and ends an Operation with ``mw.commit()`` notifies the issuer
    as ``claim_next()`` does. Such a commit reads the issuer once, after the store admitted it."""
    graph = _Graph()
    notified: list = []
    with _trigger(graph, notified) as address:
        _issued(graph, address)
        driller = _middleware(DRILLER, graph)
        with pytest.raises(RuntimeError, match="no detector"):
            with driller.commit(
                entry=_status(OPERATION, "running"),
                error=lambda error: _status(OPERATION, "failed"),
            ):
                assert driller._flush_notifications(timeout=5)
                assert notified == [(OPERATION, "running")]
                raise RuntimeError("no detector")
        assert driller._flush_notifications(timeout=5)

    assert notified == [(OPERATION, "running"), (OPERATION, "failed")]
    # Each commit, then in the background the read of the issuer and of its live Service.
    assert _kinds(graph) == ["commit", "read", "read", "commit", "read", "read"]


def test_a_commit_that_sets_the_status_the_operation_already_has_notifies_its_issuer_again():
    """Every commit that sets the status notifies, also one that sets the status the Operation
    already has: the issuer ignores a repeat itself."""
    graph = _Graph()
    notified: list = []
    with _trigger(graph, notified) as address:
        _issued(graph, address)
        driller = _middleware(DRILLER, graph)
        for _ in range(2):
            with driller.commit(entry=_status(OPERATION, "running")):
                pass
        assert driller._flush_notifications(timeout=5)

    assert notified == [(OPERATION, "running"), (OPERATION, "running")]


def test_a_handover_that_completes_an_operation_notifies_its_issuer():
    """``handover(operation=...)`` makes the Operation ``done`` in its entry commit."""
    graph = _Graph()
    notified: list = []
    box, state = IRI(f"{EX}box"), IRI(f"{EX}next_possession")
    next_module, own_state = IRI(f"{EX}next_module"), IRI(f"{EX}driller_possession")

    def rows(sparql: str, answer=graph._rows) -> list[dict]:
        if str(CFC.hasPossessedWorkpiece) in sparql:  # this resource possesses the box
            return [{"ps": own_state}]
        if str(MES.complements) in sparql:  # the counterpart's complementary ability
            return [{"a": MES.Retrieve}]
        if str(CFC.hasPossessor) in sparql:  # the counterpart's one possession state
            return [{"state": state}]
        return answer(sparql)

    graph._rows = rows  # type: ignore[method-assign]
    with _trigger(graph, notified) as address:
        _issued(graph, address)
        graph.status[OPERATION] = OperationStatus.RUNNING
        driller = _middleware(DRILLER, graph)
        with handover(
            driller,
            mode=MES.Pass, workpiece=box, counterpart=next_module, operation=OPERATION
        ):
            assert driller._flush_notifications(timeout=5)
            assert notified == [(OPERATION, "done")]


def test_the_crash_recovery_at_startup_notifies_the_issuer_of_each_orphan():
    graph = _Graph()
    notified: list = []

    def rows(sparql: str, answer=graph._rows) -> list[dict]:
        if str(CFC.hasCapability) in sparql:  # the resource's own Operations, by status
            return [{"op": OPERATION}] if '"running"' in sparql else []
        return answer(sparql)

    graph._rows = rows  # type: ignore[method-assign]
    with _trigger(graph, notified) as address:
        _issued(graph, address)
        graph.status[OPERATION] = OperationStatus.RUNNING
        driller = _middleware(DRILLER, graph)
        asyncio.run(driller._reconstruct_queue())
        assert driller._flush_notifications(timeout=5)

    assert graph.status[OPERATION] == OperationStatus.FAILED
    assert notified == [(OPERATION, "failed")]


def _claim_with_commit(driller: SemanticMiddleware) -> None:
    with driller.commit(entry=_status(OPERATION, "running")):
        pass


def _claim_with_claim_next(driller: SemanticMiddleware) -> None:
    driller._operation_queue.append(OPERATION)
    with driller.claim_next():
        pass


@pytest.mark.parametrize("claim", [_claim_with_commit, _claim_with_claim_next])
def test_an_issuer_that_hangs_does_not_delay_the_claimer(claim):
    """The notification runs in the background: the claimer's commits return while the issuer
    still holds the first notification, and the notifications arrive later."""
    graph = _Graph()
    notified: list = []
    gate = threading.Event()
    with _trigger(graph, notified, gate=gate) as address:
        _issued(graph, address)
        driller = _middleware(DRILLER, graph)
        claim(driller)  # returns: the issuer has not answered
        assert not gate.is_set()
        assert not driller._flush_notifications(timeout=0.2)  # still in flight
        gate.set()
        assert driller._flush_notifications(timeout=10)

    # The claim's notification, and with claim_next() the end's too, all of the one Operation.
    assert notified and {operation for operation, _ in notified} == {OPERATION}


def test_the_notifications_leave_one_after_the_other_in_the_order_of_their_commits():
    """One worker sends them first in, first out: each leaves when the previous one is answered,
    so the claim's notification of an Operation leaves before the end's. Two Operations show the
    order, since a notification names only its Operation."""
    graph = _Graph()
    notified: list = []
    arrivals: list = []
    second = IRI(f"{EX}Drilling_2")
    with _trigger(graph, notified, hang=0.3, arrivals=arrivals) as address:
        _issued(graph, address)
        graph.status[second] = OperationStatus.QUEUED
        graph.issuer[second] = PLANNER
        driller = _middleware(DRILLER, graph)
        for operation in (OPERATION, second, OPERATION):
            status = "running" if graph.status[operation] == "queued" else "done"
            with driller.commit(entry=_status(operation, status)):
                pass
        assert driller._flush_notifications(timeout=10)

    assert [operation for operation, _ in notified] == [OPERATION, second, OPERATION]
    # Each one left after the previous one was answered, not beside it.
    assert all(later - earlier >= 0.25 for earlier, later in zip(arrivals, arrivals[1:]))


def test_a_stop_sends_what_is_waiting_when_the_issuer_answers():
    graph = _Graph()
    notified: list = []
    with _trigger(graph, notified) as address:
        _issued(graph, address)
        driller = _middleware(DRILLER, graph)
        with driller.commit(entry=_status(OPERATION, "running"), exit=_status(OPERATION, "done")):
            pass
        asyncio.run(driller._stop_notifications())

    assert len(notified) == 2


def test_a_stop_drops_what_is_still_waiting_after_its_time_and_says_so(caplog):
    """A stop does not wait for an issuer that hangs: after its time it drops the notifications
    that have not started, and logs one WARNING line with their number."""
    graph = _Graph()
    notified: list = []
    gate = threading.Event()
    with _trigger(graph, notified, gate=gate) as address:
        _issued(graph, address)
        driller = _middleware(DRILLER, graph)
        with driller.commit(entry=_status(OPERATION, "running"), exit=_status(OPERATION, "done")):
            pass
        with caplog.at_level(logging.WARNING, logger=MIDDLEWARE_LOGGER):
            asyncio.run(driller._stop_notifications())
        gate.set()
        assert driller._flush_notifications(timeout=10)

    assert len(notified) == 1  # the claim's, which was in flight; the end's was dropped
    (dropped,) = [r for r in caplog.records if "dropped" in r.getMessage()]
    assert dropped.args[1] == 1


def _claim_data(changes) -> dict:
    (change,) = changes
    return {str(key): values for key, values in change.data.items()}


def test_a_claim_records_the_claimers_own_workflow_as_its_executor():
    """The claim commits `running` and the executor, this instance's own Workflow of the
    Operation's Capability, in one change, even when the graph names another Service's Workflow
    as the Capability's realizer; the end records the same Workflow."""
    graph = _Graph()
    graph.status[OPERATION] = OperationStatus.QUEUED

    def rows(sparql: str, answer=graph._rows) -> list[dict]:
        if str(SVC.realizedByWorkflow) in sparql:  # the claim's read, with the Capability
            return [{"wf": WORKFLOW, "cap": CAPABILITY}]
        return answer(sparql)

    graph._rows = rows  # type: ignore[method-assign]
    driller = _queued(graph)

    @driller.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    def drill(operation: Node) -> None:
        """Drill."""

    own = mint_workflow_iri(driller.service_iri, "drill")
    assert own != WORKFLOW
    with driller.claim_next() as claimed:
        claimed.result = "4 holes done"
    assert driller._flush_notifications(timeout=5)

    claim, end = [entry[1] for entry in graph.log if entry[0] == "commit"]
    assert _claim_data(claim) == {
        str(SVC.hasOperationStatus): [OperationStatus.RUNNING],
        str(SVC.executedByWorkflow): [{"id": str(own)}],
    }
    assert _claim_data(end)[str(SVC.executedByWorkflow)] == [{"id": str(own)}]


def test_a_claim_without_an_own_workflow_records_no_executor():
    """An instance that exposes no workflow for the Capability claims with the status alone, and
    the end records the realizing Workflow, as before."""
    graph = _Graph()
    graph.status[OPERATION] = OperationStatus.QUEUED
    driller = _queued(graph)
    with driller.claim_next() as claimed:
        claimed.result = "4 holes done"
    assert driller._flush_notifications(timeout=5)

    claim, end = [entry[1] for entry in graph.log if entry[0] == "commit"]
    assert _claim_data(claim) == {str(SVC.hasOperationStatus): [OperationStatus.RUNNING]}
    assert _claim_data(end)[str(SVC.executedByWorkflow)] == [{"id": str(WORKFLOW)}]


def test_a_claim_through_a_generic_commit_records_no_executor():
    """The common pattern, a `commit` whose entry sets `running`, writes the status alone,
    also on an instance that exposes a workflow for the Capability, so the sweep fails such an
    Operation as before."""
    graph = _Graph()
    graph.status[OPERATION] = OperationStatus.QUEUED
    driller = _queued(graph)

    @driller.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    def drill(operation: Node) -> None:
        """Drill."""

    with driller.commit(entry=_status(OPERATION, "running"), exit=_status(OPERATION, "done")):
        pass
    assert driller._flush_notifications(timeout=5)

    claim = [entry[1] for entry in graph.log if entry[0] == "commit"][0]
    assert str(SVC.executedByWorkflow) not in _claim_data(claim)


def test_an_operation_without_an_issuer_notifies_nobody():
    graph = _Graph()
    graph.status[OPERATION] = OperationStatus.QUEUED
    driller = _queued(graph)
    with driller.claim_next() as claimed:
        claimed.result = "4 holes done"
    assert driller._flush_notifications(timeout=5)

    # No read of a live Service: there is no issuer to notify.
    assert _kinds(graph) == ["read", "commit", "fetch", "commit"]


def test_a_commit_that_moves_no_operation_reads_nothing_more():
    graph = _Graph()
    torque = [Update(instance_iri=OPERATION, data={str(TORQUE): ["4.2 Nm"]})]
    driller = _middleware(DRILLER, graph)
    with driller.commit(entry=torque):
        pass
    assert driller._flush_notifications(timeout=5)

    assert _kinds(graph) == ["commit"]


def test_an_issuer_that_cannot_be_notified_is_logged_and_the_work_goes_on(caplog):
    graph = _Graph()
    _issued(graph, None)  # the planner has no live Service
    ran = []
    driller = _queued(graph)
    with caplog.at_level(logging.WARNING, logger=MIDDLEWARE_LOGGER):
        with driller.claim_next() as claimed:
            ran.append(True)
            claimed.result = "4 holes done"
        assert driller._flush_notifications(timeout=5)

    assert ran == [True]
    assert graph.status[OPERATION] == OperationStatus.DONE
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all(str(PLANNER) in r.getMessage() and "\n" not in r.getMessage() for r in warnings)


def test_the_watchdog_notifies_the_issuer_of_an_operation_that_it_fails():
    """A stale Service's stranded Operations become ``failed``, and their issuers hear of it."""
    graph = _Graph()
    notified: list = []
    stale = IRI(f"{EX}driller_service")

    def rows(sparql: str, answer=graph._rows) -> list[dict]:
        if str(CFC.hasCapability) in sparql:  # the Resource's stranded Operations
            return [{"op": OPERATION}]
        if str(SVC.lastHeartbeat) in sparql:  # the stale Services
            return [{"s": stale}]
        if f"<{stale}> <{SVC.isServiceOf}>" in sparql:  # the Resource of the stale Service
            return [{"r": DRILLER}]
        return answer(sparql)

    graph._rows = rows  # type: ignore[method-assign]
    with _trigger(graph, notified) as address:
        _issued(graph, address)
        graph.status[OPERATION] = OperationStatus.RUNNING
        watchdog = SemanticMiddleware(mode="watchdog", ogm=_OGM(graph))
        assert asyncio.run(watchdog.sweep()) == [str(stale)]
        assert watchdog._flush_notifications(timeout=5)

    assert notified == [(OPERATION, "failed")]


# --------------------------------------------------------------------------- #
# The issuer's middleware: the feedback is no work for it, and it wakes the handle.
# --------------------------------------------------------------------------- #


def test_the_issuer_does_not_queue_a_notification_of_its_own_operation_that_is_claimed():
    graph = _Graph()
    planner = _middleware(PLANNER, graph)
    _issued(graph, planner.address)
    graph.status[OPERATION] = OperationStatus.RUNNING
    with _served(planner):
        answer = httpx.post(
            f"{planner.address}/workflows/event_trigger/execute",
            json={"operation_iri": str(OPERATION)},
            timeout=5,
        )

    assert answer.status_code == 200
    assert answer.json() == {"operation": str(OPERATION)}
    with pytest.raises(OperationQueueEmpty):
        with planner.claim_next():
            pass


def test_the_issuers_notification_handler_sees_both_notifications():
    graph = _Graph()
    planner = _middleware(PLANNER, graph)
    seen: list = []
    planner.on_notification(lambda operation: seen.append(operation.id))
    _issued(graph, planner.address)
    with _served(planner):
        with _queued(graph).claim_next() as claimed:
            claimed.result = "4 holes done"
        _wait_for(lambda: len(seen) == 2)

    assert seen == [OPERATION, OPERATION]


def _issue(graph: _Graph, planner: SemanticMiddleware, receiver_address: str) -> IssuedOperation:
    graph.target = (CAPABILITY, IRI(f"{EX}driller_service"), receiver_address)
    with planner.issue_operation(
        capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
    ) as op:
        pass
    graph.live[PLANNER] = [(IRI(f"{EX}planner_service"), planner.address)]
    return op


def _claim_and_end(graph: _Graph, operation: IRI, end: str, pause: float = 0.3) -> None:
    """The driller, in another thread: it claims ``operation`` after ``pause``, and ends it
    ``end`` after another ``pause``, each through ``mw.commit()``."""

    def run() -> None:
        driller = _middleware(DRILLER, graph)
        time.sleep(pause)
        with driller.commit(entry=_status(operation, "running"), exit=_status(operation, end)):
            time.sleep(pause)

    threading.Thread(target=run, daemon=True).start()


def test_the_handle_wakes_when_the_claim_and_the_end_are_notified():
    graph = _Graph()
    planner = _middleware(PLANNER, graph)
    notified: list = []
    with _served(planner), _trigger(graph, notified) as receiver:
        op = _issue(graph, planner, receiver)
        _claim_and_end(graph, op.iri, "done")
        started = time.monotonic()
        assert op.wait_claimed(timeout=10) is True
        assert op.wait_ended(timeout=10) == "done"
        # Woken by the notifications, not by the end of a timeout.
        assert time.monotonic() - started < 5


def test_the_handle_awaits_the_claim_and_the_end():
    graph = _Graph()
    planner = _middleware(PLANNER, graph)
    notified: list = []
    with _served(planner), _trigger(graph, notified) as receiver:
        op = _issue(graph, planner, receiver)
        _claim_and_end(graph, op.iri, "failed")

        async def wait() -> tuple:
            return await op.claimed(timeout=10), await op.ended(timeout=10)

        started = time.monotonic()
        assert asyncio.run(wait()) == (True, "failed")
        assert time.monotonic() - started < 5


def _with_a_handler_whose_read_fails(graph: _Graph, planner: SemanticMiddleware) -> None:
    """The planner has a notification handler, and the intake's read fails."""
    planner.on_notification(lambda operation: None)

    def fetch(**kwargs):
        raise RuntimeError("the store is down")

    planner.ogm.fetch = fetch  # type: ignore[method-assign]


def _without_a_handler_whose_read_fails(graph: _Graph, planner: SemanticMiddleware) -> None:
    """The planner has no notification handler, and its read of the notified Operation fails."""

    def rows(sparql: str, answer=graph._rows) -> list[dict]:
        if "?type" in sparql:
            raise RuntimeError("the store is down")
        return answer(sparql)

    graph._rows = rows  # type: ignore[method-assign]


@pytest.mark.parametrize(
    "fail", [_with_a_handler_whose_read_fails, _without_a_handler_whose_read_fails]
)
def test_a_notification_wakes_the_handle_also_when_the_triggers_own_read_fails(fail):
    """The handle reads the status itself, so the event trigger wakes it before its own read."""
    graph = _Graph()
    planner = _middleware(PLANNER, graph)
    fail(graph, planner)
    notified: list = []
    with _served(planner), _trigger(graph, notified) as receiver:
        op = _issue(graph, planner, receiver)
        _claim_and_end(graph, op.iri, "done")
        started = time.monotonic()
        assert op.wait_ended(timeout=4) == "done"
        # Woken by the notification, not by the end of the timeout.
        assert time.monotonic() - started < 2


def test_a_claim_before_the_wait_is_read_from_the_graph():
    graph = _Graph()
    planner = _middleware(PLANNER, graph)  # not served: no notification can arrive
    notified: list = []
    with _trigger(graph, notified) as receiver:
        op = _issue(graph, planner, receiver)
    graph.status[op.iri] = OperationStatus.DONE

    assert op.wait_claimed(timeout=0.2) is True
    assert op.wait_ended(timeout=0.2) == "done"
    assert asyncio.run(op.claimed(timeout=0.2)) is True


def test_claimed_is_false_and_ended_raises_when_the_timeout_passes_first():
    graph = _Graph()
    planner = _middleware(PLANNER, graph)
    notified: list = []
    with _trigger(graph, notified) as receiver:
        op = _issue(graph, planner, receiver)

    assert op.wait_claimed(timeout=0.2) is False
    assert asyncio.run(op.claimed(timeout=0.2)) is False
    with pytest.raises(TimeoutError):
        op.wait_ended(timeout=0.2)
    with pytest.raises(TimeoutError):
        asyncio.run(op.ended(timeout=0.2))


def _driller_that_drills(graph: _Graph, runs: list, seen: list) -> SemanticMiddleware:
    """The driller, live in the graph, with a workflow that claims and ends each Operation it
    gets, and a notification handler."""
    driller = _middleware(DRILLER, graph)

    @driller.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    def drill(operation: Node) -> None:
        runs.append(operation.id)
        with driller.commit(
            entry=_status(operation.id, "running"), exit=_status(operation.id, "done")
        ):
            pass

    driller.on_notification(lambda operation: seen.append(operation.id))
    graph.live[DRILLER] = [(driller.service_iri, driller.address)]
    return driller


def test_after_a_restart_an_operation_issued_to_itself_runs_its_workflow_once():
    """The instance issued the Operation to itself before it restarted, so nothing in its memory
    tells it so. The claim and the end still reach the handler and never the workflow: the intake
    reads the issuer and the status from the graph."""
    graph = _Graph()
    runs: list = []
    seen: list = []
    driller = _driller_that_drills(graph, runs, seen)
    graph.status[OPERATION] = OperationStatus.QUEUED
    graph.issuer[OPERATION] = DRILLER
    with _served(driller):
        httpx.post(
            f"{driller.address}/workflows/event_trigger/execute",
            json={"operation_iri": str(OPERATION)},
            timeout=5,
        ).raise_for_status()
        _wait_for(lambda: len(seen) == 2)
        time.sleep(0.3)

    assert runs == [OPERATION]
    assert seen == [OPERATION, OPERATION]
    assert graph.status[OPERATION] == OperationStatus.DONE


def test_an_operation_that_an_instance_issues_to_itself_runs_its_workflow_once():
    """The feedback of a claim reaches the instance that claimed: it goes to its notification
    handler and never runs the workflow again."""
    graph = _Graph()
    runs: list = []
    seen: list = []
    driller = _driller_that_drills(graph, runs, seen)
    graph.target = (CAPABILITY, driller.service_iri, driller.address)
    with _served(driller):
        with driller.issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ) as op:
            pass
        assert op.wait_ended(timeout=10) == "done"
        _wait_for(lambda: len(seen) == 2)
        time.sleep(0.3)

    assert runs == [op.iri]
    assert seen == [op.iri, op.iri]
