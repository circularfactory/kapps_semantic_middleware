"""The write-status seam: ``converging`` -> ``settled`` | ``rejected`` | ``diverged`` (#82).

**Why this is Python and not JavaScript.** The classification began life in
``station_board.html``'s own script, which put a domain judgement -- "has this write
stopped converging?" -- in a template, where this repo's test runner cannot reach it.
#82's acceptance requires ``rejected`` and ``diverged`` to be distinguishable *in a
test*, so the judgement moved to :class:`WriteTracker` and the page now renders the
verdict the server already reached.

**``diverged`` means stopped converging, not unequal.** #83 gave the belt momentum, so
commanded and actual are unequal during every set *by design* (#81 amended #31 on exactly
this point). An equality test would fire on every write; what actually distinguishes a
stuck belt is that its observed value has stopped moving while still short of the
command.

Nothing here needs GraphDB or a broker: the tracker is a pure state machine over
observations, which is the point of having it be its own object.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo.transferunits.controller import WriteStatus, WriteTracker  # noqa: E402

SETTLED = WriteStatus.SETTLED
CONVERGING = WriteStatus.CONVERGING
DIVERGED = WriteStatus.DIVERGED
REJECTED = WriteStatus.REJECTED


class FakeClock:
    """A monotonic clock a test can wind forward. Divergence is judged against elapsed
    time, so ageing a write must not mean sleeping through it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


STILL_SECONDS = 6.0


def _tracker(clock=None) -> WriteTracker:
    return WriteTracker(still_seconds=STILL_SECONDS, clock=clock or FakeClock())

BELT = "http://example.org/i#ConveyorBelt1_left"
SPEED = "tu__hasConveyorSpeed"
OTHER_BELT = "http://example.org/i#ConveyorBelt1_right"


class TestNothingCommanded:
    def test_a_parameter_never_written_through_this_controller_has_no_status(self):
        """No badge at all -- not "settled". The board shows a status only for a value
        this controller itself commanded, because the served datamodel carries no
        setpoint to compare against (ADR 0024's locator pattern)."""
        assert _tracker().observe(BELT, SPEED, 1.5) is None

    def test_an_uncommanded_parameter_stays_silent_however_long_it_sits(self):
        """A belt sitting still that nobody has driven must never read as diverged."""
        clock = FakeClock()
        tracker = _tracker(clock)

        statuses = []
        for _ in range(5):
            statuses.append(tracker.observe(BELT, SPEED, 1.5))
            clock.advance(STILL_SECONDS)

        assert statuses == [None] * 5


class TestSettled:
    def test_an_actual_value_matching_the_command_is_settled(self):
        tracker = _tracker()
        tracker.record_commanded(BELT, SPEED, 3.0, origin="operator")

        assert tracker.observe(BELT, SPEED, 3.0) == SETTLED

    def test_float_noise_still_counts_as_settled(self):
        """The value makes a round trip through MQTT, JSON and a float, so an exact
        equality test would leave a write reading ``converging`` forever."""
        tracker = _tracker()
        tracker.record_commanded(BELT, SPEED, 3.0, origin="operator")

        assert tracker.observe(BELT, SPEED, 2.9999999999) == SETTLED

    def test_a_command_recorded_as_a_one_element_list_settles_against_a_scalar(self):
        """``inf:hasValue`` is a one-element list on the datamodel, so a write path may
        hand over ``[3.0]`` while every observation reads back ``3.0``. The tracker
        normalizes, because the two write paths disagreed on this exact point and the
        algorithm's writes could never reach ``settled`` as a result.
        """
        tracker = _tracker()
        tracker.record_commanded(BELT, SPEED, [3.0], origin="algorithm")

        assert tracker.observe(BELT, SPEED, 3.0) == SETTLED

    def test_a_list_command_still_ramps_through_converging_first(self):
        """The normalization must not turn every list-shaped write into an instant
        settle -- it only makes the two shapes comparable."""
        tracker = _tracker()
        tracker.record_commanded(BELT, SPEED, [3.0], origin="algorithm")

        assert tracker.observe(BELT, SPEED, 1.0) == CONVERGING


