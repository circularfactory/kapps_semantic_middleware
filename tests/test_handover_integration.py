"""Handover primitive integration tests against a live store.

The change-of-possession primitive: a source resource that currently possesses a
workpiece hands it to a counterpart carrying the complementary handover ability. Possession
is Core's reified `cfc:PossessionState`, one state per resource. The entry of the handover
commits the possession change, one OGM commit that re-points the workpiece to the
counterpart's state, before the body moves the workpiece. The shape "a module holds at most one
box" plays the domain's rule. Skipped when TRIPLESTORE_* env vars are absent (see conftest).
A test that needs a shape sets it with the store's `shapes_set`, which replaces every shape
of the dataset, and removes every shape again at its end.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import patch

import pytest
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import (
    PreconditionFailedError,
    TripleStoreInterfaceError,
)
from rdflib import Literal, URIRef
from rdflib.namespace import RDF

from kapps_ogm import OGM, Create, Update
from _explicit import explicit_ask, explicit_select
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import triplestore_env_present, triplestore_for
from kapps_semantic_middleware.possession import possession_change, possession_state
from kapps_semantic_middleware.registration import (
    HandoverPreconditionError,
    PossessionSwitchConflict,
    create_possession,
    find_possession_state,
    switch_possession,
)
from kapps_semantic_middleware.seeding import load_shared_ontologies
from kapps_semantic_middleware.vocabulary import CFC, MES, SVC, OperationStatus

requires_store = pytest.mark.skipif(
    not triplestore_env_present(),
    reason="TRIPLESTORE_* environment variables not set; skipping live-store integration test",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402


# The handover middleware is never served, so its service_class is unused by handover();
# it only reads/writes possession through the OGM using resource_iri.
_TRANSFER_SERVICE_CLASS = IRI(f"{seed.DEMO_NS}TransferModuleService")


# A domain property that a handover changes together with the possession, and two of its values.
_TRANSPORT_STATE = IRI(f"{seed.DEMO_NS}hasTransportState")
_TRANSPORT_STATE_CLASS = IRI(f"{seed.DEMO_NS}TransportState")
_AT_SOURCE = IRI(f"{seed.DEMO_NS}AtSource")
_IN_TRANSIT = IRI(f"{seed.DEMO_NS}InTransit")

# A property that the OGM writes must be declared, as a domain ontology declares it.
_TRANSPORT_STATE_TBOX = f"""
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{_TRANSPORT_STATE_CLASS}> a owl:Class .
<{_TRANSPORT_STATE}> a owl:ObjectProperty ;
    rdfs:domain <{seed.BOX_CLASS}> ;
    rdfs:range <{_TRANSPORT_STATE_CLASS}> .
<{_AT_SOURCE}> a <{_TRANSPORT_STATE_CLASS}> .
<{_IN_TRANSIT}> a <{_TRANSPORT_STATE_CLASS}> .
"""


# A transfer module possesses at most one workpiece: the path runs from the module to its
# PossessionStates and back to the workpieces that point at them.
_ONE_WORKPIECE_PER_MODULE = f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
<{seed.DEMO_NS}OneWorkpieceShape> a sh:NodeShape ;
    sh:targetClass <{seed.TRANSFER_MODULE_CLASS}> ;
    sh:property <{seed.DEMO_NS}OneWorkpiecePossessionShape> .
<{seed.DEMO_NS}OneWorkpiecePossessionShape> a sh:PropertyShape ;
    sh:path ( <{CFC.hasPossessor}> [ sh:inversePath <{CFC.hasPossessedWorkpiece}> ] ) ;
    sh:maxCount 1 .
"""


def _source_middleware(store, ogm: Optional[OGM] = None) -> SemanticMiddleware:
    """The source transfer module (which currently possesses the box), as a middleware."""
    return SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HANDOVER_SOURCE,
        service_class=_TRANSFER_SERVICE_CLASS,
        ogm=ogm or OGM(db=triplestore_for(store.dataset)),
        host="127.0.0.1",
        port=8999,
    )


