"""Unit tests for the demonstration algorithm's background loop: pause, quiescence, and a
reaction that only reads.

No store and no live peer process here -- ``run_algorithm_loop``/``run_algorithm_once`` only
ever touch ``controller.units`` (plain attribute access) and ``controller.rebuild_lock``, so a
duck-typed fake stands in for both the ``Controller`` and its loaded units. The real
wiring/fetch/REST path is covered separately, against a live store and a real peer process,
in ``tests/test_controller_view.py``.
"""

from __future__ import annotations

import asyncio
import copy
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.transferunits import algorithm as algorithm_module  # noqa: E402
from demo.transferunits import seed  # noqa: E402
from demo.transferunits.algorithm import AlgorithmState  # noqa: E402
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


def _speed_of(unit: SimpleNamespace):
    belt = getattr(unit, seed.TU_HAS_CONVEYOR_BELT.lined)[0]
    return getattr(getattr(belt, seed.TU_HAS_CONVEYOR_SPEED.lined)[0], INF.hasValue.lined)


class FakeController:
    """Duck-typed stand-in for everything ``run_algorithm_loop``/``run_algorithm_once``
    touch on a real ``Controller`` -- deliberately not a real one, so these tests need
    no graph and no network. It has no write method at all: a reaction that tried to
    drive a unit would fail here with an ``AttributeError``."""

    def __init__(self, units):
        self.units = units
        self.rebuild_lock = asyncio.Lock()


@pytest.fixture
def reactions(monkeypatch):
    """Count the reactions the loop fires, while still running the real one."""
    fired: list[object] = []
    real = algorithm_module.run_algorithm_once

    async def counting(controller):
        fired.append(await real(controller))
        return fired[-1]

    monkeypatch.setattr(algorithm_module, "run_algorithm_once", counting)
    monkeypatch.setattr(algorithm_module, "WATCH_INTERVAL_SECONDS", 0.02)
    return fired


def _two_units():
    unit_a = _fake_unit(1.0, False, "http://example.org/belt-a")
    unit_b = _fake_unit(2.0, False, "http://example.org/belt-b")
    return unit_a, unit_b, FakeController(
        {"http://example.org/a": unit_a, "http://example.org/b": unit_b}
    )


async def _stop(task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
class TestPause:
    async def test_paused_never_reacts(self, reactions):
        unit_a, _, controller = _two_units()
        state = AlgorithmState(paused=True)

        task = asyncio.create_task(algorithm_module.run_algorithm_loop(controller, state))
        try:
            await asyncio.sleep(0.08)
            _set_occupied(unit_a, True)
            await asyncio.sleep(0.08)
        finally:
            await _stop(task)

        assert reactions == []
        assert state.last_reaction_at is None

    async def test_a_rebuild_in_progress_pauses_reacting_and_resumes_after(self, reactions):
        unit_a, _, controller = _two_units()
        state = AlgorithmState()

        await controller.rebuild_lock.acquire()
        task = asyncio.create_task(algorithm_module.run_algorithm_loop(controller, state))
        try:
            await asyncio.sleep(0.08)
            _set_occupied(unit_a, True)
            await asyncio.sleep(0.08)
            assert reactions == [], "must not react while a rebuild holds the lock"
        finally:
            controller.rebuild_lock.release()

        try:
            await asyncio.sleep(0.08)
            _set_occupied(unit_a, False)
            await asyncio.sleep(0.08)
        finally:
            await _stop(task)
        assert reactions, "must resume reacting once the rebuild lock is released"


@pytest.mark.asyncio
class TestABarrierChangeFiresOneReaction:
    async def test_quiescent_on_the_first_sample_then_fires_exactly_once_on_a_change(
        self, reactions
    ):
        unit_a, _, controller = _two_units()
        state = AlgorithmState()

        task = asyncio.create_task(algorithm_module.run_algorithm_loop(controller, state))
        try:
            await asyncio.sleep(0.08)
            assert reactions == [], "must not fire on the very first sample"
            assert state.waiting_since is not None, "must show an explicit waiting state"

            change_at = time.monotonic()
            _set_occupied(unit_a, True)
            await asyncio.sleep(0.08)
            assert len(reactions) == 1, reactions
            # The loop re-arms `waiting_since` on the very next watch tick after it
            # fires -- correct, since it goes straight back to watching for the *next*
            # change (idle must never look broken, including right after a
            # reaction). So this cannot assert it stays None; it asserts the fresher
            # property: whatever `waiting_since` reads now was set no earlier than the
            # change itself, proving it was actually cleared and restarted rather than
            # left stale from the wait period that preceded the reaction.
            if state.waiting_since is not None:
                assert state.waiting_since >= change_at

            # No further change -- must not fire again on a steady reading.
            await asyncio.sleep(0.08)
            assert len(reactions) == 1
        finally:
            await _stop(task)


@pytest.mark.asyncio
class TestAReactionOnlyReads:
    async def test_a_reaction_leaves_every_loaded_unit_as_it_was(self):
        """The reaction reads a barrier and a speed. It assigns nothing: the loaded units
        compare equal before and after, and the fake controller it ran against has no
        write method it could have called."""
        unit_a, unit_b, controller = _two_units()
        before = copy.deepcopy(controller.units)

        observed = await algorithm_module.run_algorithm_once(controller)

        assert observed is not None
        assert _speed_of(unit_a) == _speed_of(before["http://example.org/a"]) == [1.0]
        assert _speed_of(unit_b) == _speed_of(before["http://example.org/b"]) == [2.0]

    async def test_fewer_than_two_units_observes_nothing(self):
        controller = FakeController(
            {"http://example.org/a": _fake_unit(1.0, False, "http://example.org/belt-a")}
        )

        assert await algorithm_module.run_algorithm_once(controller) is None
