"""The watchdog runner for the factory demo.

It serves one watchdog-mode SemanticMiddleware instance against the demo dataset. The instance
registers no Service and holds no Resource, so it has no address in the graph. It sweeps the
graph every ``--sweep-interval`` seconds: a Service whose heartbeat is older than the
middleware's default staleness threshold loses its address, and the Operations of its Resource
that no live Service will finish are marked ``failed``. Uvicorn runs on the main thread, as in
the other runners, so the sweep task starts with the app and stops with it.

The process reads TRIPLESTORE_* from its environment and names its dataset on the command line,
like every graph-side child.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect

from kapps_ogm import OGM

from kapps_semantic_middleware import Mode, SemanticMiddleware
from kapps_semantic_middleware.credentials import DEMO_DATASET, triplestore_for

from .launcher import GRACEFUL_SHUTDOWN_SECONDS, WATCHDOG_READY_MARKER, WATCHDOG_SWEEP_SECONDS


def _default_staleness_threshold() -> float:
    """The middleware's default staleness threshold, read off its constructor, never copied."""
    default = inspect.signature(SemanticMiddleware.__init__).parameters["staleness_threshold"].default
    return float(default)


async def run_server(host: str, port: int, middleware: SemanticMiddleware) -> None:
    """Run the FastAPI server directly on this loop (uvicorn on the main thread)."""
    config = __import__("uvicorn").Config(
        app=middleware.app,
        host=host,
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    await __import__("uvicorn").Server(config=config).serve()


async def main() -> None:
    parser = argparse.ArgumentParser(description="The factory demo's watchdog runner")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="The host to bind")
    parser.add_argument("--port", type=int, default=0, help="The port to bind (0 = free)")
    parser.add_argument(
        "--dataset",
        type=str,
        default=DEMO_DATASET,
        help=f"The dataset of the store to sweep (default: {DEMO_DATASET}).",
    )
    parser.add_argument(
        "--sweep-interval",
        type=float,
        default=WATCHDOG_SWEEP_SECONDS,
        help="Seconds between sweeps (default: %(default)s)",
    )
    args = parser.parse_args()

    ogm = OGM(db=triplestore_for(args.dataset))
    middleware = SemanticMiddleware(
        mode=Mode.WATCHDOG,
        ogm=ogm,
        host=args.host,
        port=args.port,
        sweep_interval=args.sweep_interval,
    )

    # No address line: the watchdog registers nothing, so there is nothing to forward. The
    # launcher reads this line as the sign that the process is live.
    print(
        f"{WATCHDOG_READY_MARKER}: sweeping every {args.sweep_interval:g} s, staleness "
        f"threshold {_default_staleness_threshold():g} s.",
        flush=True,
    )
    await run_server(args.host, args.port, middleware)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
