"""Northbound sync through the full middleware, not just the bare connector (#86).

``test_scenario3_roundtrip_integration.py`` proves a value reaches a bare
``MqttClientConnector``'s queue. It never drives ``Middleware``'s background sync
machinery -- ``SyncedConnector.receive()``, the ``run_receive`` task, and the
persistence-write fan-out to sibling connectors -- which is the layer #86's bug actually
lived in: a persistence write's best-effort notification to a sibling connector raised,
and that exception propagated out of ``consume()`` into the *receiving* connector's own
background task, killing it. A topic answered exactly once, then went silent forever.

These tests start a real ``SemanticMiddleware`` server and read values back over its
ADR 0017 REST route, the way an operator's panel or a controller actually would, so they
exercise the path the bare-connector tests cannot reach. Where the issue's own acceptance
criteria name "the panel," these drive ``TransferUnit.set_speed`` / ``set_occupied`` directly
-- exactly the calls ``demo/transferunits/plc/panel.py``'s REST handlers make, without the
HTML/HTTP chrome around them, which the issue's own diagnosis had already ruled out as
neither the cause nor the fix's location.
"""

from __future__ import annotations

import asyncio
import logging
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
from demo.transferunits.plc.transfer_unit import TransferUnit  # noqa: E402

PORT = 8991


