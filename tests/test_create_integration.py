"""`mw.create()` and its predefined scopes against a live store.

The OGM is the real one, subclassed only to record each commit it makes, so "one commit" is
counted at the seam that `mw.create()` writes through. A refusal comes from the store's own SHACL
engine or from its check that an instance to create does not exist yet. Skipped when the
TRIPLESTORE_* variables are absent (see conftest). The unit counterpart is `test_create_unit.py`.
A test that needs a shape sets it joined with the shipped shapes (`_shapes.py`), because the
store's `shapes_set` replaces every shape of the dataset, and restores the shipped ones at its end.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest
from _shapes import restore_shipped_shapes, with_shipped_shapes
from kapps_ogm import OGM, Create, Update
from kapps_ogm.utils.class_scope import ClassScope
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import (
    PreconditionFailedError,
    TripleStoreInterfaceError,
)
from rdflib.namespace import RDF

from _explicit import explicit_select
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import triplestore_for
from kapps_semantic_middleware.errors import NotificationUndelivered
from kapps_semantic_middleware.operation import operation
from kapps_semantic_middleware.possession import handover, workpiece
from kapps_semantic_middleware.registration import (
    create_operation,
    mint_capability_iri,
    mint_operation_iri,
    mint_service_iri,
    mint_workflow_iri,
    register_service,
    register_workflow,
)
from kapps_semantic_middleware.vocabulary import CFC, MES, SVC, OperationStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

EX = "http://example.org/test#"
SNAPSHOT = IRI(f"{EX}DecisionSnapshot")
DECISION = IRI(f"{EX}decision")
ABOUT = IRI(f"{EX}aboutOperation")
TRANSPORT = IRI(f"{EX}Transport")
HAS_BOX = IRI(f"{EX}hasBox")
PLANNER = IRI(f"{EX}planner")
TRANSPORT_CAPABILITY = IRI(f"{EX}planner_capability_transport")

# A participant's decision snapshot, and a transport with its box and its planner.
_TBOX = f"""
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix cfc: <http://w3id.org/circularfactory/Core#> .
<{SNAPSHOT}> a owl:Class .
<{DECISION}> a owl:DatatypeProperty ; rdfs:domain <{SNAPSHOT}> ; rdfs:range xsd:string .
<{ABOUT}> a owl:ObjectProperty ; rdfs:domain <{SNAPSHOT}> ; rdfs:range cfc:Operation .
<{TRANSPORT}> a owl:Class ; rdfs:subClassOf cfc:Operation .
<{HAS_BOX}> a owl:ObjectProperty ; rdfs:domain <{TRANSPORT}> ; rdfs:range <{seed.BOX_CLASS}> .
<{seed.BOX_CLASS}> a owl:Class ; rdfs:subClassOf cfc:Workpiece .
<{EX}Planner> a owl:Class ; rdfs:subClassOf cfc:Resource .
<{EX}TransportCapability> a owl:Class ; rdfs:subClassOf cfc:Capability .
<{PLANNER}> a <{EX}Planner> .
<{TRANSPORT_CAPABILITY}> a <{EX}TransportCapability> .
"""

_PORT = 8993
_ADDRESS = f"http://127.0.0.1:{_PORT}"


def _status_shape(*allowed: str) -> str:
    """A shape that admits only the given values of an Operation's status."""
    values = " ".join(f'"{value}"' for value in allowed)
    return f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
<urn:test:StatusShape> a sh:NodeShape ;
    sh:targetClass <{CFC.Operation}> ;
    sh:property <urn:test:StatusValues> .
<urn:test:StatusValues> a sh:PropertyShape ;
    sh:path <{SVC.hasOperationStatus}> ;
    sh:in ( {values} ) .
