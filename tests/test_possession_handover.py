"""The possession extension's handover, on a middleware that has only its public surface.

`possession.handover(mw, ...)` uses `mw.mode`, `mw.resource`, `mw.ogm` and `mw.commit()`, and
nothing else of the middleware. So these tests give it exactly that: a stand-in whose OGM answers
the handover's reads from a table, and whose `commit()` records each entry and can refuse it with
the store's `PreconditionFailedError`. The store's own behaviour, the SHACL refusals and the races
between two processes, is in `test_handover_integration.py`.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Optional

import pytest
from kapps_ogm import Create, Update
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import (
    PreconditionFailedError,
    TripleStoreInterfaceError,
)

from kapps_semantic_middleware import possession
from kapps_semantic_middleware.errors import (
    HandoverPreconditionError,
    PossessionSwitchConflict,
)
from kapps_semantic_middleware.modes import Mode
from kapps_semantic_middleware.possession import handover
from kapps_semantic_middleware.vocabulary import CFC, MES, SVC, OperationStatus

EX = "http://example.org/"
GIVER = IRI(f"{EX}giver")
PEER = IRI(f"{EX}peer")
WORKPIECE = IRI(f"{EX}workpiece")
GIVER_STATE = IRI(f"{EX}giver_state")
PEER_STATE = IRI(f"{EX}peer_state")
OPERATION = IRI(f"{EX}operation")


@dataclass
class _Graph:
    """What the handover's reads find: who possesses the workpiece, the peer's ability, and the
    peer's possession states."""

    possessed_by: Optional[IRI] = GIVER
    peer_complements: bool = True
    peer_states: List[IRI] = field(default_factory=lambda: [PEER_STATE])

    def query(self, sparql: str, convert_bindings: bool = False) -> dict:
        if str(MES.complements) in sparql:
            rows = [{"a": f"{EX}ability"}] if self.peer_complements and str(PEER) in sparql else []
        elif str(CFC.hasPossessedWorkpiece) in sparql:
            rows = [{"ps": str(GIVER_STATE)}] if f"<{self.possessed_by}>" in sparql else []
        else:  # the possession state of a resource
            rows = [{"state": state} for state in self.peer_states] if str(PEER) in sparql else []
        return {"results": {"bindings": rows}}


@dataclass
class _OGM:
    db: _Graph


class _Middleware:
    """A middleware's public surface: its mode, its Resource, its OGM, and its `commit()`.

    `refusals` lists, per call of `commit()`, whether the store refuses that entry: its error is
    raised at the entry, before the body, as `mw.commit()` raises it. `on_refusal` runs just before
    a refusal, as the write that made the store refuse it. After the entry, an admitted body ends
    as `mw.commit()` ends it: a clean exit commits `exit`, an error commits `error(exc)` and is
    raised as it is. Each such commit is one more item of `commits`, with the verb of the call.
    """

    def __init__(self, graph: _Graph, *refusals: bool, mode: Mode = Mode.RESOURCE) -> None:
        self.mode = mode
        self.resource = GIVER if mode == Mode.RESOURCE else None
        self.ogm = _OGM(graph)
        self.refusals = list(refusals)
        self.on_refusal = lambda: None
        self.commits: List[tuple] = []

    def commit(
        self, *, entry: Any = (), exit: Any = (), error: Any = (), verb: str = "commit"
    ) -> Any:
        self.commits.append((list(entry), verb))
        refused = self.refusals.pop(0) if self.refusals else False
        return self._entered(refused, exit, error, verb)

    @contextlib.contextmanager
    def _entered(self, refused: bool, exit: Any, error: Any, verb: str) -> Iterator[None]:
        if refused:
            self.on_refusal()
            raise PreconditionFailedError("the precondition of the commit was false")
        try:
            yield
        except Exception as exc:
            changes = error(exc) if callable(error) else list(error)
            if changes:
                self.commits.append((changes, verb))
            raise
        changes = exit() if callable(exit) else list(exit)
        if changes:
            self.commits.append((changes, verb))


def _moved_to(entry: List[Any]) -> list:
    """The possession states that the entry points the workpiece at."""
    return [
        change.data[str(CFC.hasPossessedWorkpiece)]
        for change in entry
        if isinstance(change, Update) and change.instance_iri == WORKPIECE
    ]


