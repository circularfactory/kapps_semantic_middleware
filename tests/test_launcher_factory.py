"""Offline tests for Factory's state machine (#72): starting -> live/slow/failed/stopped.

No live GraphDB and no real subprocess here -- every ChildHandle wraps a MagicMock standing
in for a subprocess.Popen, and the graph lookup is monkeypatched.
"""

from __future__ import annotations

import subprocess
import time
from unittest.mock import MagicMock

import demo.transferunits.launcher as launcher
from demo.transferunits.launcher import ChildHandle, Factory


def make_factory() -> Factory:
    """A Factory with no real GraphDB connection -- __init__ is bypassed."""
    factory = Factory.__new__(Factory)
    factory.children = []
    factory._db = MagicMock()
    return factory


def make_child(kind: str, unit_index, state: str = "starting", alive: bool = True) -> ChildHandle:
    proc = MagicMock()
    proc.poll.return_value = None if alive else 1
    return ChildHandle(proc=proc, pid=4242, kind=kind, unit_index=unit_index, cmdline="x", state=state)


class TestWatchOnce:
    """This class tests Factory._watch_once's starting -> live/slow/failed promotion."""

    def test_middleware_promotes_to_live_when_address_appears(self, monkeypatch):
        factory = make_factory()
        child = make_child("middleware", 1)
        factory.children = [child]

        monkeypatch.setattr(launcher, "_check_service_address", lambda db, iri: "http://127.0.0.1:9001/")

        factory._watch_once()

        assert child.state == "live"
        assert child.address == "http://127.0.0.1:9001/"
        assert child.source == "graph"

    def test_controller_stays_starting_with_no_address_yet(self, monkeypatch):
        factory = make_factory()
        child = make_child("controller", None)
        factory.children = [child]

        monkeypatch.setattr(launcher, "_check_service_address", lambda db, iri: None)

        factory._watch_once()

        assert child.state == "starting"
        assert child.address is None

    def test_promotes_to_slow_after_the_threshold(self, monkeypatch):
        factory = make_factory()
        child = make_child("middleware", 1)
        child.started_at = time.time() - launcher.SLOW_AFTER_SECONDS - 1

        factory.children = [child]
        monkeypatch.setattr(launcher, "_check_service_address", lambda db, iri: None)

        factory._watch_once()

        assert child.state == "slow"

    def test_process_exit_before_address_is_failed(self, monkeypatch):
        factory = make_factory()
        child = make_child("middleware", 1, alive=False)
        factory.children = [child]

        monkeypatch.setattr(launcher, "_check_service_address", lambda db, iri: None)

        factory._watch_once()

        assert child.state == "failed"

    def test_plc_is_never_touched_by_watch(self, monkeypatch):
        """PLC state is resolved synchronously at spawn time (_spawn_plc), not by _watch_once."""
        factory = make_factory()
        child = make_child("plc", 1, state="starting")
        factory.children = [child]

        def boom(db, iri):
            raise AssertionError("the graph should never be queried for a PLC")

        monkeypatch.setattr(launcher, "_check_service_address", boom)

        factory._watch_once()

        assert child.state == "starting"

    def test_stopping_child_is_skipped(self, monkeypatch):
        factory = make_factory()
        child = make_child("middleware", 1, alive=False)
        child.stopping = True
        factory.children = [child]

        monkeypatch.setattr(launcher, "_check_service_address", lambda db, iri: None)

        factory._watch_once()

        assert child.state == "starting"


class TestStopUnit:
    """This class tests Factory.stop_unit and stop_all mark the right children stopped."""

    def test_stop_unit_marks_only_that_units_children(self, monkeypatch):
        factory = make_factory()
        unit1_mw = make_child("middleware", 1, state="live")
        unit1_mw.address = "http://127.0.0.1:9001/"
        unit1_plc = make_child("plc", 1, state="live")
        unit2_mw = make_child("middleware", 2, state="live")
        factory.children = [unit1_mw, unit1_plc, unit2_mw]

        monkeypatch.setattr(launcher, "_terminate_and_wait", lambda handles, timeout: [])

        factory.stop_unit(1)

        assert unit1_mw.state == "stopped" and unit1_mw.address is None
        assert unit1_plc.state == "stopped"
        assert unit2_mw.state == "live", "stopping unit 1 must not touch unit 2"

    def test_stop_all_marks_every_child_stopped(self, monkeypatch):
        factory = make_factory()
        children = [make_child("middleware", 1), make_child("plc", 1), make_child("controller", None)]
        factory.children = children

        monkeypatch.setattr(launcher, "stop_factory", lambda children, timeout=5.0: None)

        factory.stop_all()

        assert all(c.state == "stopped" for c in children)


