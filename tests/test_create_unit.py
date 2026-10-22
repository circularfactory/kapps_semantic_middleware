"""`mw.create()` on `mw.commit()`, without a triple store.

The OGM is faked at the calls the middleware makes through it: `commit(changes=...)`, `fetch`,
and `db.query` for the reads of a predefined scope. The fake keeps kapps_ogm's commit rules that
`mw.create()` must respect: a call is admitted as a whole or refused as a whole, and two changes
of one call must not name one instance. The live counterpart is `test_create_integration.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Iterator

import pytest
from kapps_ogm import Create, Update
from kapps_ogm.utils.class_scope import ClassScope
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import TripleStoreResponseError

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.errors import NotificationUndelivered
from kapps_semantic_middleware.operation import operation
from kapps_semantic_middleware.possession import handover, workpiece
from kapps_semantic_middleware.vocabulary import CFC, MES, SVC

EX = "http://example.org/test#"
OP = IRI(f"{EX}op_1")
STATUS = IRI(f"{EX}status")
SNAPSHOT = IRI(f"{EX}DecisionSnapshot")
DECISION = IRI(f"{EX}decision")
TRANSPORT = IRI(f"{EX}Transport")
BOX = IRI(f"{EX}Box")
HAS_BOX = IRI(f"{EX}hasBox")


class _FakeDB:
    """``ogm.db``: answers each read with the rows that ``rows`` gives for its query."""

    def __init__(self) -> None:
        self.rows = lambda sparql: []

    def query(self, sparql, convert_bindings=False):
        return {"results": {"bindings": self.rows(sparql)}}


class _FakeOGM:
    """Records the calls that the store admitted, and refuses the ones ``refuse`` names."""

    def __init__(self, refuse=None) -> None:
        self.refuse = refuse or (lambda changes: False)
        self.written: list[list] = []
        self.graphs: list = []
        self.db = _FakeDB()

    def commit(self, *, changes, named_graph=None):
        changes = list(changes)
        named = [IRI(c.instance_iri) for c in changes if c.instance_iri is not None]
        if len(named) != len(set(named)):
            raise ValueError("Two changes name one instance.")
        if self.refuse(changes):
            raise TripleStoreResponseError("SHACL validation failed")
        self.written.append(changes)
        self.graphs.append(named_graph)
        return [SimpleNamespace(id=change.instance_iri) for change in changes]

    def fetch(self, *, instance_iri, **kwargs):
        return instance_iri


def _middleware(ogm, *, resource: str = f"{EX}detector", named_graph=None) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=resource,
        service_class=f"{EX}DetectorService",
        ogm=ogm,
        named_graph=named_graph,
        heartbeat_interval=None,
    )


def _status(value: str) -> list:
    return [Update(instance_iri=OP, data={STATUS: [value]})]


def _written(ogm) -> list[list[tuple]]:
    """Each admitted call, as (change type, instance, data) per change."""
    return [
        [
            (type(c).__name__, str(c.instance_iri), {str(k): v for k, v in c.data.items()})
            for c in call
        ]
        for call in ogm.written
    ]


# --------------------------------------------------------------------------- #
# Nesting: a creation joins the commit that is still to come, in both forms.
# --------------------------------------------------------------------------- #


def test_a_creation_nested_in_a_commit_joins_its_clean_exit():
    ogm = _FakeOGM()
    mw = _middleware(ogm)
    with mw.commit(OP, entry=_status("running"), exit=_status("done")):
        with mw.create(SNAPSHOT, ClassScope()) as snapshot:
            snapshot.update({DECISION: ["drill"]})
    assert _written(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [
            ("Update", OP, {STATUS: ["done"]}),
            ("Create", snapshot.iri, {DECISION: ["drill"]}),
        ],
    ]


def test_an_update_of_a_nested_creation_folds_into_its_create_and_names_its_property():
    """The ``Create`` names the property that the transition's update adds, so kapps_ogm does
    not drop it."""
    ogm = _FakeOGM()
    mw = _middleware(ogm)
    with mw.commit() as transition:
        with mw.create(SNAPSHOT, ClassScope()) as snapshot:
            snapshot.update({DECISION: ["drill"]})
        transition.update(snapshot.iri, {STATUS: ["checked"], DECISION: ["keep"]})
    ((create,),) = ogm.written
    assert isinstance(create, Create)
    assert {str(k): v for k, v in create.data.items()} == {
        DECISION: ["keep"],
        STATUS: ["checked"],
    }
    assert set(create.class_scope) == {DECISION, STATUS}


def test_a_creation_nested_in_an_async_commit_joins_its_clean_exit():
    """Under `async with`, the commit's entry runs in a worker thread, and the body still finds
    the open commit."""
    ogm = _FakeOGM()
    mw = _middleware(ogm)

    async def scenario():
        async with mw.commit(OP, entry=_status("running"), exit=_status("done")):
            async with mw.create(SNAPSHOT, ClassScope()) as snapshot:
                snapshot.update({DECISION: ["drill"]})
        return snapshot

    snapshot = asyncio.run(scenario())
    assert _written(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [
            ("Update", OP, {STATUS: ["done"]}),
            ("Create", snapshot.iri, {DECISION: ["drill"]}),
        ],
    ]


def test_a_creation_of_another_middleware_does_not_join_and_commits_through_its_own_ogm():
    """Scenario 1 runs a planner and a resource in one process: each writes through its own OGM."""
    resource_ogm, planner_ogm = _FakeOGM(), _FakeOGM()
    resource = _middleware(resource_ogm)
    planner = _middleware(planner_ogm, resource=f"{EX}planner")
    with resource.commit(OP, entry=_status("running"), exit=_status("done")):
        with planner.create(SNAPSHOT, ClassScope()) as snapshot:
            pass
        assert _written(planner_ogm) == [[("Create", snapshot.iri, {})]]
    assert _written(resource_ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [("Update", OP, {STATUS: ["done"]})],
    ]


def test_a_creation_into_another_named_graph_does_not_join_even_through_the_same_ogm():
    ogm = _FakeOGM()
    first = _middleware(ogm, named_graph=IRI(f"{EX}graph_1"))
    second = _middleware(ogm, named_graph=IRI(f"{EX}graph_2"))
    with first.create(TRANSPORT, ClassScope()) as transport:
        with second.create(BOX, ClassScope()) as box:
            pass
    assert _written(ogm) == [[("Create", box.iri, {})], [("Create", transport.iri, {})]]
    assert ogm.graphs == [IRI(f"{EX}graph_2"), IRI(f"{EX}graph_1")]


# --------------------------------------------------------------------------- #
# Alone: the creation commits at its exit.
# --------------------------------------------------------------------------- #


def test_a_creation_alone_commits_its_create_at_the_exit_with_a_minted_iri():
    ogm = _FakeOGM()
    with _middleware(ogm).create(SNAPSHOT, ClassScope()) as snapshot:
        assert ogm.written == []
        snapshot.update({DECISION: ["drill"]})

    ((create,),) = ogm.written
    assert isinstance(create, Create)
    assert create.class_iri == SNAPSHOT and create.instance_iri == snapshot.iri
    assert str(snapshot.iri).startswith(f"{SNAPSHOT}_")
    assert len(str(snapshot.iri)) == len(f"{SNAPSHOT}_") + 12
    # kapps_ogm drops a property that the class scope does not name.
    assert set(create.class_scope) == {DECISION}


def test_a_creation_with_a_given_iri_creates_that_instance():
    ogm = _FakeOGM()
    named = IRI(f"{EX}snapshot_7")
    with _middleware(ogm).create(SNAPSHOT, ClassScope(), instance_iri=str(named)) as snapshot:
        assert snapshot.iri == named
    assert _written(ogm) == [[("Create", named, {})]]


def test_an_error_in_the_body_of_a_creation_alone_writes_nothing_and_is_raised():
    ogm = _FakeOGM()
    with pytest.raises(RuntimeError, match="camera failed"):
        with _middleware(ogm).create(SNAPSHOT, ClassScope()) as snapshot:
            snapshot.update({DECISION: ["drill"]})
            raise RuntimeError("camera failed")
    assert ogm.written == []


def test_a_refused_creation_raises_the_stores_error_and_writes_nothing():
    ogm = _FakeOGM(refuse=lambda changes: True)
    with pytest.raises(TripleStoreResponseError, match="SHACL validation failed"):
        with _middleware(ogm).create(SNAPSHOT, ClassScope()) as snapshot:
            snapshot.update({DECISION: ["drill"]})
    assert ogm.written == []


def test_an_attribute_set_on_a_creation_raises_instead_of_being_lost():
    ogm = _FakeOGM()
    with pytest.raises(AttributeError, match=r"creation.update"):
        with _middleware(ogm).create(SNAPSHOT, ClassScope()) as snapshot:
            snapshot.decision = "drill"
    assert ogm.written == []


def test_a_value_that_is_no_list_raises_where_it_is_set_and_nothing_is_written():
    ogm = _FakeOGM()
    with pytest.raises(ValueError, match="list"):
        with _middleware(ogm).create(SNAPSHOT, ClassScope()) as snapshot:
            snapshot.update({DECISION: "drill"})
    assert ogm.written == []


# --------------------------------------------------------------------------- #
# Nested in a creation: one commit, linked before it.
# --------------------------------------------------------------------------- #



def test_a_creation_nested_in_a_creation_is_one_commit_and_is_linked_by_its_iri():
    ogm = _FakeOGM()
    mw = _middleware(ogm)
    with mw.create(TRANSPORT, ClassScope()) as transport:
        with mw.create(BOX, ClassScope()) as box:
            pass
        transport.update({HAS_BOX: [box.iri]})

    assert _written(ogm) == [
        [
            ("Create", transport.iri, {HAS_BOX: [{"id": str(box.iri)}]}),
            ("Create", box.iri, {}),
        ]
    ]


def test_an_error_in_the_outer_body_drops_a_nested_creation_that_had_finished():
    ogm = _FakeOGM()
    mw = _middleware(ogm)
    with pytest.raises(RuntimeError, match="no box free"):
        with mw.create(TRANSPORT, ClassScope()):
            with mw.create(BOX, ClassScope()):
                pass
            raise RuntimeError("no box free")
    assert ogm.written == []


def test_a_refused_outer_commit_leaves_neither_its_change_nor_the_nested_creation():
    """The detector's case: the decision snapshot and the Operation's `done` are one commit."""
    def refuse_done(changes) -> bool:
        return any(isinstance(c, Update) and c.data.get(STATUS) == ["done"] for c in changes)

    ogm = _FakeOGM(refuse=refuse_done)
    mw = _middleware(ogm)
    with pytest.raises(TripleStoreResponseError):
        with mw.commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ):
            with mw.create(SNAPSHOT, ClassScope()) as snapshot:
                snapshot.update({DECISION: ["drill"]})
    assert _written(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [("Update", OP, {STATUS: ["failed"]})],
    ]


def test_an_error_in_a_nested_body_that_the_outer_body_catches_drops_only_that_creation():
    ogm = _FakeOGM()
    mw = _middleware(ogm)
    with mw.create(TRANSPORT, ClassScope()) as transport:
        try:
            with mw.create(BOX, ClassScope()) as box:
                with mw.create(SNAPSHOT, ClassScope()):
                    pass
                raise RuntimeError("box jammed")
        except RuntimeError:
            pass
    assert _written(ogm) == [[("Create", transport.iri, {})]]
    assert box.iri not in {c.instance_iri for c in ogm.written[0]}


# --------------------------------------------------------------------------- #
# In the body of handover(), whose commit already happened: alone.
# --------------------------------------------------------------------------- #

WORKPIECE = IRI(f"{EX}box_1")
COUNTERPART = IRI(f"{EX}module_2")
COUNTERPART_STATE = IRI(f"{EX}module_2_possession")


def test_a_creation_in_the_body_of_a_handover_commits_on_its_own():
    """A handover's clean exit writes nothing more, so a creation cannot join it."""
    ogm = _FakeOGM()
    # One row answers each read of the handover: the possession, the ability, the state.
    ogm.db.rows = lambda sparql: [
        {"ps": f"{EX}detector_possession", "a": str(MES.Retrieve), "state": str(COUNTERPART_STATE)}
    ]
    mw = _middleware(ogm)
    with handover(mw, mode=MES.Pass, workpiece=WORKPIECE, counterpart=COUNTERPART):
        assert len(ogm.written) == 1  # the possession change
        with mw.create(SNAPSHOT, ClassScope()) as snapshot:
            snapshot.update({DECISION: ["handed on"]})
        assert len(ogm.written) == 2

    assert _written(ogm)[1] == [("Create", snapshot.iri, {DECISION: ["handed on"]})]
    assert len(ogm.written) == 2


