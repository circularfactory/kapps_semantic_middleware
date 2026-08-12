"""The control station runner for the factory demo.

It serves the Controller (ticket #43) as a resource-mode middleware, with the station
board's own routes and template (ticket #82, ADR 0029's split) grafted onto the same
app. Uvicorn runs on the main thread, and owns the process event loop, the same way the
middleware runner does (ADR 0029). The process reads GRAPHDB_* from its environment.

This file is the *runner* half of the #82 split: it names no FastAPI route of its own
(a guard test, ``tests/test_station_board_guard.py``, holds that split the way
``tests/test_launcher_index_guard.py`` holds ``index.py``/``launcher.py``'s). Routes and
the template live in ``station_board.py``; this file only constructs the Controller, the
algorithm's shared runtime state, and grafts one onto the other before serving.

The view mechanism (ADR 0033, ticket #80) runs here: ``main()`` calls
``controller.view()`` with the algorithm's SPARQL query, then ``controller.wire_view()``
to recognize and register REST connectors for every hit. This must happen synchronously
before the server starts serving, because connector registration must precede the app's
lifespan connecting everything (see ``Controller.wire_view``'s docstring for why). Once
the app starts, each wired hit's northbound datamodel loads into ``controller.units``.
Every later re-run of the view -- the editable heuristic's "run"/"reset", and every
poll -- goes through ``Controller.rebuild_view`` instead (ticket #82), reached from
``station_board.py``'s routes, never from here again.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket

from kapps_ogm import OGM

from kapps_semantic_middleware.credentials import DEMO_REPOSITORY, graphdb_for

from . import algorithm, seed, station_board
from .controller import Controller

logger = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 8.0
"""The timed mode's default interval (#82's own acceptance criterion: it must exceed one
lap of PUT -> unit middleware -> MQTT -> PLC -> MQTT back -> connector read).

**Measured 2026-08-07**, once #94 made a lap completable at all. Before that fix the belt
froze short of its setpoint, so a lap never finished and this value shipped reasoned rather
than measured. ``tests/test_lap_measurement.py`` (marked ``lap``, deselected by default)
reproduces the table below against a real GraphDB, a real broker and a real middleware:

    ramp (m/s)   device settled   observed back
          0.05           0.06 s          0.02 s
          0.5            0.52 s          0.52 s
          1.5            1.54 s          1.52 s
          3.0            3.06 s          3.05 s

**A lap is ramp distance over ramp rate, plus about 20 ms.** Transport -- PUT, MQTT out,
PLC, MQTT back, connector read -- is the 0.02 s row and is negligible. Everything else is
#83's momentum (``transfer_unit.DEFAULT_RAMP_RATE``, 1 m/s per second), so the lap is a
function of *distance* and has no single value. That is why it had to be measured rather
than assumed, and why the criterion is "exceeds one lap" rather than "equals" it.

8 s clears the slowest measured lap by 2.6x, which buys headroom for a commanded change of
roughly 8 m/s. The ontology puts no bounds on ``tu:hasConveyorSpeed``
(``transferunit.ttl:88``), so no tick is correct for *every* conceivable command; this one
covers every move the demo's own seed and UI can produce. A deployment that commands larger
jumps, or lowers the ramp rate, should raise it.

The earlier reasoning from ``rest_binding.DEFAULT_POLL_INTERVAL_SECONDS`` (2 s) turned out
to be measuring the wrong path: that is the *controller's* REST poll of a peer, not the
unit's own MQTT round trip, and it does not appear in a lap at all."""


def bind_free_socket(host: str) -> socket.socket:
    """Bind and listen on an OS-assigned free port, without releasing it.

    Reading back a discovered port, closing the socket, and letting uvicorn bind a new
    one reopens the allocate-hand-off-bind race ADR 0029 credits self-allocation with
    removing -- another process could take the port in between. Handing uvicorn this same,
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
    config = __import__("uvicorn").Config(
        app=controller.app, host=host, port=port, log_level="warning"
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
        "--tick",
        type=float,
        default=DEFAULT_TICK_SECONDS,
        help=(
            "Timed mode's interval in seconds (default: %(default)s). Must exceed one "
            "lap of the loop (PUT -> unit middleware -> MQTT -> PLC -> MQTT back -> "
            "connector read), or the board oscillates (#82)."
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

    db = graphdb_for(DEMO_REPOSITORY)
    ogm = OGM(db=db)

    controller = Controller(
        resource_iri=resource_iri,
        service_class=str(seed.CONTROL_STATION_SERVICE_CLASS),
        ogm=ogm,
        host=args.host,
        port=port,
        activity_feed=True,
    )

    # ADR 0033 steps 1-4: run the view, then wire a driving REST connector for every
    # hit. Synchronous, and before run_server -- connector registration must precede
    # the app's lifespan connecting everything (Controller.wire_view's own docstring).
    default_query = algorithm.build_view_query()
    hits = controller.view(default_query)
    logger.info("The view found %d live, even-indexed unit(s)", len(hits))
    controller.wire_view(hits, class_scope=algorithm.unit_class_scope())

    state = algorithm.AlgorithmState(tick_seconds=args.tick)
    _wire_algorithm(controller, state)

    # Graft the board's own routes and template onto this same app (#82, ADR 0029's
    # split): the board reads controller.units and calls controller.push()/rebuild_view()
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