def _start_server(mw):
    import uvicorn

    config = uvicorn.Config(mw.app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


async def _await_true(predicate, timeout, message):
    """Poll ``predicate`` with ``asyncio.sleep`` rather than ``time.sleep``.

    The in-process ``amqtt`` broker (``mqtt_broker`` fixture) runs its tasks on *this*
    event loop -- the same one pytest-asyncio drives this fixture and the test itself on.
    A blocking ``time.sleep`` here would starve the broker's own tasks of the loop for
    the whole wait, so the uvicorn server's connectors could never complete their MQTT
    handshake against it: a deadlock, not a slow test.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


async def _route_ending(mw, suffix, timeout=10.0):
    """A parameter route, waited for since ``_parameter_routes`` fills in on startup."""
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


@pytest_asyncio.fixture
async def running_unit(graphdb, mqtt_broker, unit_scope):
    """A real ``SemanticMiddleware`` server for TransferUnit1, wired over the test broker.

    Reads come back through the middleware's own REST route -- the actual path #86 broke,
    not the bare connector queue ``test_scenario3_roundtrip_integration.py`` reads.
    """
    host, port = mqtt_broker.split(":")
    seed.seed_scenario3(graphdb, OGM(db=graphdb))
    # Declare both host and port in the graph, the way provisioning will (#69). No manual
    # patch of the built connectors is needed once `inf:hasMQTTBrokerPort` reaches the
    # connector through the binding itself.
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
        await _await_true(
            lambda: server.started, 30.0, "server did not start in time"
        )
        yield mw, host, int(port)
    finally:
        server.should_exit = True
        try:
            await _await_true(
                lambda: not thread.is_alive(), 20.0, "server thread did not stop in time"
            )
        except AssertionError:
            pass


async def _read(url):
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=5.0)
        response.raise_for_status()
        return response.json()


async def _wait_for_value(url, expected, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        [body] = await _read(url)
        if body[INF.hasValue.lined] == [expected]:
            return True
        await asyncio.sleep(0.1)
    return False


@requires_graphdb
@pytest.mark.asyncio
class TestConsecutiveChangesReachPersistence:
    """#86: a topic answered exactly once, then went silent -- the background task
    reading it died on its first successful update, and stayed dead."""

    async def test_three_consecutive_speed_changes_all_land(self, running_unit, caplog):
        mw, host, port = running_unit
        route = await _route_ending(
            mw,
            f"/{IRI(str(seed.CONVEYOR_BELT_LEFT)).lined}/{seed.TU_HAS_CONVEYOR_SPEED.lined}",
        )
        url = f"http://127.0.0.1:{PORT}{route}"

        # A start value distinct from every value this test sets, so the PLC's own startup
        # publish (every TransferUnit publishes its full state once on start, so a
        # middleware that starts after the device still converges) can never coincide with
        # one of the values below and be mistaken for one of the three changes under test.
        async with TransferUnit(
            broker=host, port=port, publish_interval=5.0, initial_speeds={"left": -1.0}
        ) as unit:
            assert await _wait_for_value(url, -1.0), "startup publish never reached persistence"

            caplog.clear()
            caplog.set_level(
                logging.INFO, logger="kapps_semantic_middleware.connectors.mqtt_binding"
            )

            # One value is what the pre-#86 suite already proved. A second and third are
            # what shipped broken: the first successful update killed the receive task for
            # good. `set_speed` is the same call `demo/transferunits/plc/panel.py`'s `set`
            # REST handler makes -- "the panel's set" from the issue's acceptance criteria.
            for value in (0.0, 1.11, 2.22):
                await unit.set_speed("left", value)
                assert await _wait_for_value(url, value), (
                    f"{value} never reached persistence over the ADR 0017 route"
                )

        # Every value the belt settled on was reported as news (#67's log-on-change, seen
        # through the whole pipeline rather than at the formatter).
        #
        # This asserted `len(info_lines) == 3` when it was written, one commit before #83
        # landed. A setpoint no longer snaps: `_ramp_loop` walks the belt toward its target
        # and publishes every step, so three setpoints legitimately produce dozens of
        # *distinct* values, every one of them genuine news. The count measured the ramp
        # rate, not the logging -- three setpoints only ever meant three lines while a
        # setpoint was an instant assignment.
        #
        # The suppression half of #67 -- that an unchanged republish drops to DEBUG -- is
        # not asserted here, and deliberately not. `_ramp_loop` publishes only when it
        # actually moves the belt, so a converged belt is silent and there is no unchanged
        # value to suppress unless this test either runs long enough for the 5s periodic
        # republish or shortens that interval; the second was tried and destabilizes the
        # value assertions above, which are what this test is actually for.
        # `test_semantic_connectors.py::test_a_repeated_inbound_value_drops_to_debug` owns
        # that property directly and deterministically.
        # `args[-1]` rather than a fixed index: the value is the last argument on both the
        # inbound and the outbound line, and #76 dropped the topic out of the INFO record, so
        # what used to sit at index 2 now sits at index 1. Read from the end and this survives
        # the next such change too.
        logged_values = [
            r.args[-1]
            for r in caplog.records
            if r.levelno == logging.INFO and "ConveyorBelt1_left hasConveyorSpeed" in r.message
        ]
        for value in (0.0, 1.11, 2.22):
            assert value in logged_values, (
                f"the belt settled on {value} but no INFO line reported it as a change; "
                f"logged: {logged_values}"
            )

    async def test_a_barrier_flips_both_ways_repeatedly(self, running_unit):
        mw, host, port = running_unit
        route = await _route_ending(
            mw,
            f"/{IRI(str(seed.LIGHT_BARRIER_FRONT)).lined}/{seed.TU_IS_OCCUPIED.lined}",
        )
        url = f"http://127.0.0.1:{PORT}{route}"

        async with TransferUnit(broker=host, port=port, publish_interval=5.0) as unit:
            assert await _wait_for_value(url, False), "startup publish never reached persistence"

            # The barrier toggled on the panel, both ways, repeatedly -- `set_occupied` is
            # what a workpiece passing the sensor (or an operator on the panel) does.
            for expected in (True, False, True, False):
                await unit.set_occupied("front", expected)
                assert await _wait_for_value(url, expected), (
                    f"barrier flip to {expected} never reached persistence"
                )


@requires_graphdb
@pytest.mark.asyncio
class TestReceiveFailureIsVisible:
    """#86 acceptance: "A receive task that dies logs a traceback instead of vanishing."

    The fix in ``persisted_connector.py`` isolates the *specific* failure #86 diagnosed (a
    persistence-write notification broadcasting the whole model to a sibling connector's
    formatter), so that path no longer kills a receive task at all. This test proves the
    other half of the fix -- ``middleware.py``'s ``run_receive`` -- independently, by
    forcing a genuinely different failure (a formatter that raises on its own connector's
    own value, nothing to do with the notify fan-out) and asserting it is logged with a
    traceback rather than disappearing.
    """

    async def test_a_dying_receive_task_logs_a_traceback(self, running_unit, caplog):
        mw, host, port = running_unit
        read = next(
            r
            for _, r in mw._wiring.registrations
            if r.connector.topic.endswith("left/speed")
            and r.sync_direction is SyncDirection.TO_PERSISTENCE
        )

        def _boom(_data):
            raise RuntimeError("synthetic formatter failure, unrelated to the notify fan-out")

        read.formatter.deserialize = _boom

        caplog.set_level(logging.ERROR, logger="transitional_sync_middleware.middleware.middleware")

        async with TransferUnit(broker=host, port=port, publish_interval=5.0) as unit:
            await unit.set_speed("left", 9.99)
            await _await_true(
                lambda: any(
                    r.levelno == logging.ERROR
                    and r.exc_info is not None
                    and "died" in r.message
                    for r in caplog.records
                ),
                timeout=5.0,
                message="the dying receive task never logged an error with a traceback",
            )