def test_the_entry_commits_the_possession_change_before_the_body_runs():
    mw = _Middleware(_Graph())
    seen = []

    with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
        seen.append(len(mw.commits))

    assert seen == [1]  # without an Operation, one commit at entry and none at exit
    ((entry, verb),) = mw.commits
    assert verb == "handover"
    assert _moved_to(entry) == [[{"id": str(PEER_STATE)}]]


def test_the_entry_carries_the_operation_and_the_callers_changes_in_the_same_commit():
    mw = _Middleware(_Graph())
    other = Update(instance_iri=IRI(f"{EX}transport"), data={f"{EX}p": ["v"]})

    with handover(
        mw,
        mode=MES.Pass,
        workpiece=WORKPIECE,
        counterpart=PEER,
        workpiece_update={f"{EX}state": ["arrived"]},
        operation=OPERATION,
        entry=[other],
    ):
        pass

    (entry, _), _exit = mw.commits
    (moved,) = [c for c in entry if getattr(c, "instance_iri", None) == WORKPIECE]
    assert moved.data[f"{EX}state"] == ["arrived"]
    (running,) = [c for c in entry if getattr(c, "instance_iri", None) == OPERATION]
    assert running.data == {str(SVC.hasOperationStatus): [OperationStatus.RUNNING]}
    assert other in entry
    assert len(mw.commits) == 2  # the entry, then `done`


def _operation_statuses(mw: _Middleware) -> list:
    """The status that each commit gives the Operation, in order."""
    return [
        change.data[str(SVC.hasOperationStatus)][0]
        for changes, _ in mw.commits
        for change in changes
        if getattr(change, "instance_iri", None) == OPERATION
    ]


def test_a_clean_body_commits_done_after_the_move_in_its_own_commit():
    mw = _Middleware(_Graph())
    seen = []

    with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER, operation=OPERATION):
        seen.append(_operation_statuses(mw))

    assert seen == [[OperationStatus.RUNNING]]  # nothing `done` while the body runs
    assert _operation_statuses(mw) == [OperationStatus.RUNNING, OperationStatus.DONE]
    assert [verb for _, verb in mw.commits] == ["handover", "handover"]
    exit_changes, _ = mw.commits[1]
    assert len(exit_changes) == 1  # only the Operation


def test_an_error_in_the_body_commits_failed_with_its_message_and_keeps_the_possession():
    mw = _Middleware(_Graph())

    with pytest.raises(RuntimeError, match="boom"):
        with handover(
            mw,
            mode=MES.Pass,
            workpiece=WORKPIECE,
            counterpart=PEER,
            operation=OPERATION,
        ):
            raise RuntimeError("boom")

    assert _operation_statuses(mw) == [OperationStatus.RUNNING, OperationStatus.FAILED]
    ((failed,), _) = mw.commits[1]
    assert failed.data[str(SVC.hasExecutionResult)] == ["boom"]
    assert _moved_to(mw.commits[0][0]) == [[{"id": str(PEER_STATE)}]]  # nothing undoes it


def test_a_refused_entry_writes_nothing_of_the_operation_and_runs_no_body():
    mw = _Middleware(_Graph(), True)

    with pytest.raises(PossessionSwitchConflict):
        with handover(
            mw,
            mode=MES.Pass,
            workpiece=WORKPIECE,
            counterpart=PEER,
            operation=OPERATION,
        ):
            pytest.fail("the body of a refused handover ran")

    assert len(mw.commits) == 1  # the refused entry, and no exit or error commit


def test_the_retry_of_the_entry_carries_running_and_the_same_exit():
    graph = _Graph(peer_states=[])
    mw = _Middleware(graph, True)
    mw.on_refusal = lambda: graph.peer_states.append(PEER_STATE)

    with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER, operation=OPERATION):
        pass

    assert _operation_statuses(mw) == [
        OperationStatus.RUNNING,
        OperationStatus.RUNNING,
        OperationStatus.DONE,
    ]

    graph = _Graph(peer_states=[])
    mw = _Middleware(graph, True)
    mw.on_refusal = lambda: graph.peer_states.append(PEER_STATE)
    with pytest.raises(ValueError, match="boom"):
        with handover(
            mw,
            mode=MES.Pass,
            workpiece=WORKPIECE,
            counterpart=PEER,
            operation=OPERATION,
        ):
            raise ValueError("boom")
    assert _operation_statuses(mw)[-1] == OperationStatus.FAILED


