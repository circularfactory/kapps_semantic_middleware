"""Scenario 2: a door and a mobile robot — direct workflow/state invocation.

A debugger-friendly, plain-Python equivalent of ``scenario2_door.ipynb``. It demonstrates
the *direct* interaction pattern, as opposed to the operation coordination of scenario 1. A
door resource exposes open/close workflows and a live status StateProperty, with **no
operation queue**. A minimal mobile robot discovers the door purely through the knowledge
graph, and it uses a SPARQL query. It reads the door's live status over the StateProperty GET
endpoint. When it finds the door closed, it invokes the open workflow of the door directly.
It uses the endpoint it found in the graph. This is not operation based.

Run from a debugger or as a script. The numbered functions are convenient breakpoints.
"""

from __future__ import annotations

import threading
import time

import httpx
import uvicorn
from kapps_triplestore_interface import GraphDB, IRI
from rdflib.namespace import RDF

from handlers import door_close, door_open, door_status, reset_door
from kapps_ogm import OGM
from kapps_semantic_middleware import Mode, SemanticMiddleware
from kapps_semantic_middleware.credentials import DEMO_REPOSITORY, graphdb_for
from kapps_semantic_middleware.registration import (
    mint_capability_iri,
    mint_state_property_iri,
    mint_workflow_iri,
    services_of_resource,
)
from kapps_semantic_middleware.vocabulary import SVC

import seed

DOOR_PORT = 8997
ROBOT_PORT = 8998
SERVER_START_TIMEOUT_SECONDS = 30
SERVER_STOP_TIMEOUT_SECONDS = 20


