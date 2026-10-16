"""Offline tests for Factory's state machine: starting -> live/slow/failed/stopped.

No live store here, and the graph lookup is monkeypatched. Most ChildHandles wrap a MagicMock
standing in for a subprocess.Popen. The PLC spawn wait's tests run real child processes, a
stand-in script or the real PLC against a free port, because the wait's subject is a real pipe
and a real process that has to be gone afterwards.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import demo.transferunits.launcher as launcher
from demo.transferunits.launcher import ChildHandle, Factory


def make_factory() -> Factory:
    """A Factory with no real store connection -- __init__ is bypassed."""
    factory = Factory.__new__(Factory)
    factory.children = []
    factory._db = MagicMock()
    factory._store_url = "http://store.invalid:7200"
    return factory


def make_child(kind: str, unit_index, state: str = "starting", alive: bool = True) -> ChildHandle:
    proc = MagicMock()
    proc.poll.return_value = None if alive else 1
    proc.wait.return_value = 0  # a drained pipe ends with a clean exit unless a test says not
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
    """Regression pin: each unit's middleware must be spawned before its own PLC.

    ``_spawn_plc`` blocks reading its process's stdout until the panel announces itself, and
    the panel announces itself only once the PLC's own (retrying) broker connection succeeds.
    That broker does not exist until this unit's middleware brings it up. Spawning the PLC first would have the launcher block forever on a
    broker nothing was ever asked to start.
    """

    def test_middleware_is_spawned_before_its_units_plc(self, monkeypatch):
        factory = make_factory()
        order = []

        def fake_spawn_middleware(n):
            order.append(("middleware", n))
            return make_child("middleware", n)

        def fake_spawn_plc(n, *, middleware=None):
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

    def test_each_plc_wait_watches_its_own_units_middleware(self, monkeypatch):
        """The middleware runs its unit's broker, so its PLC's wait is handed that one."""
        factory = make_factory()
        watched = {}

        monkeypatch.setattr(launcher, "probe_and_seed", lambda units, force=False: None)
        monkeypatch.setattr(launcher, "_spawn_middleware", lambda n: make_child("middleware", n))

        def fake_spawn_plc(n, *, middleware=None):
            watched[n] = middleware
            return make_child("plc", n)

        monkeypatch.setattr(launcher, "_spawn_plc", fake_spawn_plc)
        monkeypatch.setattr(launcher, "_spawn_controller", lambda: make_child("controller", None))
        monkeypatch.setattr(launcher.threading, "Thread", lambda *a, **k: MagicMock())

        factory.start(units=2)

        assert {n: (m.kind, m.unit_index) for n, m in watched.items()} == {
            1: ("middleware", 1),
            2: ("middleware", 2),
        }


class TestStateSnapshot:
    """This class tests Factory.state_snapshot's shape: graph/launcher/controller/unit,
    where each unit now carries its own broker -- there is no
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
        """The broker is a thread inside its unit's middleware process, so its display state is exactly that middleware's -- no separate probe."""
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
    """This class tests that _drain_output echoes a child's address lines to stdout."""

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


class TestFailedChildOutputReachesTheConsole:
    """A child that exits abnormally has its buffered output echoed past the URL filter.

    The console echoes only lines that carry an address, a rule for IDE port forwarding. That
    rule stays, and a healthy run echoes what it did before. But a child that exits with a
    non-zero code while nobody stops it has its whole buffer echoed, so a traceback -- such as
    the one a missing broker package leaves -- is on the console, not only on the status page.
    """

    def test_an_abnormal_exit_echoes_every_buffered_line(self, capsys):
        factory = make_factory()
        child = make_child("middleware", 1)
        child.proc.wait.return_value = 1
        child.proc.stdout = iter(
            [
                "Traceback (most recent call last):\n",
                "RuntimeError: The MQTT broker at 127.0.0.1:18831 did not start: "
                "ModuleNotFoundError: No module named 'amqtt'\n",
            ]
        )
        factory.children = [child]

        factory._drain_output(child)

        out = capsys.readouterr().out
        assert "middleware-1" in out
        assert "exited with code 1" in out
        assert "Traceback (most recent call last):" in out
        assert "No module named 'amqtt'" in out
        assert child.ended.is_set()

    def test_a_clean_exit_echoes_nothing_more(self, capsys):
        factory = make_factory()
        child = make_child("middleware", 1)
        child.proc.stdout = iter(["some ordinary log line, no address here\n"])
        factory.children = [child]

        factory._drain_output(child)

        assert capsys.readouterr().out == ""
        assert child.ended.is_set()

    def test_a_child_the_launcher_stops_echoes_nothing_more(self, capsys):
        """A stop is not a failure, even when the stopped child ends with a non-zero code."""
        factory = make_factory()
        child = make_child("middleware", 1)
        child.stopping = True
        child.proc.wait.return_value = -15
        child.proc.stdout = iter(["some ordinary log line, no address here\n"])
        factory.children = [child]

        factory._drain_output(child)

        assert capsys.readouterr().out == ""

    def test_a_child_that_ctrl_c_ended_echoes_nothing_more(self, capsys):
        """Ctrl+C reaches every child on POSIX, and one still starting up dies by the signal."""
        factory = make_factory()
        child = make_child("middleware", 1)
        child.proc.wait.return_value = -signal.SIGINT
        child.proc.stdout = iter(["KeyboardInterrupt\n"])
        factory.children = [child]

        factory._drain_output(child)

        assert capsys.readouterr().out == ""
        assert child.ended.is_set()


