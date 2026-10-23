"""The heartbeat and sweep loops survive a store error, offline.

A store error (the store unreachable, a dropped connection, an error answer) costs one beat or
one sweep: the loop logs one warning that names the error and tries again at the next interval.
Cancellation still ends both loops, and any other error ends them and is raised. The waits are
replaced by recorders, so the tests check order, not a duration.

Registration writes the address and the first heartbeat in one commit. Inside one sweep, a store
error is isolated to the Service or Operation it hit, and a deregistration removes the address
last, so a half-done one is found and finished by the next sweep.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import (
    AuthenticationError,
    PreconditionFailedError,
    TripleStoreResponseError,
    TripleStoreUnavailableError,
)

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware import middleware as middleware_module
from kapps_semantic_middleware import registration as registration_module
from kapps_semantic_middleware.registration import (
    deregister_service,
    register_service,
    sweep_stale_services,
)
from kapps_semantic_middleware.vocabulary import SVC

RESOURCE = "http://example.org/test#Module1"
SERVICE_CLASS = "http://example.org/test#ModuleService"
INTERVAL = 30.0
SWEEP_INTERVAL = 5.0


class _Stop(Exception):
    """Raised by a recording wait to end a loop under test."""


def _reset() -> TripleStoreUnavailableError:
    return TripleStoreUnavailableError("No answer from the store: connection reset by peer")


# --------------------------------------------------------------------------- #
# The sweep loop.
# --------------------------------------------------------------------------- #


def _watchdog() -> SemanticMiddleware:
    return SemanticMiddleware(mode="watchdog", ogm=Mock(), sweep_interval=SWEEP_INTERVAL)


@pytest.mark.parametrize(
    "error",
    [
        _reset(),
        TripleStoreResponseError("503 from the store", status=503),
        AuthenticationError("401 from the store", status=401),
        PreconditionFailedError("the write's precondition was false"),
    ],
)
def test_a_sweep_that_fails_on_a_store_error_is_logged_and_the_next_interval_sweeps(
    monkeypatch, caplog, error
):
    watchdog = _watchdog()
    record: list = []

    async def sweep():
        record.append("sweep")
        if record.count("sweep") == 1:
            raise error
        return []

    async def wait(seconds):
        record.append(("wait", seconds))
        if record.count("sweep") == 2:
            raise _Stop

    monkeypatch.setattr(watchdog, "sweep", sweep)
    monkeypatch.setattr(middleware_module, "_sweep_wait", wait)

    with caplog.at_level(logging.WARNING, logger=middleware_module.__name__):
        with pytest.raises(_Stop):
            asyncio.run(watchdog._sweep_loop())

    assert record == ["sweep", ("wait", SWEEP_INTERVAL), "sweep", ("wait", SWEEP_INTERVAL)]
    # A loop that an earlier live test left running may log into this caplog: count only this error.
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and str(error) in r.getMessage()
    ]
    assert len(warnings) == 1
    assert f"{SWEEP_INTERVAL:g} s" in warnings[0].getMessage()


def test_a_programming_error_in_a_sweep_ends_the_loop_and_is_raised(monkeypatch, caplog):
    watchdog = _watchdog()

    async def sweep():
        raise TypeError("a bug")

    async def wait(seconds):  # pragma: no cover - the loop must not reach it
        raise AssertionError("the loop went on after a programming error")

    monkeypatch.setattr(watchdog, "sweep", sweep)
    monkeypatch.setattr(middleware_module, "_sweep_wait", wait)

    with caplog.at_level(logging.ERROR, logger=middleware_module.__name__):
        with pytest.raises(TypeError, match="a bug"):
            asyncio.run(watchdog._sweep_loop())

    # Count only this error: a loop that an earlier live test left running may log into this caplog.
    assert [r.levelno for r in caplog.records if r.exc_info and "a bug" in str(r.exc_info[1])] == [
        logging.ERROR
    ]


def test_cancellation_still_stops_the_sweep_loop(monkeypatch):
    watchdog = _watchdog()
    sweeps: list = []
    # The first sweep fails, so the cancellation lands in a loop that survived a store error.

    async def sweep():
        sweeps.append(1)
        if len(sweeps) == 1:
            raise _reset()
        return []

    monkeypatch.setattr(watchdog, "sweep", sweep)
    monkeypatch.setattr(middleware_module, "_sweep_wait", lambda seconds: asyncio.sleep(0))

    async def run() -> None:
        await watchdog._start_sweep()
        for _ in range(1000):  # bounded: without the fix the loop ends after the first sweep
            if len(sweeps) >= 3:
                break
            await asyncio.sleep(0)
        await asyncio.wait_for(watchdog._stop_sweep(), timeout=5)
        assert watchdog._sweep_task is not None and watchdog._sweep_task.done()

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert len(sweeps) >= 3


# --------------------------------------------------------------------------- #
# The heartbeat loop.
# --------------------------------------------------------------------------- #


class _FlakyOGM:
    """A stand-in store whose commits fail with the errors given, in order, then succeed."""

    def __init__(self, record: list, failures: list) -> None:
        self.record = record
        self.failures = list(failures)

    def commit(self, **kwargs):
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                self.record.append("failed write")
                raise failure
        self.record.append("write")


def _resource(ogm) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=RESOURCE,
        service_class=SERVICE_CLASS,
        ogm=ogm,
        heartbeat_interval=INTERVAL,
    )


@pytest.mark.parametrize(
    "error",
    [
        _reset(),
        TripleStoreResponseError("503 from the store", status=503),
        AuthenticationError("401 from the store", status=401),
        PreconditionFailedError("the write's precondition was false"),
    ],
)
def test_a_beat_that_fails_on_a_store_error_is_logged_and_the_next_interval_beats(
    monkeypatch, caplog, error
):
    record: list = []
    # The startup beat writes, the first periodic beat fails, the next one writes.
    mw = _resource(_FlakyOGM(record, [None, error]))

    async def wait(seconds):
        record.append(("wait", seconds))
        if record.count("write") == 2:
            raise _Stop

    monkeypatch.setattr(middleware_module, "_heartbeat_phase", lambda low, high: 7.5)
    monkeypatch.setattr(middleware_module, "_heartbeat_wait", wait)

    with caplog.at_level(logging.WARNING, logger=middleware_module.__name__):
        with pytest.raises(_Stop):
            asyncio.run(mw._heartbeat_loop())

    assert record == [
        "write",
        ("wait", 7.5),
        "failed write",
        ("wait", INTERVAL),
        "write",
        ("wait", INTERVAL),
    ]
    # A loop that an earlier live test left running may log into this caplog: count only this error.
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and str(error) in r.getMessage()
    ]
    assert len(warnings) == 1
    assert str(mw.service_iri) in warnings[0].getMessage()


def test_a_failed_startup_beat_does_not_end_the_heartbeat_loop(monkeypatch):
    record: list = []
    mw = _resource(_FlakyOGM(record, [_reset()]))

    async def wait(seconds):
        record.append(("wait", seconds))
        if record.count("write") == 1:
            raise _Stop

    monkeypatch.setattr(middleware_module, "_heartbeat_phase", lambda low, high: 7.5)
    monkeypatch.setattr(middleware_module, "_heartbeat_wait", wait)

    with pytest.raises(_Stop):
        asyncio.run(mw._heartbeat_loop())

    assert record == ["failed write", ("wait", 7.5), "write", ("wait", INTERVAL)]


def test_a_programming_error_in_a_beat_ends_the_heartbeat_loop_and_is_raised(monkeypatch):
    record: list = []
    mw = _resource(_FlakyOGM(record, [TypeError("a bug")]))

    async def wait(seconds):  # pragma: no cover - the loop must not reach it
        raise AssertionError("the loop went on after a programming error")

    monkeypatch.setattr(middleware_module, "_heartbeat_wait", wait)

    with pytest.raises(TypeError, match="a bug"):
        asyncio.run(mw._heartbeat_loop())


def test_cancellation_still_stops_the_heartbeat_loop(monkeypatch):
    record: list = []
    mw = _resource(_FlakyOGM(record, [_reset()]))
    monkeypatch.setattr(middleware_module, "_heartbeat_phase", lambda low, high: 0.0)
    monkeypatch.setattr(middleware_module, "_heartbeat_wait", lambda seconds: asyncio.sleep(0))

    async def run() -> None:
        await mw._start_heartbeat()
        for _ in range(500):  # bounded: without the fix the loop ends after the first beat
            if record.count("write") >= 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.wait_for(mw._stop_heartbeat(), timeout=5)
        assert mw._heartbeat_task is not None and mw._heartbeat_task.done()

    asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert record[0] == "failed write"


# --------------------------------------------------------------------------- #
# Registration: no address without a heartbeat.
# --------------------------------------------------------------------------- #


def test_registration_writes_the_address_and_a_fresh_heartbeat_in_one_commit():
    ogm = Mock()
    service = IRI("http://example.org/test#Module1_service")
    before = datetime.now(timezone.utc)

    register_service(
        ogm,
        resource_iri=IRI(RESOURCE),
        service_iri=service,
        service_class=IRI(SERVICE_CLASS),
        address="http://127.0.0.1:8001",
    )

    addressed = [
        call.kwargs
        for call in ogm.commit.call_args_list
        if str(SVC.hasAddress) in call.kwargs.get("data", {})
    ]
    assert len(addressed) == 1, "the address is written in one commit"
    data = addressed[0]["data"]
    assert data[str(SVC.hasAddress)] == ["http://127.0.0.1:8001"]
    [heartbeat] = data[str(SVC.hasLastHeartbeat)]
    written = datetime.fromisoformat(heartbeat)
    assert before <= written <= datetime.now(timezone.utc) + timedelta(seconds=1)


# --------------------------------------------------------------------------- #
# Inside one sweep: a failure is isolated, and the address goes last.
# --------------------------------------------------------------------------- #

FIRST = IRI("http://example.org/test#Service1")
SECOND = IRI("http://example.org/test#Service2")
RESOURCE_1 = IRI("http://example.org/test#Resource1")
RESOURCE_2 = IRI("http://example.org/test#Resource2")
OPERATION_1 = IRI("http://example.org/test#Operation1")
OPERATION_2 = IRI("http://example.org/test#Operation2")


def _sweep_with(monkeypatch, *, failing_commit_on: set, failing_read_of: set = frozenset()):
    """Run one sweep over two stale Services, each of its own resource with one stranded
    Operation, against a stand-in store whose commits on ``failing_commit_on`` and reads of the
    stranded Operations of ``failing_read_of`` are reset. Return the swept Services, the committed
    instances and the Operations reported failed."""
    monkeypatch.setattr(
        registration_module, "find_stale_services", lambda *a, **k: [FIRST, SECOND]
    )
    monkeypatch.setattr(
        registration_module,
        "_resource_of_service",
        lambda ogm, service, named_graph=None: RESOURCE_1 if service == FIRST else RESOURCE_2,
    )

    def stranded(ogm, resource, *args, **kwargs):
        if resource in failing_read_of:
            raise _reset()
        return [OPERATION_1] if resource == RESOURCE_1 else [OPERATION_2]

    monkeypatch.setattr(registration_module, "find_stranded_operations", stranded)
    committed: list = []
    ogm = Mock()
    ogm.db.triples_get.return_value = []

    def commit(*, instance_iri, data, named_graph=None):
        if instance_iri in failing_commit_on:
            raise _reset()
        committed.append(instance_iri)

    ogm.commit.side_effect = commit
    reported: list = []
    swept = registration_module._sweep_stale_services(ogm, 60.0, failed=reported.append)
    return swept, committed, reported


def test_a_stale_service_whose_deregistration_fails_does_not_block_the_next(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=registration_module.__name__):
        swept, committed, reported = _sweep_with(monkeypatch, failing_commit_on={FIRST})

    assert swept == [SECOND]
    assert committed == [OPERATION_1, OPERATION_2, SECOND]
    assert reported == [OPERATION_1, OPERATION_2]
    [warning] = [
        r for r in caplog.records if r.levelno == logging.WARNING and str(FIRST) in r.getMessage()
    ]
    assert str(FIRST) in warning.getMessage()


def test_a_stranded_operation_that_is_not_failed_keeps_its_service_for_the_next_sweep(
    monkeypatch, caplog
):
    with caplog.at_level(logging.WARNING, logger=registration_module.__name__):
        swept, committed, reported = _sweep_with(monkeypatch, failing_commit_on={OPERATION_1})

    # The first Service keeps its address, so the next sweep finds it and fails the Operation.
    assert swept == [SECOND]
    assert committed == [OPERATION_2, SECOND]
    assert reported == [OPERATION_2]
    [warning] = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and str(OPERATION_1) in r.getMessage()
    ]
    assert str(OPERATION_1) in warning.getMessage() and str(FIRST) in warning.getMessage()


def test_a_failed_read_of_one_service_does_not_block_the_next(monkeypatch):
    swept, committed, reported = _sweep_with(
        monkeypatch, failing_commit_on=set(), failing_read_of={RESOURCE_1}
    )

    assert swept == [SECOND]
    assert committed == [OPERATION_2, SECOND]
    assert reported == [OPERATION_2]


def test_a_programming_error_inside_the_sweep_is_raised(monkeypatch):
    monkeypatch.setattr(registration_module, "find_stale_services", lambda *a, **k: [FIRST])
    monkeypatch.setattr(
        registration_module,
        "_resource_of_service",
        lambda *a, **k: (_ for _ in ()).throw(TypeError("a bug")),
    )

    with pytest.raises(TypeError, match="a bug"):
        sweep_stale_services(Mock(), 60.0)


def test_deregistration_removes_the_endpoints_first_and_the_address_last():
    workflow = IRI("http://example.org/test#Workflow1")
    state_property = IRI("http://example.org/test#State1")
    ogm = Mock()

    def triples_get(*, pred, obj, named_graph=None):
        subject = workflow if pred == SVC.isWorkflowOf else state_property
        return [(subject, pred, obj)]

    ogm.db.triples_get.side_effect = triples_get

    deregister_service(ogm, FIRST)

    calls = [(c.kwargs["instance_iri"], set(c.kwargs["data"])) for c in ogm.commit.call_args_list]
    assert calls == [
        (workflow, {str(SVC.hasEndpoint)}),
        (state_property, {str(SVC.hasEndpoint)}),
        (FIRST, {str(SVC.hasAddress)}),
    ]


def test_a_deregistration_stopped_by_a_store_error_keeps_the_address():
    workflow = IRI("http://example.org/test#Workflow1")
    ogm = Mock()
    ogm.db.triples_get.side_effect = lambda *, pred, obj, named_graph=None: (
        [(workflow, pred, obj)] if pred == SVC.isWorkflowOf else []
    )
    ogm.commit.side_effect = _reset()

    with pytest.raises(TripleStoreUnavailableError):
        deregister_service(ogm, FIRST)

    # Only the endpoint's commit was tried: the address is still there for the next sweep.
    assert [c.kwargs["instance_iri"] for c in ogm.commit.call_args_list] == [workflow]
