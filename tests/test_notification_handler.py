"""The notification handler at the event trigger, offline.

An instance that registered a handler with ``on_notification`` hands every notification that
matches none of its workflows to it, as the Operation object, in the background, with no
lifecycle: no status write, and the Operation does not enter the in-memory queue. An instance
without one keeps the queue and ``claim_next``'s lifecycle. The two registrations exclude each
other. The OGM is faked: it fetches anything, as an Operation that implements no Capability, and
it records and refuses every other use, so a notification that wrote or queried the store is
seen even when the middleware catches the refusal.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from kapps_ogm import Node
from kapps_triplestore_interface import IRI
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.vocabulary import OperationStatus

RESOURCE = "http://example.org/test#Module1"
SERVICE_CLASS = "http://example.org/test#ModuleService"
OPERATION = IRI("http://example.org/test#Transport1")
MIDDLEWARE_LOGGER = "kapps_semantic_middleware.middleware"


class _FakeOGM:
    """An OGM that fetches anything, and records and refuses every other use."""

    def __init__(self):
        self.refused: list = []

    def fetch(self, **kwargs):
        return Node(id=kwargs["instance_iri"])

    def __getattr__(self, name):
        self.refused.append(name)
        raise AssertionError(f"the notification used the OGM's {name}")


def _module(ogm: _FakeOGM) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=RESOURCE,
        service_class=SERVICE_CLASS,
        ogm=ogm,
        heartbeat_interval=None,
    )


async def _settle(mw: SemanticMiddleware) -> tuple[set, set]:
    """Wait for the background tasks that the notification started.

    Returns those tasks, and the tasks still left afterwards. ``asyncio.wait`` does not
    retrieve a task's exception, so a test can still see one that escaped its task.
    """
    started = set(mw._background_tasks)
    if started:
        await asyncio.wait(started)
    return started, set(mw._background_tasks)


def _errors(caplog) -> list:
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_a_notification_reaches_the_handler_with_no_lifecycle(caplog):
    ogm = _FakeOGM()
    mw = _module(ogm)
    received = []
    mw.on_notification(received.append)

    async def deliver():
        answer = await mw._handle_event_trigger(OPERATION)
        started, left = await _settle(mw)
        return answer, started, left

    with caplog.at_level(logging.ERROR, logger=MIDDLEWARE_LOGGER):
        answer, started, left = asyncio.run(deliver())

    assert [operation.id for operation in received] == [OPERATION]
    assert answer == {
        "operation": str(OPERATION)
    }  # it claims no status it did not write
    assert OPERATION not in mw._operation_queue
    # Nothing ran after the handler: the runner used no store (the fake would refuse it and
    # the runner would log the refusal), and it left no task behind.
    assert ogm.refused == []
    assert _errors(caplog) == []
    assert started and not left


def test_the_trigger_answers_before_the_handler_finishes():
    mw = _module(_FakeOGM())
    release = threading.Event()
    finished = threading.Event()

    def handler(operation_iri):
        assert release.wait(timeout=10), "the test never released the handler"
        finished.set()

    mw.on_notification(handler)

    async def deliver():
        answer = await mw._handle_event_trigger(OPERATION)
        answered_while_blocked = not finished.is_set()
        release.set()
        await _settle(mw)
        return answer, answered_while_blocked

    answer, answered_while_blocked = asyncio.run(deliver())

    assert answer == {"operation": str(OPERATION)}
    assert answered_while_blocked
    assert finished.is_set()


def test_a_coroutine_handler_runs_on_the_event_loop():
    mw = _module(_FakeOGM())
    received = []

    async def handler(operation_iri):
        received.append(operation_iri)

    mw.on_notification(handler)

    async def deliver():
        await mw._handle_event_trigger(OPERATION)
        await _settle(mw)

    asyncio.run(deliver())

    assert [operation.id for operation in received] == [OPERATION]


def test_a_handler_exception_is_logged_and_writes_nothing(caplog):
    ogm = _FakeOGM()
    mw = _module(ogm)

    def handler(operation_iri):
        raise RuntimeError("motor jammed")

    mw.on_notification(handler)

    async def deliver():
        await mw._handle_event_trigger(OPERATION)
        return await _settle(mw)

    with caplog.at_level(logging.ERROR, logger=MIDDLEWARE_LOGGER):
        started, left = asyncio.run(deliver())

    assert started and not left
    assert all(task.exception() is None for task in started)  # nothing left unretrieved
    assert "motor jammed" in caplog.text
    assert str(OPERATION) in caplog.text
    assert ogm.refused == []
    assert OPERATION not in mw._operation_queue


class _QueuedStore:
    """``ogm.db`` of an instance without a handler: it reads each notified Operation, in one
    query, as a queued one that another resource issued."""

    def query(self, sparql, convert_bindings=False):
        assert f"<{OPERATION}>" in sparql
        row = {"type": IRI("http://example.org/test#Transport"), "status": "queued"}
        return {"results": {"bindings": [row]}}


def test_without_a_handler_a_notification_enters_the_queue_as_before():
    ogm = _FakeOGM()
    ogm.__dict__["db"] = _QueuedStore()
    mw = _module(ogm)

    answer = asyncio.run(mw._handle_event_trigger(OPERATION))

    assert answer == {"operation": str(OPERATION), "status": OperationStatus.QUEUED}
    assert mw._operation_queue == [OPERATION]


def test_a_handler_and_a_callback_exclude_each_other():
    with_handler = _module(_FakeOGM())
    with_handler.on_notification(lambda operation_iri: None)
    with pytest.raises(RuntimeError, match="exclude each other"):
        with_handler.register_callback(lambda operation: None)

    with_callback = _module(_FakeOGM())
    with_callback.register_callback(lambda operation: None)
    with pytest.raises(RuntimeError, match="exclude each other"):
        with_callback.on_notification(lambda operation_iri: None)


def test_the_handler_is_resource_mode_only():
    watchdog = SemanticMiddleware(mode="watchdog", ogm=_FakeOGM())
    with pytest.raises(RuntimeError, match="resource mode"):
        watchdog.on_notification(lambda operation_iri: None)