@pytest.mark.parametrize(
    "graph",
    [_Graph(possessed_by=PEER), _Graph(peer_complements=False)],
    ids=["the giver does not possess it", "the peer lacks the complementary ability"],
)
def test_a_failed_precondition_writes_nothing_and_skips_the_body(graph):
    mw = _Middleware(graph)

    with pytest.raises(HandoverPreconditionError):
        with handover(
            mw,
            mode=MES.Pass,
            workpiece=WORKPIECE,
            counterpart=PEER,
            operation=OPERATION,
        ):
            pytest.fail("the body of a refused handover ran")

    assert mw.commits == []


def test_a_handover_outside_resource_mode_is_refused_before_any_read():
    mw = _Middleware(_Graph(), mode=Mode.WATCHDOG)

    with pytest.raises(RuntimeError, match="resource mode"):
        with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
            pytest.fail("the body ran outside resource mode")

    assert mw.commits == []


def test_a_first_handover_whose_peer_got_its_state_meanwhile_reads_again_and_commits_once_more():
    graph = _Graph(peer_states=[])
    mw = _Middleware(graph, True)
    mw.on_refusal = lambda: graph.peer_states.append(PEER_STATE)
    bodies = []

    with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
        bodies.append("ran")

    assert bodies == ["ran"]
    (first, _), (second, verb) = mw.commits
    assert any(isinstance(change, Create) for change in first)  # it created the state
    assert not any(isinstance(change, Create) for change in second)
    assert _moved_to(second) == [[{"id": str(PEER_STATE)}]]
    assert verb == "handover"


def test_the_second_try_checks_the_preconditions_again():
    """The write that created the peer's state may have handed this same workpiece."""
    graph = _Graph(peer_states=[])
    mw = _Middleware(graph, True)

    def the_other_write() -> None:
        graph.peer_states.append(PEER_STATE)
        graph.possessed_by = PEER

    mw.on_refusal = the_other_write

    with pytest.raises(HandoverPreconditionError):
        with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
            pytest.fail("the body ran after the precondition went false")

    assert len(mw.commits) == 1


@pytest.mark.parametrize(
    "graph, refusals",
    [
        (_Graph(), (True,)),
        (_Graph(peer_states=[]), (True,)),
        (_Graph(peer_states=[]), (True, True)),
    ],
    ids=[
        "the peer had its state",
        "the peer still has no state",
        "the second commit is refused too",
    ],
)
def test_any_other_refusal_is_a_conflict_and_skips_the_body(graph, refusals):
    mw = _Middleware(graph, *refusals)
    if len(refusals) == 2:

        def the_peer_gains_its_state():
            if not graph.peer_states:
                graph.peer_states.append(PEER_STATE)

        mw.on_refusal = the_peer_gains_its_state

    with pytest.raises(PossessionSwitchConflict) as conflict:
        with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
            pytest.fail("the body of a refused handover ran")

    assert isinstance(conflict.value.__cause__, PreconditionFailedError)
    assert len(mw.commits) == len(refusals)


def test_an_error_in_the_body_undoes_nothing():
    mw = _Middleware(_Graph())

    with pytest.raises(RuntimeError, match="boom"):
        with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
            raise RuntimeError("boom")

    assert len(mw.commits) == 1  # no Operation: nothing at the error either


# --------------------------------------------------------------------------- #
# Two handovers in one process: one lock per workpiece.
# --------------------------------------------------------------------------- #

PEER2 = IRI(f"{EX}peer2")


def _users(workpiece: IRI) -> int:
    """How many handovers hold or wait on the lock of the workpiece."""
    slot = possession._workpiece_locks.get(workpiece)
    return slot.users if slot else 0


class _TwoPeers(_Graph):
    """A graph in which the second peer carries the same ability and state as the first."""

    def query(self, sparql: str, convert_bindings: bool = False) -> dict:
        return super().query(sparql.replace(str(PEER2), str(PEER)))


class _HoldingMiddleware(_Middleware):
    """A middleware whose commit lands the way the store's would: the giver stops possessing the
    workpiece. It can hold a handover inside its entry, between the read and the commit."""

    def __init__(self, graph: _Graph, *, entered=None, release=None) -> None:
        super().__init__(graph)
        self.entered, self.release = entered, release

    def commit(self, **kwargs: Any) -> Any:
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(5)
        self.ogm.db.possessed_by = None
        return super().commit(**kwargs)


