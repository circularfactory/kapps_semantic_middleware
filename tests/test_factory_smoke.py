"""Boot the whole factory and re-check the four claims only a run can show (#93 item 4).

#79 and #88 each closed with acceptance boxes that were verified live, by hand, and recorded
in a commit message:

1. ``python -m demo.transferunits --units 2`` runs end to end with no broker running beforehand.
2. Two brokers listen, on 18831 and 18832. Nothing listens on 1883 because of this demo.
3. A unit's PLC, middleware and broker all die together, and stopping one unit leaves the
   other unit's MQTT traffic untouched.
4. The ``/activity`` feed shows real lines during a run -- at minimum an outbound setpoint.

Nothing in the repository re-checked any of them. Every other test in this suite runs one
process, and this map's real breakage has only ever been visible *between* processes: a broker
that never came up, a feed switched off everywhere, a stop that took a sibling's transport with
it. Those are facts about six processes and two sockets, and a test that builds a middleware
in-process cannot hold any of them down.

So this file runs the factory the way the acceptance boxes are written -- ``--units 2``, the real
launcher, the real graph, real brokers -- and asserts what a person would look for on screen.

It is slow, it needs a reachable GraphDB, it **seeds the repository the ``GRAPHDB_*`` variables
point at**, and it needs the launcher's fixed port free. Marked ``smoke`` and deselected by
default for exactly those reasons:

    python -m pytest tests/test_factory_smoke.py -m smoke
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, List, Sequence, Tuple

import aiomqtt
import httpx
import pytest

from conftest import requires_graphdb

from demo.transferunits import seed
from demo.transferunits.__main__ import LAUNCHER_HOST, LAUNCHER_PORT
from demo.transferunits.middleware import _listening

# `live` is stated here rather than inferred: conftest.py derives that marker from the
# `graphdb` fixture, and this file needs the credentials -- which it hands to six child
# processes -- without ever needing the client object itself.
pytestmark = [requires_graphdb, pytest.mark.live, pytest.mark.smoke]

UNITS = 2
"""The acceptance box's own command is ``--units 2``, so this is not a knob."""

STOPPED_UNIT = 1
SURVIVING_UNIT = 2
"""Which unit gets killed, and which has to be unharmed by it.

Not interchangeable. The control station's view selects even-indexed units, so at this scale
unit 2 is the only one the board can drive -- which makes it both the unit a setpoint can be
sent to and the unit whose traffic has to survive unit 1's death."""

SETPOINT = 1.75
"""A speed the seed never writes and no belt ever settles on by itself, so this value appearing
in the feed cannot be an echo of the initial state or of a ramp passing through."""

SECOND_SETPOINT = 2.5
"""The value the loop test drives, distinct from ``SETPOINT`` so that a board already showing
the first one cannot pass the second test without the belt having moved again."""

EXPECTED_BROKER_PORTS = {STOPPED_UNIT: 18831, SURVIVING_UNIT: 18832}
"""#79's box names two literal numbers: "Two brokers listen, on 18831 and 18832."

Written out rather than read off ``seed.broker_port``, because the decision being pinned is
*which* numbers. ``18830 + n`` was chosen over the obvious ``1883 + n`` because 1900 is SSDP/UPnP
and is live on most Linux desktops, so a large enough factory would collide with it and present
the collision as a broker fault (ADR 0030 as amended). A test that recomputed the port from the
same function the seed uses would agree with any renumbering, including that one."""

READY_TIMEOUT_SECONDS = 180.0
"""How long the launcher gets to print its readiness line. It clears the repository, loads the
shared ontologies, seeds two units, and spawns five processes before it prints anything, and
every one of those steps is a round trip to a GraphDB that may not be on this machine."""

LIVE_TIMEOUT_SECONDS = 60.0
"""How long every participant then gets to reach ``live``. A middleware and the control station
turn live only once their ``svc:address`` is in the graph and the launcher's watch thread has
seen it, and that thread polls once a second."""

