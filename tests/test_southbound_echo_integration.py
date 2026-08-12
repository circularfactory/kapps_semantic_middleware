"""#94: nothing reaches a set topic unless something actually commanded it.

A set topic carries **commands**. Under ADR 0024's locator pattern a parameter has one
value slot, so the commanded value and the observed value share it, and a write leg asked
to re-derive its slice at the wrong moment publishes an *observation* onto a **command**
channel. The device reads its own actual speed back as a new setpoint, the ramp finds
itself converged, and the belt freezes short of where it was sent.

Two triggers reach that same wrong moment, and the fan-out
(``PersistedConnector._notify_synced_connectors``) is the common path:

1. **A sibling's device read.** The PLC republishes a barrier. That reaches persistence,
   the fan-out asks *every* write leg on the unit to re-derive, and the left belt's write
   leg publishes its current actual speed to ``speed_set``. #92's origin skip cannot help:
   the origin is the *barrier's* ``ConnectionInfo``, not the speed's.

2. **An external write to a sibling field.** A PUT to the left belt arrives with no origin
   at all, so the fan-out reaches the *right* belt's write leg, which republishes the right
   belt's last observed speed. This is #82's algorithm's ordinary behaviour -- write one
   belt while another is mid-ramp.

These tests spy on the real set topics with a real broker, a real GraphDB and a real
``SemanticMiddleware`` in ``BIDIRECTIONAL`` mode. ``test_northbound_sync_integration.py``
cannot catch this: it asserts each value *reaches* persistence and never that the belt
*stays* there.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection
from kapps_triplestore_interface import IRI
from kapps_ogm import OGM

from kapps_semantic_middleware import Mode, SemanticMiddleware
from kapps_semantic_middleware.vocabulary import INF

from conftest import requires_graphdb  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import examples.seed as seed  # noqa: E402
from demo.transferunits.plc.transfer_unit import (  # noqa: E402
    DEFAULT_RAMP_RATE,
    RAMP_TICK_SECONDS,
    TransferUnit,
)

PORT = 8992
"""Not 8991: `test_northbound_sync_integration.py` holds that one, and two servers racing
for a port produce a bind error that reads like a middleware fault."""

SET_TOPIC_WILDCARD = "TransferUnit1/ConveyorBelt/+/speed_set"


def _start_server(mw):
    import uvicorn

    config = uvicorn.Config(mw.app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


async def _await_true(predicate, timeout, message):
    """Poll with ``asyncio.sleep``. A blocking sleep starves the in-process broker.

    The ``mqtt_broker`` fixture runs ``amqtt`` on *this* event loop, so a ``time.sleep``
    here would deadlock rather than merely slow the test: the server's connectors could
    never finish their handshake against a broker that never gets the loop back.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


async def _route_ending(mw, suffix, timeout=10.0):
    found = {}

    def _has_route():
        for route in mw._parameter_routes:
            if route.endswith(suffix):
                found["route"] = route
                return True
        return False

    await _await_true(
        _has_route, timeout, f"no parameter route ending {suffix!r} in {mw._parameter_routes}"
    )
    return found["route"]


def _speed_route_suffix(belt_iri):
    return f"/{IRI(str(belt_iri)).lined}/{seed.TU_HAS_CONVEYOR_SPEED.lined}"


class SetTopicSpy:
    """Every message that lands on any belt's set topic, with the topic it landed on.

    A plain counter would not distinguish "the belt we commanded" from "its sibling",
    which is the whole difference between the two triggers.
    """

    def __init__(self):
        self.messages: list[tuple[str, float]] = []
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()

    def values_on(self, position: str) -> list[float]:
        suffix = f"/ConveyorBelt/{position}/speed_set"
        return [value for topic, value in self.messages if topic.endswith(suffix)]

    async def _run(self, host: str, port: int):
        import json

        import aiomqtt

        async with aiomqtt.Client(hostname=host, port=port) as client:
            await client.subscribe(SET_TOPIC_WILDCARD)
            self._ready.set()
            async for message in client.messages:
                self.messages.append(
                    (str(message.topic), json.loads(message.payload.decode()))
                )


