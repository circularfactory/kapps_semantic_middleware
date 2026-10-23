"""Launcher machinery for the multi-process TransferUnit factory.

Builds the initial situation (seeds the graph) and spawns every participant
process: one PLC and panel, and one middleware, per unit, plus one control
station. Credentials go only to graph-side children. A PLC process never
receives the store's variables. Teardown is ordered: middleware and the
control station first, so each deregisters while its PLC still answers,
then the PLCs.

No HTTP route lives here -- the ``Factory`` class only
seeds, spawns, tracks and stops. ``index.py`` reads its state to serve the
Launcher's index page.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, NoReturn, Optional, Tuple

from kapps_triplestore_interface import TripleStore
from kapps_semantic_middleware.credentials import DEMO_DATASET, endpoint_for, triplestore_for
from kapps_semantic_middleware.vocabulary import SVC

from . import seed

# Every child is spawned as `python -m <this package>.<module>`, derived rather than written
# out. In this checkout that resolves to `demo.transferunits`; in an installed wheel the same
# code resolves to `kapps_semantic_middleware.demonstrations.transferunits`, because the demo
# ships under the library's namespace rather than claiming a top-level `demo` on PyPI. The
# scenario rule fixes where these files live on disk, not what they are called once installed.
_PACKAGE = __package__ or "demo.transferunits"

SLOW_AFTER_SECONDS = 30.0
WATCH_INTERVAL_SECONDS = 1.0

WATCHDOG_SWEEP_SECONDS = 5.0
"""How often the demo's watchdog sweeps. A killed unit's address is gone from the graph within the
middleware's default staleness threshold plus this interval. The threshold itself is the
middleware's default, which the demo does not override."""

WATCHDOG_READY_MARKER = "Watchdog running"
"""The line the watchdog process prints once its sweep loop is about to run. The watchdog
registers no Service and has no address, so this line is the only sign the launcher has that it
is live."""

PANEL_TIMEOUT_SECONDS = 60.0
"""How long the launcher waits for a PLC's panel line before it fails the factory.

The panel comes up only once the PLC reaches its unit's broker, and that broker comes up only
once the unit's middleware has imported, read its interface from the graph and wired its
connectors. Generous for that reason: it bounds a stall, and it must not fail a healthy factory
on a slow store. The usual failures do not wait for it: a broker that does not start ends its
middleware within seconds, and the wait ends with it."""

PANEL_POLL_SECONDS = 0.1
"""How often the PLC spawn wait looks at its unit's middleware. The panel line itself ends the
wait at once."""

STOP_TIMEOUT_SECONDS = 5.0
"""How long a child gets to stop gracefully before the launcher kills it. A kill skips the
middleware's deregistration."""

GRACEFUL_SHUTDOWN_SECONDS = 3.0
"""How long a child's uvicorn waits for open connections before it cancels them.

uvicorn runs the lifespan shutdown, where a middleware deregisters, only after that wait, and it
waits without a limit by default. So one connection that stays open holds the stop open until
``STOP_TIMEOUT_SECONDS`` passes and the kill comes. The bound leaves the deregistration two
seconds before the kill. The activity stream already ends by itself within its lifetime; the
bound covers every other connection that stays open."""
if sys.platform == "win32":
    _NEW_PROCESS_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    _NEW_PROCESS_GROUP = 0


@dataclass
class ChildHandle:
    """Tracking information, and live state, for one spawned child process."""

    proc: subprocess.Popen
    pid: int
    kind: str  # "plc", "middleware", "controller", or "watchdog"
    unit_index: Optional[int]  # None for the controller and the watchdog
    cmdline: str
    address: Optional[str] = None
    source: Optional[str] = None  # "pipe", "graph", "flag", or "env"; None for the watchdog
    state: str = "starting"  # starting | slow | live | failed | stopped
    started_at: float = field(default_factory=time.time)
    stopping: bool = False
    output: Deque[str] = field(default_factory=lambda: deque(maxlen=20))
    ended: threading.Event = field(default_factory=threading.Event)
    """Set once the child's output is drained to its end and, if it failed, echoed."""