class _BarrierOGM(OGM):
    """The real OGM, whose first commit of changes waits at a barrier for the other writers'.

    It records the instances that each commit of changes creates.
    """

    def __init__(self, db, barrier: threading.Barrier) -> None:
        super().__init__(db=db)
        self._barrier: Optional[threading.Barrier] = barrier
        self.created: list[list[str]] = []

    def commit(self, **kwargs):  # type: ignore[override]
        changes = kwargs.get("changes")
        if changes is not None:
            self.created.append([str(c.instance_iri) for c in changes if isinstance(c, Create)])
            if self._barrier is not None:
                barrier, self._barrier = self._barrier, None
                barrier.wait(timeout=60)
        return super().commit(**kwargs)


class _LateOGM(OGM):
    """The real OGM, which lets another write land before its first commit of changes."""

    def __init__(self, db, other_write: Callable[[], None]) -> None:
        super().__init__(db=db)
        self._other_write: Optional[Callable[[], None]] = other_write

    def commit(self, **kwargs):  # type: ignore[override]
        if kwargs.get("changes") is not None and self._other_write is not None:
            other_write, self._other_write = self._other_write, None
            other_write()
        return super().commit(**kwargs)


class _RacedOGM(OGM):
    """The real OGM, which lets another write land inside its first commit of changes: after the
    commit has read the instances that it changes, and before its write."""

    def __init__(self, db, other_write: Callable[[], None]) -> None:
        super().__init__(db=db)
        self._other_write: Optional[Callable[[], None]] = other_write

    def commit(self, **kwargs):  # type: ignore[override]
        if kwargs.get("changes") is None or self._other_write is None:
            return super().commit(**kwargs)
        other_write, self._other_write = self._other_write, None
        write = self.db.triples_update

        def late_write(*args, **kw):
            other_write()
            return write(*args, **kw)

        with patch.object(self.db, "triples_update", side_effect=late_write):
            return super().commit(**kwargs)


def _explicit_statements(db) -> set:
    """Every explicit statement in the store, to compare before and after."""
    result = db.query(
        explicit_select(["?s", "?p", "?o"], "?s ?p ?o"),
        convert_bindings=True,
    )
    return {(str(b["s"]), str(b["p"]), str(b["o"])) for b in result["results"]["bindings"]}


def _explicit_objects(db, subject: str, predicate: str) -> list[str]:
    """The explicit values of one property of one subject, sorted."""
    result = db.query(
        explicit_select(["?o"], f"<{subject}> <{predicate}> ?o"),
        convert_bindings=True,
    )
    return sorted(str(b["o"]) for b in result["results"]["bindings"])


def _another_box(db, possessor: IRI, name: str = "box_002") -> IRI:
    """A further box, which ``possessor`` possesses."""
    box = IRI(f"{seed.DEMO_NS}{name}")
    db.triple_add((box, RDF.type, seed.BOX_CLASS))
    create_possession(OGM(db=db), workpiece_iri=box, possessor_iri=possessor)
    return box


def _box_at_the_source_in_its_transport_state(db) -> None:
    """Declare the box's transport state, and set it to AtSource."""
    db.graph_add(_TRANSPORT_STATE_TBOX, content_type="application/x-turtle")
    db.triple_add((seed.HANDOVER_BOX, _TRANSPORT_STATE, _AT_SOURCE))


def _running_operation(db) -> IRI:
    """An Operation that the handover completes, and the vocabulary its status needs."""
    load_shared_ontologies(db)
    operation = IRI(f"{seed.DEMO_NS}transport_001")
    db.triple_add((operation, RDF.type, CFC.Operation))
    db.triple_add((operation, SVC.operationStatus, Literal(OperationStatus.RUNNING)))
    return operation


# --------------------------------------------------------------------------- #
# The two preconditions, read before anything is written.
# --------------------------------------------------------------------------- #


@requires_store
def test_handover_rejects_without_possession(store):
    """A handover of a workpiece the caller does not possess is rejected before the body runs."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    not_possessed = IRI(f"{seed.DEMO_NS}box_999")
    db.triple_add((not_possessed, RDF.type, seed.BOX_CLASS))
    with pytest.raises(HandoverPreconditionError):
        with _source_middleware(store).handover(
            mode=MES.Pass, workpiece=not_possessed, counterpart=seed.HANDOVER_DEST
        ):
            pass


@requires_store
def test_handover_rejects_without_counterpart_ability(store):
    """A handover to a counterpart lacking the complementary ability is rejected before the body."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    no_ability = IRI(f"{seed.DEMO_NS}transfer_module_C")
    db.triple_add((no_ability, RDF.type, seed.TRANSFER_MODULE_CLASS))  # no hasHandoverAbility
    with pytest.raises(HandoverPreconditionError):
        with _source_middleware(store).handover(
            mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=no_ability
        ):
            pass