MQTT_TIMEOUT_SECONDS = 15.0
"""How long a unit's own broker gets to deliver one publish. The PLC publishes its four topics
every 0.5 s, so this is not a measurement -- it is a bound loose enough that a slow machine
cannot make a live belt read as a silent one."""

FEED_TIMEOUT_SECONDS = 30.0
"""How long the activity feed gets to report a setpoint. The stream replays its whole buffer to
a new reader before streaming anything new, so a record logged before the test connected still
arrives; this covers the PUT's own trip through persistence and out to the write leg."""

LOOP_TIMEOUT_SECONDS = 60.0
"""How long the return half gets, from an accepted command to the unit's middleware observing
the belt at it. Dominated by #83's ramp, which is distance over rate -- 2.5 m/s is about 2.5 s
at ``DEFAULT_RAMP_RATE`` -- plus the PLC's 0.5 s republish. ``test_lap_measurement.py`` measures
this properly and tabulates it; this is a bound around it, not a measurement."""

STOP_TIMEOUT_SECONDS = 20.0
"""How long a stopped unit gets to actually be gone. ``Factory.stop_unit`` already waits on its
children before returning, so this covers reaping and socket teardown, not the kill itself."""

POLL_INTERVAL_SECONDS = 0.25


def _await(predicate: Callable[[], bool], timeout: float, message: str) -> None:
    """Poll until the predicate holds, or fail with ``message``.

    The synchronous twin of ``test_southbound_echo_integration.py``'s ``_await_true``: nothing
    in this file runs under pytest-asyncio, because the things being waited for are other
    processes rather than tasks on this loop.
    """
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        time.sleep(POLL_INTERVAL_SECONDS)


def _url(address: str, path: str) -> str:
    """Join a path onto an address that came from the graph or from a pipe.

    The two disagree: a middleware registers ``http://host:port`` (``middleware.py``'s
    ``self.address``) and the launcher records a panel as ``http://host:port/``. Joining by
    concatenation therefore produces ``http://host:41295api/state`` for one of them, which
    fails as a DNS error and reads as a dead process.
    """
    return f"{address.rstrip('/')}/{path}"


def _first(candidates: List[Any], message: str) -> Any:
    """The first thing in a list, or a failure that says what was actually there.

    Every lookup in this file runs against a live snapshot of six processes, so "it was not
    there" is an ordinary outcome, and a bare ``IndexError`` at some line number is the least
    useful way to report it.
    """
    assert candidates, message
    return candidates[0]


def _lagging(snapshot: dict) -> List[str]:
    """Every participant in the snapshot that is not ``live``, named and with its state.

    Shared by the fixture's wait and the first test below: the wait needs the predicate and
    the failure needs the list, and deriving them separately is how the two drift apart.
    """
    behind = []
    controller = snapshot.get("controller", {})
    if controller.get("state") != "live":
        behind.append(f"control station is {controller.get('state')}")
    for unit in snapshot.get("unit", []):
        for kind in ("middleware", "plc"):
            state = unit.get(kind, {}).get("state")
            if state != "live":
                behind.append(f"unit {unit.get('index')}'s {kind} is {state}")
    return behind


