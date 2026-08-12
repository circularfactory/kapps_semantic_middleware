"""Queue durability + recovery integration tests (#17) against a live GraphDB.

On startup a resource reconstructs its operation queue from the graph — re-enqueuing its
own ``queued`` Operations and failing its own orphaned ``running`` ones (never auto-rerun) —
and a watchdog marks a dead resource's stranded Operations ``failed``. A resource's own
Operations are found by ontology traversal (Operation --implementsCapability--> Capability
<--hasCapability-- resource), never by matching IRI names. Skipped when GRAPHDB_* env vars
are absent (see conftest).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from kapps_ogm import OGM
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import graphdb_env_present, graphdb_for
from kapps_semantic_middleware.registration import (
    create_operation,
    find_resource_operations,
    mint_capability_iri,
    mint_operation_iri,
    mint_service_iri,
    mint_workflow_iri,
    register_service,
    register_workflow,
)
from kapps_semantic_middleware.vocabulary import CFC, OperationStatus, SVC

requires_graphdb = pytest.mark.skipif(
    not graphdb_env_present(),
    reason="GRAPHDB_* environment variables not set; skipping live-GraphDB integration test",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402


def _register_hello_resource(ogm) -> tuple[str, str]:
    """Register the hello resource's service + workflow (writes the Resource->Capability link).

    Returns (service_iri, capability_iri). Simulates a resource that existed before a crash.
    """
    # The address is the middleware's own (port 8993 below), so the restarting instance
    # re-adopts this node rather than minting a second one (ADR 0022).
    service_iri = mint_service_iri(seed.HELLO_RESOURCE, "http://127.0.0.1:8993")
    cap_iri = mint_capability_iri(seed.HELLO_RESOURCE, "hello_world")
    wf_iri = mint_workflow_iri(service_iri, "hello_world")
    register_service(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=service_iri,
        service_class=seed.HELLO_SERVICE_CLASS,
        address="http://127.0.0.1:8993",
    )
    register_workflow(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=service_iri,
        workflow_iri=wf_iri,
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
        capability_iri=cap_iri,
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        endpoint="http://127.0.0.1:8993/workflows/hello_world/execute",
    )
    return service_iri, cap_iri


def _create_op(ogm, cap_iri, status: str):
    op_iri = mint_operation_iri(CFC.Operation)
    create_operation(
        ogm,
        operation_iri=op_iri,
        operation_class=CFC.Operation,
        capability_iri=cap_iri,
        status=status,
    )
    return op_iri


def _resource_middleware(graphdb):
    return SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=8993,
    )


def _status(db, op_iri) -> str | None:
    triples = list(db.triples_get(sub=op_iri, pred=SVC.operationStatus))
    return str(triples[0][2]) if triples else None


@requires_graphdb
def test_startup_reconstructs_queued_operation(graphdb):
    """A restarting resource re-enqueues its own queued Operations (ontology traversal)."""
    db = graphdb
    seed.seed_scenario1(db)
    ogm = OGM(db=db)
    _service, cap_iri = _register_hello_resource(ogm)
    op_iri = _create_op(ogm, cap_iri, OperationStatus.QUEUED)

    # The 2-hop, name-independent query finds it.
    assert op_iri in find_resource_operations(ogm, seed.HELLO_RESOURCE, [OperationStatus.QUEUED])

    # A fresh middleware for the same resource reconstructs its queue on startup.
    mw = _resource_middleware(graphdb)
    asyncio.run(mw._reconstruct_queue())
    assert op_iri in mw._operation_queue


@requires_graphdb
def test_startup_fails_orphaned_running_operation(graphdb):
    """A resource that crashed mid-execution fails its orphaned running Operation, never reruns."""
    db = graphdb
    seed.seed_scenario1(db)
    ogm = OGM(db=db)
    _service, cap_iri = _register_hello_resource(ogm)
    op_iri = _create_op(ogm, cap_iri, OperationStatus.RUNNING)

    mw = _resource_middleware(graphdb)
    asyncio.run(mw._reconstruct_queue())

    # The orphaned running op is failed (not auto-rerun) and NOT put back on the queue.
    assert _status(db, op_iri) == OperationStatus.FAILED
    assert op_iri not in mw._operation_queue


@requires_graphdb
def test_watchdog_sweeps_stranded_operations(graphdb):
    """A watchdog marks a stale resource's stranded queued + running Operations failed."""
    db = graphdb
    seed.seed_scenario1(db)
    ogm = OGM(db=db)
    service_iri, cap_iri = _register_hello_resource(ogm)
    queued_op = _create_op(ogm, cap_iri, OperationStatus.QUEUED)
    running_op = _create_op(ogm, cap_iri, OperationStatus.RUNNING)

    # The service advertised an address but never heartbeated -> stale. A watchdog sweeps it.
    watchdog = SemanticMiddleware(mode="watchdog", ogm=OGM(db=graphdb_for(graphdb.repository)))
    swept = asyncio.run(watchdog.sweep())

    assert service_iri in swept
    assert _status(db, queued_op) == OperationStatus.FAILED
    assert _status(db, running_op) == OperationStatus.FAILED