# --------------------------------------------------------------------------- #
# The possession change: the workpiece's link, and nothing that a Resource holds.
# --------------------------------------------------------------------------- #


@requires_store
def test_a_handover_moves_possession_to_the_counterpart(store):
    """A clean handover changes the possession of the workpiece to the counterpart."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    ogm = OGM(db=db)
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_SOURCE) is not None
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_DEST) is None

    with _source_middleware(store).handover(
        mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST
    ):
        pass  # domain-owned physical transport

    # Possession moved to the counterpart; the source no longer currently possesses it.
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_DEST) is not None
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_SOURCE) is None
    # The workpiece points to exactly ONE current PossessionState (Core cardinality preserved).
    states = list(db.triples_get(sub=seed.HANDOVER_BOX, pred=CFC.hasPossessedWorkpiece))
    assert len(states) == 1


@requires_store
def test_a_handover_changes_only_the_workpieces_link_its_update_and_its_operation(store):
    """The possession change re-points the box, and writes no Resource's link.

    The giver and the counterpart keep their one ``cfc:hasPossessor`` each. The same commit
    carries the box's transport state and the Operation that the handover completes, and nothing
    else.
    """
    db = store
    seed.seed_handover(db, OGM(db=db))
    _another_box(db, seed.HANDOVER_DEST)  # so the counterpart has its state already
    _box_at_the_source_in_its_transport_state(db)
    operation = _running_operation(db)
    before = _explicit_statements(db)

    with _source_middleware(store).handover(
        mode=MES.Pass,
        workpiece=seed.HANDOVER_BOX,
        counterpart=seed.HANDOVER_DEST,
        workpiece_update={str(_TRANSPORT_STATE): [{"id": str(_IN_TRANSIT)}]},
        operation=operation,
    ):
        pass

    after = _explicit_statements(db)
    box = str(seed.HANDOVER_BOX)
    giver_state = f"{seed.HANDOVER_SOURCE}_possession"
    counterpart_state = f"{seed.HANDOVER_DEST}_possession"
    assert before - after == {
        (box, str(CFC.hasPossessedWorkpiece), giver_state),
        (box, str(_TRANSPORT_STATE), str(_AT_SOURCE)),
        (str(operation), str(SVC.operationStatus), "running"),
    }
    assert after - before == {
        (box, str(CFC.hasPossessedWorkpiece), counterpart_state),
        (box, str(_TRANSPORT_STATE), str(_IN_TRANSIT)),
        (str(operation), str(SVC.operationStatus), "done"),
    }
    assert _explicit_objects(db, seed.HANDOVER_SOURCE, CFC.hasPossessor) == [giver_state]
    assert _explicit_objects(db, seed.HANDOVER_DEST, CFC.hasPossessor) == [counterpart_state]


@requires_store
def test_handover_carries_a_workpiece_update_with_the_possession_change(store):
    """A `workpiece_update` lands together with the possession change."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    ogm = OGM(db=db)
    _box_at_the_source_in_its_transport_state(db)

    with _source_middleware(store).handover(
        mode=MES.Pass,
        workpiece=seed.HANDOVER_BOX,
        counterpart=seed.HANDOVER_DEST,
        workpiece_update={str(_TRANSPORT_STATE): [{"id": str(_IN_TRANSIT)}]},
    ):
        pass

    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_DEST) is not None
    states = [str(t[2]) for t in db.triples_get(sub=seed.HANDOVER_BOX, pred=_TRANSPORT_STATE)]
    assert states == [str(_IN_TRANSIT)]


