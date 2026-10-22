"""`mw.commit()` against a live store, with the store's own SHACL engine as the refusal.

Two Operations of the hello resource carry the state: the commit is about the first, and the
second is a further instance that the body or the exit transition changes. The OGM is the real
one, subclassed only to record each `commit` call, so "one commit" is counted at the seam that
`mw.commit()` writes through. Skipped when the TRIPLESTORE_* variables are absent (see conftest).
A test that needs a shape sets it with the store's `shapes_set`, which replaces every shape
of the dataset, and removes every shape again at its end.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest
from kapps_ogm import OGM, Update
from kapps_ogm.utils.class_scope import ClassScope
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import TripleStoreInterfaceError

from _explicit import explicit_select
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import triplestore_for
from kapps_semantic_middleware.registration import (
    create_operation,
    mint_capability_iri,
    mint_operation_iri,
    mint_service_iri,
    mint_workflow_iri,
    register_service,
    register_workflow,
)
from kapps_semantic_middleware.vocabulary import CFC, OperationStatus, SVC

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

_ADDRESS = "http://127.0.0.1:8994"


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
    """The real OGM, recording each commit: whether it carried changes, when, and on which thread."""

    def __init__(self, db) -> None:
        super().__init__(db=db)
        self.calls: list[tuple[str, float, int]] = []

    def commit(self, **kwargs):  # type: ignore[override]
        result = super().commit(**kwargs)
        kind = "single" if kwargs.get("changes") is None else "changes"
        self.calls.append((kind, time.monotonic(), threading.get_ident()))
        return result


def _seed(db) -> tuple[IRI, IRI]:
    """The hello resource with one workflow, and two queued Operations of its capability."""
    seed.seed_scenario1(db)
    ogm = OGM(db=db)
    service_iri = mint_service_iri(seed.HELLO_RESOURCE, _ADDRESS)
    capability_iri = mint_capability_iri(seed.HELLO_RESOURCE, "hello_world")
    register_service(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=service_iri,
        service_class=seed.HELLO_SERVICE_CLASS,
        address=_ADDRESS,
    )
    register_workflow(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=service_iri,
        workflow_iri=mint_workflow_iri(service_iri, "hello_world"),
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
        capability_iri=capability_iri,
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        endpoint=f"{_ADDRESS}/workflows/hello_world/execute",
    )
    operations = []
    for _ in range(2):
        operation_iri = mint_operation_iri(CFC.Operation)
        create_operation(
            ogm,
            operation_iri=operation_iri,
            operation_class=CFC.Operation,
            capability_iri=capability_iri,
            status=OperationStatus.QUEUED,
        )
        operations.append(operation_iri)
    return operations[0], operations[1]


def _middleware(store, heartbeat_interval=None) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=_RecordingOGM(triplestore_for(store.dataset)),
        host="127.0.0.1",
        port=8994,
        heartbeat_interval=heartbeat_interval,
    )


def _status(operation_iri: IRI, value: str) -> list:
    return [Update(instance_iri=operation_iri, data={str(SVC.hasOperationStatus): [value]})]


def _values(db, subject: IRI, predicate: IRI) -> list[str]:
    return sorted(str(t[2]) for t in db.triples_get(sub=subject, pred=predicate))


def _explicit_statements(db) -> set:
    """Every explicit statement of the default graph, to compare before and after."""
    result = db.query(
        explicit_select(["?s", "?p", "?o"], "?s ?p ?o"),
        convert_bindings=True,
    )
    return {(str(b["s"]), str(b["p"]), str(b["o"])) for b in result["results"]["bindings"]}


def test_a_refused_entry_runs_no_body_and_writes_nothing(store):
    """The store's SHACL engine refuses `running`; the body never starts."""
    op, _ = _seed(store)
    mw = _middleware(store)
    ran = []
    store.shapes_set(_status_shape("queued", "done", "failed"), "text/turtle")
    try:
        before = _explicit_statements(store)
        with pytest.raises(TripleStoreInterfaceError, match="ValidationReport"):
            with mw.commit(
                op,
                entry=_status(op, "running"),
                exit=_status(op, "done"),
                error=_status(op, "failed"),
            ):
                ran.append(True)
        after = _explicit_statements(store)
    finally:
        store.shapes_set("", "text/turtle")
    assert ran == []
    assert after == before


def test_an_error_in_the_body_leaves_only_the_error_transition(store):
    op, other = _seed(store)
    mw = _middleware(store)
    with pytest.raises(RuntimeError, match="gripper jammed"):
        with mw.commit(
            op, entry=_status(op, "running"), exit=_status(op, "done"), error=_status(op, "failed")
        ) as transition:
            transition.update(op, {str(SVC.executionResult): ["half a hole"]})
            transition.update(other, {str(SVC.executionResult): ["never written"]})
            raise RuntimeError("gripper jammed")
    assert _values(store, op, SVC.hasOperationStatus) == ["failed"]
    assert _values(store, op, SVC.executionResult) == []
    assert _values(store, other, SVC.executionResult) == []
    assert _values(store, other, SVC.hasOperationStatus) == ["queued"]