# --------------------------------------------------------------------------- #
# The workpiece scope: a Workpiece is created in the possession of its possessor.
# --------------------------------------------------------------------------- #

PLANNER = IRI(f"{EX}planner")
PLANNER_STATE = IRI(f"{EX}planner_possession")


def _possession_rows(states: dict):
    """Answers the read of a Resource's possession state: ``states`` maps Resource to state."""

    def rows(sparql):
        return [
            {"state": str(state)}
            for resource, state in states.items()
            if f"<{resource}>" in sparql
        ]

    return rows


def test_a_workpiece_without_a_possessor_raises_at_entry_before_any_write():
    ogm = _FakeOGM()
    ran = []
    with pytest.raises(ValueError, match="possessor"):
        with _middleware(ogm).create(BOX, workpiece(ClassScope(), possessor=None)):
            ran.append(True)
    assert ran == []
    assert ogm.written == []


def test_a_workpiece_without_a_possessor_in_an_outer_body_writes_nothing_of_it():
    ogm = _FakeOGM()
    mw = _middleware(ogm)
    with pytest.raises(ValueError, match="possessor"):
        with mw.create(TRANSPORT, ClassScope()):
            with mw.create(BOX, workpiece(ClassScope(), possessor=None)):
                pass
    assert ogm.written == []


