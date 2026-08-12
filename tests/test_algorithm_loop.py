"""Unit tests for the demonstration algorithm's background loop: pause, mode switch, and
event-driven quiescence (#82).

No GraphDB and no live peer process here -- ``run_algorithm_loop``/``run_algorithm_once`` only
ever touch ``controller.units`` (plain attribute access), ``controller.push()`` and
``controller.writes``, so a duck-typed fake stands in for both the ``Controller``
and its loaded units. The real wiring/fetch/REST path is covered separately, against a live
GraphDB and a real peer process, in ``tests/test_controller_view.py``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.transferunits import algorithm as algorithm_module  # noqa: E402
from demo.transferunits import seed  # noqa: E402
from demo.transferunits.algorithm import AlgorithmMode, AlgorithmState  # noqa: E402
from demo.transferunits.controller import WriteTracker  # noqa: E402
from kapps_semantic_middleware.vocabulary import INF  # noqa: E402


def _fake_param(value):
    param = SimpleNamespace()
    setattr(param, INF.hasValue.lined, [value])
    return param


def _fake_belt(speed: float, belt_iri: str) -> SimpleNamespace:
    belt = SimpleNamespace(id=belt_iri)
    setattr(belt, seed.TU_HAS_CONVEYOR_SPEED.lined, [_fake_param(speed)])
    return belt


def _fake_barrier(occupied: bool) -> SimpleNamespace:
    barrier = SimpleNamespace()
    setattr(barrier, seed.TU_IS_OCCUPIED.lined, [_fake_param(occupied)])
    return barrier


def _fake_unit(speed: float, occupied: bool, belt_iri: str) -> SimpleNamespace:
    unit = SimpleNamespace()
    setattr(unit, seed.TU_HAS_CONVEYOR_BELT.lined, [_fake_belt(speed, belt_iri)])
    setattr(unit, seed.TU_HAS_LIGHT_BARRIER.lined, [_fake_barrier(occupied)])
    return unit


def _set_occupied(unit: SimpleNamespace, occupied: bool) -> None:
    """Mutate a fake unit's barrier reading in place, the way a REST poll's formatter
    would overwrite the loaded object's value -- no new object, same attribute list."""
    barrier = getattr(unit, seed.TU_HAS_LIGHT_BARRIER.lined)[0]
    param = getattr(barrier, seed.TU_IS_OCCUPIED.lined)[0]
    setattr(param, INF.hasValue.lined, [occupied])


class FakeController:
    """Duck-typed stand-in for everything ``run_algorithm_loop``/``run_algorithm_once``
    touch on a real ``Controller`` -- deliberately not a real one, so these tests need
    no graph and no network."""

    def __init__(self, units):
        self.units = units
        self.rebuild_lock = asyncio.Lock()
        self.pushed: list[str] = []
        # The real thing, not a stub: it holds no graph, connector or network, so a fake
        # of it would only be a second implementation of the rules to keep in step.
        self.writes = WriteTracker()

    async def push(self, resource_iri):
        self.pushed.append(str(resource_iri))


async def _run_briefly(controller, state, seconds: float) -> None:
    task = asyncio.create_task(algorithm_module.run_algorithm_loop(controller, state))
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
class TestPause:
    async def test_paused_never_ticks(self):
        controller = FakeController(
            {
                "http://example.org/u1": _fake_unit(1.0, False, "http://example.org/belt1"),
                "http://example.org/u2": _fake_unit(2.0, False, "http://example.org/belt2"),
            }
        )
        state = AlgorithmState(tick_seconds=0.03, paused=True)

        await _run_briefly(controller, state, 0.3)

        assert controller.pushed == []
        assert state.last_tick_at is None

    async def test_a_rebuild_in_progress_pauses_ticking_and_resumes_after(self):
        controller = FakeController(
            {
                "http://example.org/u1": _fake_unit(1.0, False, "http://example.org/belt1"),
                "http://example.org/u2": _fake_unit(2.0, False, "http://example.org/belt2"),
            }
        )
        state = AlgorithmState(tick_seconds=0.03)

        await controller.rebuild_lock.acquire()
        task = asyncio.create_task(algorithm_module.run_algorithm_loop(controller, state))
        try:
            await asyncio.sleep(0.3)
            assert controller.pushed == [], "must not tick while a rebuild holds the lock"
        finally:
            controller.rebuild_lock.release()

        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert controller.pushed, "must resume ticking once the rebuild lock is released"


