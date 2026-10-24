"""A clean stop of a resource-mode instance leaves it gone from the graph, offline.

The framework runs the stop steps one after the other, with no guard around a step. So a step that
raises skips every later one, and the process still exits. The stop therefore stops the heartbeat
first, then deregisters, then stops the issuers' notifications, and none of the three raises. The
deregistration is tried again on a store error for a short window. A store call that hangs is
abandoned at the end of its window, in a daemon thread that does not hold the process's exit.

The tests run the registered stop steps in order, the way the framework runs them, over a stand-in
store. They check what an observer of the graph and the log sees: whether the address and the
endpoints are gone, whether the heartbeat loop is done, whether the notifications' stop ran, the
log records, and how long the stop took.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import TripleStoreUnavailableError

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware import middleware as middleware_module
from kapps_semantic_middleware.vocabulary import SVC

RESOURCE = "http://example.org/test#Module1"
SERVICE_CLASS = "http://example.org/test#ModuleService"
WORKFLOW = IRI("http://example.org/test#Workflow1")

WINDOW = 0.3
PAUSE = 0.05
SLACK = 0.5
NOTIFICATIONS_BOUND = 1.0

ADDRESS = str(SVC.hasAddress)
ENDPOINT = str(SVC.hasEndpoint)
HEARTBEAT = str(SVC.hasLastHeartbeat)

STOP_BOUND_SECONDS = 5.0
"""A supervisor's stop budget: a 10 s stop timeout, minus 5 s of graceful shutdown."""


def _reset() -> TripleStoreUnavailableError:
    return TripleStoreUnavailableError(
        "No answer from the store: connection reset by peer"
    )


class _Hang:
    """A commit outcome: the call does not return until the test ends."""


HANG = _Hang()


class _Store:
    """A stand-in store over one Service with an address and one Workflow with an endpoint.

    ``deregistrations`` holds the outcome of each deregistration attempt, in order: an error to
    raise, ``HANG``, or ``None`` to succeed; past its end every attempt succeeds. An attempt's
    outcome applies to its first commit, the endpoint's. ``beats`` does the same for heartbeat
    commits. A hung call waits on an event that the test sets at its end, so no thread outlives it.
    """

    def __init__(self, *, deregistrations=(), beats=()) -> None:
        self.deregistrations = list(deregistrations)
        self.beats = list(beats)
        self.address = True
        self.endpoint = True
        self.attempts = 0
        self.heartbeat_done_at_first_attempt: bool | None = None
        self.heartbeat_task = None
        self.beat_started = threading.Event()
        self.release = threading.Event()
        self.db = self

    # The read that deregister_service makes, through ogm.db.
    def triples_get(self, *, pred, obj, named_graph=None):
        if pred == SVC.isWorkflowOf:
            return [(WORKFLOW, pred, obj)]
        return []

    def _outcome(self, outcomes: list):
        outcome = outcomes.pop(0) if outcomes else None
        if outcome is HANG:
            self.release.wait()
            raise RuntimeError("released at the end of the test")
        if outcome is not None:
            raise outcome

    def commit(self, *, instance_iri, data, named_graph=None):
        if HEARTBEAT in data:
            self.beat_started.set()
            self._outcome(self.beats)
            return
        if ENDPOINT in data:
            self.attempts += 1
            if self.heartbeat_done_at_first_attempt is None:
                task = self.heartbeat_task
                self.heartbeat_done_at_first_attempt = task is None or task.done()
            self._outcome(self.deregistrations)
            self.endpoint = False
            return
        if ADDRESS in data:
            self.address = False


def _resource(store: _Store) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=RESOURCE,
        service_class=SERVICE_CLASS,
        ogm=store,
        heartbeat_interval=30.0,
    )


@pytest.fixture
def fast(monkeypatch):
    """A short deregistration window and pause, and a heartbeat that waits on nothing real."""
    monkeypatch.setattr(middleware_module, "_DEREGISTRATION_WINDOW", WINDOW)
    monkeypatch.setattr(middleware_module, "_DEREGISTRATION_PAUSE", PAUSE)
    monkeypatch.setattr(middleware_module, "_heartbeat_phase", lambda low, high: 0.0)
    monkeypatch.setattr(
        middleware_module, "_heartbeat_wait", lambda seconds: asyncio.sleep(3600)
    )