def test_a_workpiece_whose_possessor_has_no_state_is_created_with_that_state_in_one_commit():
    ogm = _FakeOGM()
    with _middleware(ogm).create(BOX, workpiece(ClassScope(), possessor=PLANNER)) as box:
        pass

    assert _written(ogm) == [
        [
            ("Create", box.iri, {str(CFC.hasPossessedWorkpiece): [{"id": str(PLANNER_STATE)}]}),
            ("Create", PLANNER_STATE, {}),
            ("Update", PLANNER, {str(CFC.hasPossessor): [{"id": str(PLANNER_STATE)}]}),
        ]
    ]
    (create, state, _) = ogm.written[0]
    assert set(create.class_scope) == {CFC.hasPossessedWorkpiece}
    assert state.class_iri == CFC.PossessionState


def test_a_workpiece_whose_possessor_has_a_state_points_at_it_and_writes_nothing_else():
    seeded_state = IRI(f"{EX}a_state_a_seed_wrote")
    ogm = _FakeOGM()
    ogm.db.rows = _possession_rows({PLANNER: seeded_state})
    with _middleware(ogm).create(BOX, workpiece(ClassScope(), possessor=PLANNER)) as box:
        pass

    assert _written(ogm) == [
        [("Create", box.iri, {str(CFC.hasPossessedWorkpiece): [{"id": str(seeded_state)}]})]
    ]