@pytest.mark.asyncio
class TestTimedMode:
    async def test_ticks_repeatedly_at_the_configured_interval(self):
        controller = FakeController(
            {
                "http://example.org/u1": _fake_unit(1.0, False, "http://example.org/belt1"),
                "http://example.org/u2": _fake_unit(2.0, False, "http://example.org/belt2"),
            }
        )
        state = AlgorithmState(tick_seconds=0.03, mode=AlgorithmMode.TIMED)

        await _run_briefly(controller, state, 0.35)

        assert len(controller.pushed) >= 3, controller.pushed
        assert state.last_tick_at is not None


@pytest.mark.asyncio
class TestEventDrivenMode:
    async def test_quiescent_on_the_first_sample_then_fires_exactly_once_on_a_change(
        self, monkeypatch
    ):
        monkeypatch.setattr(algorithm_module, "WATCH_INTERVAL_SECONDS", 0.02)
        unit_a = _fake_unit(1.0, False, "http://example.org/belt-a")
        unit_b = _fake_unit(2.0, False, "http://example.org/belt-b")
        controller = FakeController({"http://example.org/a": unit_a, "http://example.org/b": unit_b})
        state = AlgorithmState(tick_seconds=999.0, mode=AlgorithmMode.EVENT_DRIVEN)

        task = asyncio.create_task(algorithm_module.run_algorithm_loop(controller, state))
        try:
            await asyncio.sleep(0.08)
            assert controller.pushed == [], "must not fire on the very first sample"
            assert state.waiting_since is not None, "must show an explicit waiting state"

            change_at = time.monotonic()
            _set_occupied(unit_a, True)
            await asyncio.sleep(0.08)
            assert len(controller.pushed) == 1, controller.pushed
            # The loop re-arms `waiting_since` on the very next watch tick after it
            # fires -- correct, since it goes straight back to watching for the *next*
            # change (#82: idle must never look broken, including right after a
            # reaction). So this cannot assert it stays None; it asserts the fresher
            # property: whatever `waiting_since` reads now was set no earlier than the
            # change itself, proving it was actually cleared and restarted rather than
            # left stale from the wait period that preceded the reaction.
            if state.waiting_since is not None:
                assert state.waiting_since >= change_at

            # No further change -- must not fire again on a steady reading.
            await asyncio.sleep(0.08)
            assert len(controller.pushed) == 1
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_mode_switch_takes_effect_on_the_same_running_loop(self, monkeypatch):
        """Flipping state.mode at runtime -- what a station-board route does -- must be
        honoured by the loop already running, with no restart (#82: "the toggle switches
        between them at runtime")."""
        monkeypatch.setattr(algorithm_module, "WATCH_INTERVAL_SECONDS", 0.02)
        unit_a = _fake_unit(1.0, False, "http://example.org/belt-a")
        unit_b = _fake_unit(2.0, False, "http://example.org/belt-b")
        controller = FakeController({"http://example.org/a": unit_a, "http://example.org/b": unit_b})
        # Start in EVENT_DRIVEN rather than TIMED: TIMED fires its first tick
        # immediately (see TestTimedMode), which would make "no push yet" trivially
        # false regardless of whether a mode switch works at all. EVENT_DRIVEN's own
        # sleep granularity is the small WATCH_INTERVAL_SECONDS patched above, so a
        # switch made while it is running is picked up on the very next watch tick --
        # unlike a switch made while TIMED is mid-sleep for a long tick_seconds, which
        # only takes effect once that sleep completes (a separate, coarser-grained
        # property this test does not need to exercise).
        state = AlgorithmState(tick_seconds=0.03, mode=AlgorithmMode.EVENT_DRIVEN)

        task = asyncio.create_task(algorithm_module.run_algorithm_loop(controller, state))
        try:
            await asyncio.sleep(0.08)
            assert controller.pushed == [], "must be quiescent before any barrier changes"

            state.mode = AlgorithmMode.TIMED
            await asyncio.sleep(0.15)

            assert controller.pushed, "the running loop must honour the mode switch"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