@requires_store
def test_a_workpiece_update_that_names_the_possession_link_is_refused_before_anything_is_written(
    store,
):
    """The handover sets ``cfc:hasPossessedWorkpiece`` itself, so ``workpiece_update`` must not."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    before = _explicit_statements(db)
    body_ran = False

    with pytest.raises(ValueError, match="must not set"):
        with _source_middleware(store).handover(
            mode=MES.Pass,
            workpiece=seed.HANDOVER_BOX,
            counterpart=seed.HANDOVER_DEST,
            workpiece_update={str(CFC.hasPossessedWorkpiece): [{"id": str(seed.HANDOVER_DEST)}]},
        ):
            body_ran = True

    assert not body_ran
    assert _explicit_statements(db) == before


@requires_store
@pytest.mark.parametrize("as_iri", [IRI, URIRef], ids=["IRI", "URIRef"])
def test_an_iri_value_in_a_workpiece_update_is_written_as_a_reference(store, as_iri):
    """A bare IRI value names the object it refers to, as ``{"id": iri}`` does; it is no literal."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    _box_at_the_source_in_its_transport_state(db)

    with _source_middleware(store).handover(
        mode=MES.Pass,
        workpiece=seed.HANDOVER_BOX,
        counterpart=seed.HANDOVER_DEST,
        workpiece_update={str(_TRANSPORT_STATE): [as_iri(str(_IN_TRANSIT))]},
    ):
        pass

    as_object = db.query(
        explicit_ask(f"<{seed.HANDOVER_BOX}> <{_TRANSPORT_STATE}> <{_IN_TRANSIT}>")
    )
    assert as_object["boolean"]
    assert _explicit_objects(db, seed.HANDOVER_BOX, _TRANSPORT_STATE) == [str(_IN_TRANSIT)]