def test_the_middlewares_own_resource_is_the_short_form_of_a_possessor():
    ogm = _FakeOGM()
    mw = _middleware(ogm)
    own_state = IRI(f"{EX}detector_state")
    ogm.db.rows = _possession_rows({mw.resource: own_state})
    with mw.create(BOX, workpiece(ClassScope(), possessor=mw.resource)) as box:
        pass

    assert mw.resource == IRI(f"{EX}detector")
    assert _written(ogm) == [
        [("Create", box.iri, {str(CFC.hasPossessedWorkpiece): [{"id": str(own_state)}]})]
    ]


def test_two_workpieces_of_one_new_possessor_in_one_commit_create_its_state_once():
    ogm = _FakeOGM()
    mw = _middleware(ogm)
    with mw.create(TRANSPORT, ClassScope()) as transport:
        with mw.create(BOX, workpiece(ClassScope(), possessor=PLANNER)) as first:
            pass
        with mw.create(BOX, workpiece(ClassScope(), possessor=PLANNER)) as second:
            pass

    link = {str(CFC.hasPossessedWorkpiece): [{"id": str(PLANNER_STATE)}]}
    assert _written(ogm) == [
        [
            ("Create", transport.iri, {}),
            ("Create", first.iri, link),
            ("Create", PLANNER_STATE, {}),
            ("Update", PLANNER, {str(CFC.hasPossessor): [{"id": str(PLANNER_STATE)}]}),
            ("Create", second.iri, link),
        ]
    ]