def _start_server(middleware: SemanticMiddleware, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    """Start a middleware server and wait until its startup registration completes.

    **The thread is what costs you the signal handlers, and it is why the caller must stop
    this server explicitly.** Uvicorn installs signal handlers only on the main thread
    (``uvicorn/server.py``: *"Signals can only be listened to from the main thread"*), so off
    it, SIGTERM and SIGINT never reach the ASGI lifespan, and the library's three
    ``on_shutdown`` callbacks -- deregistration among them (ADR 0007) -- never run. A daemon
    thread then dies with the process without unwinding, leaving an ``svc:address`` published
    for a process that is gone.

    A walkthrough script wants to keep doing things after the server is up, which is why the
    thread is here at all. The mitigation is ``_stop`` below: ``server.should_exit = True`` and
    join. Uvicorn runs its own lifespan shutdown off that flag with no signal involved, so the
    callbacks fire. **Copy this helper and you inherit both halves** -- if the copy never calls
    ``_stop``, it leaks a Service on every run (#65).

    ``demo/transferunits/`` takes the other route: one process per middleware with uvicorn on
    the main thread, which turns the callbacks on for free (ADR 0029).
    """
    config = uvicorn.Config(middleware.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    started_at = time.time()
    while not server.started and time.time() - started_at < SERVER_START_TIMEOUT_SECONDS:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("server did not start in time")
    return server, thread


def step_1_seed_clean_repository(db: GraphDB) -> None:
    """Clear the repository, load the Scenario 2 ontology, and create the door + robot."""
    print("\nStep 1 — Seed a Clean Repository")
    reset_door()
    seed.seed_scenario2(db)
    door_exists = db.triple_exists((seed.DOOR_RESOURCE, RDF.type, seed.DOOR_RESOURCE_CLASS))
    robot_exists = db.triple_exists(
        (seed.MOBILE_ROBOT, RDF.type, seed.MOBILE_ROBOT_RESOURCE_CLASS)
    )
    print(f"Door resource instantiated:  {door_exists}")
    print(f"Robot resource instantiated: {robot_exists}")


def step_2_start_door_middleware(db: GraphDB) -> tuple[SemanticMiddleware, uvicorn.Server, threading.Thread]:
    """Register the two door workflows + live status StateProperty, and start its server."""
    print("\nStep 2 — Start the Door Middleware")
    door = SemanticMiddleware(
        mode=Mode.RESOURCE,
        resource_iri=seed.DOOR_RESOURCE,
        service_class=seed.DOOR_SERVICE_CLASS,
        ogm=OGM(db=db),
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
    server, thread = _start_server(door, DOOR_PORT)
    print(f"Door middleware started on port {DOOR_PORT} (no operation queue — direct invocation)")
    print("Routes:", [r.path for r in door.app.routes if "workflows" in r.path or "state" in r.path])
    return door, server, thread


def step_3_inspect_registration(db: GraphDB, ogm: OGM) -> str:
    """Verify the door workflows, state property, and reachable endpoints are in the graph.

    The Service IRI is *discovered* through ``svc:isServiceOf`` rather than reconstructed from
    the resource IRI. It carries an instance discriminator now (ADR 0022), and one resource may
    carry several Services. The door runs a single instance, so exactly one is reachable.
    """
    print("\nStep 3 — Inspect What Registration Wrote")
    reachable = services_of_resource(ogm, seed.DOOR_RESOURCE, reachable_only=True)
    assert len(reachable) == 1, f"expected one reachable door service, found {len(reachable)}"
    service_iri = reachable[0]
    open_wf = mint_workflow_iri(service_iri, "door_open")
    status_sp = mint_state_property_iri(service_iri, "door_status")
    status_cap = mint_capability_iri(seed.DOOR_RESOURCE, "door_status")

    assert db.triple_exists((open_wf, SVC.isWorkflowOf, service_iri))
    assert db.triple_exists((status_sp, SVC.isStatePropertyOf, service_iri))
    assert db.triple_exists((status_cap, SVC.providedByStateProperty, status_sp))
    open_url = list(db.triples_get(sub=open_wf, pred=SVC.endpoint))
    status_url = list(db.triples_get(sub=status_sp, pred=SVC.endpoint))
    print(f"Open workflow endpoint:  {open_url[0][2] if open_url else 'NONE'}")
    print(f"Status state endpoint:   {status_url[0][2] if status_url else 'NONE'}")
    return service_iri


def _discover_door_endpoints(ogm, door_resource_iri) -> tuple[str, str]:
    """Discover the door live status GET endpoint and open-workflow execute URL via SPARQL."""
    sparql = f"""
    SELECT ?status_url ?open_url WHERE {{
        ?svc <{SVC.isServiceOf}> <{door_resource_iri}> .
        ?svc <{SVC.address}> ?addr .
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


def step_4_robot_passes_through_door(robot_ogm, door_resource_iri) -> None:
    """The mobile robot behavior: discover the door, ensure it is open, pass it.

    Deterministic, one step after another. The only forks are "is the door open?" and, if
    not, "open it". Discovery is via SPARQL. The live state and the workflow invocation are
    direct REST calls to the endpoints found in the graph.
    """
    print("\nStep 4 — The Mobile Robot Discovers and Passes the Door")
    status_url, open_url = _discover_door_endpoints(robot_ogm, door_resource_iri)
    print(f"Robot discovered (via SPARQL): status={status_url}")
    print(f"                               open  ={open_url}")

    def ensure_open(phase: str) -> None:
        state = httpx.get(status_url).json()
        print(f"  {phase}: door is {state}")
        if state == "closed":
            httpx.post(open_url)  # invoke the open workflow directly at its execute URL
            print(f"  {phase}: door was closed -> invoked the open workflow directly")
            state = httpx.get(status_url).json()
            print(f"  {phase}: door is now {state}")
        assert state == "opened", f"door failed to open ({phase})"

    ensure_open("approach")
    print("  drove through the door")
    # (Story: drop the load behind the door, then drive back. The door auto-closes after
    # 30 s, but the drop-off took less, so on return it is still open.)
    ensure_open("return")
    print("  drove back through the door")


def step_5_verify_state_not_persisted(db: GraphDB, service_iri: str) -> None:
    """Confirm the live status value is never written to the graph."""
    print("\nStep 5 — The Live Status is Never Persisted")
    status_sp = mint_state_property_iri(service_iri, "door_status")
    for _, _, obj in db.triples_get(sub=status_sp):
        assert str(obj) not in ("opened", "closed"), "state value must not be persisted"
    print("Confirmed: no 'opened'/'closed' literal is written onto the state property.")


def step_6_shutdown(
    db: GraphDB,
    service_iri: str,
    robot_service: IRI,
    servers: list[tuple[uvicorn.Server, threading.Thread]],
) -> None:
    """Stop both servers and verify reachability is removed while individuals remain.

    Both, because both are served (#44). A client that registers on startup must deregister
    on shutdown like any other peer, or the graph keeps an address nobody answers on.
    """
    print("\nStep 6 — Shutdown and Deregistration")
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)
    time.sleep(0.5)

    status_sp = mint_state_property_iri(service_iri, "door_status")
    endpoint_gone = len(list(db.triples_get(sub=status_sp, pred=SVC.endpoint))) == 0
    preserved = db.triple_exists((status_sp, RDF.type, seed.DOOR_STATUS_STATE_CLASS))
    robot_address_gone = len(list(db.triples_get(sub=robot_service, pred=SVC.address))) == 0

    print(f"  State property endpoint removed:    {endpoint_gone}")
    print(f"  State property individual preserved: {preserved}")
    print(f"  Robot address removed:               {robot_address_gone}")
    print(
        "  Robot Service individual preserved:  "
        f"{db.triple_exists((robot_service, SVC.isServiceOf, seed.MOBILE_ROBOT))}"
    )


def main() -> None:
    """Run the complete Scenario 2 lifecycle."""
    db = graphdb_for(DEMO_REPOSITORY)
    print(f"Connected to GraphDB repository: {db.repository}")

    step_1_seed_clean_repository(db)
    door_mw, server, thread = step_2_start_door_middleware(db)
    running: list[tuple[uvicorn.Server, threading.Thread]] = [(server, thread)]
    service_iri: str | None = None
    robot_service: IRI | None = None
    try:
        service_iri = step_3_inspect_registration(db, door_mw.ogm)
        # The mobile robot: a minimal second middleware. Its scripted logic discovers and
        # drives the door through the graph + REST, and it uses its own OGM for the SPARQL query.
        robot = SemanticMiddleware(
            mode=Mode.RESOURCE,
            resource_iri=seed.MOBILE_ROBOT,
            service_class=seed.MOBILE_ROBOT_SERVICE_CLASS,
            ogm=OGM(db=graphdb_for(DEMO_REPOSITORY)),
            host="127.0.0.1",
            port=ROBOT_PORT,
        )
        # The robot is *served*, not merely constructed (#44). Constructing a resource-mode
        # instance and never running it means `on_start_up` never fires: no Service, no
        # `svc:address`, no heartbeat. The robot would exist only to hold an `ogm`, making
        # `mode=Mode.RESOURCE` a label with no runtime consequence. A robot that drives a
        # door is a peer, and peers are discoverable — the door could ring it back.
        robot_server, robot_thread = _start_server(robot, ROBOT_PORT)
        running.append((robot_server, robot_thread))
        assert robot.service_iri is not None, "the robot is served, so it must have registered"
        dispatched_robot_service = robot.service_iri
        robot_service = dispatched_robot_service
        robot_address = list(db.triples_get(sub=robot_service, pred=SVC.address))
        print(f"\nMobile robot served on port {ROBOT_PORT}")
        print(f"  Service registered:   {bool(robot_address)}")
        print(f"  svc:address in graph: {robot_address[0][2] if robot_address else 'NONE'}")
        assert robot_address, "the robot is served, so it must be discoverable in the graph"

        step_4_robot_passes_through_door(robot.ogm, seed.DOOR_RESOURCE)
        step_5_verify_state_not_persisted(db, service_iri)
    finally:
        reset_door()
        if service_iri is not None and robot_service is not None:
            step_6_shutdown(db, service_iri, robot_service, running)
        else:
            for srv, thr in running:
                srv.should_exit = True
                thr.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)


if __name__ == "__main__":
    main()