def _pid_alive(pid: int) -> bool:
    """Whether a process still exists. Signal 0 checks, it does not kill.

    The launcher reaps its own children (``_terminate_and_wait`` waits on every pid it
    signals), so a stopped child leaves no zombie behind for this to read as alive.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, and owned by somebody else -- which cannot happen to our own grandchildren,
        # but "cannot signal it" is never evidence that it is gone.
        return True
    return True


def _traffic_flows(unit_index: int, timeout: float) -> bool:
    """Whether this unit's PLC is still publishing on this unit's own broker.

    Subscribes to the topic the graph was seeded with, via the same minter
    (``seed._mqtt_topic``) the seed used, so the test cannot end up quietly subscribed to a
    topic the scheme no longer produces.

    This is the only honest reading of "that unit's MQTT traffic is untouched". A listening
    socket says the broker is up; it says nothing about whether the device on the other side
    of it still talks, and ADR 0029's claim is about the unit, not about the port.
    """
    topic = seed._mqtt_topic(unit_index, "ConveyorBelt", "left", "speed")

    async def _first_publish() -> bool:
        async with aiomqtt.Client(
            hostname=seed.MQTT_BROKER_IP, port=seed.broker_port(unit_index)
        ) as client:
            await client.subscribe(topic)
            async for _message in client.messages:
                return True
        return False

    async def _bounded() -> bool:
        # The bound lives here rather than inside the iteration: `async for` on a broker that
        # has gone silent never comes back at all, so a deadline checked per message is a
        # deadline that is never checked.
        try:
            return await asyncio.wait_for(_first_publish(), timeout)
        except (asyncio.TimeoutError, aiomqtt.MqttError, OSError):
            return False

    return asyncio.run(_bounded())


def _feed_search(
    address: str, fragments: Sequence[str], timeout: float
) -> Tuple[bool, List[str]]:
    """Read a middleware's activity feed until one line carries every fragment.

    Returns the verdict *and* every message read, because "the feed said nothing at all" and
    "the feed was busy but never carried this line" are different failures of #88's box and a
    bare ``False`` cannot tell them apart.

    The stream is endless by design, so it is read against a deadline rather than to its end,
    and a read timeout is the ordinary way that ends -- not an error.
    """
    seen: List[str] = []
    deadline = time.monotonic() + timeout

    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
        try:
            with client.stream("GET", _url(address, "activity/stream")) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    # Anything else is an SSE keepalive comment or the blank line that
                    # terminates an event.
                    if line.startswith("data: "):
                        message = json.loads(line[len("data: ") :]).get("message", "")
                        seen.append(message)
                        if all(fragment in message for fragment in fragments):
                            return True, seen
                    if time.monotonic() >= deadline:
                        break
        except httpx.ReadTimeout:
            pass

    return False, seen


@dataclass(frozen=True)
class FactoryHandle:
    """One booted factory, as the tests see it."""

    proc: subprocess.Popen
    """The launcher. Its pid is the sixth process."""

    lines: List[str]
    """Everything the launcher has said, for a failure to quote."""

    port_1883_before_boot: bool
    """Whether anything answered on 1883 before any of this started."""

    launcher_url: str

    def state(self) -> dict:
        """The launcher's own snapshot of every participant."""
        response = httpx.get(_url(self.launcher_url, "api/state"), timeout=10.0)
        response.raise_for_status()
        return response.json()

    def stop_unit(self, index: int) -> dict:
        """Stop one unit, and return the snapshot that results from it."""
        response = httpx.post(_url(self.launcher_url, f"api/stop/{index}"), timeout=60.0)
        response.raise_for_status()
        return response.json()