def test_a_body_that_sets_the_link_that_the_workpiece_scope_sets_raises_and_writes_nothing():
    ogm = _FakeOGM()
    with pytest.raises(ValueError, match="which its scope sets itself"):
        with _middleware(ogm).create(BOX, workpiece(ClassScope(), possessor=PLANNER)) as box:
            box.update({CFC.hasPossessedWorkpiece: [IRI(f"{EX}elsewhere")]})
    assert ogm.written == []


# --------------------------------------------------------------------------- #
# The operation scope: an Operation implements its Capability.
# --------------------------------------------------------------------------- #

CAPABILITY = IRI(f"{EX}conveyor_capability_transport")


def test_an_operation_without_a_capability_raises_at_entry_before_any_write():
    ogm = _FakeOGM()
    ran = []
    with pytest.raises(ValueError, match="capability"):
        with _middleware(ogm).create(TRANSPORT, operation(ClassScope(), capability=None)):
            ran.append(True)
    assert ran == []
    assert ogm.written == []


def test_an_operation_is_created_with_the_capability_it_implements():
    ogm = _FakeOGM()
    with _middleware(ogm).create(
        TRANSPORT, operation(ClassScope(), capability=CAPABILITY)
    ) as transport:
        transport.update({STATUS: ["queued"]})

    assert _written(ogm) == [
        [
            (
                "Create",
                transport.iri,
                {STATUS: ["queued"], str(CFC.implementsCapability): [{"id": str(CAPABILITY)}]},
            )
        ]
    ]
    assert set(ogm.written[0][0].class_scope) == {STATUS, CFC.implementsCapability}


def test_opening_a_transport_is_one_commit_of_the_operation_the_box_and_the_first_possession():
    ogm = _FakeOGM()
    mw = _middleware(ogm)
    with mw.create(TRANSPORT, operation(ClassScope(), capability=CAPABILITY)) as transport:
        with mw.create(BOX, workpiece(ClassScope(), possessor=PLANNER)) as box:
            pass
        transport.update({HAS_BOX: [box.iri]})

    assert _written(ogm) == [
        [
            (
                "Create",
                transport.iri,
                {
                    HAS_BOX: [{"id": str(box.iri)}],
                    str(CFC.implementsCapability): [{"id": str(CAPABILITY)}],
                },
            ),
            ("Create", box.iri, {str(CFC.hasPossessedWorkpiece): [{"id": str(PLANNER_STATE)}]}),
            ("Create", PLANNER_STATE, {}),
            ("Update", PLANNER, {str(CFC.hasPossessor): [{"id": str(PLANNER_STATE)}]}),
        ]
    ]