# vendor-check: off -- the deprecated variables, until they go.
_DEPRECATED_STORE_ENV_PREFIXES = ("GRAPHDB_",)
# vendor-check: on

# The prefixes of the store's variables: TRIPLESTORE_*, and their deprecated fallbacks.
_STORE_ENV_PREFIXES = ("TRIPLESTORE_", *_DEPRECATED_STORE_ENV_PREFIXES)


def _strip_store_env(env: dict) -> dict:
    """Return a copy of env without the store's variables, the deprecated ones included."""
    return {k: v for k, v in env.items() if not k.startswith(_STORE_ENV_PREFIXES)}


_PANEL_PORT_RE = re.compile(r":(\d+)/?\s*$")
_ADDRESS_RE = re.compile(r"https?://")
_IDENTITY_COLUMN_WIDTH = len("middleware-10")  # widest identity at the demo's usual scale


def _child_identity(child: ChildHandle) -> str:
    """The short label an echoed line is prefixed with, e.g. "plc-1", "middleware-2"."""
    if child.kind == "controller":
        return "control"
    if child.kind == "watchdog":
        return "watchdog"
    return f"{child.kind}-{child.unit_index}"


def _echo_address_line(child: ChildHandle, line: str) -> None:
    """Echo one address line to the launcher's own stdout, prefixed with the child's
    identity. An IDE forwards ports by scanning terminal output for `http://host:port`
    -- only lines that carry one are echoed here, unmodified and flushed; everything else
    stays only in the child's ring buffer.
    """
    if not _ADDRESS_RE.search(line):
        return
    print(f"  {_child_identity(child):<{_IDENTITY_COLUMN_WIDTH}}{line}", flush=True)


def _buffered_output(child: ChildHandle) -> Tuple[str, List[str]]:
    """A child's buffered lines, and the line that introduces them, for the console or an error."""
    identity = _child_identity(child)
    lines = list(child.output)
    if not lines:
        return f"{identity} printed nothing.", []
    return f"{identity}'s last {len(lines)} line(s) of output:", lines


def _echo_buffered_output(child: ChildHandle, reason: str) -> None:
    """Echo every buffered line of a failed child to the launcher's own stdout.

    The failure path around ``_echo_address_line``'s filter: that rule stays for a healthy run,
    but a child that fails has its last lines on the console, a traceback among them, and not
    only on the index page.
    """
    identity = _child_identity(child)
    header, lines = _buffered_output(child)
    print(f"  {identity} {reason}. {header}", flush=True)
    for line in lines:
        print(f"  {identity:<{_IDENTITY_COLUMN_WIDTH}}{line}", flush=True)


def _finish_drain(child: ChildHandle) -> None:
    """Close a child's drain at the end of its output: echo it if the child failed.

    A failure is an exit with a non-zero code while the launcher was not stopping the child. A
    child the launcher stops echoes nothing more, and neither does a child that Ctrl+C ended:
    on POSIX the terminal sends it to every child, and a child still starting up then ends by
    the signal (code ``-SIGINT``) rather than cleanly. That is the user's stop, not a failure.
    """
    returncode = child.proc.wait()
    if returncode not in (0, -signal.SIGINT) and not child.stopping:
        _echo_buffered_output(child, f"exited with code {returncode}")
    child.ended.set()