@contextlib.asynccontextmanager
async def spying_on_set_topics(host: str, port: int):
    spy = SetTopicSpy()
    spy._task = asyncio.create_task(spy._run(host, port))
    try:
        # Subscribe before the caller acts, or an early publish is missed and the test
        # passes for the wrong reason.
        await asyncio.wait_for(spy._ready.wait(), timeout=10.0)
        yield spy
    finally:
        spy._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await spy._task


@pytest_asyncio.fixture
async def running_unit(graphdb, mqtt_broker, unit_scope):
    """A real bidirectional middleware over TransferUnit1, wired to the test broker."""
    host, port = mqtt_broker.split(":")
    seed.seed_scenario3(graphdb, OGM(db=graphdb))
    graphdb.query(
        f"DELETE {{ ?n <{INF.hasMQTTBrokerIP}> ?old }} "
        f'INSERT {{ ?n <{INF.hasMQTTBrokerIP}> "{host}" ; '
        f'    <{INF.hasMQTTBrokerPort}> "{port}"^^<http://www.w3.org/2001/XMLSchema#integer> }} '
        f"WHERE  {{ ?n <{INF.hasMQTTBrokerIP}> ?old }}",
        update=True,
    )

    mw = SemanticMiddleware(
        mode=Mode.RESOURCE,
        resource_iri=seed.TRANSFER_UNIT_1,
        service_class=f"{seed.TU_NS}TransferUnitService",
        ogm=OGM(db=graphdb),
        host="127.0.0.1",
        port=PORT,
        class_scope=unit_scope,
        connector_sync_direction=SyncDirection.BIDIRECTIONAL,
        heartbeat_interval=None,
    )

    server, thread = _start_server(mw)
    try:
        await _await_true(lambda: server.started, 30.0, "server did not start in time")
        yield mw, host, int(port)
    finally:
        server.should_exit = True
        with contextlib.suppress(AssertionError):
            await _await_true(
                lambda: not thread.is_alive(), 20.0, "server thread did not stop in time"
            )


async def _read(url):
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=5.0)
        response.raise_for_status()
        return response.json()


async def _put(url, body):
    async with httpx.AsyncClient() as client:
        response = await client.put(url, json=body, timeout=5.0)
        response.raise_for_status()
        return response


