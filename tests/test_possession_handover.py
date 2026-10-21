"""The possession extension's handover, on a middleware that has only its public surface.

`possession.handover(mw, ...)` uses `mw.mode`, `mw.resource`, `mw.ogm` and `mw.commit()`, and
nothing else of the middleware. So these tests give it exactly that: a stand-in whose OGM answers
the handover's reads from a table, and whose `commit()` records each entry and can refuse it with
the store's `PreconditionFailedError`. The store's own behaviour, the SHACL refusals and the races
between two processes, is in `test_handover_integration.py`.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Optional

import pytest
from kapps_ogm import Create, Update
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import PreconditionFailedError

from kapps_semantic_middleware.errors import HandoverPreconditionError, PossessionSwitchConflict
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
    a refusal, as the write that made the store refuse it.
    """

    def __init__(self, graph: _Graph, *refusals: bool, mode: Mode = Mode.RESOURCE) -> None:
        self.mode = mode
        self.resource = GIVER if mode == Mode.RESOURCE else None
        self.ogm = _OGM(graph)
        self.refusals = list(refusals)
        self.on_refusal = lambda: None
        self.commits: List[tuple] = []

    def commit(self, *, entry: Any = (), verb: str = "commit") -> Any:
        self.commits.append((list(entry), verb))
        refused = self.refusals.pop(0) if self.refusals else False
        return self._entered(refused)

    @contextlib.contextmanager
    def _entered(self, refused: bool) -> Iterator[None]:
        if refused:
            self.on_refusal()
            raise PreconditionFailedError("the precondition of the commit was false")
        yield


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

    assert seen == [1]
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

    ((entry, _),) = mw.commits
    (moved,) = [c for c in entry if getattr(c, "instance_iri", None) == WORKPIECE]
    assert moved.data[f"{EX}state"] == ["arrived"]
    (done,) = [c for c in entry if getattr(c, "instance_iri", None) == OPERATION]
    assert done.data == {str(SVC.operationStatus): [OperationStatus.DONE]}
    assert other in entry


@pytest.mark.parametrize(
    "graph",
    [_Graph(possessed_by=PEER), _Graph(peer_complements=False)],
    ids=["the giver does not possess it", "the peer lacks the complementary ability"],
)
def test_a_failed_precondition_writes_nothing_and_skips_the_body(graph):
    mw = _Middleware(graph)

    with pytest.raises(HandoverPreconditionError):
        with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=PEER):
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

    assert len(mw.commits) == 1
