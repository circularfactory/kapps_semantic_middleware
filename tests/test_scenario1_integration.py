"""Scenario 1 end-to-end integration test against a live store.

Drives the public SemanticMiddleware API (the one test seam): a hello-world middleware
registers a workflow on startup; a second middleware (a planner) dispatches an operation
through the event-trigger coordination model — creating it `queued` in the graph and
ringing the hello resource's event trigger over REST — and the hello resource pulls and
runs it, recording the outcome. The graph is asserted at each step. Skipped when TRIPLESTORE_*
env vars are absent (see conftest).
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from rdflib.namespace import RDF

from kapps_ogm import OGM
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import triplestore_env_present, triplestore_for
from kapps_semantic_middleware.registration import (
    mint_capability_iri,
    mint_service_iri,
    mint_workflow_iri,
)
from kapps_semantic_middleware.vocabulary import CFC, OperationStatus, SVC

requires_store = pytest.mark.skipif(
    not triplestore_env_present(),
    reason="TRIPLESTORE_* environment variables not set; skipping live-store integration test",
)

# examples/ is not a package; add it to the path to reuse the scenario seed helper.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

HELLO_PORT = 8993


def hello_world() -> str:
    """The most basic workflow: return a greeting."""
    return "hello world"


def _start_server(mw: SemanticMiddleware, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    config = uvicorn.Config(mw.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    t0 = time.time()
    while not server.started and time.time() - t0 < 30:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("server did not start in time")
    return server, thread


@requires_store
def test_scenario1_hello_world_end_to_end(store):
    db = store
    seed.seed_scenario1(db)

    mw1 = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=triplestore_for(store.dataset)),
        host="127.0.0.1",
        port=HELLO_PORT,
    )
    # Per-instance: read off the instance, not rebuilt from the resource.
    service_iri = mw1.service_iri
    cap_instance = mint_capability_iri(seed.HELLO_RESOURCE, "hello_world")
    wf_instance = mint_workflow_iri(service_iri, "hello_world")
    mw1.workflow(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
    )(hello_world)

    server, thread = _start_server(mw1, HELLO_PORT)
    try:
        # Registration wrote the full Service/Capability/Workflow structure +
        # reachability. Links are materialized on the instance-owned (inverse) side:
        # a Service knows its resource via isServiceOf, a Workflow knows
        # its Service via isWorkflowOf, a Capability its Workflow via realizedByWorkflow.
        assert db.triple_exists((service_iri, RDF.type, seed.HELLO_SERVICE_CLASS))
        assert db.triple_exists((service_iri, SVC.isServiceOf, seed.HELLO_RESOURCE))
        assert db.triple_exists((cap_instance, RDF.type, seed.HELLO_CAPABILITY_CLASS))
        assert db.triple_exists((cap_instance, SVC.realizedByWorkflow, wf_instance))
        assert db.triple_exists((wf_instance, SVC.isWorkflowOf, service_iri))
        assert db.triples_get(sub=service_iri, pred=SVC.hasAddress)
        assert db.triples_get(sub=wf_instance, pred=SVC.hasEndpoint)

        # A second middleware (a planner) dispatches an operation for the hello capability:
        # it creates the Operation `queued` in the graph and rings the hello resource's
        # event trigger over REST — the peer is resolved purely through the graph, never
        # hardcoded.
        mw2 = SemanticMiddleware(
            mode="resource",
            resource_iri=seed.PLANNER_RESOURCE,
            service_class=seed.PLANNER_SERVICE_CLASS,
            ogm=OGM(db=triplestore_for(store.dataset)),
            host="127.0.0.1",
            port=8994,
        )
        with mw2.issue_operation(
            capability_class=seed.HELLO_CAPABILITY_CLASS,
            operation_class=str(CFC.Operation),
        ) as op:
            pass  # helloworld takes no arguments to populate
        op_iri = op.iri

        # The dispatch created the Operation, addressed via its Capability, queued on the
        # hello resource. The hello resource then pulls and runs it (pull-and-run).
        assert db.triple_exists((op_iri, CFC.implementsCapability, cap_instance))
        with mw1.claim_next() as claimed:
            assert claimed.iri == op_iri
            claimed.result = hello_world()

        # The terminal transition recorded status `done` + execution provenance in one
        # atomic write (the status is itself the provenance record).
        status = list(db.triples_get(sub=op_iri, pred=SVC.hasOperationStatus))
        assert status and str(status[0][2]) == OperationStatus.DONE
        assert db.triple_exists((op_iri, SVC.executedByWorkflow, wf_instance))
        assert db.triples_get(sub=op_iri, pred=SVC.hasExecutionTimestamp)
        result = list(db.triples_get(sub=op_iri, pred=SVC.hasExecutionResult))
        assert result and str(result[0][2]) == "hello world"
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        time.sleep(0.5)

    # Deregistration on shutdown removed reachability but preserved the individuals.
    assert not db.triples_get(sub=service_iri, pred=SVC.hasAddress)
    assert not db.triples_get(sub=wf_instance, pred=SVC.hasEndpoint)
    assert db.triple_exists((wf_instance, RDF.type, seed.HELLO_WORKFLOW_CLASS))


@requires_store
def test_missing_ground_truth_class_fails_fast(store):
    """A workflow referencing a non-existent class is rejected at registration."""
    from kapps_semantic_middleware.registration import (
        OntologyGroundTruthError,
        register_workflow,
    )

    seed.seed_scenario1(store)
    ogm = OGM(db=store)
    ghost_service = mint_service_iri(seed.HELLO_RESOURCE, "http://127.0.0.1:9")
    with pytest.raises(OntologyGroundTruthError):
        register_workflow(
            ogm,
            resource_iri=seed.HELLO_RESOURCE,
            service_iri=ghost_service,
            workflow_iri=mint_workflow_iri(ghost_service, "ghost"),
            workflow_class="http://example.org/kapps-demo#NonexistentWorkflow",
            capability_iri=mint_capability_iri(seed.HELLO_RESOURCE, "ghost"),
            capability_class=seed.HELLO_CAPABILITY_CLASS,
            endpoint="http://127.0.0.1:9/workflows/ghost/execute",
        )
