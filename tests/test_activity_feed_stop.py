"""A stop deregisters an instance even while a client holds its activity feed open.

uvicorn stops in three steps. It closes its listeners, then it waits for every open connection to
finish, and only then it runs the lifespan shutdown, where the instance removes its address. The
wait has no limit by default. A response that never completes therefore holds the stop open before
deregistration, and the process that supervises the instance kills it. A kill skips
deregistration, so the address stays in the graph.

``/activity/stream`` was such a response. It now ends after a short lifetime, and a client
reconnects. This file stops a real instance, in a child process with uvicorn on its main thread,
while a client that reconnects like a browser's ``EventSource`` holds the stream.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from kapps_semantic_middleware.registration import mint_service_iri
from kapps_semantic_middleware.vocabulary import SVC

from _sse import hold
from conftest import TEST_REPOSITORY, requires_graphdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

SERVED_PORT = 8983
ADDRESS = f"http://127.0.0.1:{SERVED_PORT}"
HELLO_PROCESS = Path(__file__).resolve().parent / "_hello_process.py"

STOP_BOUND_SECONDS = 5.0
"""The demo launcher's stop timeout. A supervisor kills an instance that takes longer."""

posix_signals = pytest.mark.skipif(
    sys.platform == "win32", reason="sends SIGTERM, which Windows cannot deliver to a child"
)


def _address(db, service_iri) -> list[str]:
    return [str(triple[2]) for triple in db.triples_get(sub=service_iri, pred=SVC.address)]


def _await_serving(proc: subprocess.Popen, timeout: float = 60.0) -> None:
    """Return once the instance answers HTTP.

    The address appears in the graph before startup is complete, and the listener accepts only
    after it, so the first attempts are refused.
    """
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None:
            pytest.fail(f"the instance exited before it served:\n{proc.stderr.read()}")
        try:
            httpx.get(f"{ADDRESS}/activity", timeout=5.0).raise_for_status()
            return
        except httpx.TransportError:
            if time.monotonic() > deadline:
                pytest.fail(f"the instance did not serve within {timeout} s")
            time.sleep(0.1)


@requires_graphdb
@posix_signals
def test_a_stop_deregisters_while_a_client_holds_the_activity_stream(graphdb):
    seed.seed_scenario1(graphdb)
    service_iri = mint_service_iri(seed.HELLO_RESOURCE, ADDRESS)

    proc = subprocess.Popen(
        [
            sys.executable,
            str(HELLO_PROCESS),
            str(SERVED_PORT),
            TEST_REPOSITORY,
            "--activity-feed",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    # A client that stopped reconnecting would let go of the stream by itself, and the test
    # would then show nothing about a stop while a page is open. So it follows, as a browser
    # does, and keeps retrying once the listener is closed.
    connected = threading.Event()
    stop_holding = threading.Event()
    holder: threading.Thread | None = None
    try:
        _await_serving(proc)
        holder = hold(f"{ADDRESS}/activity/stream", until=stop_holding.is_set, opened=connected)
        assert connected.wait(timeout=10.0), "the activity stream never opened"
        assert _address(graphdb, service_iri) == [ADDRESS]

        proc.send_signal(signal.SIGTERM)
        stopped_at = time.monotonic()
        try:
            proc.wait(timeout=STOP_BOUND_SECONDS)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"the instance did not stop within {STOP_BOUND_SECONDS} s while its activity "
                "stream was held open, so a supervisor would kill it before it deregisters"
            )
        took = time.monotonic() - stopped_at
    finally:
        stop_holding.set()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if holder is not None:
            holder.join(timeout=15)

    # No assertion on the exit code: after a graceful shutdown, uvicorn raises the signal it
    # captured again, so a clean stop by SIGTERM exits with -15 as a kill by SIGTERM would.
    # The address is what tells the two apart.
    assert _address(graphdb, service_iri) == [], (
        f"the instance stopped in {took:.1f} s but left its address in the graph:\n"
        f"{proc.stderr.read()}"
    )