def test_a_clean_exit_is_one_commit_and_merges_the_changes_to_one_instance(store):
    """kapps_ogm refuses two changes that write one instance, so the exit merges them."""
    op, other = _seed(store)
    mw = _middleware(store)
    with mw.commit(op, entry=_status(op, "running"), exit=_status(op, "done")) as transition:
        assert _values(store, op, SVC.hasOperationStatus) == ["running"]
        transition.update(op, {str(SVC.executionResult): ["drilled"]})
        transition.update(other, {str(SVC.executionResult): ["handed on"]})
    assert [kind for kind, _, _ in mw.ogm.calls] == ["changes", "changes"]  # entry, exit
    assert _values(store, op, SVC.hasOperationStatus) == ["done"]
    assert _values(store, op, SVC.executionResult) == ["drilled"]
    assert _values(store, other, SVC.executionResult) == ["handed on"]


def test_a_body_that_sets_the_status_commits_its_value_and_skips_the_exit_transition(store):
    op, other = _seed(store)
    mw = _middleware(store)
    exit_transition = [
        Update(instance_iri=op, data={str(SVC.hasOperationStatus): ["done"]}),
        Update(instance_iri=other, data={str(SVC.executionResult): ["exit ran"]}),
    ]
    with mw.commit(op, entry=_status(op, "running"), exit=exit_transition) as transition:
        transition.update(op, {str(SVC.hasOperationStatus): ["queued"]})  # put back for a later try
    assert _values(store, op, SVC.hasOperationStatus) == ["queued"]
    assert _values(store, other, SVC.executionResult) == []


def test_a_refused_exit_commits_the_error_transition_and_raises(store):
    op, _ = _seed(store)
    mw = _middleware(store)
    store.shapes_set(_status_shape("queued", "running", "failed"), "text/turtle")
    try:
        with pytest.raises(TripleStoreInterfaceError, match="ValidationReport"):
            with mw.commit(
                op, entry=_status(op, "running"), exit=_status(op, "done"), error=_status(op, "failed")
            ) as transition:
                transition.update(op, {str(SVC.executionResult): ["drilled"]})
    finally:
        store.shapes_set("", "text/turtle")
    assert _values(store, op, SVC.hasOperationStatus) == ["failed"]
    assert _values(store, op, SVC.executionResult) == []


def test_a_body_awaiting_longer_than_the_heartbeat_interval_delays_no_heartbeat(store):
    """Heartbeats keep landing in the store while the body awaits, and the commit's own graph
    I/O runs in worker threads, not on the event loop's thread."""
    op, _ = _seed(store)
    interval = 0.2
    mw = _middleware(store, heartbeat_interval=interval)

    async def scenario() -> tuple[float, float, int]:
        await mw._register_service()
        # The loop that resource mode starts at startup, started alone.
        await mw._start_heartbeat()
        try:
            async with mw.commit(
                op, entry=_status(op, "running"), exit=_status(op, "done")
            ) as transition:
                started = time.monotonic()
                await asyncio.sleep(2.0)  # a robot's move, awaited
                transition.update(op, {str(SVC.executionResult): ["drilled"]})
                ended = time.monotonic()
        finally:
            await mw._stop_heartbeat()
        return started, ended, threading.get_ident()

    started, ended, loop_thread = asyncio.run(scenario())

    heartbeats = [t for kind, t, _ in mw.ogm.calls if kind == "single" and started <= t <= ended]
    assert len(heartbeats) >= 3, heartbeats
    # A heartbeat lands every interval plus one write, about 0.3 s on KIT. A loop blocked for
    # the body's 2 s would leave a gap that long.
    marks = [started, *heartbeats, ended]
    assert max(b - a for a, b in zip(marks, marks[1:])) < 5 * interval
    commit_threads = {thread for kind, _, thread in mw.ogm.calls if kind == "changes"}
    assert loop_thread not in commit_threads
    assert _values(store, op, SVC.hasOperationStatus) == ["done"]
    assert _values(store, op, SVC.executionResult) == ["drilled"]


def test_fetch_reads_an_operation_under_a_class_scope(store):
    op, _ = _seed(store)
    node = _middleware(store).fetch(
        str(op),
        class_scope=ClassScope.from_property_chains([[SVC.hasOperationStatus]]),
        materialize=True,
    )
    assert node.id == op
    assert [str(value) for value in node.data[SVC.hasOperationStatus]] == ["queued"]
    assert node.instance is not None


def test_a_deadline_that_cancels_the_body_leaves_the_error_transition(store):
    op, _ = _seed(store)
    mw = _middleware(store)

    async def scenario() -> bool:
        with anyio.move_on_after(1.0) as deadline:
            async with mw.commit(
                op, entry=_status(op, "running"), exit=_status(op, "done"), error=_status(op, "failed")
            ):
                await anyio.sleep(30)  # a move that takes too long
        return deadline.cancelled_caught

    assert asyncio.run(scenario())
    assert _values(store, op, SVC.hasOperationStatus) == ["failed"]