class TestStartSpawnOrder:
    """Regression pin (#79): each unit's middleware must be spawned before its own PLC.

    ``_spawn_plc`` blocks reading its process's stdout until the panel announces itself, and
    the panel announces itself only once the PLC's own (retrying) broker connection succeeds.
    That broker does not exist until this unit's middleware brings it up (ADR 0029 as
    amended, ADR 0034). Spawning the PLC first would have the launcher block forever on a
    broker nothing was ever asked to start.
    """

    def test_middleware_is_spawned_before_its_units_plc(self, monkeypatch):
        factory = make_factory()
        order = []

        def fake_spawn_middleware(n):
            order.append(("middleware", n))
            return make_child("middleware", n)

        def fake_spawn_plc(n):
            order.append(("plc", n))
            return make_child("plc", n)

        def fake_spawn_controller():
            order.append(("controller", None))
            return make_child("controller", None)

        monkeypatch.setattr(launcher, "probe_and_seed", lambda units, force=False: None)
        monkeypatch.setattr(launcher, "_spawn_middleware", fake_spawn_middleware)
        monkeypatch.setattr(launcher, "_spawn_plc", fake_spawn_plc)
        monkeypatch.setattr(launcher, "_spawn_controller", fake_spawn_controller)
        monkeypatch.setattr(launcher.threading, "Thread", lambda *a, **k: MagicMock())

        factory.start(units=2)

        assert order == [
            ("middleware", 1),
            ("plc", 1),
            ("middleware", 2),
            ("plc", 2),
            ("controller", None),
        ]


class TestStateSnapshot:
    """This class tests Factory.state_snapshot's shape: graph/launcher/controller/unit,
    where each unit now carries its own broker (#79, ADR 0029 as amended) -- there is no
    top-level, factory-wide broker entry any more."""

    def test_snapshot_groups_children_by_unit(self):
        factory = make_factory()
        mw1 = make_child("middleware", 1, state="live")
        plc1 = make_child("plc", 1, state="live")
        ctrl = make_child("controller", None, state="live")
        factory.children = [mw1, plc1, ctrl]

        snapshot = factory.state_snapshot("http://127.0.0.1:8080/")

        assert snapshot["launcher"]["address"] == "http://127.0.0.1:8080/"
        assert snapshot["controller"]["state"] == "live"
        assert "broker" not in snapshot
        assert snapshot["unit"] == [
            {
                "index": 1,
                "middleware": {"state": "live", "address": None, "source": None, "pid": 4242},
                "plc": {"state": "live", "address": None, "source": None, "pid": 4242},
                "broker": {"state": "live", "address": "127.0.0.1:18831", "source": "flag"},
            }
        ]

    def test_snapshot_broker_state_mirrors_its_unit_s_middleware(self):
        """The broker is a thread inside its unit's middleware process (ADR 0029 as
        amended), so its display state is exactly that middleware's -- no separate probe."""
        factory = make_factory()
        mw2 = make_child("middleware", 2, state="failed")
        factory.children = [mw2]

        snapshot = factory.state_snapshot("http://127.0.0.1:8080/")

        unit2 = snapshot["unit"][0]
        assert unit2["broker"] == {"state": "failed", "address": "127.0.0.1:18832", "source": "flag"}

    def test_snapshot_with_no_children_has_a_placeholder_controller(self):
        factory = make_factory()

        snapshot = factory.state_snapshot("http://127.0.0.1:8080/")

        assert snapshot["controller"]["state"] == "starting"
        assert snapshot["unit"] == []


class TestOutput:
    """This class tests Factory.output selects the right child, including the controller."""

    def test_output_selects_by_unit_index_and_kind(self):
        factory = make_factory()
        mw1 = make_child("middleware", 1)
        mw1.output.extend(["line one", "line two"])
        factory.children = [mw1]

        assert factory.output(1, "middleware") == ["line one", "line two"]
        assert factory.output(1, "plc") == []

    def test_output_index_zero_selects_the_controller(self):
        factory = make_factory()
        ctrl = make_child("controller", None)
        ctrl.output.append("controller booted")
        factory.children = [ctrl]

        assert factory.output(0, "controller") == ["controller booted"]


