"""A refused write against a live store: the store's own SHACL engine refuses it, and the
instance's two pages show it.

Each test sets a shape with the store's `shapes_set`, which replaces every shape of the
dataset, makes the store refuse a `commit`, a `create` or a `handover`, and reads the instance as
a person would: the dashboard's box "Refused writes", fetched as the page names it, and the
activity feed's stream. Then it removes every shape again. The report is the one the store sent,
so these tests also hold the reader against the store's own N-Quads. Skipped when the
TRIPLESTORE_* variables are absent (see conftest).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from _refusal_view import BrowserView
from fastapi.testclient import TestClient
from kapps_ogm import OGM, Update
from kapps_ogm.utils.class_scope import ClassScope
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import ShaclValidationError
from rdflib import Literal
from rdflib.namespace import RDF, SH

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import triplestore_for
from kapps_semantic_middleware.dashboard import enable_dashboard
from kapps_semantic_middleware.possession import create_possession, handover
from kapps_semantic_middleware.validation_report import (
    Term,
    ValidationResult,
    readable_results,
    rule_names,
    validation_results,
)
from kapps_semantic_middleware.vocabulary import CFC, MES, SVC

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

EX = "http://example.org/test#"
SNAPSHOT = IRI(f"{EX}DecisionSnapshot")
DECISION = IRI(f"{EX}decision")
ABOUT = IRI(f"{EX}aboutOperation")

_TBOX = f"""
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix cfc: <http://w3id.org/circularfactory/Core#> .
<{SNAPSHOT}> a owl:Class .
<{DECISION}> a owl:DatatypeProperty ; rdfs:domain <{SNAPSHOT}> ; rdfs:range xsd:string .
<{ABOUT}> a owl:ObjectProperty ; rdfs:domain <{SNAPSHOT}> ; rdfs:range cfc:Operation .
"""

# Two rules for a decision snapshot. One has a path and two messages; the other is an `sh:or`,
# which names no path. A snapshot that decides "drill" about no Operation breaks both.
_SNAPSHOT_SHAPES = f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
<{EX}DecisionShape> a sh:NodeShape ;
    sh:targetClass <{SNAPSHOT}> ;
    sh:property <{EX}DecisionValueShape> .
<{EX}DecisionValueShape> a sh:PropertyShape ;
    sh:path <{DECISION}> ;
    sh:in ( "keep" ) ;
    sh:message "A snapshot decides keep."@en, "Ein Schnappschuss entscheidet keep."@de .
<{EX}KeepOrAboutShape> a sh:NodeShape ;
    sh:targetClass <{SNAPSHOT}> ;
    sh:or (
        [ sh:path <{DECISION}> ; sh:in ( "keep" ) ]
        [ sh:path <{ABOUT}> ; sh:minCount 1 ]
    ) ;
    sh:message "A snapshot keeps, or is about an Operation." .
"""

# An Operation is never running: the shape the entry of a commit breaks.
_NEVER_RUNNING = f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
<{EX}StatusShape> a sh:NodeShape ;
    sh:targetClass <{CFC.Operation}> ;
    sh:property <{EX}StatusValueShape> .
<{EX}StatusValueShape> a sh:PropertyShape ;
    sh:path <{SVC.hasOperationStatus}> ;
    sh:in ( "queued" "done" "failed" ) .
"""

# An Operation is never failed: the shape the error transition of a commit breaks.
_NEVER_FAILED = f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
<{EX}NotFailedShape> a sh:NodeShape ;
    sh:targetClass <{CFC.Operation}> ;
    sh:property <{EX}NotFailedValueShape> .
<{EX}NotFailedValueShape> a sh:PropertyShape ;
    sh:path <{SVC.hasOperationStatus}> ;
    sh:in ( "queued" "running" "done" ) .
"""

# A transfer module possesses at most one workpiece.
_ONE_WORKPIECE_PER_MODULE = f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
<{EX}OneWorkpieceShape> a sh:NodeShape ;
    sh:targetClass <{seed.TRANSFER_MODULE_CLASS}> ;
    sh:property <{EX}OneWorkpiecePossessionShape> .
<{EX}OneWorkpiecePossessionShape> a sh:PropertyShape ;
    sh:path ( <{CFC.hasPossessor}> [ sh:inversePath <{CFC.hasPossessedWorkpiece}> ] ) ;
    sh:maxCount 1 ;
    sh:message "<b>A transfer module holds one box</b> & no more." .