def _in_thread(counterpart: IRI, mw: Any, workpiece: IRI, errors: list, bodies: list):
    def work() -> None:
        try:
            with handover(mw, mode=MES.Pass, workpiece=workpiece, counterpart=counterpart):
                bodies.append(counterpart)
        except Exception as error:  # noqa: BLE001 - recorded for the assertions
            errors.append(error)

    thread = threading.Thread(target=work)
    thread.start()
    return thread


def test_two_handovers_of_one_workpiece_make_one_commit_and_run_one_body():
    graph = _TwoPeers()
    entered, release = threading.Event(), threading.Event()
    first = _HoldingMiddleware(graph, entered=entered, release=release)
    second = _HoldingMiddleware(graph)
    errors: list = []
    bodies: list = []

    t1 = _in_thread(PEER, first, WORKPIECE, errors, bodies)
    assert entered.wait(5)  # the first is between its read and its commit
    t2 = _in_thread(PEER2, second, WORKPIECE, errors, bodies)
    deadline = time.monotonic() + 5
    while _users(WORKPIECE) < 2:
        assert time.monotonic() < deadline, "the second handover never queued on the lock"
        time.sleep(0.01)
    release.set()
    t1.join(5)
    t2.join(5)

    assert len(first.commits) + len(second.commits) == 1
    assert bodies == [PEER]
    assert len(errors) == 1 and isinstance(errors[0], HandoverPreconditionError)
    assert possession._workpiece_locks == {}


def test_handovers_of_different_workpieces_do_not_wait_on_each_other():
    barrier = threading.Barrier(2, timeout=5)
    other = IRI(f"{EX}other_workpiece")

    def meeting(mw: _Middleware) -> _Middleware:
        """Both entries wait for each other: they pass only if both run at the same time."""
        original = mw.commit

        def commit(**kwargs):
            barrier.wait()
            return original(**kwargs)

        mw.commit = commit
        return mw

    first, second = meeting(_Middleware(_Graph())), meeting(_Middleware(_Graph()))
    errors: list = []
    bodies: list = []

    t1 = _in_thread(PEER, first, WORKPIECE, errors, bodies)
    t2 = _in_thread(PEER, second, other, errors, bodies)
    t1.join(5)
    t2.join(5)

    assert errors == []
    assert len(bodies) == 2
    assert possession._workpiece_locks == {}


@pytest.mark.parametrize(
    "refusal",
    [
        PreconditionFailedError("the precondition of the commit was false"),
        TripleStoreInterfaceError("the store refuses"),
    ],
)
def test_a_refused_entry_releases_the_lock(refusal):
    mw = _Middleware(_Graph())
    original = mw.commit

    def refuse(**kwargs):
        raise refusal

    mw.commit = refuse
    with pytest.raises(Exception) as raised:
        with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
            pytest.fail("the body of a refused handover ran")
    assert raised.value is refusal or raised.value.__cause__ is refusal
    assert possession._workpiece_locks == {}

    mw.commit = original
    ran = []
    with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
        ran.append(1)
    assert ran == [1]


def test_a_failed_precondition_releases_the_lock():
    mw = _Middleware(_Graph(possessed_by=PEER))
    with pytest.raises(HandoverPreconditionError):
        with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
            pass
    assert possession._workpiece_locks == {}
    mw.ogm.db.possessed_by = GIVER
    with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
        pass
    assert len(mw.commits) == 1


def test_an_error_in_the_body_does_not_hold_the_lock():
    mw = _Middleware(_Graph())
    with pytest.raises(RuntimeError, match="boom"):
        with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
            assert WORKPIECE not in possession._workpiece_locks  # released before the body
            raise RuntimeError("boom")
    with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
        pass
    assert possession._workpiece_locks == {}


def test_the_workpiece_guard_is_reentrant_on_one_thread():
    """A handover of a workpiece started inside another one's commit, on the same thread, must not
    wait on itself (the live test of two handovers of one box hung on this)."""
    from kapps_semantic_middleware.possession import _workpiece_guard, _workpiece_locks

    with _workpiece_guard("urn:box"):
        with _workpiece_guard("urn:box"):
            pass
    assert "urn:box" not in _workpiece_locks
