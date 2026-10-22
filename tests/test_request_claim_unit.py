"""`issue_operation()` and `claim_next()` on `mw.commit()`, without a triple store.

The OGM is faked at the calls the middleware makes through it: `commit` (either form), whose
result is the node of each change, `fetch`, and the reads on `ogm.db`. The fake has no direct
write, so one would raise. Every write, every fetch and every notification that a receiver gets
go into one log, so a test reads what reached the store and in which order. The receiver of a
dispatch is a real HTTP server on the loopback interface. The issuer of an Operation, and what
the claimer tells it, are `test_issued_operation.py`'s. The live counterpart is
`test_request_claim_integration.py`.
"""

from __future__ import annotations

import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Iterator

import pytest
from kapps_ogm import Create, Update
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import TripleStoreResponseError

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.vocabulary import CFC, SVC

EX = "http://example.org/test#"
RESOURCE = IRI(f"{EX}driller")
OPERATION_CLASS = IRI(f"{EX}Drilling")
CAPABILITY_CLASS = IRI(f"{EX}DrillingCapability")
CAPABILITY = IRI(f"{EX}driller_capability_drill")
SERVICE = IRI(f"{EX}driller_service")
WORKFLOW = IRI(f"{EX}driller_service_workflow_drill")
TORQUE = IRI(f"{EX}torque")


class _FakeDB:
    """``ogm.db``: answers every read with one row. It has no write."""

    def __init__(self, log: list, address: str) -> None:
        self.log = log
        self.address = address

    def query(self, sparql, convert_bindings=False):
        # One row serves each read the middleware makes here: the dispatch target (cap, svc,
        # addr) and the Workflow that realizes an Operation's Capability (wf). No Operation has
        # an issuer, so nobody is told of a claim.
        row = {"cap": CAPABILITY, "svc": SERVICE, "addr": self.address, "wf": WORKFLOW}
        return {"results": {"bindings": [row]}}


class _FakeOGM:
    """Records each admitted commit as its list of changes; the one-instance form becomes an
    ``Update``, so both forms read alike. ``refuse`` names the commits the store refuses, and
    ``unfetchable`` the instances it holds no class for. Each fetch is recorded too."""

    def __init__(self, *, address: str = "http://127.0.0.1:9", refuse=None, unfetchable=()) -> None:
        self.log: list = []
        self.refuse = refuse or (lambda changes: False)
        self.unfetchable = set(unfetchable)
        self.db = _FakeDB(self.log, address)

    def commit(self, *, changes=None, instance_iri=None, data=None, named_graph=None):
        if changes is None:
            changes = [Update(instance_iri=instance_iri, data=data)]
        changes = list(changes)
        if self.refuse(changes):
            raise TripleStoreResponseError("SHACL validation failed")
        self.log.append(("commit", changes))
        return [SimpleNamespace(id=change.instance_iri) for change in changes]

    def fetch(self, *, instance_iri, class_scope=None, as_reference=False, materialize=False):
        self.log.append(("fetch", IRI(instance_iri)))
        if IRI(instance_iri) in self.unfetchable:
            raise ValueError(f"Could not determine class IRI for instance {instance_iri}")
        fetched_with = {
            "class_scope": class_scope,
            "as_reference": as_reference,
            "materialize": materialize,
        }
        return SimpleNamespace(
            id=IRI(instance_iri), instance={"id": str(instance_iri)}, fetched_with=fetched_with
        )


def _middleware(ogm) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=str(RESOURCE),
        service_class=f"{EX}DrillerService",
        ogm=ogm,
        heartbeat_interval=None,
    )