@pytest.fixture(scope="module")
def factory() -> Iterator[FactoryHandle]:
    """Boot the factory once for this whole file, then take it down.

    The wait at the end of this fixture *is* #79's first box: a factory that never comes up
    fails here, quoting what the launcher said, before any test body runs.

    One boot for the file rather than one per test, because a boot costs a repository wipe and
    a re-seed. That makes the order of the tests below load-bearing, and the last class says so.
    """
    launcher_url = f"http://{LAUNCHER_HOST}:{LAUNCHER_PORT}/"

    if _listening(LAUNCHER_HOST, LAUNCHER_PORT):
        pytest.skip(
            f"Something already answers on {LAUNCHER_HOST}:{LAUNCHER_PORT}, the launcher's "
            "fixed port. A stale factory or another server has to go first -- this test "
            "cannot mean anything against one it did not start."
        )

    # #79's box says the factory runs "with no broker running beforehand", so the absence has
    # to be established here. Afterwards a listening port cannot say who bound it, and the
    # fixture's own teardown names the way that goes wrong: an orphaned PLC from an earlier
    # run keeps a broker port bound.
    for index, port in EXPECTED_BROKER_PORTS.items():
        if _listening(seed.MQTT_BROKER_IP, port):
            pytest.skip(
                f"Something already answers on {port}, unit {index}'s broker port. This test "
                "cannot show a middleware bringing its own broker up against a broker that "
                "was already there."
            )

    # 1883 is different: it is not ours, and on a machine that legitimately runs its own
    # broker there the claim "nothing listens on 1883 *because of this factory*" is only
    # provable as a before-and-after.
    port_1883_before_boot = _listening(seed.MQTT_BROKER_IP, 1883)

    # --force because the graph may still carry a live factory from a previous run, and
    # start_new_session because teardown wants a process group to fall back on: the launcher
    # spawns five children of its own, and a launcher killed mid-spawn would orphan them.
    proc = subprocess.Popen(
        [sys.executable, "-m", "demo.transferunits", "--units", str(UNITS), "--force"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(Path(__file__).resolve().parents[1]),
        env=os.environ.copy(),
        start_new_session=True,
    )

    # Drained continuously, and from a thread, for two reasons: the launcher echoes every
    # child's address line and would block on a full pipe otherwise, and a boot that fails
    # has to be able to quote itself.
    lines: List[str] = []

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))

    threading.Thread(target=_drain, daemon=True).start()

    def _tail() -> str:
        return "\n".join(lines[-30:])

    def _ready() -> bool:
        if proc.poll() is not None:
            raise AssertionError(
                f"The launcher exited with code {proc.returncode} before it came up. "
                f"Its last words:\n{_tail()}"
            )
        return any("Factory running. Index page at" in line for line in lines)

    handle = FactoryHandle(
        proc=proc,
        lines=lines,
        port_1883_before_boot=port_1883_before_boot,
        launcher_url=launcher_url,
    )

    try:
        _await(
            _ready,
            READY_TIMEOUT_SECONDS,
            f"The launcher never came up within {READY_TIMEOUT_SECONDS}s. Its last "
            f"words:\n{_tail()}",
        )

        def _every_participant_live() -> bool:
            # Through the handle the tests use, rather than a second fetch written out here:
            # a wait that reads the snapshot its own way is a wait that can pass while the
            # tests' reading of it fails.
            try:
                return _lagging(handle.state()) == []
            except httpx.HTTPError:
                return False

        _await(
            _every_participant_live,
            LIVE_TIMEOUT_SECONDS,
            f"Not every participant reached `live` within {LIVE_TIMEOUT_SECONDS}s. The "
            f"launcher's last words:\n{_tail()}",
        )

        yield handle
    finally:
        # Stop the children through the launcher first, so every middleware deregisters its
        # service from the graph while its PLC still answers (ADR 0029's teardown order). A
        # factory killed outright leaves stale services behind, and the next run of it
        # refuses to start on them.
        try:
            httpx.post(_url(launcher_url, "api/stop"), timeout=60.0)
        except httpx.HTTPError:
            pass

        # Then the launcher itself. uvicorn turns SIGTERM into a clean shutdown, which is
        # what runs `__main__.py`'s own `finally: factory.stop_all()`.
        try:
            proc.terminate()
            proc.wait(timeout=30.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass

        # Only then the net -- and unconditionally, which is the correction. This used to be
        # guarded by `if proc.poll() is None`, so it fired only while the *launcher* was
        # still alive. Orphans arise in exactly the opposite case: the launcher exits
        # cleanly and its five children, in their own session, do not. Measured after a run
        # that skipped the `/api/stop` above -- the launcher was gone, the guard was
        # therefore false, and three children kept unit 1's broker port bound. That port is
        # also conftest.py's MQTT_TEST_PORT, so twelve unrelated tests in the *non-live*
        # tier then failed with "Broker startup failed".
        #
        # Signalling an already-reaped group raises ProcessLookupError, which is the normal
        # path when everything did stop, and is caught.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class TestTheFactoryComesUp:
    """This class tests #79's first two boxes: the factory runs end to end with no broker
    running beforehand, and each unit ends up with its own broker and no shared one."""

    def test_every_participant_is_live_and_addressable(self, factory: FactoryHandle) -> None:
        """Six processes, each one live, each one reachable, each one its own.

        The pid set is what makes this "six processes" rather than "six boxes on a page": the
        launcher, the control station, and a middleware and a PLC per unit are separate
        operating-system processes (ADR 0029), and a factory that quietly served two of them from
        one process would still render identically.
        """
        snapshot = factory.state()

        behind = _lagging(snapshot)
        assert behind == [], f"the factory came up incomplete: {'; '.join(behind)}"
        assert len(snapshot["unit"]) == UNITS, (
            f"the launcher reports {len(snapshot['unit'])} unit(s), not the {UNITS} it seeded"
        )

        pids = {factory.proc.pid, snapshot["controller"]["pid"]}
        addresses = [snapshot["controller"]["address"]]
        for unit in snapshot["unit"]:
            for kind in ("middleware", "plc"):
                pids.add(unit[kind]["pid"])
                addresses.append(unit[kind]["address"])

        assert None not in addresses, (
            f"a participant came up live with no address of its own: {addresses}"
        )
        assert len(pids) == 2 + 2 * UNITS, (
            f"the factory is not {2 + 2 * UNITS} separate processes; its pids are {pids}"
        )

    def test_each_unit_brought_up_its_own_broker(self, factory: FactoryHandle) -> None:
        """Both of #79's ports answer now, and the fixture established that neither did before.

        Nothing in this file starts a broker, and neither does the launcher -- #79 took
        ``_start_broker`` out of it. Each unit's middleware brings its own up on a daemon
        thread the first time it registers an MQTT connector (ADR 0029 as amended, ADR 0034),
        so two ports answering, from an absence the fixture checked, is that whole mechanism
        seen from outside.
        """
        for index, port in EXPECTED_BROKER_PORTS.items():
            assert _listening(seed.MQTT_BROKER_IP, port), (
                f"unit {index} is live, but nothing answers on {port} -- so its middleware "
                "never brought its broker up, and the unit is talking to something else"
            )
            assert seed.broker_port(index) == port, (
                f"unit {index}'s parameters were seeded against port "
                f"{seed.broker_port(index)}, not the {port} #79 chose. 1883 + n is the "
                "renumbering ADR 0030 rejected: 1900 is SSDP/UPnP and is live on most Linux "
                "desktops, where the collision presents as a broker fault."
            )

    def test_the_factory_brings_up_nothing_on_1883(self, factory: FactoryHandle) -> None:
        """1883 is exactly as it was before the factory started.

        Asserted as a change rather than as an empty port on purpose. The box says "nothing
        listens on 1883 *because of this demo*", and a developer machine may well run its own
        broker there; a bare "nothing answers on 1883" would fail on that machine while
        proving nothing on any other.
        """
        assert _listening(seed.MQTT_BROKER_IP, 1883) == factory.port_1883_before_boot, (
            "1883 changed state across the factory's boot. It was "
            f"{'busy' if factory.port_1883_before_boot else 'free'} before it started: no "
            "part of this factory may touch that port (ADR 0030 as amended)."
        )


@dataclass(frozen=True)
class DrivenBelt:
    """One belt, commanded from the control station, and the addresses that reach it again."""

    station: str
    """The control station's address -- where the board's own routes live."""

    unit_iri: str
    holder_iri: str
    field_id: str
    """What the board needs back to identify the same parameter on a later call."""

    label: str
    """How this belt's speed names itself in a feed line -- ``ConnectionMetadata.label``'s
    ``"<holder fragment> <property fragment>"``, derived from what the board reported rather
    than guessed. Both directions of this one parameter carry it, so it identifies *which*
    belt a line is about; the ``<-`` / ``->`` marker is what says which way the value went."""

    middleware: str
    """The unit's own middleware -- the process whose feed carries both directions."""


def _drive_a_belt(factory: FactoryHandle, value: float) -> DrivenBelt:
    """Command one belt of the surviving unit from the control station, as a person would.

    Shared by the two tests below, which assert different things about the same act: that the
    command reaches the device's topic (#88's box), and that the device's own reading comes
    back to the screen the command was given on (milestone 1's own sentence). Driving twice
    from one helper keeps them independent -- neither reads the other's leftovers -- while the
    lookup, which is the fiddly half, is written once.
    """
    snapshot = factory.state()
    station = snapshot["controller"]["address"]

    # The board refuses a hand-driven set while the algorithm runs (#82), and says so with a
    # 409 rather than by writing anyway.
    paused = httpx.post(
        _url(station, "api/algorithm/pause"), json={"paused": True}, timeout=10.0
    )
    paused.raise_for_status()

    board = httpx.get(_url(station, "api/state"), timeout=30.0)
    board.raise_for_status()
    units_on_board = board.json()["units"]

    unit_iri = str(seed._mint_transfer_unit_iri(SURVIVING_UNIT))
    unit = _first(
        [u for u in units_on_board if u["resource_iri"] == unit_iri],
        f"unit {SURVIVING_UNIT} is not on the board; it holds "
        f"{[u['resource_iri'] for u in units_on_board]}",
    )

    speed = _first(
        [p for p in unit["parameters"] if p["field_id"] == seed.TU_HAS_CONVEYOR_SPEED.lined],
        f"no conveyor speed among unit {SURVIVING_UNIT}'s parameters: "
        f"{[p['field_id'] for p in unit['parameters']]}",
    )

    # ConveyorBelt2_left. The board reports which belt it is offering, and a feed line names
    # that belt exactly as `ConnectionMetadata.label` builds it -- the holder's fragment, a
    # space, the property's fragment -- so the expected line is derived rather than guessed.
    holder = speed["holder_iri"]
    local_name = holder.rsplit("#", 1)[-1]
    label = f"{local_name} {seed.TU_HAS_CONVEYOR_SPEED.fragment}"

    driven = httpx.post(
        _url(station, "api/set"),
        json={
            "resource_iri": unit_iri,
            "holder_iri": holder,
            "field_id": speed["field_id"],
            "value": value,
        },
        timeout=30.0,
    )
    assert driven.status_code == 200, (
        f"the board refused to drive {local_name}: {driven.status_code} {driven.text}"
    )
    assert driven.json().get("ok") is True, (
        f"the board accepted the write but it did not reach the unit: {driven.json()}"
    )

    return DrivenBelt(
        station=station,
        unit_iri=unit_iri,
        holder_iri=holder,
        field_id=speed["field_id"],
        label=label,
        middleware=_first(
            [
                u["middleware"]["address"]
                for u in snapshot["unit"]
                if u["index"] == SURVIVING_UNIT
            ],
            f"unit {SURVIVING_UNIT} has no middleware address in the launcher's snapshot",
        ),
    )


class TestTheFeedCarriesRealLines:
    """This class tests #88's last box: the activity feed shows real lines during a run.

    #88 shipped with the feed switched off everywhere in the factory -- a built and tested
    feature dark in the only place it was built for -- and the box that closed it was checked
    by opening the page. A page that renders is not the property; a page that carries the line
    a write just produced is.
    """

    def test_an_outbound_setpoint_reaches_that_unit_s_feed(
        self, factory: FactoryHandle
    ) -> None:
        """Drive one belt from the control station, and find that command in the unit's feed.

        The path under test spans four processes: the board's PUT, the unit middleware's
        persistence, its MQTT write leg, and the feed that middleware serves. The setpoint
        line is logged unconditionally at INFO (``mqtt_binding.py``: "a setpoint is always
        news"), so its absence is a break in that path rather than a quiet feed.
        """
        belt = _drive_a_belt(factory, SETPOINT)

        # `->` is the outbound marker mqtt_binding writes; the same feed carries `<-` lines
        # for every value the belt publishes back, and the ramp passes through this setpoint on
        # its way there, so the direction is what separates the command from its echo.
        #
        # **The topic is deliberately absent from what this matches.** #76 moved it to DEBUG
        # precisely so it cannot reach this feed, which is served over HTTP -- ADR 0028 strips
        # a broker topic from the datamodel, and a log line one URL along was handing it back.
        # The label pins which belt, and the marker pins the leg on its own: `->` is written in
        # one place, `MQTTParameterFormatter.serialize`, and `<-` in one other. The topic was
        # never what separated them -- this test's earlier probe had already found the pair
        # matching loosely in either direction, because `.../speed` is a prefix of
        # `.../speed_set`.
        #
        # Probed by mutation at the value instead, which is the mutation that means something
        # here: both markers legitimately carry this label in a healthy run, so flipping one in
        # the test asserts the other true fact rather than a false one. Matched against a value
        # nobody drove, both this and the loop test below fail with the message written for
        # them.
        #
        # That the value really travels on the right *topic* is asserted elsewhere in this
        # file, by `_traffic_flows` subscribing to the real broker rather than by reading a log.
        carried, seen = _feed_search(
            belt.middleware,
            (f"{belt.label} -> {SETPOINT}",),
            FEED_TIMEOUT_SECONDS,
        )
        assert carried, (
            f"unit {SURVIVING_UNIT}'s feed never carried the setpoint {SETPOINT} for "
            f"{belt.label}. In {FEED_TIMEOUT_SECONDS}s it carried {len(seen)} line(s): "
            f"{seen[-10:]}"
        )


class TestTheLoopCloses:
    """This class tests the return half of milestone 1's own sentence:

    > *"The loop runs end to end: PLC, MQTT, middleware, graph, controller, REST, back to the
    > PLC."*

    The class above proves a command leaves the control station and reaches the device's
    topic. This one proves the device's *own* reading comes back and is observed -- the belt
    really moved, and its middleware really saw it move.

    **It stops at the unit's middleware, and that is a property of the design rather than a
    gap in the test.** Under ADR 0024's locator pattern a parameter has one value slot, and a
    PUT writes the commanded value straight into it -- at the unit's middleware, and (because
    ``/api/set`` assigns in place before pushing) at the controller too. So the value the board
    renders reads as the setpoint the instant the command is accepted, whatever the belt is
    doing, and "the controller sees the device" is not distinguishable at the board from "the
    controller sees its own command". ``test_lap_measurement.py`` records the same limit and
    times its lap against the device and the inbound publish for exactly this reason.

    The first version of this test did read the board back, and passed in 0.12 s against its
    own write. The inbound log line is the last point on this path where the two are still
    telling apart.
    """

    def test_the_device_s_own_speed_comes_back_and_is_observed(
        self, factory: FactoryHandle
    ) -> None:
        """Command a belt, and wait for the unit's middleware to report observing it there.

        Every hop is in this one wait: the PUT reaches the unit's middleware, its write leg
        publishes to the device's set topic, the PLC ramps toward it (#83's momentum, so the
        belt climbs rather than jumps), it publishes its *actual* speed on its own topic, and
        the unit middleware's read leg observes that and logs it.

        **``<-`` is what makes this the device's reading rather than the command.** That
        marker is written in one place, ``MQTTParameterFormatter.deserialize``, which runs
        only on a message arriving *from* the broker -- so a matching line cannot have been
        produced by the PUT that started this. A second setpoint, distinct from the one the
        class above drove, so a line already in the feed cannot satisfy this either.

        This used to match the belt's **actual** topic as well, and take some care not to
        match the set topic, since ``.../speed`` is a prefix of ``.../speed_set``. #76 took
        the topic out of the feed -- it is southbound metadata, and the feed is HTTP -- and
        the care went with it: the marker identifies the leg on its own, because one function
        writes each. The label pins which belt. See the probe recorded on the test above:
        flipping the marker is not the discriminating mutation here, since both legs carry
        this label in a healthy run; driving a value nobody commanded is, and it fails.
        """
        belt = _drive_a_belt(factory, SECOND_SETPOINT)

        # `2.5` rather than a parsed float: the ramp snaps exactly onto its target on the
        # last step (#83, "float-exact at rest"), so the settled publish reprs as the setpoint
        # itself, while every value on the way there is visibly short of it.
        observed, seen = _feed_search(
            belt.middleware,
            (f"{belt.label} <- {SECOND_SETPOINT}",),
            LOOP_TIMEOUT_SECONDS,
        )
        assert observed, (
            f"unit {SURVIVING_UNIT}'s middleware never observed {belt.label} reach "
            f"{SECOND_SETPOINT}. The command was accepted, so the break is on the way back: "
            f"the publish out, the PLC's ramp, or the read leg. In "
            f"{LOOP_TIMEOUT_SECONDS}s the feed carried {len(seen)} line(s): {seen[-10:]}"
        )


class TestStoppingOneUnit:
    """This class tests #79's third box: a unit's three parts die together, and the other unit
    does not notice.

    It runs last, and the order is load-bearing: it destroys unit 1, and every test above it
    shares this file's one booted factory.
    """

    def test_a_unit_s_processes_and_broker_die_together_and_spare_the_other(
        self, factory: FactoryHandle
    ) -> None:
        """Stop one unit; watch its three parts go and the other unit keep talking.

        One test rather than two, because the "untouched" half is only worth anything against
        a baseline taken before the stop -- a belt that was already silent proves nothing
        about what the stop did to it.
        """
        assert _traffic_flows(SURVIVING_UNIT, MQTT_TIMEOUT_SECONDS), (
            f"unit {SURVIVING_UNIT} was not publishing before unit {STOPPED_UNIT} was "
            "stopped, so this test could not tell a broken sibling from a quiet one"
        )

        snapshot = factory.state()
        doomed = _first(
            [u for u in snapshot["unit"] if u["index"] == STOPPED_UNIT],
            f"unit {STOPPED_UNIT} is not in the launcher's snapshot",
        )
        middleware_pid = doomed["middleware"]["pid"]
        plc_pid = doomed["plc"]["pid"]

        after = factory.stop_unit(STOPPED_UNIT)

        _await(
            lambda: not _pid_alive(middleware_pid),
            STOP_TIMEOUT_SECONDS,
            f"unit {STOPPED_UNIT}'s middleware ({middleware_pid}) outlived the stop",
        )
        _await(
            lambda: not _pid_alive(plc_pid),
            STOP_TIMEOUT_SECONDS,
            f"unit {STOPPED_UNIT}'s PLC ({plc_pid}) outlived the stop",
        )

        # The broker is a daemon thread inside that middleware process, so nothing anywhere
        # was asked to stop it and its port going quiet is the whole of ADR 0029's amendment
        # ("a unit's broker dies with its unit") observed from outside.
        stopped_port = seed.broker_port(STOPPED_UNIT)
        _await(
            lambda: not _listening(seed.MQTT_BROKER_IP, stopped_port),
            STOP_TIMEOUT_SECONDS,
            f"unit {STOPPED_UNIT}'s broker still answers on {stopped_port} after its "
            "middleware died, so it is not the thread ADR 0029 says it is",
        )

        surviving_port = seed.broker_port(SURVIVING_UNIT)
        assert _listening(seed.MQTT_BROKER_IP, surviving_port), (
            f"stopping unit {STOPPED_UNIT} took unit {SURVIVING_UNIT}'s broker with it"
        )
        assert _traffic_flows(SURVIVING_UNIT, MQTT_TIMEOUT_SECONDS), (
            f"unit {SURVIVING_UNIT}'s broker still answers, but its PLC stopped publishing "
            f"when unit {STOPPED_UNIT} was stopped -- the shared point of failure ADR 0029's "
            "amendment names is still there"
        )

        # And the page says all of that, since the page is the only place a person sees it.
        by_index = {u["index"]: u for u in after["unit"]}
        for kind in ("middleware", "plc"):
            assert by_index[STOPPED_UNIT][kind]["state"] == "stopped", (
                f"unit {STOPPED_UNIT}'s {kind} is gone, but the page still shows it "
                f"{by_index[STOPPED_UNIT][kind]['state']}"
            )
            assert by_index[SURVIVING_UNIT][kind]["state"] == "live", (
                f"the page marked unit {SURVIVING_UNIT}'s {kind} "
                f"{by_index[SURVIVING_UNIT][kind]['state']} when its neighbour was stopped"
            )