def _spawn_plc(
    unit_index: int,
    *,
    timeout: float = PANEL_TIMEOUT_SECONDS,
    middleware: Optional[ChildHandle] = None,
) -> ChildHandle:
    """Spawn a PLC and panel process for one unit.

    Its environment carries no store variable — enforced by construction,
    not by discipline. Its panel address arrives as one line on
    stdout, since a PLC holds no graph credentials to register one itself.
    Blocks until that line arrives or the process exits, so the returned
    handle already carries its outcome — starting a PLC panel is fast, and a
    live index page has nothing useful to show before it either way.

    The pipe itself is drained on a background thread from the moment the
    process spawns, and for the rest of its life -- not read line-by-line on
    this (the caller's) thread. A middleware process once blocked writing
    to a full, unread stdout pipe while a PLC waited on it to come up; nothing
    here reads this PLC's own pipe on this thread, so it cannot fill and
    block this PLC the same way while this function waits for its panel line.
    This is also why the child is not handed to ``_drain_output`` again later:
    this thread already keeps draining it for the whole process lifetime.

    The wait is bounded. The panel comes up only once the PLC reaches its broker, and a
    broker that never listens would otherwise hold the launcher here forever. So the wait
    fails, with the PLC's buffered output, after ``timeout`` seconds, and at once when
    ``middleware``, the unit's middleware whose process runs that broker, has ended, since
    the broker has ended with it. Either way the PLC is stopped before the error is raised.

    Its own broker is this unit's alone: the port is
    ``seed.broker_port(unit_index)``, the same pure function of the index the seed writes
    into the graph, so both readers agree before either process starts.
    """
    cmdline = [
        sys.executable,
        "-m",
        f"{_PACKAGE}.plc",
        "--unit-index",
        str(unit_index),
        "--broker",
        seed.MQTT_BROKER_IP,
        "--broker-port",
        str(seed.broker_port(unit_index)),
        "--panel-port",
        "0",
    ]
    cmdline_str = " ".join(cmdline)
    print(cmdline_str, flush=True)

    env = _strip_store_env(os.environ.copy())
    proc = subprocess.Popen(
        cmdline,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        bufsize=1,
        creationflags=_NEW_PROCESS_GROUP,
    )

    handle = ChildHandle(proc=proc, pid=proc.pid, kind="plc", unit_index=unit_index, cmdline=cmdline_str)

    assert proc.stdout is not None
    panel_seen = threading.Event()

    def _drain_until_panel() -> None:
        # The assert above does not narrow `proc.stdout` inside this nested function.
        for line in proc.stdout:  # type: ignore[union-attr]
            stripped = line.rstrip("\n")
            handle.output.append(stripped)
            _echo_address_line(handle, stripped)
            if not panel_seen.is_set() and "Panel running on http://" in line:
                match = _PANEL_PORT_RE.search(line.strip())
                if match:
                    handle.address = f"http://127.0.0.1:{match.group(1)}/"
                    handle.source = "pipe"
                    handle.state = "live"
                panel_seen.set()
        _finish_drain(handle)
        panel_seen.set()  # the process exited without ever printing the panel line

    threading.Thread(target=_drain_until_panel, daemon=True).start()

    deadline = time.monotonic() + timeout
    while not panel_seen.wait(PANEL_POLL_SECONDS):
        if middleware is not None and middleware.ended.is_set():
            last = middleware.output[-1] if middleware.output else "(none)"
            _fail_plc_wait(
                handle,
                f"{_child_identity(handle)} cannot reach its broker: "
                f"{_child_identity(middleware)}, whose process runs that broker, exited with "
                f"code {middleware.proc.poll()} before the panel came up. Its last line: {last}",
            )
        if time.monotonic() >= deadline:
            _fail_plc_wait(
                handle,
                f"{_child_identity(handle)} did not announce its panel within {timeout:g} s.",
                stalled=middleware,
            )

    if handle.state != "live" and proc.poll() is not None:
        handle.state = "failed"

    return handle