@requires_store
def test_a_plain_string_for_an_object_property_is_refused_before_anything_is_written(store):
    """A ``str`` is no reference: the OGM refuses it for an object property, before any write."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    _box_at_the_source_in_its_transport_state(db)
    before = _explicit_statements(db)
    body_ran = False

    with pytest.raises(ValueError, match="expected Node instances"):
        with _source_middleware(store).handover(
            mode=MES.Pass,
            workpiece=seed.HANDOVER_BOX,
            counterpart=seed.HANDOVER_DEST,
            workpiece_update={str(_TRANSPORT_STATE): [str(_IN_TRANSIT)]},
        ):
            body_ran = True

    assert not body_ran
    assert _explicit_statements(db) == before


@requires_store
def test_handover_preserves_counterpart_existing_possession(store):
    """A workpiece the counterpart already holds stays with it: its one state now holds both.

    A resource may possess several workpieces unless a domain rule limits it, so handing a
    workpiece to a counterpart must not take away what it possesses.
    """
    db = store
    seed.seed_handover(db, OGM(db=db))
    ogm = OGM(db=db)
    # The destination already currently possesses a DIFFERENT box.
    other_box = _another_box(db, seed.HANDOVER_DEST)
    assert find_possession_state(ogm, other_box, seed.HANDOVER_DEST) is not None

    with _source_middleware(store).handover(
        mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST
    ):
        pass

    # The destination now possesses BOTH boxes; the earlier possession survived the handover.
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_DEST) is not None
    assert find_possession_state(ogm, other_box, seed.HANDOVER_DEST) is not None


# --------------------------------------------------------------------------- #
# The counterpart's one state: created by its first handover, then reused.
# --------------------------------------------------------------------------- #


@requires_store
def test_the_first_handover_to_a_resource_creates_its_state_and_the_next_reuses_it(store):
    """A counterpart without a possession state gets ``<counterpart>_possession`` with its first
    handover.

    The next handover to it points the next box at that same state, and adds no second link.
    """
    db = store
    seed.seed_handover(db, OGM(db=db))
    second_box = _another_box(db, seed.HANDOVER_SOURCE)
    counterpart_state = f"{seed.HANDOVER_DEST}_possession"
    assert _explicit_objects(db, seed.HANDOVER_DEST, CFC.hasPossessor) == []

    for box in (seed.HANDOVER_BOX, second_box):
        with _source_middleware(store).handover(
            mode=MES.Pass, workpiece=box, counterpart=seed.HANDOVER_DEST
        ):
            pass
        assert _explicit_objects(db, seed.HANDOVER_DEST, CFC.hasPossessor) == [counterpart_state]
        assert _explicit_objects(db, box, CFC.hasPossessedWorkpiece) == [counterpart_state]

    assert str(CFC.PossessionState) in _explicit_objects(db, IRI(counterpart_state), RDF.type)


@requires_store
def test_two_first_handovers_to_one_resource_at_the_same_moment_leave_one_state(store):
    """Both handovers read that the counterpart has no state, and both commit its creation at once.

    Two middlewares hold their entry commits at one barrier, so the race is real. Commits that truly
    coincide both write the same state triples; one that lands later is refused by the store
    (``PreconditionFailedError``) and committed again with the state. Either way both handovers
    are admitted, and the counterpart ends with one state, which both boxes point at.
    """
    db = store
    seed.seed_handover(db, OGM(db=db))
    boxes = [seed.HANDOVER_BOX, _another_box(db, seed.HANDOVER_SOURCE)]
    barrier = threading.Barrier(len(boxes))
    middlewares = [
        _source_middleware(store, ogm=_BarrierOGM(triplestore_for(store.dataset), barrier))
        for _ in boxes
    ]
    errors: list[BaseException] = []

    def hand_over(middleware: SemanticMiddleware, box: IRI) -> None:
        try:
            with middleware.handover(
                mode=MES.Pass, workpiece=box, counterpart=seed.HANDOVER_DEST
            ):
                pass
        except BaseException as error:  # noqa: BLE001 - reported by the assertion below
            errors.append(error)

    threads = [
        threading.Thread(target=hand_over, args=(middleware, box))
        for middleware, box in zip(middlewares, boxes)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    counterpart_state = f"{seed.HANDOVER_DEST}_possession"
    assert [m.ogm.created[0] for m in middlewares] == [[counterpart_state], [counterpart_state]]
    assert errors == []
    assert _explicit_objects(db, seed.HANDOVER_DEST, CFC.hasPossessor) == [counterpart_state]
    for box in boxes:
        assert _explicit_objects(db, box, CFC.hasPossessedWorkpiece) == [counterpart_state]


@requires_store
def test_a_first_handover_whose_counterpart_got_its_state_meanwhile_is_admitted_with_it(store):
    """Between this handover's read and its commit, another handover gives the counterpart its
    state.

    The store refuses this handover's creation of the same state (``PreconditionFailedError``).
    The handover checks both preconditions again, which still hold, and
    commits with the state that exists now, so both boxes point at the counterpart's one state.
    """
    db = store
    seed.seed_handover(db, OGM(db=db))
    other_box = _another_box(db, seed.HANDOVER_SOURCE)

    def the_other_handover() -> None:
        with _source_middleware(store).handover(
            mode=MES.Pass, workpiece=other_box, counterpart=seed.HANDOVER_DEST
        ):
            pass

    late = _source_middleware(
        store, ogm=_LateOGM(triplestore_for(store.dataset), the_other_handover)
    )
    with late.handover(mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST):
        pass

    counterpart_state = f"{seed.HANDOVER_DEST}_possession"
    assert _explicit_objects(db, seed.HANDOVER_DEST, CFC.hasPossessor) == [counterpart_state]
    for box in (seed.HANDOVER_BOX, other_box):
        assert _explicit_objects(db, box, CFC.hasPossessedWorkpiece) == [counterpart_state]


@requires_store
def test_one_box_handed_twice_at_once_to_a_counterpart_without_a_state_is_admitted_once(store):
    """Two handovers of one box by its giver race to a counterpart that has no state yet.

    The second checks its preconditions before the first commits, and commits after it. The store
    refuses the second creation of the counterpart's state (``PreconditionFailedError``); the
    handover then checks both preconditions again, and the giver no longer possesses the box. So
    one handover is admitted, the other raises, and the body runs once.
    """
    db = store
    seed.seed_handover(db, OGM(db=db))
    bodies: list[str] = []

    def the_first_handover() -> None:
        with _source_middleware(store).handover(
            mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST
        ):
            bodies.append("first")

    late = _source_middleware(
        store, ogm=_LateOGM(triplestore_for(store.dataset), the_first_handover)
    )
    with pytest.raises(HandoverPreconditionError):
        with late.handover(
            mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST
        ):
            bodies.append("second")

    assert bodies == ["first"]
    counterpart_state = f"{seed.HANDOVER_DEST}_possession"
    links = _explicit_objects(db, seed.HANDOVER_BOX, CFC.hasPossessedWorkpiece)
    assert links == [counterpart_state]


@requires_store
def test_a_handover_whose_box_moved_inside_its_commit_raises_the_conflict_before_its_body(store):
    """Another handover of the same box lands after this handover's commit read the box, and
    before its write.

    The box no longer points at the state that the commit replaces, so the store refuses the
    commit and writes nothing of it. The handover raises ``PossessionSwitchConflict``, with the
    store's refusal as its cause, and its body does not run. The counterpart has its state
    already, so no creation of a state is tried again.
    """
    db = store
    seed.seed_handover(db, OGM(db=db))
    _another_box(db, seed.HANDOVER_DEST)
    counterpart_state, _ = possession_state(OGM(db=db), seed.HANDOVER_DEST)
    bodies: list[str] = []

    def the_other_handover() -> None:
        with _source_middleware(store).handover(
            mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST
        ):
            bodies.append("other")

    raced = _source_middleware(
        store, ogm=_RacedOGM(triplestore_for(store.dataset), the_other_handover)
    )
    with pytest.raises(PossessionSwitchConflict) as conflict:
        with raced.handover(
            mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST
        ):
            bodies.append("raced")

    assert isinstance(conflict.value.__cause__, PreconditionFailedError)
    assert bodies == ["other"]
    links = _explicit_objects(db, seed.HANDOVER_BOX, CFC.hasPossessedWorkpiece)
    assert links == [str(counterpart_state)]


# --------------------------------------------------------------------------- #
# The order: the store admits the possession change before the body moves the box.
# --------------------------------------------------------------------------- #


@requires_store
def test_a_handover_onto_an_occupied_counterpart_is_refused_before_its_body_runs(store):
    """The store refuses the possession change at the entry: nothing is written, the body never
    runs, and the refusal names the counterpart.

    The counterpart already possesses another box, and the domain's shape allows one per module.
    """
    db = store
    seed.seed_handover(db, OGM(db=db))
    _another_box(db, seed.HANDOVER_DEST)
    _box_at_the_source_in_its_transport_state(db)
    before = _explicit_statements(db)
    body_ran = False

    # The shape goes in only now, because the seed itself is valid under it. Outside the
    # `raises`: a store that cannot set the shape must fail the test, not pass it.
    db.shapes_set(_ONE_WORKPIECE_PER_MODULE, "text/turtle")
    try:
        with pytest.raises(TripleStoreInterfaceError) as refusal:
            with _source_middleware(store).handover(
                mode=MES.Pass,
                workpiece=seed.HANDOVER_BOX,
                counterpart=seed.HANDOVER_DEST,
                workpiece_update={str(_TRANSPORT_STATE): [{"id": str(_IN_TRANSIT)}]},
            ):
                body_ran = True
    finally:
        db.shapes_set("", "text/turtle")

    assert not body_ran
    assert str(seed.HANDOVER_DEST) in str(refusal.value)
    assert _explicit_statements(db) == before


@requires_store
def test_an_error_in_the_body_undoes_nothing_and_writes_nothing_more(store):
    """The possession changed before the body ran, so an error in the move is raised as it is.

    The handover writes no error transition: the caller records what really happened.
    """
    db = store
    seed.seed_handover(db, OGM(db=db))
    ogm = OGM(db=db)
    with pytest.raises(RuntimeError, match="boom"):
        with _source_middleware(store).handover(
            mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST
        ):
            admitted = _explicit_statements(db)
            raise RuntimeError("boom")

    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_DEST) is not None
    assert _explicit_statements(db) == admitted


# --------------------------------------------------------------------------- #
# More changes in the entry: UC2 reassigns its transport at each hop.
# --------------------------------------------------------------------------- #

_NEXT_CAPABILITY = IRI(f"{seed.DEMO_NS}transfer_module_B_capability_box_transport")


class _RecordingOGM(OGM):
    """The real OGM, which records the changes of each commit."""

    def __init__(self, db) -> None:
        super().__init__(db=db)
        self.commits: list[list] = []

    def commit(self, **kwargs):  # type: ignore[override]
        if kwargs.get("changes") is not None:
            self.commits.append(list(kwargs["changes"]))
        return super().commit(**kwargs)


def _reassigned(transport: IRI) -> Update:
    """UC2's reassignment of its transport to the next module, as the caller builds it."""
    return Update(
        instance_iri=transport,
        data={
            str(CFC.executedOn): [{"id": str(seed.HANDOVER_DEST)}],
            str(CFC.implementsCapability): [{"id": str(_NEXT_CAPABILITY)}],
        },
    )


