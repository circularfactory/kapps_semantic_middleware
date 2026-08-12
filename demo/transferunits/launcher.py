"""Launcher machinery for the multi-process TransferUnit factory.

Builds the initial situation (seeds the graph) and spawns every participant
process: one PLC and panel, and one middleware, per unit, plus one control
station. Credentials go only to graph-side children. A PLC process never
receives GRAPHDB_* (ADR 0029). Teardown is ordered: middleware and the
control station first, so each deregisters while its PLC still answers,
then the PLCs.

No HTTP route lives here (ADR 0029 / #72) -- the ``Factory`` class only
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
from typing import Deque, List, Optional

from kapps_triplestore_interface import GraphDB
from kapps_semantic_middleware.credentials import DEMO_REPOSITORY, graphdb_for
from kapps_semantic_middleware.vocabulary import SVC

from . import seed

# Every child is spawned as `python -m <this package>.<module>`, derived rather than written
# out. In this checkout that resolves to `demo.transferunits`; in an installed wheel the same
# code resolves to `kapps_semantic_middleware.demonstrations.transferunits`, because the demo
# ships under the library's namespace rather than claiming a top-level `demo` on PyPI. Root
# ADR 0004 fixes where these files live on disk, not what they are called once installed.
_PACKAGE = __package__ or "demo.transferunits"

SLOW_AFTER_SECONDS = 30.0
WATCH_INTERVAL_SECONDS = 1.0
if sys.platform == "win32":
    _NEW_PROCESS_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    _NEW_PROCESS_GROUP = 0


@dataclass
class ChildHandle:
    """Tracking information, and live state, for one spawned child process."""

    proc: subprocess.Popen
    pid: int
    kind: str  # "plc", "middleware", or "controller"
    unit_index: Optional[int]  # None for the controller
    cmdline: str
    address: Optional[str] = None
    source: Optional[str] = None  # "pipe", "graph", "flag", or "env"
    state: str = "starting"  # starting | slow | live | failed | stopped
    started_at: float = field(default_factory=time.time)
    stopping: bool = False
    output: Deque[str] = field(default_factory=lambda: deque(maxlen=20))


def _strip_graphdb_env(env: dict) -> dict:
    """Return a copy of env without GRAPHDB_* variables."""
    return {k: v for k, v in env.items() if not k.startswith("GRAPHDB_")}


_PANEL_PORT_RE = re.compile(r":(\d+)/?\s*$")
_ADDRESS_RE = re.compile(r"https?://")
_IDENTITY_COLUMN_WIDTH = len("middleware-10")  # widest identity at the demo's usual scale


def _child_identity(child: ChildHandle) -> str:
    """The short label an echoed line is prefixed with, e.g. "plc-1", "middleware-2"."""
    if child.kind == "controller":
        return "control"
    return f"{child.kind}-{child.unit_index}"


def _echo_address_line(child: ChildHandle, line: str) -> None:
    """Echo one address line to the launcher's own stdout, prefixed with the child's
    identity (#85). An IDE forwards ports by scanning terminal output for `http://host:port`
    -- only lines that carry one are echoed here, unmodified and flushed; everything else
    stays only in the child's ring buffer.
    """
    if not _ADDRESS_RE.search(line):
        return
    print(f"  {_child_identity(child):<{_IDENTITY_COLUMN_WIDTH}}{line}", flush=True)


def _spawn_plc(unit_index: int) -> ChildHandle:
    """Spawn a PLC and panel process for one unit.

    Its environment carries no GRAPHDB_* variable — enforced by construction,
    not by discipline (ADR 0029). Its panel address arrives as one line on
    stdout, since a PLC holds no graph credentials to register one itself.
    Blocks until that line arrives or the process exits, so the returned
    handle already carries its outcome — starting a PLC panel is fast, and a
    live index page has nothing useful to show before it either way.

    Its own broker (ADR 0029 as amended) is this unit's alone: the port is
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

    env = _strip_graphdb_env(os.environ.copy())
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
    panel_port = None
    for line in proc.stdout:
        stripped = line.rstrip("\n")
        handle.output.append(stripped)
        _echo_address_line(handle, stripped)
        if "Panel running on http://" in line:
            match = _PANEL_PORT_RE.search(line.strip())
            if match:
                panel_port = int(match.group(1))
            break

    if panel_port:
        handle.address = f"http://127.0.0.1:{panel_port}/"
        handle.source = "pipe"
        handle.state = "live"
    elif proc.poll() is not None:
        handle.state = "failed"

    return handle


def _spawn_middleware(unit_index: int) -> ChildHandle:
    """Spawn a middleware process for one unit, inheriting GRAPHDB_* from the launcher."""
    cmdline = [
        sys.executable,
        "-m",
        f"{_PACKAGE}.middleware",
        "--unit-index",
        str(unit_index),
        "--port",
        "0",
        # Named, not inherited: the child ignores GRAPHDB_REPOSITORY (issue #146), so a
        # shared default is the only other thing that could keep parent and child in one
        # graph -- and a default is exactly what drifts when one side changes.
        "--repository",
        DEMO_REPOSITORY,
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


def _spawn_controller() -> ChildHandle:
    """Spawn the control station process, inheriting GRAPHDB_* from the launcher."""
    cmdline = [sys.executable, "-m", f"{_PACKAGE}.control_station", "--port", "0"]
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


def _check_service_address(db: GraphDB, resource_iri: str) -> Optional[str]:
    """One-shot check of the graph for the svc:address of a resource. No wait, no loop."""
    sparql = f"""
    SELECT ?addr WHERE {{
        ?svc <{SVC.isServiceOf}> <{resource_iri}> .
        ?svc <{SVC.address}> ?addr .
    }}
    """
    result = db.query(sparql, convert_bindings=True)
    bindings = result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    return str(bindings[0]["addr"]) if bindings else None


def probe_and_seed(units: int, force: bool = False) -> None:
    """Probe for a live factory and seed if the graph is clear, or if you force it.

    Args:
        units: Number of units to seed.
        force: Clear and seed even if a live factory exists.

    Raises:
        RuntimeError: A live factory exists, and force is False.
    """
    db = graphdb_for(DEMO_REPOSITORY)
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
    gracefully, allowing deregistration before exit (ADR 0029). Each child is
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


def stop_factory(children: List[ChildHandle], timeout: float = 5.0) -> None:
    """Stop every child, middleware and the control station first, then the PLCs."""
    graph_children = [c for c in children if c.kind in ("middleware", "controller")]
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

    def __init__(self) -> None:
        self.children: List[ChildHandle] = []
        self._db = graphdb_for(DEMO_REPOSITORY)

    def start(self, units: int, force: bool = False) -> None:
        """Seed the graph and spawn every participant. Blocking; call once at startup.

        No broker is started here (ADR 0029 as amended): each unit's own middleware brings
        its own up, the moment its wiring registers its first MQTT connector.

        The middleware is spawned before its unit's PLC, and that order is load-bearing.
        ``_spawn_middleware`` returns as soon as the process exists, without waiting on it,
        so that unit's broker gets a head start coming up in its own process while
        ``_spawn_plc`` -- which blocks until its panel announces itself, and the panel
        announces itself only once the PLC's own retrying connection attempt succeeds --
        does its own spawn and read. Spawning the PLC first would have the launcher block on
        a connection to a broker that nothing has even been asked to bring up yet.
        """
        probe_and_seed(units, force=force)

        for n in range(1, units + 1):
            self.children.append(_spawn_middleware(n))
            self.children.append(_spawn_plc(n))
        self.children.append(_spawn_controller())

        for child in self.children:
            threading.Thread(target=self._drain_output, args=(child,), daemon=True).start()

        threading.Thread(target=self._watch, daemon=True).start()

    def _drain_output(self, child: ChildHandle) -> None:
        """Keep reading a child's stdout after spawn, so /api/output stays current.

        Also echoes any address line to the launcher's own stdout (#85) -- this is where
        the middleware and control station's address arrives, since neither blocks spawn
        the way a PLC's panel line does in _spawn_plc.
        """
        assert child.proc.stdout is not None
        for line in child.proc.stdout:
            stripped = line.rstrip("\n")
            child.output.append(stripped)
            _echo_address_line(child, stripped)

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
        """Stop one unit: SIGTERM its middleware first, then its PLC (ADR 0029)."""
        children = self._unit_children(index)
        for c in children:
            c.stopping = True

        graph_children = [c for c in children if c.kind == "middleware"]
        plc_children = [c for c in children if c.kind == "plc"]
        _terminate_and_wait(graph_children, timeout=5.0)
        _terminate_and_wait(plc_children, timeout=5.0)

        for c in children:
            c.state = "stopped"
            c.address = None
            c.source = None

    def stop_all(self) -> None:
        """Stop the whole factory: every middleware and the control station, then every PLC.

        Nothing to stop here beyond the children themselves: each unit's broker is a daemon
        thread inside that unit's own middleware process (ADR 0029 as amended), so it dies
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
        """The last 20 lines of output of one process. index=0 selects the controller."""
        for c in self.children:
            if part == "controller" and c.kind == "controller":
                return list(c.output)
            if c.unit_index == index and c.kind == part:
                return list(c.output)
        return []

    def state_snapshot(self, launcher_address: str) -> dict:
        """Serialize every participant's live state for the index page.

        No broker is tracked here as its own participant (ADR 0029 as amended): each unit's
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
        for child in self.children:
            if child.kind == "controller":
                controller_entry = entry(child)
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

        return {
            "graph": {"state": "live", "address": os.environ.get("GRAPHDB_URL", ""), "source": "env"},
            "launcher": {"state": "live", "address": launcher_address, "source": "flag"},
            "controller": controller_entry
            or {"state": "starting", "address": None, "source": None, "pid": None},
            "unit": [units[i] for i in sorted(units)],
        }
