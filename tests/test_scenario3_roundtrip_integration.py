"""Scenario 3 end to end: mock PLC -> MQTT -> the connectors the graph wired (#40).

The full loop the map is named for. The graph says where each value lives; the middleware
reads that, builds connectors, and a value published by the device arrives through them —
nothing about the topics or the broker is written in this file, it all comes out of the
ontology and the seeded ABox.

Live GraphDB plus a live in-process MQTT broker.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection
from kapps_ogm import OGM
from kapps_ogm.utils.class_scope import ClassScope

from kapps_semantic_middleware.connectors.mqtt_binding import MQTTBinding
from kapps_semantic_middleware.connectors.semantic import SemanticConnectorRegistry
from kapps_semantic_middleware.connectors.wiring import plan_wiring
from kapps_semantic_middleware.vocabulary import INF

from conftest import requires_graphdb  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import examples.seed as seed  # noqa: E402
from demo.transferunits.plc.transfer_unit import TransferUnit  # noqa: E402


def _wire_scenario3_to_test_broker(graphdb, mqtt_broker, *, declare_port):
    """Seed scenario 3, point its broker address at the live test broker, plan its wiring.

    Shared by ``wired`` and ``wired_with_declared_port`` below, which differ only in
    whether ``inf:hasMQTTBrokerPort`` also gets declared in the graph.
    """
    host, port = mqtt_broker.split(":")
    ogm = OGM(db=graphdb)
    seed.seed_scenario3(graphdb, ogm)

    port_triple = (
        f'; <{INF.hasMQTTBrokerPort}> "{port}"^^<http://www.w3.org/2001/XMLSchema#integer> '
        if declare_port
        else ""
    )
    graphdb.query(
        f"DELETE {{ ?n <{INF.hasMQTTBrokerIP}> ?old }} "
        f'INSERT {{ ?n <{INF.hasMQTTBrokerIP}> "{host}" {port_triple}}} '
        f"WHERE  {{ ?n <{INF.hasMQTTBrokerIP}> ?old }}",
        update=True,
    )

    scope = ClassScope.from_property_chains(
        [
            [seed.TU_HAS_CONVEYOR_BELT, seed.TU_HAS_CONVEYOR_SPEED],
            [seed.TU_HAS_LIGHT_BARRIER, seed.TU_IS_OCCUPIED],
        ]
    )
    plan = plan_wiring(
        ogm=OGM(db=graphdb),
        resource_iri=seed.TRANSFER_UNIT_1,
        class_scope=scope,
        registry=SemanticConnectorRegistry([MQTTBinding]),
        flavour=SyncDirection.BIDIRECTIONAL,
    )
    return plan, host, int(port)


@pytest.fixture
def wired(graphdb, mqtt_broker):
    """Seed scenario 3 pointed at the test broker, then plan its wiring.

    The seeded broker address is rewritten to the test broker's host:port, because the
    binding reads the address out of the graph — which is the property under test. Pointing
    the graph at the test broker is how you redirect a whole TransferUnit, and it is exactly
    what provisioning (#54) will do for real.
    """
    return _wire_scenario3_to_test_broker(graphdb, mqtt_broker, declare_port=False)


def _connector_for(plan, topic_suffix, direction):
    """The connector the graph produced for one topic, found by suffix rather than by name."""
    for _, registration in plan.registrations:
        if registration.connector.topic.endswith(topic_suffix) and (
            registration.sync_direction is direction
        ):
            return registration
    raise AssertionError(f"no {direction} connector for a topic ending {topic_suffix!r}")


@requires_graphdb
@pytest.mark.asyncio
class TestDeviceToMiddleware:
    """A value the device publishes reaches the connector the graph wired."""

    async def test_a_published_speed_reaches_its_connector(self, wired):
        plan, host, port = wired
        registration = _connector_for(plan, "left/speed", SyncDirection.TO_PERSISTENCE)
        connector = registration.connector
        # The connector was built with the address the graph carried, not one this test set.
        assert connector.mqtt_broker_ip == host
        connector.mqtt_broker_port = port

        async with TransferUnit(
            broker=host, port=port, publish_interval=0.1, initial_speeds={"left": 1.25, "right": 0.0}
        ):
            await connector.connect()
            try:
                value = await asyncio.wait_for(connector.queue.get(), timeout=5.0)
            finally:
                await connector.disconnect()

        assert value == 1.25

    async def test_the_formatter_rebuilds_the_whole_parameter_node(self, wired):
        """A bare scalar would blank the unit in the model that gets served (ADR 0023)."""
        plan, host, port = wired
        registration = _connector_for(plan, "left/speed", SyncDirection.TO_PERSISTENCE)

        [node] = registration.formatter.deserialize(1.25)

        assert getattr(node, INF.hasValue.lined) == [1.25]
        assert getattr(node, seed.TU_HAS_UNIT.lined) == ["m/s"]
        assert getattr(node, INF.accessMode.lined) == ["readwrite"]

    async def test_a_light_barrier_reaches_its_connector(self, wired):
        plan, host, port = wired
        registration = _connector_for(
            plan, "front/occupied", SyncDirection.TO_PERSISTENCE
        )
        connector = registration.connector
        connector.mqtt_broker_port = port

        async with TransferUnit(broker=host, port=port, publish_interval=0.2) as unit:
            await connector.connect()
            try:
                await asyncio.sleep(0.2)
                await unit.set_occupied("front", True)

                # Drain rather than take the first message: the unit publishes its initial
                # state as soon as it starts, so a plain get() races the barrier being
                # tripped and would sometimes read the "clear" that preceded it.
                async def until_occupied():
                    while True:
                        if await connector.queue.get() is True:
                            return True

                value = await asyncio.wait_for(until_occupied(), timeout=5.0)
            finally:
                await connector.disconnect()

        assert value is True


@requires_graphdb
@pytest.mark.asyncio
class TestMiddlewareToDevice:
    """A setpoint written through the write connector moves the device."""

    async def test_a_setpoint_drives_the_mock_plc(self, wired):
        plan, host, port = wired
        registration = _connector_for(
            plan, "left/speed_set", SyncDirection.FROM_PERSISTENCE
        )
        connector = registration.connector
        connector.mqtt_broker_port = port

        async with TransferUnit(broker=host, port=port, publish_interval=0.1) as unit:
            await connector.connect()
            try:
                # Exactly the path the sync machinery takes: persistence value -> formatter
                # -> connector.consume.
                [node] = registration.formatter.deserialize(2.75)
                await connector.consume(registration.formatter.serialize([node]))
                await unit.wait_for_setpoint(timeout=5.0)
                # The setpoint ramps the belt rather than snapping it (#83); wait for the ramp
                # to converge before asserting the exact value.
                await unit.wait_for_convergence("left", timeout=10.0)
            finally:
                await connector.disconnect()

            assert unit.speeds["left"] == 2.75

    async def test_a_read_only_parameter_has_no_write_connector(self, wired):
        """A controller cannot write a sensor, structurally (ADR 0023)."""
        plan, _, _ = wired

        with pytest.raises(AssertionError):
            _connector_for(plan, "occupied", SyncDirection.FROM_PERSISTENCE)


@requires_graphdb
@pytest.mark.asyncio
class TestFullLoop:
    async def test_setpoint_then_readback_closes_the_loop(self, wired):
        """Write a speed, and read the device's own report of it back."""
        plan, host, port = wired
        write = _connector_for(plan, "left/speed_set", SyncDirection.FROM_PERSISTENCE)
        read = _connector_for(plan, "left/speed", SyncDirection.TO_PERSISTENCE)
        write.connector.mqtt_broker_port = port
        read.connector.mqtt_broker_port = port

        async with TransferUnit(broker=host, port=port, publish_interval=0.1):
            await read.connector.connect()
            await write.connector.connect()
            try:
                [node] = write.formatter.deserialize(3.5)
                await write.connector.consume(write.formatter.serialize([node]))

                # Drain until the new setpoint shows up on the read topic.
                async def until_new_speed():
                    while True:
                        value = await read.connector.queue.get()
                        if value == 3.5:
                            return value

                assert await asyncio.wait_for(until_new_speed(), timeout=5.0) == 3.5
            finally:
                await write.connector.disconnect()
                await read.connector.disconnect()


@pytest.fixture
def wired_with_declared_port(graphdb, mqtt_broker):
    """Like ``wired``, but the port comes from the graph too -- no manual patch afterward.

    ``wired`` (above) rewrites only the broker IP to the test broker's host, and every test
    that uses it patches ``connector.mqtt_broker_port`` by hand afterward, because there was
    nothing in the graph to carry a non-default port. This fixture declares
    ``inf:hasMQTTBrokerPort`` too, the same way provisioning will, so the connector the graph
    wires is already pointed at the live test broker with no test-side patch at all.
    """
    return _wire_scenario3_to_test_broker(graphdb, mqtt_broker, declare_port=True)


@requires_graphdb
@pytest.mark.asyncio
class TestDeclaredPortReachesALiveBroker:
    """#69's stated acceptance: proven against a live broker on a non-default port, not a mock."""

    async def test_the_connector_is_already_built_on_the_declared_port(
        self, wired_with_declared_port
    ):
        plan, host, port = wired_with_declared_port
        registration = _connector_for(plan, "left/speed", SyncDirection.TO_PERSISTENCE)

        assert port != 1883  # the whole point: a non-default port
        assert registration.connector.mqtt_broker_port == port  # no manual patch needed

    async def test_a_published_value_reaches_the_connector_on_the_declared_port(
        self, wired_with_declared_port
    ):
        plan, host, port = wired_with_declared_port
        registration = _connector_for(plan, "left/speed", SyncDirection.TO_PERSISTENCE)
        connector = registration.connector

        async with TransferUnit(
            broker=host,
            port=port,
            publish_interval=0.1,
            initial_speeds={"left": 4.5, "right": 0.0},
        ):
            await connector.connect()
            try:
                value = await asyncio.wait_for(connector.queue.get(), timeout=5.0)
            finally:
                await connector.disconnect()

        assert value == 4.5