_REAL_POPEN = subprocess.Popen


def _run_instead(script: str, spawned: list):
    """A Popen stand-in that runs ``script`` in a real child process instead of the PLC.

    The stand-in dials nothing and binds nothing, so no test here depends on the factory's
    fixed ports. Each real process is recorded in ``spawned`` so a test can check it is gone.
    """

    def fake_popen(cmdline, **kwargs):
        proc = _REAL_POPEN([sys.executable, "-c", script], **kwargs)
        spawned.append(proc)
        return proc

    return fake_popen


_STALLS = (
    "import time\n"
    "print('broker at 127.0.0.1:1 not ready yet', flush=True)\n"
    "print('still retrying', flush=True)\n"
    "time.sleep(60)\n"
)

_EXITS = (
    "import sys\n"
    "print('Traceback (most recent call last):', flush=True)\n"
    "print('OSError: no panel today', flush=True)\n"
    "sys.exit(3)\n"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestPlcSpawnWait:
    """The PLC spawn wait ends: at its panel line, at its exit, at its timeout, or when the
    unit's middleware, whose process runs the unit's broker, has ended.

    Before, the wait had no timeout, so a broker that never listened held the launcher forever
    and ``Factory running`` never printed.
    """

    def test_an_expired_wait_fails_with_the_childs_buffered_output(self, monkeypatch):
        spawned: list = []
        monkeypatch.setattr(subprocess, "Popen", _run_instead(_STALLS, spawned))

        started = time.monotonic()
        with pytest.raises(RuntimeError) as raised:
            launcher._spawn_plc(1, timeout=1.0)

        message = str(raised.value)
        assert "plc-1" in message
        assert "1 s" in message
        assert "broker at 127.0.0.1:1 not ready yet" in message
        assert "still retrying" in message
        assert time.monotonic() - started < 1.0 + launcher.STOP_TIMEOUT_SECONDS + 2.0
        assert spawned[0].poll() is not None, "the stalled PLC must not outlive the failure"

    def test_the_wait_ends_early_when_the_units_middleware_has_ended(self, monkeypatch):
        """A missing or silent broker kills the unit's middleware within seconds, and with it
        the broker; the PLC can never connect after that, so the wait does not sit out its
        timeout."""
        spawned: list = []
        monkeypatch.setattr(subprocess, "Popen", _run_instead(_STALLS, spawned))
        middleware = make_child("middleware", 1, alive=False)
        middleware.output.extend(
            [
                "Traceback (most recent call last):",
                "RuntimeError: The MQTT broker at 127.0.0.1:18831 did not start: "
                "ModuleNotFoundError: No module named 'amqtt'",
            ]
        )
        middleware.ended.set()

        started = time.monotonic()
        with pytest.raises(RuntimeError) as raised:
            launcher._spawn_plc(1, timeout=60.0, middleware=middleware)

        message = str(raised.value)
        assert "middleware-1" in message
        assert "No module named 'amqtt'" in message
        assert time.monotonic() - started < 10.0, "the wait must not sit out its timeout"
        assert spawned[0].poll() is not None, "the waiting PLC must not outlive the failure"

    def test_an_expired_wait_carries_a_still_running_middlewares_output(self, monkeypatch):
        """A stall is likely the middleware's own, and the stop that follows ends it silently."""
        spawned: list = []
        monkeypatch.setattr(subprocess, "Popen", _run_instead(_STALLS, spawned))
        middleware = make_child("middleware", 1)
        middleware.output.append("still reading the interface from the graph")

        with pytest.raises(RuntimeError) as raised:
            launcher._spawn_plc(1, timeout=1.0, middleware=middleware)

        message = str(raised.value)
        assert "middleware-1, whose process runs this unit's broker, is still running." in message
        assert "still reading the interface from the graph" in message

    def test_a_plc_that_exits_without_its_panel_is_failed_and_echoed(self, monkeypatch, capsys):
        spawned: list = []
        monkeypatch.setattr(subprocess, "Popen", _run_instead(_EXITS, spawned))

        handle = launcher._spawn_plc(1, timeout=30.0)

        assert handle.state == "failed"
        out = capsys.readouterr().out
        assert "exited with code 3" in out
        assert "OSError: no panel today" in out

    def test_the_real_plc_against_a_port_nobody_listens_on_is_bounded(self, monkeypatch):
        """The hang path itself: the real PLC retries a refused connection forever, and the
        wait ends it. Its broker port is a free one, not the unit's fixed port, which another
        run may hold."""
        port = _free_port()
        monkeypatch.setattr(launcher.seed, "broker_port", lambda n: port)

        with pytest.raises(RuntimeError) as raised:
            launcher._spawn_plc(1, timeout=3.0)

        assert "plc-1" in str(raised.value)
        assert "3 s" in str(raised.value)


class TestLauncherEntryPoint:
    """A failed start ends the launcher before its readiness line, and stops what it spawned."""

    def test_a_failed_start_never_prints_factory_running(self, monkeypatch, capsys):
        import demo.transferunits.__main__ as entry

        stopped = []

        class FailingFactory:
            def start(self, units, force=False):
                raise RuntimeError("plc-1 cannot reach its broker")

            def stop_all(self):
                stopped.append(True)

        def must_not_serve(*args, **kwargs):
            raise AssertionError("a failed start must not serve the index page")

        monkeypatch.setattr(entry, "Factory", FailingFactory)
        monkeypatch.setattr(entry.uvicorn, "run", must_not_serve)
        monkeypatch.setattr(sys, "argv", ["launcher", "--units", "1"])

        with pytest.raises(RuntimeError, match="cannot reach its broker"):
            entry.main()

        assert "Factory running" not in capsys.readouterr().out
        assert stopped == [True]


_REPO_ROOT = Path(__file__).resolve().parents[1]


class TestABrokerThatNeverListensFailsTheFactory:
    """The chain end to end, offline, with the broker package present.

    A stand-in for the unit's middleware calls the demo's real transport hook, with a broker
    start that never listens, on a free port nobody listens on; the real middleware reaches
    the same hook from its constructor after reading the graph, which this tier cannot do. The
    real PLC dials that port. The hook raises, the stand-in exits, its output reaches the
    console, and the PLC wait ends within seconds instead of holding the launcher forever.
    """

    def test_a_broker_that_never_listens_fails_the_factory_within_seconds(
        self, monkeypatch, capsys
    ):
        port = _free_port()
        stand_in = (
            "import asyncio\n"
            "import demo.transferunits.middleware as mw\n"
            "async def never_listens(host, port, ready):\n"
            "    await asyncio.sleep(30)\n"
            "mw._serve_broker = never_listens\n"
            "mw.BROKER_READY_TIMEOUT_SECONDS = 0.5\n"
            f"mw.ensure_transport('127.0.0.1', {port})\n"
            "print('Middleware running on http://127.0.0.1:1/', flush=True)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", stand_in],
            cwd=_REPO_ROOT,
            env=launcher._strip_store_env(dict(os.environ)),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        middleware = ChildHandle(
            proc=proc, pid=proc.pid, kind="middleware", unit_index=1, cmdline="stand-in"
        )
        factory = make_factory()
        factory.children = [middleware]
        threading.Thread(target=factory._drain_output, args=(middleware,), daemon=True).start()
        monkeypatch.setattr(launcher.seed, "broker_port", lambda n: port)

        started = time.monotonic()
        try:
            with pytest.raises(RuntimeError) as raised:
                launcher._spawn_plc(1, middleware=middleware)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

        assert time.monotonic() - started < 30.0, "the wait must not sit out its 60 s"
        expected = f"The MQTT broker at 127.0.0.1:{port} is not listening after 0.5 s."
        assert "middleware-1" in str(raised.value)
        assert expected in str(raised.value)
        out = capsys.readouterr().out
        assert "middleware-1 exited with code 1" in out
        assert expected in out
        assert "Middleware running" not in out
