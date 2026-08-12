"""The panel's convergence verdict: `diverged` means stopped converging, not unequal.

#81 redefined the word and amended #31 to say so; the panel kept the older reading
(`abs(cmd - speed) > 1e-9`) until #93 item 3. Under #83's ramp the two values are unequal
during every set by design, so the old rule painted a healthy belt as a fault the whole way
to its setpoint.

No broker, no graph, no HTTP: `ConvergenceTracker` is a state machine over observations, so
these run with no fixtures and an injected clock — the same shape, and the same reason, as
`test_write_status.py` for the controller's `WriteTracker` (#82).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo.transferunits.plc.panel import ConvergenceTracker  # noqa: E402


class _Clock:
    """A hand-wound monotonic clock, so a belt can be aged without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tracker(still_seconds: float = 6.0):
    clock = _Clock()
    return ConvergenceTracker(still_seconds=still_seconds, clock=clock), clock


class TestARampingBeltIsHealthy:
    def test_a_belt_still_moving_never_reads_diverged(self):
        """The regression #93 item 3 names: a belt ramping toward its setpoint is healthy
        however long the ramp takes, because it is still closing the gap."""
        tracker, clock = _tracker(still_seconds=6.0)

        for speed in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5):
            status = tracker.status("left", speed, 3.0)
            assert status == "converging", f"a moving belt at {speed} read {status}"
            clock.advance(5.0)  # each step well short of still_seconds, but it moved

    def test_reaching_the_setpoint_settles(self):
        tracker, _ = _tracker()
        assert tracker.status("left", 1.0, 3.0) == "converging"
        assert tracker.status("left", 3.0, 3.0) == "settled"

    def test_a_belt_nobody_commanded_gets_no_verdict(self):
        """No setpoint means nothing to compare against, so the row shows no badge at all
        rather than inventing one."""
        tracker, _ = _tracker()
        assert tracker.status("left", 1.4, None) is None


class TestStoppingShortIsTheFault:
    def test_a_belt_that_stops_short_reads_diverged(self):
        tracker, clock = _tracker(still_seconds=6.0)

        assert tracker.status("left", 2.0, 3.0) == "converging"
        clock.advance(6.0)
        assert tracker.status("left", 2.0, 3.0) == "diverged"

    def test_movement_resets_the_clock(self):
        """Only movement resets it. A belt that crawls stays healthy; one that stops does
        not become healthy again by being looked at."""
        tracker, clock = _tracker(still_seconds=6.0)

        tracker.status("left", 2.0, 3.0)
        clock.advance(5.0)
        assert tracker.status("left", 2.5, 3.0) == "converging"  # it moved
        clock.advance(5.0)
        assert tracker.status("left", 2.5, 3.0) == "converging"  # not yet still enough
        clock.advance(1.0)
        assert tracker.status("left", 2.5, 3.0) == "diverged"

    def test_polling_harder_does_not_hasten_divergence(self):
        """#82's property, held here too: a poll is a browser asking. Counting polls would
        let two open tabs call a belt stuck in half the time."""
        tracker, clock = _tracker(still_seconds=6.0)

        for _ in range(50):
            assert tracker.status("left", 2.0, 3.0) == "converging"

        clock.advance(6.0)
        assert tracker.status("left", 2.0, 3.0) == "diverged"


class TestBeltsAreJudgedIndependently:
    def test_one_stuck_belt_does_not_condemn_its_sibling(self):
        tracker, clock = _tracker(still_seconds=6.0)

        tracker.status("left", 2.0, 3.0)
        tracker.status("right", 1.0, 3.0)
        clock.advance(6.0)

        assert tracker.status("left", 2.0, 3.0) == "diverged"
        assert tracker.status("right", 1.5, 3.0) == "converging"