class TestConverging:
    def test_a_ramp_in_progress_is_converging_not_diverged(self):
        """#83's ramp: unequal on every poll, but moving. This is the case an equality
        test would have wrongly called divergence on every single set."""
        tracker = _tracker()
        tracker.record_commanded(BELT, SPEED, 3.0, origin="operator")

        statuses = [tracker.observe(BELT, SPEED, v) for v in (0.0, 1.0, 2.0, 2.5)]

        assert statuses == [CONVERGING] * 4, statuses

    def test_a_long_ramp_never_diverges_while_it_keeps_moving(self):
        """Guards #82's rule head-on: no passage of time turns a *moving* value into a
        diverged one. Only stillness does."""
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.record_commanded(BELT, SPEED, 100.0, origin="operator")

        statuses = []
        for step in range(20):
            statuses.append(tracker.observe(BELT, SPEED, float(step)))
            clock.advance(STILL_SECONDS)

        assert DIVERGED not in statuses


class TestDiverged:
    def test_a_value_frozen_short_of_the_command_diverges(self):
        """#94's real symptom: the belt freezes one ramp step short and stays there."""
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.record_commanded(BELT, SPEED, 3.0, origin="operator")
        tracker.observe(BELT, SPEED, 2.95)

        clock.advance(STILL_SECONDS)

        assert tracker.observe(BELT, SPEED, 2.95) == DIVERGED

    def test_divergence_is_not_declared_the_moment_a_value_stops(self):
        """A quiet moment is a slow lap, not a stuck belt -- #82's own "the tick must
        exceed one lap" constraint says a lap can straddle a poll."""
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.record_commanded(BELT, SPEED, 3.0, origin="operator")
        tracker.observe(BELT, SPEED, 2.95)

        clock.advance(STILL_SECONDS / 2)

        assert tracker.observe(BELT, SPEED, 2.95) == CONVERGING

    def test_polling_harder_does_not_hasten_divergence(self):
        """Divergence is judged against the clock, not against a count of observations.
        A poll is a browser asking, so counting them would let a second open tab declare
        a belt stuck in half the time -- and a closed page never declare it at all."""
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.record_commanded(BELT, SPEED, 3.0, origin="operator")

        statuses = [tracker.observe(BELT, SPEED, 2.95) for _ in range(50)]

        assert set(statuses) == {CONVERGING}, "an idle clock produced a verdict anyway"

    def test_a_belt_that_starts_moving_again_leaves_diverged(self):
        """Divergence is a live judgement, not a latch: a belt that was stuck and then
        resumes its ramp reads as converging again. The command is far from the observed
        values here so that "moving again" cannot be confused with "arrived"."""
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.record_commanded(BELT, SPEED, 10.0, origin="operator")
        tracker.observe(BELT, SPEED, 5.0)
        clock.advance(STILL_SECONDS)
        assert tracker.observe(BELT, SPEED, 5.0) == DIVERGED

        clock.advance(1.0)

        assert tracker.observe(BELT, SPEED, 6.0) == CONVERGING

    def test_a_diverged_belt_that_reaches_the_command_settles(self):
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.record_commanded(BELT, SPEED, 3.0, origin="operator")
        tracker.observe(BELT, SPEED, 2.95)
        clock.advance(STILL_SECONDS)
        assert tracker.observe(BELT, SPEED, 2.95) == DIVERGED

        assert tracker.observe(BELT, SPEED, 3.0) == SETTLED

    def test_a_new_command_restarts_the_convergence_watch(self):
        """A fresh write must not inherit the previous one's stillness, or a retry would
        read as diverged on its very first observation."""
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.record_commanded(BELT, SPEED, 3.0, origin="operator")
        tracker.observe(BELT, SPEED, 2.95)
        clock.advance(STILL_SECONDS)
        assert tracker.observe(BELT, SPEED, 2.95) == DIVERGED

        tracker.record_commanded(BELT, SPEED, 5.0, origin="operator")

        assert tracker.observe(BELT, SPEED, 2.95) == CONVERGING


