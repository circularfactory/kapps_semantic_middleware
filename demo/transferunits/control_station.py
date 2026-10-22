"""The control station runner for the factory demo.

It serves the Controller as a resource-mode middleware, with the station
board's own routes and template grafted onto the same
app. Uvicorn runs on the main thread, and owns the process event loop, the same way the
middleware runner does. The process reads TRIPLESTORE_* from its environment.

This file is the *runner* half of the station-board split: it names no FastAPI route of its own
(a guard test, ``tests/test_station_board_guard.py``, holds that split the way
``tests/test_launcher_index_guard.py`` holds ``index.py``/``launcher.py``'s). Routes and
the template live in ``station_board.py``; this file only constructs the Controller, the
algorithm's shared runtime state, and grafts one onto the other before serving.

The view mechanism runs here: ``main()`` calls
``controller.view()`` with the algorithm's SPARQL query, then ``controller.wire_view()``
to recognize and register REST read connectors for every hit. This must happen synchronously
before the server starts serving, because connector registration must precede the app's
lifespan connecting everything (see ``Controller.wire_view``'s docstring for why). Once
the app starts, each wired hit's northbound datamodel loads into ``controller.units``.
Every later re-run of the view -- the editable heuristic's "run"/"reset", and every
poll -- goes through ``Controller.rebuild_view`` instead, reached from
``station_board.py``'s routes, never from here again.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket

from kapps_ogm import OGM

from kapps_semantic_middleware.credentials import DEMO_DATASET, triplestore_for

from . import algorithm, seed, station_board
from .controller import Controller
from .launcher import GRACEFUL_SHUTDOWN_SECONDS

logger = logging.getLogger(__name__)

def bind_free_socket(host: str) -> socket.socket:
    """Bind and listen on an OS-assigned free port, without releasing it.

    Reading back a discovered port, closing the socket, and letting uvicorn bind a new
    one reopens the allocate-hand-off-bind race self-allocation exists to
    remove -- another process could take the port in between. Handing uvicorn this same,
    still-listening socket instead of a bare port number closes that gap.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(1)
    return sock


async def run_server(
    host: str, port: int, controller: Controller, sock: socket.socket | None = None
) -> None:
    """Run the FastAPI server on this loop (uvicorn on the main thread)."""
    # Bounded below the launcher's stop timeout, so that a connection which stays open cannot
    # hold the stop open past the kill and cost the deregistration.
    config = __import__("uvicorn").Config(
        app=controller.app,
        host=host,
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = __import__("uvicorn").Server(config=config)
    await server.serve(sockets=[sock] if sock is not None else None)


def _wire_algorithm(controller: Controller, state: algorithm.AlgorithmState) -> None:
    """Register the demonstration algorithm's start-up/shutdown callback pair.

    Mirrors ``SemanticMiddleware``'s own heartbeat convention
    (``middleware.py``'s ``_start_heartbeat``/``_stop_heartbeat``): a background task
    created on ``on_start_up``, cancelled and awaited cleanly on ``on_shutdown``. The
    one-element list is the closure's box for the task object -- ``_start_algorithm``
    needs to *set* it, and a bare ``nonlocal`` has nothing to rebind across the two
    separately-registered callbacks otherwise.
    """
    algorithm_task: list[asyncio.Task] = []

    async def _start_algorithm() -> None:
        algorithm_task.append(
            asyncio.create_task(algorithm.run_algorithm_loop(controller, state))
        )

    async def _stop_algorithm() -> None:
        if algorithm_task:
            algorithm_task[0].cancel()
            try:
                await algorithm_task[0]
            except asyncio.CancelledError:
                pass

    controller.add_callback("on_start_up", _start_algorithm)
    controller.add_callback("on_shutdown", _stop_algorithm)


async def main() -> None:
    parser = argparse.ArgumentParser(description="The control station runner")
    parser.add_argument(
        "--resource-iri",
        type=str,
        default=None,
        help="Resource IRI (default: the seeded control station)",
    )
    parser.add_argument("--host", type=str, default="127.0.0.1", help="The host to bind")
    parser.add_argument("--port", type=int, default=0, help="The port to bind (0 = free)")
    parser.add_argument(
        "--dataset",
        type=str,
        default=DEMO_DATASET,
        help=(
            f"The dataset of the store to join (default: {DEMO_DATASET}). A caller that "
            "spawns this process must pass the dataset it is itself using, since the "
            "environment deliberately cannot name one (see the Provisioning and Seeding guide)."
        ),
    )
    args = parser.parse_args()

    if args.port == 0:
        sock = bind_free_socket(args.host)
        port = sock.getsockname()[1]
    else:
        sock, port = None, args.port

    resource_iri = (
        args.resource_iri if args.resource_iri is not None else str(seed.CONTROL_STATION)
    )

    db = triplestore_for(args.dataset)
    ogm = OGM(db=db)

    controller = Controller(
        resource_iri=resource_iri,
        service_class=str(seed.CONTROL_STATION_SERVICE_CLASS),
        ogm=ogm,
        host=args.host,
        port=port,
        activity_feed=True,
    )

    # The Control Expert's steps 1-4: run the view, then wire a reading REST connector for
    # every hit. Synchronous, and before run_server -- connector registration must precede
    # the app's lifespan connecting everything (Controller.wire_view's own docstring).
    default_query = algorithm.build_view_query()
    hits = controller.view(default_query)
    logger.info("The view found %d live unit(s)", len(hits))
    controller.wire_view(hits, class_scope=algorithm.unit_class_scope())

    state = algorithm.AlgorithmState()
    _wire_algorithm(controller, state)

    # Graft the board's own routes and template onto this same app: the
    # board reads controller.units and calls controller.rebuild_view()
    # in-process, so it must share this app and this event loop rather than run as a
    # second server the way the PLC's panel does for its own, unrelated, device object.
    # station_board.py's own root route replaces the one SemanticMiddleware.app already
    # installed, the same way that property replaced the base framework's -- Starlette
    # matches in order, so the original is removed rather than shadowed (see
    # station_board.mount_onto's own docstring).
    station_board.mount_onto(
        controller.app,
        controller=controller,
        algorithm_state=state,
        default_query=default_query,
    )

    print(f"The control station runs on http://{args.host}:{port}/", flush=True)
    await run_server(args.host, port, controller, sock)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
