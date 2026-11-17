"""A clean stop closes the OGM's store client, as the last step, offline.

The interface's ``close()`` removes the write receipts that the client still holds. So the stop
calls it once, after every write of the stop, and a close that raises never fails the stop. The
tests run the registered stop steps in order, the way the framework runs them, over a stand-in
store that records its calls.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from fastapi.testclient import TestClient
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware import middleware as middleware_module
from kapps_semantic_middleware.vocabulary import SVC

RESOURCE = "http://example.org/test#Module1"
SERVICE_CLASS = "http://example.org/test#ModuleService"
WORKFLOW = IRI("http://example.org/test#Workflow1")
ADDRESS = str(SVC.hasAddress)
ENDPOINT = str(SVC.hasEndpoint)


class _Store:
    """A stand-in store that records its writes and closes, in order.

    It refuses no request after a close, as the interface's client does.
    """

    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.close_error = close_error
        self.db = self

    def triples_get(self, *, pred, obj, named_graph=None):
        if pred == SVC.isWorkflowOf:
            return [(WORKFLOW, pred, obj)]
        return []

    def query(self, sparql, convert_bindings=False):
        # The watchdog's start-up sweep asks for stale services; this store has none.
        return {"results": {"bindings": []}}

    def commit(self, *, instance_iri, data, named_graph=None):
        if ENDPOINT in data:
            self.calls.append("deregister-endpoints")
        elif ADDRESS in data:
            self.calls.append("deregister-address")

    def close(self) -> None:
        self.calls.append("close")
        if self.close_error is not None:
            raise self.close_error


def _resource(store: _Store) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=RESOURCE,
        service_class=SERVICE_CLASS,
        ogm=store,
    )


async def _stop(mw: SemanticMiddleware) -> list[str]:
    ran = []
    for callback in mw.on_shutdown_callbacks:
        await callback()
        ran.append(callback.func.__name__)
    return ran


def _run_stop(mw: SemanticMiddleware) -> list[str]:
    return asyncio.run(asyncio.wait_for(_stop(mw), timeout=20))


def test_resource_mode_closes_once_after_the_deregistration_and_the_notifier():
    store = _Store()
    mw = _resource(store)
    notifier_stopped_at = []
    original = mw._notifier.stop

    def stop_notifier(timeout):
        notifier_stopped_at.append(list(store.calls))
        return original(timeout)

    mw._notifier.stop = stop_notifier

    _run_stop(mw)

    assert store.calls == ["deregister-endpoints", "deregister-address", "close"]
    assert notifier_stopped_at == [["deregister-endpoints", "deregister-address"]]


def test_watchdog_mode_closes_once_after_the_sweep_stops():
    store = _Store()
    mw = SemanticMiddleware(mode="watchdog", ogm=store)

    ran = _run_stop(mw)

    assert store.calls == ["close"]
    assert ran.index("_stop_sweep") < ran.index("_close_store_client")


def test_a_close_that_raises_is_one_warning_and_the_stop_completes(caplog):
    store = _Store(close_error=RuntimeError("receipt request refused"))
    mw = _resource(store)

    with caplog.at_level(logging.WARNING):
        ran = _run_stop(mw)

    assert ran[-1] == "_close_store_client"
    assert "_stop_notifications" in ran
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "receipt request refused" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()


def test_two_instances_on_one_client_both_stop_cleanly():
    store = _Store()
    first, second = _resource(store), _resource(store)

    _run_stop(first)
    _run_stop(second)

    assert store.calls == [
        "deregister-endpoints", "deregister-address", "close",
        "deregister-endpoints", "deregister-address", "close",
    ]


# The app's lifespan: enter it and leave it with the test client, as the framework does.
def test_watchdog_lifespan_closes_once_and_exits_without_an_error():
    store = _Store()
    mw = SemanticMiddleware(mode="watchdog", ogm=store, sweep_interval=3600)
    with TestClient(mw.app):
        assert "close" not in store.calls
    assert store.calls.count("close") == 1


def test_a_failing_close_in_the_lifespan_is_one_warning(caplog):
    store = _Store(close_error=RuntimeError("receipt request refused"))
    mw = SemanticMiddleware(mode="watchdog", ogm=store, sweep_interval=3600)
    with caplog.at_level(logging.WARNING):
        with TestClient(mw.app):
            pass
    assert store.calls.count("close") == 1
    assert sum("receipt request refused" in r.getMessage() for r in caplog.records) == 1


def test_a_close_that_hangs_is_abandoned_after_its_window(monkeypatch, caplog):
    release = threading.Event()

    class _Hanging(_Store):
        def close(self) -> None:
            release.wait()

    store = _Hanging()
    monkeypatch.setattr(middleware_module, "_CLOSE_WINDOW", 0.2)
    mw = SemanticMiddleware(mode="watchdog", ogm=store)
    try:
        with caplog.at_level(logging.WARNING):
            ran = _run_stop(mw)
    finally:
        release.set()
    assert ran[-1] == "_close_store_client"
    assert any("did not close" in r.getMessage() for r in caplog.records)