@requires_store
def test_a_handover_admits_its_entry_in_the_commit_of_the_possession_change(store):
    """The caller's changes and the possession change are one commit, before the body runs."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    transport = _running_operation(db)
    db.triple_add((_NEXT_CAPABILITY, RDF.type, CFC.Capability))
    ogm = _RecordingOGM(triplestore_for(store.dataset))
    before = _explicit_statements(db)

    with _source_middleware(store, ogm).handover(
        mode=MES.Pass,
        workpiece=seed.HANDOVER_BOX,
        counterpart=seed.HANDOVER_DEST,
        entry=[_reassigned(transport)],
    ):
        admitted = _explicit_statements(db)
        commits_before_the_body = len(ogm.commits)

    assert commits_before_the_body == 1
    assert len(ogm.commits) == 1
    [changes] = ogm.commits
    assert {str(change.instance_iri) for change in changes} >= {
        str(seed.HANDOVER_BOX),
        str(transport),
    }
    added = admitted - before
    assert (str(transport), str(CFC.executedOn), str(seed.HANDOVER_DEST)) in added
    assert (str(transport), str(CFC.implementsCapability), str(_NEXT_CAPABILITY)) in added
    assert (
        str(seed.HANDOVER_BOX),
        str(CFC.hasPossessedWorkpiece),
        f"{seed.HANDOVER_DEST}_possession",
    ) in added
    assert _explicit_statements(db) == admitted  # the exit wrote nothing more


@requires_store
def test_a_refused_possession_change_writes_no_entry_change(store):
    """The store refuses the possession change: the caller's changes are not written either."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    _another_box(db, seed.HANDOVER_DEST)
    transport = _running_operation(db)
    db.triple_add((_NEXT_CAPABILITY, RDF.type, CFC.Capability))
    before = _explicit_statements(db)
    body_ran = False

    # Outside the `raises`: a store that cannot set the shape must fail the test, not pass it.
    db.shapes_set(_ONE_WORKPIECE_PER_MODULE, "text/turtle")
    try:
        with pytest.raises(TripleStoreInterfaceError):
            with _source_middleware(store).handover(
                mode=MES.Pass,
                workpiece=seed.HANDOVER_BOX,
                counterpart=seed.HANDOVER_DEST,
                entry=[_reassigned(transport)],
            ):
                body_ran = True
    finally:
        db.shapes_set("", "text/turtle")

    assert not body_ran
    assert _explicit_statements(db) == before