class _Run:
    """What one stop showed: the order of its steps and how long each took."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.took: dict[str, float] = {}
        self.notifications_stopped = False


async def _stop(mw: SemanticMiddleware, store: _Store) -> _Run:
    """Start the heartbeat, then run the registered stop steps as the framework runs them."""
    run = _Run()
    original = mw._notifier.stop

    def stop_notifier(timeout):
        run.notifications_stopped = True
        return original(timeout)

    mw._notifier.stop = stop_notifier
    await mw._start_heartbeat()
    store.heartbeat_task = mw._heartbeat_task
    # The startup beat runs in the loop's first turn.
    for _ in range(100):
        if store.beat_started.is_set():
            break
        await asyncio.sleep(0.01)
    for callback in mw.on_shutdown_callbacks:
        # The framework holds each step as a partial of the bound method.
        name = callback.func.__name__
        started = time.monotonic()
        await callback()
        run.steps.append(name)
        run.took[name] = time.monotonic() - started
    return run


def _stopped(mw: SemanticMiddleware, store: _Store) -> _Run:
    """Stop ``mw``, and release every call of ``store`` that still hangs."""
    try:
        return asyncio.run(asyncio.wait_for(_stop(mw, store), timeout=20))
    finally:
        store.release.set()


def _errors_naming(caplog, service) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR
        and r.name == middleware_module.__name__
        and str(service) in r.getMessage()
    ]


# --------------------------------------------------------------------------- #
# Order.
# --------------------------------------------------------------------------- #


def test_the_heartbeat_stops_before_the_deregistration_and_the_notifications_after(
    fast,
):
    store = _Store()
    run = _stopped(_resource(store), store)

    assert run.steps == [
        "_stop_heartbeat",
        "_deregister_service",
        "_stop_notifications",
    ]
    # The steps ran in that order, not only were registered in it.
    assert store.heartbeat_done_at_first_attempt is True
    assert run.notifications_stopped
    assert not store.address and not store.endpoint


# --------------------------------------------------------------------------- #
# The deregistration's retry.
# --------------------------------------------------------------------------- #


def test_a_deregistration_that_fails_once_on_a_store_error_is_retried(fast, caplog):
    store = _Store(deregistrations=[_reset()])
    mw = _resource(store)
    with caplog.at_level(logging.WARNING, logger=middleware_module.__name__):
        run = _stopped(mw, store)

    assert not store.address and not store.endpoint
    assert store.heartbeat_task.done()
    assert run.notifications_stopped
    assert not _errors_naming(caplog, mw.service_iri)
    [warning] = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "connection reset" in r.getMessage()
    ]
    assert str(mw.service_iri) in warning.getMessage()


def test_a_deregistration_that_fails_in_every_attempt_logs_one_error_and_the_stop_goes_on(
    fast, caplog
):
    store = _Store(deregistrations=[_reset()] * 1000)
    mw = _resource(store)
    with caplog.at_level(logging.WARNING, logger=middleware_module.__name__):
        run = _stopped(mw, store)

    assert store.address, "no attempt succeeded, so the address stays"
    assert store.attempts > 1, "a store error is retried within the window"
    assert len(_errors_naming(caplog, mw.service_iri)) == 1
    assert run.notifications_stopped
    assert run.took["_deregister_service"] < WINDOW + PAUSE + SLACK
    assert sum(run.took.values()) < WINDOW + PAUSE + SLACK + NOTIFICATIONS_BOUND


def test_a_deregistration_attempt_that_hangs_is_abandoned_at_the_end_of_the_window(
    fast, caplog
):
    store = _Store(deregistrations=[HANG])
    mw = _resource(store)
    with caplog.at_level(logging.WARNING, logger=middleware_module.__name__):
        run = _stopped(mw, store)

    assert store.address
    assert len(_errors_naming(caplog, mw.service_iri)) == 1
    assert run.steps[-1] == "_stop_notifications" and run.notifications_stopped
    assert run.took["_deregister_service"] < WINDOW + PAUSE + SLACK


@pytest.mark.parametrize(
    "error",
    [TypeError("a bug"), middleware_module.DeploymentValueError("a blank node")],
    ids=["programming-error", "blank-node-refusal"],
)
def test_an_error_outside_the_store_errors_is_not_retried_and_the_stop_goes_on(
    fast, caplog, error
):
    store = _Store(deregistrations=[error])
    mw = _resource(store)
    with caplog.at_level(logging.WARNING, logger=middleware_module.__name__):
        run = _stopped(mw, store)

    assert store.attempts == 1
    assert store.address
    [record] = _errors_naming(caplog, mw.service_iri)
    assert record.exc_info is not None and record.exc_info[1] is error
    assert run.notifications_stopped


def test_the_public_deregistration_keeps_its_single_attempt_and_its_error():
    store = _Store(deregistrations=[_reset(), None])

    with pytest.raises(TripleStoreUnavailableError):
        middleware_module.deregister_service(store, IRI(RESOURCE + "_service"))

    assert store.attempts == 1 and store.address


# --------------------------------------------------------------------------- #
# The heartbeat's stop goes first, so it must not hold the stop.
# --------------------------------------------------------------------------- #


def test_a_heartbeat_loop_that_ended_with_an_error_does_not_keep_the_stop_from_deregistering(
    fast,
):
    store = _Store(beats=[TypeError("a bug")])
    run = _stopped(_resource(store), store)

    assert store.heartbeat_task.done()
    assert run.steps == [
        "_stop_heartbeat",
        "_deregister_service",
        "_stop_notifications",
    ]
    assert not store.address and not store.endpoint
    assert run.notifications_stopped


def test_a_beat_that_hangs_on_the_store_does_not_hold_the_heartbeats_stop(fast):
    store = _Store(beats=[HANG])
    run = _stopped(_resource(store), store)

    assert store.heartbeat_task.done()
    assert run.took["_stop_heartbeat"] < SLACK
    assert not store.address and not store.endpoint


def test_the_beats_share_one_daemon_thread_that_ends_at_the_stop(fast, monkeypatch):
    # A store client keeps one session per thread that ever called it, so a thread per beat
    # would grow that list by one session every interval for the process's life.
    monkeypatch.setattr(middleware_module, "_heartbeat_wait", lambda seconds: asyncio.sleep(0))
    threads: list[threading.Thread] = []

    class _Counting(_Store):
        def commit(self, *, instance_iri, data, named_graph=None):
            if HEARTBEAT in data:
                threads.append(threading.current_thread())
            super().commit(instance_iri=instance_iri, data=data, named_graph=named_graph)

    store = _Counting()
    mw = _resource(store)

    async def beat_then_stop() -> None:
        await mw._start_heartbeat()
        for _ in range(500):
            if len(threads) >= 4:
                break
            await asyncio.sleep(0.01)
        await mw._stop_heartbeat()

    asyncio.run(asyncio.wait_for(beat_then_stop(), timeout=10))

    assert len(threads) >= 4
    assert len(set(threads)) == 1
    (worker,) = set(threads)
    assert worker.daemon
    worker.join(timeout=2)
    assert not worker.is_alive(), "the idle beat thread outlived the heartbeat's stop"


# --------------------------------------------------------------------------- #
# A hung store call does not hold the process's exit.
# --------------------------------------------------------------------------- #

posix_signals = pytest.mark.skipif(
    sys.platform == "win32",
    reason="sends SIGTERM, which Windows cannot deliver to a child",
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until_it_answers(port: int, proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """Wait for any HTTP answer: uvicorn then serves, and its startup is over.

    A SIGTERM during uvicorn's startup skips the lifespan's shutdown, so the stop under test would
    not run. Any status counts as an answer; only a refused connection means not up yet.
    """
    # No proxy: a proxy's own error answer would pass for the child's.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"the child ended before it served:\n{proc.stderr.read()}")
        try:
            opener.open(f"http://127.0.0.1:{port}/", timeout=0.2).close()
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            time.sleep(0.05)
    pytest.fail("the child did not serve within 10 s")


@posix_signals
def test_a_process_whose_store_hangs_exits_within_the_stop_budget():
    child = Path(__file__).resolve().parent / "_hung_store_process.py"
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(child), str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # The child prints one line once its beat hangs on the store.
        line = proc.stdout.readline()
        assert line.strip() == "hanging", (
            f"the child did not start:\n{proc.stderr.read()}"
        )
        _wait_until_it_answers(port, proc)
        proc.send_signal(signal.SIGTERM)
        stopped_at = time.monotonic()
        try:
            proc.wait(timeout=STOP_BOUND_SECONDS + 5)
        except subprocess.TimeoutExpired:
            pytest.fail("the child did not exit: a hung store call holds the exit")
        took = time.monotonic() - stopped_at
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    stderr = proc.stderr.read()
    assert took < STOP_BOUND_SECONDS, f"the child took {took:.1f} s to exit:\n{stderr}"
    # The ERROR that the stop logs when it abandons the hung deregistration.
    assert "did not finish within 3 s and is abandoned" in stderr, stderr