@contextlib.contextmanager
def _receiver(log: list, status: int = 200) -> Iterator[str]:
    """A receiver's event trigger on the loopback interface. Each notification goes into
    ``log`` as ``("notification", operation IRI)``, and is answered with ``status``. Yields the
    address."""

    class EventTrigger(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            log.append(("notification", IRI(body["operation_iri"])))
            answer = json.dumps({"operation": body["operation_iri"]}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

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


def _data(change) -> dict:
    return {str(key): value for key, value in change.data.items()}


# --------------------------------------------------------------------------- #
# issue_operation(): one commit creates the Operation, then the receiver is notified.
# --------------------------------------------------------------------------- #


def test_a_dispatch_creates_its_operation_in_one_commit_before_the_receiver_is_notified():
    ogm = _FakeOGM()
    with _receiver(ogm.log) as address:
        ogm.db.address = address
        with _middleware(ogm).issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ) as op:
            op.data[str(TORQUE)] = ["4.2 Nm"]

    assert [entry[0] for entry in ogm.log] == ["commit", "notification"]
    (creation,) = ogm.log[0][1]
    assert isinstance(creation, Create)
    assert (creation.class_iri, creation.instance_iri) == (OPERATION_CLASS, op.iri)
    assert _data(creation) == {
        str(CFC.implementsCapability): [{"id": str(CAPABILITY)}],
        str(SVC.hasOperationStatus): ["queued"],
        str(SVC.issuedBy): [{"id": str(RESOURCE)}],
        str(TORQUE): ["4.2 Nm"],
    }
    # Without a scope that names them, kapps_ogm drops the properties its class does not know.
    assert set(creation.class_scope) == {
        CFC.implementsCapability,
        SVC.hasOperationStatus,
        SVC.issuedBy,
        TORQUE,
    }
    assert ogm.log[1] == ("notification", op.iri)


def test_a_refused_creation_notifies_no_receiver():
    ogm = _FakeOGM(refuse=lambda changes: True)
    with _receiver(ogm.log) as address:
        ogm.db.address = address
        with pytest.raises(TripleStoreResponseError):
            with _middleware(ogm).issue_operation(
                capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
            ) as op:
                op.data[str(TORQUE)] = ["4.2 Nm"]
    assert ogm.log == []


def test_the_deprecated_request_warns_names_issue_operation_and_still_issues_the_operation():
    ogm = _FakeOGM()
    with _receiver(ogm.log) as address:
        ogm.db.address = address
        with pytest.warns(DeprecationWarning, match=r"issue_operation\(\)") as caught:
            with _middleware(ogm).request(
                capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
            ) as op:
                op.data[str(TORQUE)] = ["4.2 Nm"]

    # The warning points at the caller, not at the alias.
    assert caught[0].filename == __file__
    # The effect is that of issue_operation(): one commit creates the Operation, then the
    # receiver is notified.
    assert [entry[0] for entry in ogm.log] == ["commit", "notification"]
    (creation,) = ogm.log[0][1]
    assert isinstance(creation, Create)
    assert (creation.class_iri, creation.instance_iri) == (OPERATION_CLASS, op.iri)
    assert _data(creation)[str(TORQUE)] == ["4.2 Nm"]


# --------------------------------------------------------------------------- #
# claim_next(): the entry commits the claim, the exit records done or failed.
# --------------------------------------------------------------------------- #

OPERATION = IRI(f"{EX}Drilling_op_1")


def _queued(ogm) -> SemanticMiddleware:
    """A middleware whose in-memory queue holds OPERATION, as after its event trigger."""
    mw = _middleware(ogm)
    mw._operation_queue.append(OPERATION)
    return mw


def _sets_status(status: str):
    """A refusal rule: the store refuses every commit that sets the status to ``status``."""
    return lambda changes: any(
        _data(change).get(str(SVC.hasOperationStatus)) == [status] for change in changes
    )


def _statuses(ogm) -> list:
    """The status each admitted commit set, in order."""
    return [
        _data(change)[str(SVC.hasOperationStatus)][0]
        for kind, changes in ogm.log
        if kind == "commit"
        for change in changes
    ]


def _commits(ogm) -> list:
    """The changes of each admitted commit, in order."""
    return [changes for kind, *rest in ogm.log if kind == "commit" for changes in rest]


def test_a_refused_done_records_failed_with_its_provenance_and_raises_the_stores_error():
    ogm = _FakeOGM(refuse=_sets_status("done"))
    with pytest.raises(TripleStoreResponseError, match="SHACL validation failed"):
        with _queued(ogm).claim_next() as claimed:
            claimed.result = "4 holes done"

    assert _statuses(ogm) == ["running", "failed"]
    (failed,) = ogm.log[-1][1]
    record = _data(failed)
    assert record[str(SVC.executedByWorkflow)] == [{"id": str(WORKFLOW)}]
    assert record[str(SVC.executionResult)] == ["SHACL validation failed"]
    assert json.loads(record[str(SVC.failureState)][0]) == {"id": str(RESOURCE)}


def test_the_claim_is_committed_before_the_body_runs_and_done_with_its_provenance_is_one_commit():
    ogm = _FakeOGM()
    scope = object()
    with _queued(ogm).claim_next(scope) as claimed:
        assert [kind for kind, *_ in ogm.log] == ["commit", "fetch"]
        assert _statuses(ogm) == ["running"]
        assert claimed.iri == OPERATION
        # The body reads the Operation as fetched after the claim, under its scope.
        assert claimed.operation.fetched_with == {
            "class_scope": scope,
            "as_reference": False,
            "materialize": True,
        }
        claimed.result = "4 holes done"

    claim, exit_commit = _commits(ogm)
    (done,) = exit_commit
    record = _data(done)
    assert record.pop(str(SVC.executionTimestamp))
    assert record == {
        str(SVC.hasOperationStatus): ["done"],
        str(SVC.executedByWorkflow): [{"id": str(WORKFLOW)}],
        str(SVC.executionResult): ["4 holes done"],
    }


def test_an_error_in_the_body_records_failed_with_its_provenance_and_the_datamodel_in_one_commit():
    ogm = _FakeOGM()
    with pytest.raises(RuntimeError, match="gripper jammed"):
        with _queued(ogm).claim_next():
            raise RuntimeError("gripper jammed")

    claim, error_commit = _commits(ogm)
    (failed,) = error_commit
    record = _data(failed)
    assert record.pop(str(SVC.executionTimestamp))
    assert json.loads(record.pop(str(SVC.failureState))[0]) == {"id": str(RESOURCE)}
    assert record == {
        str(SVC.hasOperationStatus): ["failed"],
        str(SVC.executedByWorkflow): [{"id": str(WORKFLOW)}],
        str(SVC.executionResult): ["gripper jammed"],
    }


def test_a_refused_claim_runs_no_body_and_writes_nothing():
    ogm = _FakeOGM(refuse=_sets_status("running"))
    mw = _queued(ogm)
    ran = []
    with pytest.raises(TripleStoreResponseError):
        with mw.claim_next():
            ran.append(True)
    assert ran == []
    assert _commits(ogm) == []
    # It left the in-memory queue before its claim, as it did before the claim was a commit
    # entry, so this instance does not try it again. The graph keeps it as it was.
    assert mw._operation_queue == []


def test_an_operation_that_cannot_be_fetched_after_its_claim_runs_no_body_and_is_recorded_failed():
    ogm = _FakeOGM(unfetchable={OPERATION})
    ran = []
    with pytest.raises(ValueError, match="Could not determine class"):
        with _queued(ogm).claim_next():
            ran.append(True)
    assert ran == []
    assert _statuses(ogm) == ["running", "failed"]