@requires_store
def test_a_refused_entry_change_writes_no_possession_change(store):
    """The OGM refuses one of the caller's changes: the possession does not change either."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    transport = _running_operation(db)
    before = _explicit_statements(db)
    body_ran = False
    not_a_reference = Update(
        instance_iri=transport, data={str(CFC.executedOn): [str(seed.HANDOVER_DEST)]}
    )

    with pytest.raises(ValueError):
        with _source_middleware(store).handover(
            mode=MES.Pass,
            workpiece=seed.HANDOVER_BOX,
            counterpart=seed.HANDOVER_DEST,
            entry=[not_a_reference],
        ):
            body_ran = True

    assert not body_ran
    assert _explicit_statements(db) == before
    ogm = OGM(db=db)
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_SOURCE) is not None


# --------------------------------------------------------------------------- #
# The possession module: a resource's one state, and a workpiece's change of possession.
# --------------------------------------------------------------------------- #


@requires_store
def test_a_resources_state_is_found_by_its_link_not_by_its_iri(store):
    """A state that a seed wrote under another IRI is the resource's state, and needs no change."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    seeded = IRI(f"{seed.DEMO_NS}seeded_state_of_module_B")
    db.triple_add((seed.HANDOVER_DEST, CFC.hasPossessor, seeded))

    assert possession_state(OGM(db=db), seed.HANDOVER_DEST) == (seeded, [])


