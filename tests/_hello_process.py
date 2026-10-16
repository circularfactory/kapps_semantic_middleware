"""Serve the scenario-1 hello instance in this process, as a supervised participant would.

uvicorn runs on this process's main thread, so it installs its own signal handlers and a SIGTERM
takes the path a real stop takes. ``test_activity_feed_stop.py`` starts this file as a child
process. The dataset must already hold the scenario-1 seed; this process does not seed.

    python _hello_process.py PORT DATASET [--startup-delay SECONDS] [--activity-feed]

A test builds that command line with :func:`command`, from the store of its ``store``
fixture, so the child joins the dataset the test reads.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn
from kapps_ogm import OGM
from kapps_triplestore_interface import TripleStore

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import triplestore_for

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402
from handlers import hello_world  # noqa: E402


def command(store: TripleStore, port: int, *, activity_feed: bool = False) -> list[str]:
    """The command line that starts this file as a child on the dataset of ``store``.

    It takes the test's fixture rather than a dataset name, so that the child joins the
    dataset the test reads, and a runner who points the fixture at another dataset
    moves the child with it. Only ``store.dataset`` is read.
    """
    argv = [sys.executable, str(Path(__file__).resolve()), str(port), store.dataset]
    if activity_feed:
        argv.append("--activity-feed")
    return argv


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read this process's command line, or ``argv`` in its place."""
    parser = argparse.ArgumentParser()
    parser.add_argument("port", type=int)
    parser.add_argument("dataset")
    parser.add_argument(
        "--startup-delay",
        type=float,
        default=0.0,
        help="Seconds that a last startup callback waits. It stands in for the registrations a "
        "real instance appends after construction, which make its startup take that long.",
    )
    parser.add_argument("--activity-feed", action="store_true")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()

    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=triplestore_for(args.dataset)),
        host="127.0.0.1",
        port=args.port,
        activity_feed=args.activity_feed,
    )
    mw.workflow(
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
    )(hello_world)

    if args.startup_delay > 0:

        async def slow_startup() -> None:
            await asyncio.sleep(args.startup_delay)

        mw.add_callback("on_start_up", slow_startup)

    uvicorn.Server(
        uvicorn.Config(mw.app, host="127.0.0.1", port=args.port, log_level="warning")
    ).run()


if __name__ == "__main__":
    main()
