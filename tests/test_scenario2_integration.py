"""Scenario 2 end-to-end integration test against a live GraphDB.

A decentrally-controlled door and a minimal mobile robot. Demonstrate **direct invocation**
of workflows and state. Contrast with scenario 1
asynchronous operation-coordination model. The door exposes two workflows (open,
close) and one live StateProperty (status). It has **no operation queue**. Its
workflow endpoints execute synchronously when invoked directly. The mobile robot
is a minimal consumer. It discovers the door purely through the knowledge graph.
A SPARQL query. It reads the door live state via the StateProperty GET endpoint.
It finds it closed. Invoke the door open workflow directly. Hit the execute URL
it found in the graph. This is NOT operation based. Skip when GRAPHDB_* env vars
are absent (see conftest).
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from rdflib.namespace import RDF

from kapps_ogm import OGM
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import graphdb_env_present, graphdb_for
from kapps_semantic_middleware.registration import (
    mint_capability_iri,
    mint_state_property_iri,
    mint_workflow_iri,
)
from kapps_semantic_middleware.vocabulary import SVC

requires_graphdb = pytest.mark.skipif(
    not graphdb_env_present(),
    reason="GRAPHDB_* environment variables not set; skipping live-GraphDB integration test",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402
from handlers import door_close, door_open, door_status, reset_door  # noqa: E402

DOOR_PORT = 8997
ROBOT_PORT = 8998


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


def _discover_door_endpoints(ogm, door_resource_iri) -> tuple[str, str]:
    """Discover the door live status GET endpoint and its open-workflow execute URL purely
    from the knowledge graph. Via a SPARQL query (no hardcoded URLs). This is how the robot
    finds the door it approaches and the reachable endpoints it needs."""
    sparql = f"""
    SELECT ?status_url ?open_url WHERE {{
        ?svc <{SVC.isServiceOf}> <{door_resource_iri}> .
        ?sp <{SVC.isStatePropertyOf}> ?svc .
        ?sp a <{seed.DOOR_STATUS_STATE_CLASS}> .
        ?sp <{SVC.endpoint}> ?status_url .
        ?wf <{SVC.isWorkflowOf}> ?svc .
        ?wf a <{seed.DOOR_OPEN_WORKFLOW_CLASS}> .
        ?wf <{SVC.endpoint}> ?open_url .
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    assert bindings, "robot could not discover the door endpoints in the knowledge graph"
    b = bindings[0]
    return str(b["status_url"]), str(b["open_url"])


def robot_pass_through_door(ogm, door_resource_iri) -> list[str]:
    """The mobile robot scripted behavior.

    Deterministic. One step after another. The only forks are "is the door open?"
    and, if not, "open it". Discovery is via SPARQL against the knowledge graph.
    The live state and the workflow invocation are direct REST calls. Endpoints
    found in the graph. Not operation based. No queue.
    """
    log: list[str] = []
    status_url, open_url = _discover_door_endpoints(ogm, door_resource_iri)
    log.append("discovered door endpoints via SPARQL")

    def ensure_open(phase: str) -> None:
        state = httpx.get(status_url).json()
        log.append(f"{phase}: door is {state}")
        if state == "closed":
            httpx.post(open_url)  # invoke the open workflow directly at its execute URL
            log.append(f"{phase}: door was closed -> invoked open workflow")
            state = httpx.get(status_url).json()
            log.append(f"{phase}: door is now {state}")
        assert state == "opened", f"door failed to open ({phase})"

    # Approach the door: check its state and, if closed, open it, then drive through.
    ensure_open("approach")
    log.append("drove through the door")
    # (Story, not implemented: drop the load somewhere behind the door, then drive back.)
    # On return the door auto-closes after 30 s. The drop-off took less time. It is
    # still open. The robot drives straight back through without further interaction.
    ensure_open("return")
    log.append("drove back through the door")
    return log


@requires_graphdb
def test_scenario2_door_direct_invocation_by_mobile_robot(graphdb):
    db = graphdb
    reset_door()
    seed.seed_scenario2(db)

    # The door middleware: two workflows + one live StateProperty. This scenario does not
    # use the operation queue at all. The workflow endpoints execute synchronously.
    door = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.DOOR_RESOURCE,
        service_class=seed.DOOR_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=DOOR_PORT,
    )
    door.workflow(
        capability_class=seed.DOOR_OPEN_CAPABILITY_CLASS,
        workflow_class=seed.DOOR_OPEN_WORKFLOW_CLASS,
    )(door_open)
    door.workflow(
        capability_class=seed.DOOR_CLOSE_CAPABILITY_CLASS,
        workflow_class=seed.DOOR_CLOSE_WORKFLOW_CLASS,
    )(door_close)
    door.state(
        capability_class=seed.DOOR_STATUS_CAPABILITY_CLASS,
        state_property_class=seed.DOOR_STATUS_STATE_CLASS,
    )(door_status)

    # Per-instance since ADR 0022. Everything hanging off the Service is derived from the
    # instance own IRI. Not reconstructed from the resource.
    service_iri = door.service_iri
    open_wf = mint_workflow_iri(service_iri, "door_open")
    close_wf = mint_workflow_iri(service_iri, "door_close")
    status_sp = mint_state_property_iri(service_iri, "door_status")

    server, thread = _start_server(door, DOOR_PORT)
    try:
        # Registration wrote the door structure + reachability (instance-owned inverse
        # side, ADR 0006): both workflows and the state property, with reachable endpoints.
        assert db.triple_exists((open_wf, SVC.isWorkflowOf, service_iri))
        assert db.triple_exists((close_wf, SVC.isWorkflowOf, service_iri))
        assert db.triple_exists((status_sp, SVC.isStatePropertyOf, service_iri))
        assert db.triple_exists((status_sp, RDF.type, seed.DOOR_STATUS_STATE_CLASS))
        status_cap = mint_capability_iri(seed.DOOR_RESOURCE, "door_status")
        assert db.triple_exists((status_cap, SVC.providedByStateProperty, status_sp))
        assert db.triples_get(sub=status_sp, pred=SVC.endpoint)
        assert db.triples_get(sub=open_wf, pred=SVC.endpoint)

        # The mobile robot: a minimal second middleware. It exposes nothing itself. Its
        # scripted domain logic discovers and drives the door purely through the graph +
        # REST. Use its own OGM connection for the SPARQL discovery.
        robot = SemanticMiddleware(
            mode="resource",
            resource_iri=seed.MOBILE_ROBOT,
            service_class=seed.MOBILE_ROBOT_SERVICE_CLASS,
            ogm=OGM(db=graphdb_for(graphdb.repository)),
            host="127.0.0.1",
            port=ROBOT_PORT,
        )
        log = robot_pass_through_door(robot.ogm, seed.DOOR_RESOURCE)

        # The robot found the door closed on approach. Invoked the open workflow directly.
        # Drove through. On return found it still open (auto-close had not fired).
        assert "discovered door endpoints via SPARQL" in log
        assert "drove through the door" in log
        assert "drove back through the door" in log
        # The open workflow was invoked exactly once. Only on approach. Not on return.
        assert sum("invoked open workflow" in line for line in log) == 1
        # The direct workflow invocation actually opened the door (live state).
        assert door_status() == "opened"

        # The live status value is NEVER written to the graph. The state property carries
        # only structural triples + endpoint. No "opened"/"closed" literal appears on it.
        for _, _, obj in db.triples_get(sub=status_sp):
            assert str(obj) not in ("opened", "closed"), "state value must not be persisted"
    finally:
        reset_door()
        server.should_exit = True
        thread.join(timeout=20)
        time.sleep(0.5)

    # Deregistration removed the state property endpoint too. Kept the individual.
    assert not db.triples_get(sub=status_sp, pred=SVC.endpoint)
    assert db.triple_exists((status_sp, RDF.type, seed.DOOR_STATUS_STATE_CLASS))