@requires_store
def test_the_changes_for_a_resource_without_a_state_create_its_one_state(store):
    """Committed, the changes give the resource the typed state ``<resource>_possession``."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    ogm = OGM(db=db)

    state, changes = possession_state(ogm, seed.HANDOVER_DEST)
    ogm.commit(changes=changes)

    assert state == IRI(f"{seed.HANDOVER_DEST}_possession")
    assert _explicit_objects(db, seed.HANDOVER_DEST, CFC.hasPossessor) == [str(state)]
    assert str(CFC.PossessionState) in _explicit_objects(db, state, RDF.type)
    assert possession_state(ogm, seed.HANDOVER_DEST) == (state, [])


@requires_store
def test_a_resource_with_two_states_is_an_error(store):
    """A resource has one possession state; two are an error in the graph, and name both."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    for name in ("first_state", "second_state"):
        db.triple_add((seed.HANDOVER_DEST, CFC.hasPossessor, IRI(f"{seed.DEMO_NS}{name}")))

    with pytest.raises(ValueError, match="has 2 possession states") as error:
        possession_state(OGM(db=db), seed.HANDOVER_DEST)
    assert f"{seed.DEMO_NS}first_state" in str(error.value)
    assert f"{seed.DEMO_NS}second_state" in str(error.value)


@requires_store
def test_a_possession_change_creates_the_possessors_state_only_while_it_has_none(store):
    """The change points the workpiece at the possessor's state, and says whether it creates it."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    ogm = OGM(db=db)

    box, counterpart = seed.HANDOVER_BOX, seed.HANDOVER_DEST
    first = possession_change(ogm, workpiece_iri=box, possessor_iri=counterpart)
    ogm.commit(changes=first.changes)
    second = possession_change(ogm, workpiece_iri=box, possessor_iri=counterpart)

    assert first.state == second.state == IRI(f"{seed.HANDOVER_DEST}_possession")
    assert (first.creates_state, second.creates_state) == (True, False)
    assert _explicit_objects(db, seed.HANDOVER_BOX, CFC.hasPossessedWorkpiece) == [str(first.state)]


# --------------------------------------------------------------------------- #
# The module-level helpers, for callers that hold no middleware.
# --------------------------------------------------------------------------- #


@requires_store
def test_create_possession_gives_a_workpiece_its_first_possessor(store):
    """The workpiece points at the possessor's one state, which the same commit creates."""
    db = store
    seed.seed_handover(db, OGM(db=db))
    box = IRI(f"{seed.DEMO_NS}box_003")
    db.triple_add((box, RDF.type, seed.BOX_CLASS))

    state = create_possession(OGM(db=db), workpiece_iri=box, possessor_iri=seed.HANDOVER_DEST)

    assert state == IRI(f"{seed.HANDOVER_DEST}_possession")
    assert _explicit_objects(db, box, CFC.hasPossessedWorkpiece) == [str(state)]
    assert _explicit_objects(db, seed.HANDOVER_DEST, CFC.hasPossessor) == [str(state)]


@requires_store
def test_switch_possession_writes_the_change_of_possession_alone(store):
    """The possession change of a handover, with its workpiece update, in one commit and no body.

    The giver keeps its one link; the counterpart gets its state with this first possession.
    """
    db = store
    seed.seed_handover(db, OGM(db=db))
    _box_at_the_source_in_its_transport_state(db)

    state = switch_possession(
        OGM(db=db),
        workpiece_iri=seed.HANDOVER_BOX,
        new_possessor_iri=seed.HANDOVER_DEST,
        workpiece_update={str(_TRANSPORT_STATE): [_IN_TRANSIT]},
    )

    giver_state = f"{seed.HANDOVER_SOURCE}_possession"
    assert state == IRI(f"{seed.HANDOVER_DEST}_possession")
    assert _explicit_objects(db, seed.HANDOVER_BOX, CFC.hasPossessedWorkpiece) == [str(state)]
    assert _explicit_objects(db, seed.HANDOVER_BOX, _TRANSPORT_STATE) == [str(_IN_TRANSIT)]
    assert _explicit_objects(db, seed.HANDOVER_SOURCE, CFC.hasPossessor) == [giver_state]
    assert _explicit_objects(db, seed.HANDOVER_DEST, CFC.hasPossessor) == [str(state)]
