"""Scenario 1: hello-world workflow through the knowledge graph.

A debugger-friendly, plain-Python equivalent of ``scenario1_hello_world.ipynb``. It
connects to the dedicated GraphDB repository configured through ``GRAPHDB_*`` environment
variables. It clears and seeds that repository. Then it drives the complete
**operation-coordination** lifecycle: registration, graph discovery, dispatch through the
event trigger, pull-and-run, provenance, and deregistration.

Run this file from a debugger or as a script. The numbered functions correspond to the
notebook steps and provide convenient debugger breakpoints.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import uvicorn
from kapps_triplestore_interface import GraphDB, IRI
from rdflib.namespace import RDF

from handlers import hello_world
from kapps_ogm import OGM
from kapps_semantic_middleware import Mode, SemanticMiddleware
from kapps_semantic_middleware.credentials import DEMO_REPOSITORY, graphdb_for
from kapps_semantic_middleware.registration import (
    mint_capability_iri,
    mint_workflow_iri,
    services_of_resource,
)
from kapps_semantic_middleware.vocabulary import CFC, OperationStatus, SVC

import seed

HELLO_PORT = 8993
PLANNER_PORT = 8994
SERVER_START_TIMEOUT_SECONDS = 30
SERVER_STOP_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class Registration:
    """Identifiers created deterministically during hello-world registration."""

    service_iri: IRI
    capability_iri: IRI
    workflow_iri: IRI


def step_1_seed_clean_repository(db: GraphDB) -> None:
    """Clear the dedicated repository, load Scenario 1 ontology, and create resources."""
    print("\nStep 1 — Seed a Clean Repository")
    seed.seed_scenario1(db)

    hello_exists = db.triple_exists((seed.HELLO_RESOURCE, RDF.type, seed.HELLO_RESOURCE_CLASS))
    planner_exists = db.triple_exists(
        (seed.PLANNER_RESOURCE, RDF.type, seed.PLANNER_RESOURCE_CLASS)
    )
    print(f"Hello resource instantiated:   {hello_exists}")
    print(f"Planner resource instantiated: {planner_exists}")


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


def step_2_start_hello_world_middleware(db: GraphDB) -> tuple[SemanticMiddleware, uvicorn.Server, threading.Thread]:
    """Register the hello-world workflow and start its HTTP server."""
    print("\nStep 2 — Start the Hello-World Middleware")
    middleware = SemanticMiddleware(
        mode=Mode.RESOURCE,
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=db),
        host="127.0.0.1",
        port=HELLO_PORT,
    )
    middleware.workflow(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
    )(hello_world)

    server, thread = _start_server(middleware, HELLO_PORT)
    print(f"Hello-world middleware started on port {HELLO_PORT} (registered on startup)")
    print(f"Swagger UI:  http://127.0.0.1:{HELLO_PORT}/docs")
    print("Registered routes:", [route.path for route in middleware.app.routes if "workflows" in route.path])
    return middleware, server, thread


def step_3_inspect_registration(db: GraphDB, ogm: OGM) -> Registration:
    """Verify the Service/Capability/Workflow and reachability triples.

    The Service IRI is *discovered* through ``svc:isServiceOf``, not rebuilt from the resource
    IRI. It carries an instance discriminator now, and one resource may carry several Services
    (ADR 0022). This scenario runs a single instance, so exactly one is reachable.
    """
    print("\nStep 3 — Inspect What Registration Wrote")
    reachable = services_of_resource(ogm, seed.HELLO_RESOURCE, reachable_only=True)
    assert len(reachable) == 1, f"expected one reachable hello service, found {len(reachable)}"
    service_iri = reachable[0]
    capability_iri = mint_capability_iri(seed.HELLO_RESOURCE, "hello_world")
    workflow_iri = mint_workflow_iri(service_iri, "hello_world")

    print(f"Service IRI:    {service_iri}")
    print(f"Capability IRI: {capability_iri}")
    print(f"Workflow IRI:   {workflow_iri}\n")

    assert db.triple_exists((service_iri, RDF.type, seed.HELLO_SERVICE_CLASS))
    assert db.triple_exists((service_iri, SVC.isServiceOf, seed.HELLO_RESOURCE))
    assert db.triple_exists((capability_iri, SVC.realizedByWorkflow, workflow_iri))
    assert db.triple_exists((workflow_iri, SVC.isWorkflowOf, service_iri))
    print("Structural triples present (isServiceOf, realizedByWorkflow, isWorkflowOf).")
    print("Note: only the instance-owned direction of each inverse is materialized (ADR 0008).")

    address_triples = list(db.triples_get(sub=service_iri, pred=SVC.address))
    endpoint_triples = list(db.triples_get(sub=workflow_iri, pred=SVC.endpoint))
    print(f"Service address advertised:   {address_triples[0][2] if address_triples else 'NONE'}")
    print(f"Workflow endpoint advertised: {endpoint_triples[0][2] if endpoint_triples else 'NONE'}")
    return Registration(service_iri, capability_iri, workflow_iri)


def step_4_dispatch_and_run(
    db: GraphDB, hello_mw: SemanticMiddleware, registration: Registration
) -> tuple[IRI, IRI, uvicorn.Server, threading.Thread]:
    """Dispatch an operation through the event trigger, then pull-and-run it.

    A second middleware (a planner) dispatches an operation for the hello capability. It
    creates the Operation ``queued`` in the graph. It then rings the event trigger of the
    hello resource, over REST. This resolves the peer purely through the graph. The hello resource
    then pulls the queued operation and runs the work (ADR 0009/0010).
    """
    print("\nStep 4 — Dispatch through the Event Trigger, then Pull-and-Run")
    planner = SemanticMiddleware(
        mode=Mode.RESOURCE,
        resource_iri=seed.PLANNER_RESOURCE,
        service_class=seed.PLANNER_SERVICE_CLASS,
        ogm=OGM(db=db),
        host="127.0.0.1",
        port=PLANNER_PORT,
    )
    # The planner is *served*, not merely constructed (#44). Constructing a resource-mode
    # instance and never running it means `on_start_up` never fires: no Service individual,
    # no `svc:address`, no heartbeat, no event-trigger route — so `mode="resource"` would be
    # a label with no runtime consequence, and a reader would reasonably copy that. A client
    # that dispatches work is a peer like any other, and peers are discoverable.
    planner_server, planner_thread = _start_server(planner, PLANNER_PORT)
    assert planner.service_iri is not None, "the planner is served, so it must have registered"
    planner_service = planner.service_iri
    planner_address = list(db.triples_get(sub=planner_service, pred=SVC.address))
    print(f"Planner served on port {PLANNER_PORT}")
    print(f"  Service registered:   {bool(planner_address)}")
    print(f"  svc:address in graph: {planner_address[0][2] if planner_address else 'NONE'}")
    assert planner_address, "the planner is served, so it must be discoverable in the graph"

    with planner.request(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        operation_class=str(CFC.Operation),
    ) as op:
        pass  # helloworld takes no arguments to populate
    op_iri = op.iri
    print(f"Planner dispatched operation: {op_iri}")
    print(f"  addressed via capability {registration.capability_iri}; queued on the hello resource")

    with hello_mw.claim_next() as claimed:
        claimed.result = hello_world()
    print(f"Hello resource pulled and ran the operation -> result: {claimed.result!r}")
    return op_iri, planner_service, planner_server, planner_thread


def step_5_inspect_decision_provenance(db: GraphDB, operation_iri: IRI) -> None:
    """Display the terminal status + execution provenance written to the Operation."""
    print("\nStep 5 — The Decision is Now Traceable in the Graph (R12)")
    status = list(db.triples_get(sub=operation_iri, pred=SVC.operationStatus))
    executed_by = list(db.triples_get(sub=operation_iri, pred=SVC.executedByWorkflow))
    timestamp = list(db.triples_get(sub=operation_iri, pred=SVC.executionTimestamp))
    result = list(db.triples_get(sub=operation_iri, pred=SVC.executionResult))

    print(f"Terminal state + provenance recorded on {operation_iri}:")
    print(f"  operationStatus:    {status[0][2] if status else 'NONE'}")
    print(f"  executedByWorkflow: {executed_by[0][2] if executed_by else 'NONE'}")
    print(f"  executionTimestamp: {timestamp[0][2] if timestamp else 'NONE'}")
    print(f"  executionResult:    {result[0][2] if result else 'NONE'}")
    assert status and str(status[0][2]) == OperationStatus.DONE


def _stop(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)


def step_6_shutdown_and_verify(
    db: GraphDB,
    registration: Registration,
    planner_service: IRI,
    servers: list[tuple[uvicorn.Server, threading.Thread]],
) -> None:
    """Stop both servers and verify reachability is removed while individuals remain.

    Both, because both are served (#44). A client that registers on startup must deregister
    on shutdown like any other peer, or the graph accumulates addresses nobody answers on.
    """
    print("\nStep 6 — Shutdown and Deregistration")
    for server, thread in servers:
        _stop(server, thread)
    time.sleep(0.5)

    address_after = list(db.triples_get(sub=registration.service_iri, pred=SVC.address))
    endpoint_after = list(db.triples_get(sub=registration.workflow_iri, pred=SVC.endpoint))
    planner_address_after = list(db.triples_get(sub=planner_service, pred=SVC.address))

    print("After shutdown:")
    print(f"  Service address removed:   {len(address_after) == 0}")
    print(f"  Workflow endpoint removed: {len(endpoint_after) == 0}")
    print(f"  Planner address removed:   {len(planner_address_after) == 0}")
    print(
        "  Workflow individual preserved (rdf:type): "
        f"{db.triple_exists((registration.workflow_iri, RDF.type, seed.HELLO_WORKFLOW_CLASS))}"
    )
    print(
        "  Planner Service individual preserved: "
        f"{db.triple_exists((planner_service, SVC.isServiceOf, seed.PLANNER_RESOURCE))}"
    )


def main() -> None:
    """Run the complete Scenario 1 lifecycle."""
    db = graphdb_for(DEMO_REPOSITORY)
    print(f"Connected to GraphDB repository: {db.repository}")

    step_1_seed_clean_repository(db)
    hello_mw, server, thread = step_2_start_hello_world_middleware(db)
    running: list[tuple[uvicorn.Server, threading.Thread]] = [(server, thread)]
    registration: Registration | None = None
    planner_service: IRI | None = None
    try:
        registration = step_3_inspect_registration(db, hello_mw.ogm)
        operation_iri, dispatched_planner_service, planner_server, planner_thread = step_4_dispatch_and_run(
            db, hello_mw, registration
        )
        planner_service = dispatched_planner_service
        running.append((planner_server, planner_thread))
        step_5_inspect_decision_provenance(db, operation_iri)
    finally:
        if registration is not None and planner_service is not None:
            step_6_shutdown_and_verify(db, registration, planner_service, running)
        else:
            for srv, thr in running:
                _stop(srv, thr)


if __name__ == "__main__":
    main()