"""


class _RecordingOGM(OGM):
    """The real OGM, recording the changes of each commit it makes."""

    def __init__(self, db) -> None:
        super().__init__(db=db)
        self.commits: list[list] = []

    def commit(self, **kwargs):  # type: ignore[override]
        result = super().commit(**kwargs)
        self.commits.append(list(kwargs.get("changes") or []))
        return result


def _seed(db) -> None:
    seed.seed_scenario1(db)
    db.graph_add(_TBOX, content_type="application/x-turtle")


def _middleware(store, resource: IRI = PLANNER) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=str(resource),
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=_RecordingOGM(triplestore_for(store.dataset)),
        host="127.0.0.1",
        port=_PORT,
        heartbeat_interval=None,
    )


def _values(db, subject: IRI, predicate: IRI) -> list[str]:
    return sorted(str(t[2]) for t in db.triples_get(sub=subject, pred=predicate))


def _explicit_statements(db) -> set:
    """Every explicit statement of the default graph, to compare before and after."""
    result = db.query(
        explicit_select(["?s", "?p", "?o"], "?s ?p ?o"),
        convert_bindings=True,
    )
    return {(str(b["s"]), str(b["p"]), str(b["o"])) for b in result["results"]["bindings"]}


def _statements_about(db, iri: IRI) -> list:
    """Every statement, explicit or inferred, with ``iri`` as its subject or its object."""
    statements = []
    for pattern in (f"<{iri}> ?p ?o", f"?s ?p <{iri}>"):
        result = db.query(f"SELECT * WHERE {{ {pattern} }}", convert_bindings=True)
        statements += result["results"]["bindings"]
    return statements


def _kinds(changes: list) -> list[tuple[str, str]]:
    return [(type(change).__name__, str(change.instance_iri)) for change in changes]


# --------------------------------------------------------------------------- #
# Nested: a decision snapshot joins its Operation's done.
# --------------------------------------------------------------------------- #


def _register_receiver_at(store, address: str) -> IRI:
    """The hello resource's Service and Workflow at ``address``, as if it had started there.
    Returns its Capability."""
    ogm = OGM(db=store)
    service = mint_service_iri(seed.HELLO_RESOURCE, address)
    capability = mint_capability_iri(seed.HELLO_RESOURCE, "hello_world")
    register_service(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=service,
        service_class=seed.HELLO_SERVICE_CLASS,
        address=address,
    )
    register_workflow(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=service,
        workflow_iri=mint_workflow_iri(service, "hello_world"),
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
        capability_iri=capability,
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        endpoint=f"{address}/workflows/hello_world/execute",
    )
    return capability


def _queued_operation(store) -> tuple[SemanticMiddleware, IRI]:
    """The hello resource registered at its address, and one queued Operation in its queue."""
    _seed(store)
    ogm = OGM(db=store)
    capability = _register_receiver_at(store, _ADDRESS)
    op = mint_operation_iri(CFC.Operation)
    create_operation(
        ogm, operation_iri=op, operation_class=CFC.Operation, capability_iri=capability
    )
    mw = _middleware(store, seed.HELLO_RESOURCE)
    mw._operation_queue.append(op)  # as its event trigger does
    return mw, op


def test_a_decision_snapshot_and_its_operations_done_land_in_one_commit(store):
    mw, op = _queued_operation(store)
    with mw.claim_next() as claimed:
        with mw.create(SNAPSHOT, ClassScope()) as snapshot:
            snapshot.update({DECISION: ["drill"], ABOUT: [claimed.iri]})
        assert _statements_about(store, snapshot.iri) == []  # nothing before the exit
        claimed.result = "drill"

    claim, done = mw.ogm.commits
    assert _kinds(done) == [("Update", str(op)), ("Create", str(snapshot.iri))]
    assert _values(store, op, SVC.hasOperationStatus) == [OperationStatus.DONE]
    assert store.triple_exists((snapshot.iri, RDF.type, SNAPSHOT))
    assert _values(store, snapshot.iri, DECISION) == ["drill"]
    assert _values(store, snapshot.iri, ABOUT) == [str(op)]


def test_a_refused_done_leaves_neither_the_done_nor_the_decision_snapshot(store):
    mw, op = _queued_operation(store)
    # The shape admits every status but done.
    store.shapes_set(
        with_shipped_shapes(_status_shape("queued", "running", "failed")), "text/turtle"
    )
    try:
        with pytest.raises(TripleStoreInterfaceError, match="ValidationReport"):
            with mw.claim_next() as claimed:
                with mw.create(SNAPSHOT, ClassScope()) as snapshot:
                    snapshot.update({DECISION: ["drill"], ABOUT: [claimed.iri]})
                claimed.result = "drill"
    finally:
        restore_shipped_shapes(store)

    assert _values(store, op, SVC.hasOperationStatus) == [OperationStatus.FAILED]
    assert _statements_about(store, snapshot.iri) == []


def test_a_creation_in_an_async_commit_joins_its_exit(store):
    """The asynchronous form runs the commit's entry in a worker thread; the body still finds
    the open commit."""
    mw, op = _queued_operation(store)
    running = [Update(instance_iri=op, data={str(SVC.hasOperationStatus): ["running"]})]
    done = [Update(instance_iri=op, data={str(SVC.hasOperationStatus): ["done"]})]

    async def scenario() -> IRI:
        async with mw.commit(op, entry=running, exit=done):
            async with mw.create(SNAPSHOT, ClassScope()) as snapshot:
                snapshot.update({DECISION: ["keep"]})
        return snapshot.iri

    snapshot = asyncio.run(scenario())
    assert [_kinds(changes) for changes in mw.ogm.commits] == [
        [("Update", str(op))],
        [("Update", str(op)), ("Create", str(snapshot))],
    ]
    assert _values(store, snapshot, DECISION) == ["keep"]


# --------------------------------------------------------------------------- #
# Alone: a creation commits at its exit.
# --------------------------------------------------------------------------- #


def test_a_creation_alone_commits_at_its_exit(store):
    _seed(store)
    mw = _middleware(store)
    with mw.create(SNAPSHOT, ClassScope()) as snapshot:
        snapshot.update({DECISION: ["keep"]})
        assert _statements_about(store, snapshot.iri) == []

    assert [_kinds(changes) for changes in mw.ogm.commits] == [[("Create", str(snapshot.iri))]]
    assert str(snapshot.iri).startswith(f"{SNAPSHOT}_")
    assert store.triple_exists((snapshot.iri, RDF.type, SNAPSHOT))
    assert _values(store, snapshot.iri, DECISION) == ["keep"]


def test_an_error_in_the_body_of_a_creation_alone_writes_nothing(store):
    _seed(store)
    mw = _middleware(store)
    before = _explicit_statements(store)
    with pytest.raises(RuntimeError, match="camera failed"):
        with mw.create(SNAPSHOT, ClassScope()) as snapshot:
            snapshot.update({DECISION: ["keep"]})
            raise RuntimeError("camera failed")
    assert mw.ogm.commits == []
    assert _explicit_statements(store) == before


def test_an_iri_that_the_store_already_holds_is_refused(store):
    _seed(store)
    mw = _middleware(store)
    with mw.create(SNAPSHOT, ClassScope()) as existing:
        existing.update({DECISION: ["keep"]})
    before = _explicit_statements(store)

    with pytest.raises(PreconditionFailedError):
        with mw.create(SNAPSHOT, ClassScope(), instance_iri=str(existing.iri)) as again:
            again.update({DECISION: ["discard"]})

    assert _explicit_statements(store) == before
    assert _values(store, existing.iri, DECISION) == ["keep"]


# --------------------------------------------------------------------------- #
# The workpiece scope.
# --------------------------------------------------------------------------- #


def test_a_workpiece_without_a_possessor_raises_at_entry_before_any_write(store):
    _seed(store)
    mw = _middleware(store)
    before = _explicit_statements(store)
    ran = []
    with pytest.raises(ValueError, match="possessor"):
        with mw.create(seed.BOX_CLASS, workpiece(ClassScope(), possessor=None)):
            ran.append(True)
    assert ran == []
    assert mw.ogm.commits == []
    assert _explicit_statements(store) == before


def test_a_workpiece_whose_possessor_has_no_state_is_created_with_that_state_in_one_commit(
    store,
):
    _seed(store)
    mw = _middleware(store)
    assert _values(store, PLANNER, CFC.hasPossessor) == []
    with mw.create(seed.BOX_CLASS, workpiece(ClassScope(), possessor=mw.resource)) as box:
        pass

    state = IRI(f"{PLANNER}_possession")
    assert [_kinds(changes) for changes in mw.ogm.commits] == [
        [("Create", str(box.iri)), ("Create", str(state)), ("Update", str(PLANNER))]
    ]
    assert store.triple_exists((state, RDF.type, CFC.PossessionState))
    assert _values(store, PLANNER, CFC.hasPossessor) == [str(state)]
    assert _values(store, box.iri, CFC.hasPossessedWorkpiece) == [str(state)]


def test_opening_a_transport_is_one_commit_of_the_operation_the_box_and_the_first_possession(
    store,
):
    _seed(store)
    mw = _middleware(store)
    with mw.create(
        TRANSPORT, operation(ClassScope(), capability=TRANSPORT_CAPABILITY)
    ) as transport:
        transport.update({SVC.hasOperationStatus: [OperationStatus.QUEUED]})
        with mw.create(seed.BOX_CLASS, workpiece(ClassScope(), possessor=mw.resource)) as box:
            pass
        transport.update({HAS_BOX: [box.iri]})
        assert _statements_about(store, box.iri) == []

    state = IRI(f"{PLANNER}_possession")
    ((changes),) = mw.ogm.commits
    assert _kinds(changes) == [
        ("Create", str(transport.iri)),
        ("Create", str(box.iri)),
        ("Create", str(state)),
        ("Update", str(PLANNER)),
    ]
    assert isinstance(changes[0], Create)
    assert store.triple_exists((transport.iri, RDF.type, TRANSPORT))
    assert _values(store, transport.iri, CFC.implementsCapability) == [str(TRANSPORT_CAPABILITY)]
    assert _values(store, transport.iri, SVC.hasOperationStatus) == [OperationStatus.QUEUED]
    assert _values(store, transport.iri, HAS_BOX) == [str(box.iri)]
    assert _values(store, box.iri, CFC.hasPossessedWorkpiece) == [str(state)]
    assert _values(store, PLANNER, CFC.hasPossessor) == [str(state)]


# --------------------------------------------------------------------------- #
# issue_operation(): a failed notification undoes nothing of what its commit created.
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_failed_notification_keeps_a_workpiece_created_in_the_request(store):
    """The box joins the Operation's commit. A failed notification undoes none of it: the
    Operation stays queued with its box, the planner's new possession state and the planner's link to
    it."""
    _seed(store)
    # A reachable-looking receiver: its address is a port where nothing listens.
    _register_receiver_at(store, f"http://127.0.0.1:{_free_port()}")
    mw = _middleware(store)
    before = _explicit_statements(store)

    with pytest.raises(NotificationUndelivered) as raised:
        with mw.issue_operation(
            capability_class=seed.HELLO_CAPABILITY_CLASS, operation_class=str(TRANSPORT)
        ) as op:
            with mw.create(seed.BOX_CLASS, workpiece(ClassScope(), possessor=mw.resource)) as box:
                pass
            op.data[str(HAS_BOX)] = [box.iri]

    state = IRI(f"{PLANNER}_possession")
    (created, *_) = mw.ogm.commits
    assert _kinds(created) == [
        ("Create", str(op.iri)),
        ("Create", str(box.iri)),
        ("Create", str(state)),
        ("Update", str(PLANNER)),
    ]
    assert isinstance(raised.value.__cause__, httpx.ConnectError)
    assert len(mw.ogm.commits) == 1  # nothing after the request's commit
    assert _values(store, op.iri, SVC.hasOperationStatus) == [OperationStatus.QUEUED]
    assert _values(store, op.iri, HAS_BOX) == [str(box.iri)]
    assert _values(store, box.iri, CFC.hasPossessedWorkpiece) == [str(state)]
    assert _values(store, PLANNER, CFC.hasPossessor) == [str(state)]
    assert before < _explicit_statements(store)


# --------------------------------------------------------------------------- #
# In the body of handover(), whose commit already happened.
# --------------------------------------------------------------------------- #


def test_a_workpiece_created_in_the_body_of_a_handover_commits_on_its_own(store):
    seed.seed_handover(store, OGM(db=store))
    mw = _middleware(store, seed.HANDOVER_SOURCE)
    with handover(mw, mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST):
        with mw.create(
            seed.BOX_CLASS, workpiece(ClassScope(), possessor=mw.resource)
        ) as second_box:
            pass
        assert len(mw.ogm.commits) == 2  # the possession change, then the creation

    source_state = IRI(f"{seed.HANDOVER_SOURCE}_possession")
    dest_state = IRI(f"{seed.HANDOVER_DEST}_possession")
    assert len(mw.ogm.commits) == 2  # the clean exit wrote nothing more
    assert _kinds(mw.ogm.commits[1]) == [("Create", str(second_box.iri))]
    assert _values(store, second_box.iri, CFC.hasPossessedWorkpiece) == [str(source_state)]
    assert _values(store, seed.HANDOVER_BOX, CFC.hasPossessedWorkpiece) == [str(dest_state)]