class TestTerminateAndWait:
    """This class tests _terminate_and_wait uses the Popen API and enforces graceful-before-forced."""

    def test_graceful_exit_no_kill_needed(self):
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.wait.side_effect = None
        child = ChildHandle(proc=proc, pid=4242, kind="middleware", unit_index=1, cmdline="x")

        stragglers = launcher._terminate_and_wait([child], timeout=5.0)

        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()
        assert stragglers == []

    def test_straggler_is_killed_and_reported(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=5.0)
        child = ChildHandle(proc=proc, pid=4242, kind="middleware", unit_index=1, cmdline="x")

        stragglers = launcher._terminate_and_wait([child], timeout=5.0)

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert stragglers == ["middleware[unit=1]"]

    def test_graceful_before_forced_ordering(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=5.0)
        child = ChildHandle(proc=proc, pid=4242, kind="middleware", unit_index=1, cmdline="x")

        call_order = []
        proc.terminate.side_effect = lambda: call_order.append("terminate")
        proc.kill.side_effect = lambda: call_order.append("kill")

        launcher._terminate_and_wait([child], timeout=5.0)

        assert call_order == ["terminate", "kill"]


class TestRequestStop:
    """This class tests _request_stop dispatches to the correct platform call."""

    def test_posix_branch_calls_terminate(self, monkeypatch):
        proc = MagicMock()
        monkeypatch.setattr(launcher.sys, "platform", "linux")

        launcher._request_stop(proc)

        proc.terminate.assert_called_once()
        proc.send_signal.assert_not_called()

    def test_windows_branch_calls_send_signal_ctrl_break(self, monkeypatch):
        proc = MagicMock()
        monkeypatch.setattr(launcher.sys, "platform", "win32")
        monkeypatch.setattr(launcher.signal, "CTRL_BREAK_EVENT", 999, raising=False)

        launcher._request_stop(proc)

        proc.send_signal.assert_called_once_with(999)
        proc.terminate.assert_not_called()


class TestEchoAddressLines:
    """This class tests that _drain_output echoes a child's address lines to stdout (#85)."""

    def test_address_line_reaches_stdout_prefixed_with_identity(self, capsys):
        factory = make_factory()
        child = make_child("middleware", 1)
        child.proc.stdout = iter(["The middleware runs on http://127.0.0.1:41288/\n"])
        factory.children = [child]

        factory._drain_output(child)

        out = capsys.readouterr().out
        assert "middleware-1" in out
        assert "http://127.0.0.1:41288/" in out

    def test_non_address_line_does_not_reach_stdout(self, capsys):
        factory = make_factory()
        child = make_child("middleware", 1)
        child.proc.stdout = iter(["some ordinary log line, no address here\n"])
        factory.children = [child]

        factory._drain_output(child)

        out = capsys.readouterr().out
        assert out == ""

    def test_address_line_still_lands_in_the_ring_buffer(self, capsys):
        """Echoing to stdout must not replace the existing ring-buffer capture."""
        factory = make_factory()
        child = make_child("middleware", 1)
        child.proc.stdout = iter(["The middleware runs on http://127.0.0.1:41288/\n"])
        factory.children = [child]

        factory._drain_output(child)

        assert list(child.output) == ["The middleware runs on http://127.0.0.1:41288/"]

    def test_controller_identity_is_control_not_controller(self, capsys):
        factory = make_factory()
        child = make_child("controller", None)
        child.proc.stdout = iter(["The control station runs on http://127.0.0.1:41295/\n"])
        factory.children = [child]

        factory._drain_output(child)

        prefix = capsys.readouterr().out.split("http://")[0]
        assert "control" in prefix
        assert "controller" not in prefix

    def test_url_is_not_reformatted(self, capsys):
        """Acceptance: the URL reaches stdout unmodified."""
        factory = make_factory()
        child = make_child("plc", 3)
        line = "Panel running on http://127.0.0.1:54321/\n"
        child.proc.stdout = iter([line])
        factory.children = [child]

        factory._drain_output(child)

        out = capsys.readouterr().out
        assert "http://127.0.0.1:54321/" in out