async def _wait_for_value(url, expected, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        [body] = await _read(url)
        if body[INF.hasValue.lined] == [expected]:
            return True
        await asyncio.sleep(0.1)
    return False


@requires_graphdb
@pytest.mark.asyncio
class TestADeviceReadNeverTravelsBackDown:
    """Trigger 1: a sibling's inbound publish must not command anything."""

    async def test_a_full_ramp_publishes_nothing_on_its_own_set_topic(self, running_unit):
        """The reproduction from #94, asserted rather than observed.

        3.0 is 60 ramp ticks away at the default rate, so the window in which actual and
        commanded disagree is ~3s wide -- several times the PLC's own republish interval.
        That window is the whole bug: an echo landing inside it overwrites the command
        with a value the belt has already passed through.
        """
        mw, host, port = running_unit
        route = await _route_ending(mw, _speed_route_suffix(seed.CONVEYOR_BELT_LEFT))
        url = f"http://127.0.0.1:{PORT}{route}"

        async with TransferUnit(
            broker=host,
            port=port,
            publish_interval=0.5,
            # Both belts, always: `_throughput_loop` reads `speeds["right"]`
            # (transfer_unit.py:346), so a one-belt dict KeyErrors the moment the
            # throughput simulation runs.
            initial_speeds={"left": 0.0, "right": 0.0},
        ) as unit:
            assert await _wait_for_value(url, 0.0), "startup publish never reached persistence"

            async with spying_on_set_topics(host, port) as spy:
                await unit.set_speed("left", 3.0)
                await asyncio.sleep(6.0)

                assert spy.values_on("left") == [], (
                    "the middleware published onto the left belt's set topic during a ramp "
                    f"nobody commanded: {spy.values_on('left')}"
                )

            assert unit.speeds["left"] == 3.0, (
                f"the belt froze at {unit.speeds['left']} instead of converging to 3.0 "
                f"(one ramp step is {DEFAULT_RAMP_RATE * RAMP_TICK_SECONDS})"
            )

    async def test_churning_barriers_do_not_move_a_belt(self, running_unit):
        """#83's throughput simulation cycles the barriers -- the exact sibling traffic
        that fans out onto the belts' write legs."""
        mw, host, port = running_unit
        route = await _route_ending(mw, _speed_route_suffix(seed.CONVEYOR_BELT_LEFT))
        url = f"http://127.0.0.1:{PORT}{route}"

        async with TransferUnit(
            broker=host,
            port=port,
            publish_interval=0.5,
            # Both belts, always: `_throughput_loop` reads `speeds["right"]`
            # (transfer_unit.py:346), so a one-belt dict KeyErrors the moment the
            # throughput simulation runs.
            initial_speeds={"left": 0.0, "right": 0.0},
        ) as unit:
            assert await _wait_for_value(url, 0.0), "startup publish never reached persistence"
            await unit.set_throughput_simulation(True)
            try:
                async with spying_on_set_topics(host, port) as spy:
                    await unit.set_speed("left", 2.5)
                    await asyncio.sleep(6.0)

                    assert spy.values_on("left") == [], (
                        "a cycling light barrier drove the belt's set topic: "
                        f"{spy.values_on('left')}"
                    )
                assert unit.speeds["left"] == 2.5
            finally:
                await unit.set_throughput_simulation(False)


@requires_graphdb
@pytest.mark.asyncio
class TestAWriteToOneParameterLeavesItsSiblingsAlone:
    """Trigger 2: a PUT carries no origin, so the fan-out reaches every write leg.

    Derived from `rest_router._make_put_handler` by reading, then asserted here. This is
    the trigger #82's algorithm hits in ordinary operation, and no test covered it.
    """

    async def test_a_put_to_one_belt_publishes_nothing_on_the_other(self, running_unit):
        mw, host, port = running_unit
        left = await _route_ending(mw, _speed_route_suffix(seed.CONVEYOR_BELT_LEFT))
        right = await _route_ending(mw, _speed_route_suffix(seed.CONVEYOR_BELT_RIGHT))
        left_url = f"http://127.0.0.1:{PORT}{left}"
        right_url = f"http://127.0.0.1:{PORT}{right}"

        async with TransferUnit(
            broker=host,
            port=port,
            publish_interval=0.5,
            initial_speeds={"left": 0.0, "right": 1.4},
        ) as unit:
            assert await _wait_for_value(right_url, 1.4), "right belt never reached persistence"
            assert await _wait_for_value(left_url, 0.0), "left belt never reached persistence"

            async with spying_on_set_topics(host, port) as spy:
                # Command the LEFT belt only. Read the node and put it back with a new
                # value, which is what the station board's own write path does.
                [node] = await _read(left_url)
                node[INF.hasValue.lined] = [2.0]
                await _put(left_url, [node])
                await asyncio.sleep(4.0)

                assert spy.values_on("right") == [], (
                    "a PUT to the left belt published onto the RIGHT belt's set topic: "
                    f"{spy.values_on('right')}"
                )

            assert unit.speeds["right"] == 1.4, (
                f"the right belt moved to {unit.speeds['right']} because its sibling was "
                "commanded"
            )


@requires_graphdb
@pytest.mark.asyncio
class TestACommandStillReachesTheDevice:
    """The southbound path must not be simply switched off -- #94's fourth criterion."""

    async def test_a_commanded_setpoint_reaches_the_device_and_the_belt_converges(
        self, running_unit
    ):
        mw, host, port = running_unit
        route = await _route_ending(mw, _speed_route_suffix(seed.CONVEYOR_BELT_LEFT))
        url = f"http://127.0.0.1:{PORT}{route}"

        async with TransferUnit(
            broker=host,
            port=port,
            publish_interval=0.5,
            # Both belts, always: `_throughput_loop` reads `speeds["right"]`
            # (transfer_unit.py:346), so a one-belt dict KeyErrors the moment the
            # throughput simulation runs.
            initial_speeds={"left": 0.0, "right": 0.0},
        ) as unit:
            assert await _wait_for_value(url, 0.0), "startup publish never reached persistence"

            async with spying_on_set_topics(host, port) as spy:
                [node] = await _read(url)
                node[INF.hasValue.lined] = [1.5]
                await _put(url, [node])

                await _await_true(
                    lambda: 1.5 in spy.values_on("left"),
                    10.0,
                    "a commanded setpoint never reached the device's set topic",
                )

            await _await_true(
                lambda: unit.speeds["left"] == 1.5,
                10.0,
                f"the belt never converged to the commanded 1.5 (at {unit.speeds['left']})",
            )