"""

_LOGGER = "kapps_semantic_middleware.validation_report"


def _instance(store, resource: IRI, service_class: IRI) -> SemanticMiddleware:
    """An instance with both pages: its dashboard, and its activity feed."""
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=resource,
        service_class=service_class,
        ogm=OGM(db=triplestore_for(store.dataset)),
        host="127.0.0.1",
        port=8992,
        heartbeat_interval=None,
        activity_feed=True,
    )
    enable_dashboard(mw)
    return mw


def _refused(store, shapes: str, write) -> ShaclValidationError:
    """The store's refusal of `write`, under `shapes`, which are removed again afterwards."""
    store.shapes_set(shapes, "text/turtle")
    try:
        with pytest.raises(ShaclValidationError) as refusal:
            write()
    finally:
        store.shapes_set("", "text/turtle")
    return refusal.value


def _box(mw: SemanticMiddleware) -> BrowserView:
    """The box "Refused writes" as a browser loads it: the page names its fragment."""
    client = TestClient(mw.app)
    page = client.get("/dashboard").text
    slot = re.search(r'id="box-refused-writes" hx-get="([^"]+)"', page)
    assert slot is not None, "the page has no box refused-writes"
    return BrowserView(client.get(f"/{slot.group(1)}").text)


def _streamed_refusals(mw: SemanticMiddleware) -> list[dict]:
    """The records of refused writes that the activity page's stream sends, as it sends them."""
    page = TestClient(mw.app).get("/activity").text
    assert "EventSource('activity/stream')" in page
    stream = TestClient(mw.app).get("/activity/stream").text  # ends after its lifetime
    records = [
        json.loads(line[len("data: ") :])
        for line in stream.splitlines()
        if line.startswith("data: ")
    ]
    return [record for record in records if record["logger"] == _LOGGER]


def test_a_refused_create_is_shown_readably_on_both_pages(store):
    seed.seed_scenario1(store)
    store.graph_add(_TBOX, content_type="application/x-turtle")
    mw = _instance(store, seed.HELLO_RESOURCE, seed.HELLO_SERVICE_CLASS)
    created: list[IRI] = []

    def create() -> None:
        with mw.create(SNAPSHOT, ClassScope()) as snapshot:
            created.append(snapshot.iri)
            snapshot.update({DECISION: ["drill"]})

    refusal = _refused(store, _SNAPSHOT_SHAPES, create)
    (snapshot,) = created
    local = str(snapshot).rsplit("#", 1)[-1]

    # The reader, on the report as the store sent it.
    assert validation_results(refusal.report) == {
        ValidationResult(
            focus=str(snapshot), path=f"<{DECISION}>", shape=f"{EX}DecisionValueShape"
        ),
        ValidationResult(focus=str(snapshot), path="", shape=f"{EX}KeepOrAboutShape"),
    }
    assert rule_names(refusal.report) == [
        f"DecisionValueShape at {local}",
        f"KeepOrAboutShape at {local}",
    ]
    results = readable_results(refusal.report)
    assert results is not None
    by_shape = {result.shape.short: result for result in results if result.shape}
    with_path = by_shape["DecisionValueShape"]
    assert with_path.message == Term(
        "A snapshot decides keep.\nEin Schnappschuss entscheidet keep.",
        '"A snapshot decides keep."@en\n"Ein Schnappschuss entscheidet keep."@de',
    )
    assert with_path.severity == Term("Violation", str(SH.Violation))
    assert with_path.focus == Term(local, str(snapshot))
    assert with_path.path == Term("decision", f"<{DECISION}>")
    assert with_path.value == Term("drill", '"drill"')
    assert with_path.constraint == Term("InConstraintComponent", str(SH.InConstraintComponent))
    without_path = by_shape["KeepOrAboutShape"]
    assert without_path.message == Term(
        "A snapshot keeps, or is about an Operation.",
        '"A snapshot keeps, or is about an Operation."',
    )
    assert without_path.path is None
    assert without_path.constraint == Term("OrConstraintComponent", str(SH.OrConstraintComponent))

    # The record, and the error, unchanged.
    (recorded,) = mw.refused_writes
    assert (recorded.verb, recorded.report) == ("create", refusal.report)
    first_line = f"create refused by DecisionValueShape at {local}, KeepOrAboutShape at {local}"

    # The dashboard: the first line visible, the table and the whole report behind the expander.
    (shown,) = _box(mw).refused
    assert shown.summary.endswith(first_line), shown.summary
    assert [result["Source shape"].text for result in shown.results] == [
        "DecisionValueShape",
        "KeepOrAboutShape",
    ]
    assert shown.results[0]["Path"].title == f"<{DECISION}>"
    assert shown.results[0]["Message"].text == (
        "A snapshot decides keep.\nEin Schnappschuss entscheidet keep."
    )
    assert shown.results[1]["Path"].text == "—"
    assert shown.report == refusal.report

    # The activity page: one record at INFO, its first line, then the results and the report.
    (streamed,) = _streamed_refusals(mw)
    assert streamed["level"] == "INFO"
    first, rest = streamed["message"].split("\n", 1)
    assert first == first_line
    assert "Result 1 of 2" in rest and "Result 2 of 2" in rest
    assert "  Message: A snapshot keeps, or is about an Operation." in rest
    assert rest.endswith(refusal.report.rstrip("\n"))


