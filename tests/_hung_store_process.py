"""Serve a resource-mode instance whose store never answers, in this process, offline.

uvicorn runs on this process's main thread, so a SIGTERM takes the path a real stop takes.
``test_stop_sequence.py`` starts this file as a child process. The stand-in store answers the
deregistration's read, and every commit after startup hangs: the heartbeat's beat and the
deregistration's first commit. The process prints ``hanging`` once a beat hangs.

    python _hung_store_process.py <port>

Startup runs only the heartbeat's start: the rest of startup would read a real graph.
"""

from __future__ import annotations

import sys
import threading

import uvicorn

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.vocabulary import SVC

RESOURCE = "http://example.org/test#Module1"
SERVICE_CLASS = "http://example.org/test#ModuleService"


class _HungStore:
    """Answers reads with nothing, and never returns from a commit."""

    def __init__(self) -> None:
        self.db = self

    def triples_get(self, **kwargs):
        return []

    def commit(self, *, instance_iri, data, named_graph=None):
        if str(SVC.hasLastHeartbeat) in data:
            print("hanging", flush=True)
        threading.Event().wait()


def main() -> None:
    port = int(sys.argv[1])
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=RESOURCE,
        service_class=SERVICE_CLASS,
        ogm=_HungStore(),
        host="127.0.0.1",
        port=port,
    )
    mw.on_start_up_callbacks[:] = [mw._start_heartbeat]
    uvicorn.Server(
        uvicorn.Config(mw.app, host="127.0.0.1", port=port, log_level="warning")
    ).run()


if __name__ == "__main__":
    sys.exit(main())