def _fail_plc_wait(
    handle: ChildHandle, reason: str, stalled: Optional[ChildHandle] = None
) -> NoReturn:
    """End a PLC spawn wait that will not succeed: stop the PLC, then fail with its output.

    The PLC is stopped first because a caller that gets the error never gets the handle, so
    nothing else would ever stop it, and it would retry its broker for as long as it lives.

    ``stalled`` is the unit's middleware when the wait timed out while it still runs. Its
    output joins the error, because the stall is likely its own -- a broker it never reached --
    and the launcher's stop that follows would end it without a word on the console.
    """
    handle.stopping = True
    _terminate_and_wait([handle], STOP_TIMEOUT_SECONDS)
    handle.ended.wait(STOP_TIMEOUT_SECONDS)
    handle.state = "failed"
    header, lines = _buffered_output(handle)
    message = [reason, header, *lines]
    if stalled is not None and not stalled.ended.is_set():
        stalled_header, stalled_lines = _buffered_output(stalled)
        message += [
            f"{_child_identity(stalled)}, whose process runs this unit's broker, is still running.",
            stalled_header,
            *stalled_lines,
        ]
    raise RuntimeError("\n".join(message))


def _spawn_middleware(unit_index: int, dataset: str) -> ChildHandle:
    """Spawn a middleware process for one unit, inheriting the store's variables from the launcher."""
    cmdline = [
        sys.executable,
        "-m",
        f"{_PACKAGE}.middleware",
        "--unit-index",
        str(unit_index),
        "--port",
        "0",
        # Named, not inherited: the child ignores TRIPLESTORE_DATASET, so a
        # shared default is the only other thing that could keep parent and child in one
        # graph -- and a default is exactly what drifts when one side changes.
        "--dataset",
        dataset,
    ]
    cmdline_str = " ".join(cmdline)
    print(cmdline_str, flush=True)

    proc = subprocess.Popen(
        cmdline,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
        text=True,
        bufsize=1,
        creationflags=_NEW_PROCESS_GROUP,
    )

    return ChildHandle(proc=proc, pid=proc.pid, kind="middleware", unit_index=unit_index, cmdline=cmdline_str)


def _spawn_controller(dataset: str) -> ChildHandle:
    """Spawn the control station process, inheriting the store's variables from the launcher."""
    cmdline = [
        sys.executable,
        "-m",
        f"{_PACKAGE}.control_station",
        "--port",
        "0",
        "--dataset",
        dataset,
    ]
    cmdline_str = " ".join(cmdline)
    print(cmdline_str, flush=True)

    proc = subprocess.Popen(
        cmdline,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
        text=True,
        bufsize=1,
        creationflags=_NEW_PROCESS_GROUP,
    )

    return ChildHandle(proc=proc, pid=proc.pid, kind="controller", unit_index=None, cmdline=cmdline_str)


def _spawn_watchdog(dataset: str) -> ChildHandle:
    """Spawn the watchdog process: a watchdog-mode middleware on the demo dataset.

    It inherits the store's variables, names the dataset like every graph-side child, and runs
    the middleware's default staleness threshold with the demo's sweep interval. It registers
    no Service, so it has no address.
    """
    cmdline = [
        sys.executable,
        "-m",
        f"{_PACKAGE}.watchdog",
        "--dataset",
        dataset,
        "--sweep-interval",
        f"{WATCHDOG_SWEEP_SECONDS:g}",
    ]
    cmdline_str = " ".join(cmdline)
    print(cmdline_str, flush=True)

    proc = subprocess.Popen(
        cmdline,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
        text=True,
        bufsize=1,
        creationflags=_NEW_PROCESS_GROUP,
    )

    return ChildHandle(proc=proc, pid=proc.pid, kind="watchdog", unit_index=None, cmdline=cmdline_str)


def _check_service_address(db: TripleStore, resource_iri: str) -> Optional[str]:
    """One-shot check of the graph for the svc:hasAddress of a resource. No wait, no loop."""
    sparql = f"""
    SELECT ?addr WHERE {{
        ?svc <{SVC.isServiceOf}> <{resource_iri}> .
        ?svc <{SVC.hasAddress}> ?addr .
    }}
    """
    result = db.query(sparql, convert_bindings=True)
    bindings = result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    return str(bindings[0]["addr"]) if bindings else None


