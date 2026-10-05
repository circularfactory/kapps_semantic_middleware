"""Serve the scenario-1 hello instance in this process, as a supervised participant would.

uvicorn runs on this process's main thread, so it installs its own signal handlers and a SIGTERM
takes the path a real stop takes. ``test_reachability_lifecycle.py`` starts this file as a child
process. The repository must already hold the scenario-1 seed; this process does not seed.

    python _hello_process.py PORT REPOSITORY [--startup-delay SECONDS] [--activity-feed]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn
from kapps_ogm import OGM

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import graphdb_for

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402
from handlers import hello_world  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", type=int)
    parser.add_argument("repository")
    parser.add_argument(
        "--startup-delay",
        type=float,
        default=0.0,
        help="Seconds that a last startup callback waits. It stands in for the registrations a "
        "real instance appends after construction, which make its startup take that long.",
    )
    parser.add_argument("--activity-feed", action="store_true")
    args = parser.parse_args()

    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(args.repository)),
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