# --------------------------------------------------------------------------- #
# issue_operation() on mw.create(): its receiver is notified after its own commit.
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _receiver(ogm: _FakeOGM, notified: list, status: int = 200) -> Iterator[str]:
    """A receiver's event trigger on the loopback interface, answering with ``status``. Each
    notification goes into ``notified`` with the number of commits made before it. The store
    resolves every dispatch to it, and holds no possession state. Yields the address."""

    class EventTrigger(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            notified.append((len(ogm.written), IRI(body["operation_iri"])))
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), EventTrigger)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://127.0.0.1:{server.server_address[1]}"

    def rows(sparql: str) -> list:
        if str(CFC.hasPossessor) in sparql or str(SVC.issuedBy) in sparql:
            return []
        return [{"cap": str(CAPABILITY), "svc": f"{EX}conveyor_service", "addr": address}]

    ogm.db.rows = rows
    try:
        yield address
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_a_request_in_the_body_of_a_commit_commits_its_operation_before_it_notifies():
    """A request cannot join a write still to come: its receiver reads the Operation after the
    notification."""
    ogm = _FakeOGM()
    notified: list = []
    with _receiver(ogm, notified):
        mw = _middleware(ogm)
        with mw.commit(OP, entry=_status("running"), exit=_status("done")):
            with mw.issue_operation(
                capability_class=f"{EX}TransportCapability", operation_class=TRANSPORT
            ) as op:
                pass

    # The claim, then the Operation alone, then the notification, then the outer exit.
    assert notified == [(2, op.iri)]
    assert [type(c).__name__ for c in ogm.written[1]] == ["Create"]
    assert ogm.written[1][0].instance_iri == op.iri
    assert _written(ogm)[2] == [("Update", OP, {STATUS: ["done"]})]


def test_a_creation_gives_the_changes_it_committed_or_joined_once_its_exit_built_them():
    ogm = _FakeOGM()
    mw = _middleware(ogm)
    with mw.create(TRANSPORT, ClassScope()) as transport:
        assert transport.changes == ()
        with mw.create(BOX, workpiece(ClassScope(), possessor=PLANNER)) as box:
            pass
        # Joined: the box, its possessor's new state, and the possessor's link to it.
        assert [c.instance_iri for c in box.changes] == [box.iri, PLANNER_STATE, PLANNER]
    assert [c.instance_iri for c in transport.changes] == [transport.iri, box.iri, PLANNER_STATE, PLANNER]
    assert ogm.written == [list(transport.changes)]


def test_a_failed_notification_keeps_every_instance_that_the_requests_commit_created():
    """A creation nested in the body of a request joins the Operation's commit. A failed
    notification undoes none of it: the receiver takes up its queued Operations when it starts."""
    ogm = _FakeOGM()
    notified: list = []
    with _receiver(ogm, notified, status=503):
        with pytest.raises(NotificationUndelivered):
            with _middleware(ogm, resource=str(PLANNER)).issue_operation(
                capability_class=f"{EX}TransportCapability", operation_class=TRANSPORT
            ) as op:
                with _middleware(ogm, resource=str(PLANNER)).create(
                    BOX, workpiece(ClassScope(), possessor=PLANNER)
                ) as box:
                    pass
                op.data[str(HAS_BOX)] = [box.iri]

    # The request's one commit, and nothing after it.
    (created,) = _written(ogm)
    assert [(kind, iri) for kind, iri, _ in created] == [
        ("Create", op.iri),
        ("Create", box.iri),
        ("Create", PLANNER_STATE),
        ("Update", PLANNER),
    ]