def probe_and_seed(units: int, force: bool = False, dataset: str = DEMO_DATASET) -> None:
    """Probe for a live factory and seed if the graph is clear, or if you force it.

    Args:
        units: Number of units to seed.
        force: Clear and seed even if a live factory exists.
        dataset: The dataset to probe and seed.

    Raises:
        RuntimeError: A live factory exists, and force is False.
    """
    db = triplestore_for(dataset)
    live_services = seed.factory_is_live(db)

    if live_services and not force:
        raise RuntimeError(
            f"A live factory is running: {len(live_services)} service(s) carry a fresh "
            "heartbeat. Pass --force to clear it anyway."
        )

    from kapps_ogm import OGM

    ogm = OGM(db=db)
    seed.seed_factory(db, ogm, units)
    print(f"Seeded factory with {units} unit(s).", flush=True)


def _request_stop(proc: subprocess.Popen) -> None:
    """Send the platform's graceful stop signal to a child process.

    On POSIX this is SIGTERM via ``proc.terminate()``. On Windows this is
    ``CTRL_BREAK_EVENT`` via ``proc.send_signal()`` — not ``proc.terminate()``,
    because on Windows ``terminate()`` calls ``TerminateProcess``, which is
    forced and gives the child no chance to run shutdown handlers. By contrast,
    ``CTRL_BREAK_EVENT`` is delivered as SIGBREAK, which uvicorn handles
    gracefully, allowing deregistration before exit. Each child is
    spawned in its own process group (``_NEW_PROCESS_GROUP``) so the event
    reaches that child alone and never the launcher. This mirrors the "graceful
    then forced" shape that SIGTERM provides on POSIX.
    """
    if sys.platform == "win32":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.terminate()


def _terminate_and_wait(handles: List[ChildHandle], timeout: float) -> List[str]:
    """Graceful-stop every handle, wait up to timeout, then force-kill and report stragglers.

    ``_request_stop`` sends the platform's graceful stop (SIGTERM / CTRL_BREAK) to every handle
    first, so they shut down in parallel; the wait shares one ``timeout`` deadline across them.
    A child still alive past the deadline is force-killed (``Popen.kill``: SIGKILL /
    TerminateProcess) and reported. The whole path uses the cross-platform ``subprocess.Popen``
    API, so it holds on Windows, where the old ``signal.SIGKILL`` did not exist.
    """
    for h in handles:
        try:
            _request_stop(h.proc)
        except (ProcessLookupError, OSError):
            continue

    deadline = time.time() + timeout
    for h in handles:
        remaining = max(0.0, deadline - time.time())
        try:
            h.proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass

    stragglers = []
    for h in handles:
        if h.proc.poll() is None:
            try:
                h.proc.kill()
                h.proc.wait(timeout=timeout)
            except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                pass
            stragglers.append(f"{h.kind}[unit={h.unit_index}]" if h.unit_index else h.kind)
    return stragglers


def stop_factory(children: List[ChildHandle], timeout: float = STOP_TIMEOUT_SECONDS) -> None:
    """Stop every child, middleware, control station and watchdog first, then the PLCs."""
    graph_children = [c for c in children if c.kind in ("middleware", "controller", "watchdog")]
    plc_children = [c for c in children if c.kind == "plc"]

    stragglers = _terminate_and_wait(graph_children, timeout)
    stragglers.extend(_terminate_and_wait(plc_children, timeout))

    if stragglers:
        print(f"Killed, past their timeout: {', '.join(stragglers)}", flush=True)