def test_a_refused_commit_is_shown_as_a_commit(store):
    seed.seed_scenario1(store)
    mw = _instance(store, seed.HELLO_RESOURCE, seed.HELLO_SERVICE_CLASS)
    operation = IRI(f"{EX}operation_1")
    store.triple_add((operation, RDF.type, CFC.Operation))
    store.triple_add((operation, SVC.hasOperationStatus, Literal("queued")))
    running = [Update(instance_iri=operation, data={str(SVC.hasOperationStatus): ["running"]})]

    def commit() -> None:
        with mw.commit(operation, entry=running):
            pytest.fail("the body of a refused entry ran")

    refusal = _refused(store, _NEVER_RUNNING, commit)

    (recorded,) = mw.refused_writes
    assert (recorded.verb, recorded.rules) == ("commit", ("StatusValueShape at operation_1",))
    assert recorded.report == refusal.report
    (shown,) = _box(mw).refused
    assert shown.summary.endswith("commit refused by StatusValueShape at operation_1")
    assert shown.results[0]["Value"].text == "running"
    (streamed,) = _streamed_refusals(mw)
    assert streamed["message"].split("\n")[0] == "commit refused by StatusValueShape at operation_1"


def test_a_refused_error_transition_is_shown_as_an_error_transition(store):
    seed.seed_scenario1(store)
    mw = _instance(store, seed.HELLO_RESOURCE, seed.HELLO_SERVICE_CLASS)
    operation = IRI(f"{EX}operation_2")
    store.triple_add((operation, RDF.type, CFC.Operation))
    store.triple_add((operation, SVC.hasOperationStatus, Literal("queued")))

    def status(value: str) -> list:
        return [Update(instance_iri=operation, data={str(SVC.hasOperationStatus): [value]})]

    failure = RuntimeError("the body failed")

    def commit() -> None:
        # The body's error reaches the caller; the store refuses the error transition.
        with mw.commit(operation, entry=status("running"), error=status("failed")):
            raise failure

    store.shapes_set(_NEVER_FAILED, "text/turtle")
    try:
        with pytest.raises(RuntimeError) as raised:
            commit()
    finally:
        store.shapes_set("", "text/turtle")

    assert raised.value is failure
    assert any("The error transition was not committed" in note for note in failure.__notes__)
    rule = "NotFailedValueShape at operation_2"
    (recorded,) = mw.refused_writes
    assert (recorded.verb, recorded.rules) == ("commit", (rule,))
    assert repr(recorded).startswith("RefusedWrite(")
    (shown,) = _box(mw).refused
    assert shown.summary.endswith(f"commit (error transition) refused by {rule}")
    assert shown.results[0]["Value"].text == "failed"
    (streamed,) = _streamed_refusals(mw)
    assert streamed["level"] == "INFO"
    assert streamed["message"].split("\n")[0] == f"commit (error transition) refused by {rule}"
    assert streamed["message"].endswith(recorded.report.rstrip("\n"))


def test_a_refused_handover_is_shown_as_a_handover_and_its_markup_as_text(store):
    seed.seed_handover(store, OGM(db=store))
    other_box = IRI(f"{seed.DEMO_NS}box_002")
    store.triple_add((other_box, RDF.type, seed.BOX_CLASS))
    create_possession(OGM(db=store), workpiece_iri=other_box, possessor_iri=seed.HANDOVER_DEST)
    mw = _instance(store, seed.HANDOVER_SOURCE, IRI(f"{seed.DEMO_NS}TransferModuleService"))
    destination = str(seed.HANDOVER_DEST).rsplit("#", 1)[-1]

    def hand_over() -> None:
        with handover(
            mw,
            mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST
        ):
            pytest.fail("the body of a refused handover ran")

    refusal = _refused(store, _ONE_WORKPIECE_PER_MODULE, hand_over)

    (recorded,) = mw.refused_writes
    first_line = f"handover refused by OneWorkpiecePossessionShape at {destination}"
    assert (recorded.verb, recorded.report) == ("handover", refusal.report)
    view = _box(mw)
    assert "b" not in view.tags, "a message's markup reached the page as markup"
    (shown,) = view.refused
    assert shown.summary.endswith(first_line)
    assert shown.results[0]["Message"].text == (
        "<b>A transfer module holds one box</b> & no more."
    )
    assert shown.results[0]["Path"].text == "(hasPossessor/^hasPossessedWorkpiece)"
    (streamed,) = _streamed_refusals(mw)
    assert streamed["level"] == "INFO"
    assert streamed["message"].split("\n")[0] == first_line