class TestRejected:
    def test_a_rejected_write_reports_rejected_with_its_reason(self):
        """``rejected`` is an *immediate* PUT failure -- unit down, 4xx, bad payload --
        and #82 requires the reason on screen, so the tracker carries it."""
        tracker = _tracker()
        tracker.record_rejected(BELT, SPEED, "Connection refused")

        assert tracker.observe(BELT, SPEED, 1.5) == REJECTED
        assert tracker.error_for(BELT, SPEED) == "Connection refused"

    def test_rejected_is_distinguishable_from_diverged(self):
        """The pair #82's acceptance names explicitly. One never reached the unit; the
        other reached it and stopped short."""
        clock = FakeClock()
        tracker = _tracker(clock)
        tracker.record_rejected(BELT, SPEED, "Connection refused")
        tracker.record_commanded(OTHER_BELT, SPEED, 3.0, origin="operator")
        tracker.observe(OTHER_BELT, SPEED, 2.95)
        clock.advance(STILL_SECONDS)

        assert tracker.observe(BELT, SPEED, 1.5) == REJECTED
        assert tracker.observe(OTHER_BELT, SPEED, 2.95) == DIVERGED

    def test_a_fresh_command_clears_a_previous_rejection(self):
        """Retrying after the unit comes back must not read as rejected forever."""
        tracker = _tracker()
        tracker.record_rejected(BELT, SPEED, "Connection refused")

        tracker.record_commanded(BELT, SPEED, 3.0, origin="operator")

        assert tracker.observe(BELT, SPEED, 3.0) == SETTLED
        assert tracker.error_for(BELT, SPEED) is None

    def test_a_rejection_survives_polls_until_something_changes_it(self):
        """It is recorded server-side precisely so a page reload does not lose it."""
        tracker = _tracker()
        tracker.record_rejected(BELT, SPEED, "unit down")

        statuses = [tracker.observe(BELT, SPEED, 1.5) for _ in range(4)]

        assert statuses == [REJECTED] * 4


class TestCommandedValue:
    def test_the_commanded_value_carries_its_origin(self):
        """"operator" or "algorithm" -- #81 chose one global pause precisely so exactly
        one author exists at a time, but the board still shows which one it was."""
        tracker = _tracker()
        tracker.record_commanded(BELT, SPEED, 3.0, origin="algorithm")

        commanded = tracker.commanded_for(BELT, SPEED)

        assert commanded is not None
        assert commanded.value == 3.0
        assert commanded.origin == "algorithm"

    def test_an_unwritten_parameter_has_no_commanded_value(self):
        assert _tracker().commanded_for(BELT, SPEED) is None


class TestDroppingALeaver:
    def test_dropping_a_departed_unit_forgets_its_commanded_values(self):
        """A leaver's state must not outlive it. The keys are *component* IRIs
        (``ConveyorBelt1_left``), not the unit's own, so the drop is driven by the
        wiring plan's bindings rather than by an IRI prefix -- a prefix match on the
        unit IRI silently matches nothing at all.
        """
        tracker = _tracker()
        tracker.record_commanded(BELT, SPEED, 3.0, origin="operator")
        tracker.record_rejected(OTHER_BELT, SPEED, "unit down")

        tracker.drop([(BELT, SPEED), (OTHER_BELT, SPEED)])

        assert tracker.commanded_for(BELT, SPEED) is None
        assert tracker.error_for(OTHER_BELT, SPEED) is None
        assert tracker.observe(BELT, SPEED, 2.95) is None

    def test_dropping_one_unit_leaves_another_untouched(self):
        tracker = _tracker()
        tracker.record_commanded(BELT, SPEED, 3.0, origin="operator")
        tracker.record_commanded(OTHER_BELT, SPEED, 4.0, origin="operator")

        tracker.drop([(BELT, SPEED)])

        assert tracker.commanded_for(BELT, SPEED) is None
        assert tracker.commanded_for(OTHER_BELT, SPEED) is not None