class Factory:
    """Owns every spawned child process and its live state.

    ``start`` seeds and spawns, once, at launcher startup. After that, a background
    thread promotes each graph-registered child from ``starting`` to ``live``/``slow``/
    ``failed`` as its address appears (or doesn't) in the graph, and one reader thread
    per child drains its output into a bounded ring buffer. ``index.py`` reads
    ``state_snapshot`` and ``output`` to serve the page; nothing here renders HTML or
    speaks HTTP.
    """

    def __init__(self, dataset: str = DEMO_DATASET) -> None:
        self.dataset = dataset
        self.children: List[ChildHandle] = []
        self._db = triplestore_for(dataset)
        # The store's URL, for the index page. The endpoint holds no secret.
        self._store_url = endpoint_for(dataset).url

    def start(self, units: int, force: bool = False, watchdog: bool = True) -> None:
        """Seed the graph and spawn every participant. Blocking; call once at startup.

        No broker is started here: each unit's own middleware brings
        its own up, the moment its wiring registers its first MQTT connector.

        The middleware is spawned before its unit's PLC, and that order is load-bearing.
        ``_spawn_middleware`` returns as soon as the process exists, without waiting on it,
        so that unit's broker gets a head start coming up in its own process while
        ``_spawn_plc`` -- which blocks until its panel announces itself, and the panel
        announces itself only once the PLC's own retrying connection attempt succeeds --
        does its own spawn and read. Spawning the PLC first would have the launcher block on
        a connection to a broker that nothing has even been asked to bring up yet.

        The watchdog (one watchdog-mode middleware, unless ``watchdog`` is False) is spawned last,
        after the units and the control station.

        Raises ``RuntimeError`` when a PLC's wait fails: its broker never listened, because
        the unit's middleware ended or the wait timed out. The children spawned so far stay in
        ``children``, so the caller's ``stop_all`` stops them.
        """
        probe_and_seed(units, force=force, dataset=self.dataset)

        for n in range(1, units + 1):
            middleware = _spawn_middleware(n, self.dataset)
            self.children.append(middleware)
            threading.Thread(target=self._drain_output, args=(middleware,), daemon=True).start()
            self.children.append(_spawn_plc(n, middleware=middleware))
        self.children.append(_spawn_controller(self.dataset))
        if watchdog:
            self.children.append(_spawn_watchdog(self.dataset))

        for child in self.children:
            if child.kind not in ("middleware", "plc"):
                threading.Thread(target=self._drain_output, args=(child,), daemon=True).start()

        threading.Thread(target=self._watch, daemon=True).start()

    def _drain_output(self, child: ChildHandle) -> None:
        """Keep reading a child's stdout after spawn, so /api/output stays current.

        Also echoes any address line to the launcher's own stdout -- this is where
        the middleware and control station's address arrives, since neither blocks spawn
        the way a PLC's panel line does in _spawn_plc. Never called for a PLC: it drains
        its own pipe on its own thread, for the same reason middleware and controller
        need it here -- so nothing is reading the same stream from two places at once.

        At the end of the output, a child that failed has its whole buffer echoed
        (``_finish_drain``), so its traceback is on the console too.
        """
        assert child.proc.stdout is not None
        for line in child.proc.stdout:
            stripped = line.rstrip("\n")
            child.output.append(stripped)
            _echo_address_line(child, stripped)
            if (
                child.kind == "watchdog"
                and WATCHDOG_READY_MARKER in stripped
                and child.state in ("starting", "slow")
            ):
                child.state = "live"
        _finish_drain(child)

    def _resource_iri(self, child: ChildHandle) -> Optional[str]:
        if child.kind == "middleware" and child.unit_index is not None:
            return str(seed._mint_transfer_unit_iri(child.unit_index))
        if child.kind == "controller":
            return str(seed.CONTROL_STATION)
        return None

    def _watch(self) -> None:
        """Background loop: repeat _watch_once until the process exits."""
        while True:
            time.sleep(WATCH_INTERVAL_SECONDS)
            self._watch_once()

    def _watch_once(self) -> None:
        """One pass: promote each graph-registered child starting -> live/slow/failed."""
        for child in list(self.children):
            if child.stopping or child.kind == "plc" or child.state in ("live", "failed", "stopped"):
                continue
            if child.proc.poll() is not None:
                child.state = "failed"
                continue
            if child.kind == "watchdog":
                # No Service and no address: the drain promotes it on its ready line.
                if time.time() - child.started_at > SLOW_AFTER_SECONDS:
                    child.state = "slow"
                continue
            resource_iri = self._resource_iri(child)
            address = _check_service_address(self._db, resource_iri) if resource_iri else None
            if address:
                child.address = address
                child.source = "graph"
                child.state = "live"
            elif time.time() - child.started_at > SLOW_AFTER_SECONDS:
                child.state = "slow"

    def _unit_children(self, index: int) -> List[ChildHandle]:
        return [c for c in self.children if c.unit_index == index]

    def stop_unit(self, index: int) -> None:
        """Stop one unit: SIGTERM its middleware first, then its PLC."""
        children = self._unit_children(index)
        for c in children:
            c.stopping = True

        graph_children = [c for c in children if c.kind == "middleware"]
        plc_children = [c for c in children if c.kind == "plc"]
        _terminate_and_wait(graph_children, timeout=STOP_TIMEOUT_SECONDS)
        _terminate_and_wait(plc_children, timeout=STOP_TIMEOUT_SECONDS)

        for c in children:
            c.state = "stopped"
            c.address = None
            c.source = None

    def stop_all(self) -> None:
        """Stop the whole factory: every middleware and the control station, then every PLC.

        Nothing to stop here beyond the children themselves: each unit's broker is a daemon
        thread inside that unit's own middleware process, so it dies
        the moment ``stop_factory`` kills that process.
        """
        children = list(self.children)
        for c in children:
            c.stopping = True

        stop_factory(children)

        for c in children:
            c.state = "stopped"
            c.address = None
            c.source = None

    def output(self, index: int, part: str) -> List[str]:
        """The last 20 lines of output of one process. index=0 selects the controller or the watchdog."""
        for c in self.children:
            if part in ("controller", "watchdog") and c.kind == part:
                return list(c.output)
            if c.unit_index == index and c.kind == part:
                return list(c.output)
        return []

    def state_snapshot(self, launcher_address: str) -> dict:
        """Serialize every participant's live state for the index page.

        The ``watchdog`` key is present only when the factory runs one. It has no address.

        No broker is tracked here as its own participant: each unit's
        broker is a thread inside that unit's own middleware process, so its display state
        simply mirrors that middleware's -- it is exactly as live, and it dies at exactly the
        same moment.
        """

        def entry(child: ChildHandle) -> dict:
            return {
                "state": child.state,
                "address": child.address,
                "source": child.source,
                "pid": child.pid,
            }

        units: dict = {}
        controller_entry = None
        watchdog_entry = None
        for child in self.children:
            if child.kind == "controller":
                controller_entry = entry(child)
                continue
            if child.kind == "watchdog":
                watchdog_entry = entry(child)
                continue
            bucket = units.setdefault(child.unit_index, {"index": child.unit_index})
            bucket[child.kind] = entry(child)

        for index, bucket in units.items():
            middleware_state = bucket.get("middleware", {}).get("state", "starting")
            bucket["broker"] = {
                "state": middleware_state,
                "address": f"{seed.MQTT_BROKER_IP}:{seed.broker_port(index)}",
                "source": "flag",
            }

        snapshot: dict = {
            "graph": {"state": "live", "address": self._store_url, "source": "env"},
            "launcher": {"state": "live", "address": launcher_address, "source": "flag"},
            "controller": controller_entry
            or {"state": "starting", "address": None, "source": None, "pid": None},
            "unit": [units[i] for i in sorted(units)],
        }
        if watchdog_entry is not None:
            snapshot["watchdog"] = watchdog_entry
        return snapshot
