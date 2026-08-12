"""A middleware runner for a single TransferUnit.

It serves exactly one resource-mode SemanticMiddleware instance. Uvicorn runs on
the main thread, and owns the process event loop. This is load-bearing:
It enables signal handling. This enables the library's
on_shutdown deregistration fire (ADR 0029). The process reads GRAPHDB_* from
its environment and derives its resource IRI from the unit index.

It also fills the library's transport seam (ADR 0034): ``ensure_transport`` below is this
unit's own in-process MQTT broker, brought up on first MQTT connector registration and torn
down with nothing at all, because it lives on a daemon thread of this same process (ADR 0029
as amended -- "a unit's broker dies with its unit").
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import threading

from kapps_ogm import OGM
from kapps_ogm.utils.class_scope import ClassScope

from kapps_semantic_middleware import Mode, SemanticMiddleware
from kapps_semantic_middleware.credentials import DEMO_REPOSITORY, graphdb_for

from . import seed

BROKER_READY_TIMEOUT_SECONDS = 5.0


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


def _listening(host: str, port: int) -> bool:
    """Probe whether something already answers at host:port."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1.0)
            probe.connect((host, port))
            return True
    except OSError:
        return False


async def _serve_broker(host: str, port: int, ready: threading.Event) -> None:
    """Run one amqtt broker forever, on this thread's own event loop.

    Config shape matches ``tests/conftest.py``'s ``mqtt_broker`` fixture. ``ready`` is set the
    instant the broker is actually listening, so ``ensure_transport``'s caller -- about to
    build a connector for this same address -- never races it.
    """
    from amqtt.broker import Broker

    broker = Broker(
        {
            "listeners": {
                "default": {"type": "tcp", "bind": f"{host}:{port}", "max_connections": 100}
            },
            "sys_interval": 0,
            "auth": {"allow-anonymous": True},
            "topic-check": {"enabled": False},
        }
    )
    await broker.start()
    ready.set()
    await asyncio.Event().wait()


def ensure_transport(host: str, port: int) -> None:
    """The demo's transport hook (ADR 0034): bring up this unit's own MQTT broker.

    Called once, synchronously, from inside the ``SemanticMiddleware`` constructor -- before
    this process has an event loop of its own, which is why the broker gets a thread and a
    loop of its own rather than a spot on this one. The thread is daemon and never joined: it
    outlives this call and dies only when the process does, which is what makes "a unit's
    broker dies with its unit" true with no teardown code anywhere.

    Idempotent (ADR 0034): a probe first, so a broker already listening at this address --
    this unit's own from an earlier call, or anything else already there -- means this does
    nothing.
    """
    if _listening(host, port):
        return

    ready = threading.Event()
    threading.Thread(
        target=lambda: asyncio.run(_serve_broker(host, port, ready)), daemon=True
    ).start()
    ready.wait(timeout=BROKER_READY_TIMEOUT_SECONDS)


async def run_server(
    host: str, port: int, middleware: SemanticMiddleware, sock: socket.socket | None = None
) -> None:
    """Run the FastAPI server directly on this loop (uvicorn on the main thread)."""
    config = __import__("uvicorn").Config(
        app=middleware.app, host=host, port=port, log_level="warning"
    )
    server = __import__("uvicorn").Server(config=config)
    await server.serve(sockets=[sock] if sock is not None else None)


async def main() -> None:
    parser = argparse.ArgumentParser(description="A TransferUnit middleware runner")
    parser.add_argument(
        "--unit-index", type=int, default=1, help="The unit index (default: 1)"
    )
    parser.add_argument(
        "--resource-iri",
        type=str,
        default=None,
        help="The Resource IRI (default: derived from --unit-index)",
    )
    parser.add_argument("--host", type=str, default="127.0.0.1", help="The host to bind")
    parser.add_argument("--port", type=int, default=0, help="The port to bind (0 = free)")
    parser.add_argument(
        "--repository",
        type=str,
        default=DEMO_REPOSITORY,
        help=(
            f"The GraphDB repository to join (default: {DEMO_REPOSITORY}). A caller that "
            "spawns this process must pass the repository it is itself using, since the "
            "environment deliberately cannot name one (issue #146)."
        ),
    )
    args = parser.parse_args()

    if args.port == 0:
        sock = bind_free_socket(args.host)
        port = sock.getsockname()[1]
    else:
        sock, port = None, args.port

    resource_iri = (
        args.resource_iri
        if args.resource_iri is not None
        else str(seed._mint_transfer_unit_iri(args.unit_index))
    )

    db = graphdb_for(args.repository)
    ogm = OGM(db=db)
    class_scope = ClassScope.from_property_chains(
        [
            [seed.TU_HAS_CONVEYOR_BELT, seed.TU_HAS_CONVEYOR_SPEED],
            [seed.TU_HAS_LIGHT_BARRIER, seed.TU_IS_OCCUPIED],
        ]
    )

    middleware = SemanticMiddleware(
        mode=Mode.RESOURCE,
        resource_iri=resource_iri,
        service_class=str(seed.TRANSFER_UNIT_SERVICE_CLASS),
        ogm=ogm,
        host=args.host,
        port=port,
        class_scope=class_scope,
        autoregister_connectors=True,
        activity_feed=True,
        ensure_transport=ensure_transport,
    )

    # No broker flag here: the connector reads the broker address and port off the graph
    # at wiring time (ADR 0031), and ensure_transport above brings that broker up itself
    # (ADR 0034) -- this runner names no broker host or port anywhere in its own code.
    print(f"Middleware running on http://{args.host}:{port}/", flush=True)
    await run_server(args.host, port, middleware, sock)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
