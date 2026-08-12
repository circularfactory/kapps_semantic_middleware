"""The demo's mock PLC against a real broker (#40).

Drives ``demo.transferunits.plc.transfer_unit.TransferUnit`` -- the factory's PLC, not the
retired ``examples/mock_transferunit.py`` this file was named after.

Live MQTT: a pure-Python ``amqtt`` broker in-process, real sockets, the same ``aiomqtt``
client the framework connector uses. No GraphDB -- this is the device half of scenario 3, and
the device knows nothing about the graph.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path

import aiomqtt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo.transferunits.middleware import ensure_transport  # noqa: E402
from demo.transferunits.plc.transfer_unit import DEFAULT_RAMP_RATE, TransferUnit  # noqa: E402


async def _collect(broker: str, topics, count, timeout=5.0):
    """Subscribe to `topics` and return the first `count` messages as (topic, value)."""
    host, port = broker.split(":")
    received = []
    async with aiomqtt.Client(host, port=int(port)) as client:
        for topic in topics:
            await client.subscribe(topic)

        async def pump():
            async for message in client.messages:
                received.append(
                    (str(message.topic), json.loads(message.payload.decode()))
                )
                if len(received) >= count:
                    return

        await asyncio.wait_for(pump(), timeout=timeout)
    return received


class TestTopicScheme:
    """The scheme is TransferUnit<n>/<component>/<position>/<param>, +_set (ADR 0023)."""

    def test_publishes_four_topics(self):
        unit = TransferUnit()

        assert unit.published_topics == [
            "TransferUnit1/ConveyorBelt/left/speed",
            "TransferUnit1/ConveyorBelt/right/speed",
            "TransferUnit1/LightBarrier/front/occupied",
            "TransferUnit1/LightBarrier/back/occupied",
        ]

    def test_subscribes_to_two_topics(self):
        unit = TransferUnit()

        assert unit.subscribed_topics == [
            "TransferUnit1/ConveyorBelt/left/speed_set",
            "TransferUnit1/ConveyorBelt/right/speed_set",
        ]

    def test_the_unit_name_parameterises_every_topic(self):
        """Multiple simultaneous TransferUnits are in scope for map #24."""
        unit = TransferUnit(unit_index=7)

        assert all(t.startswith("TransferUnit7/") for t in unit.published_topics)
        assert all(t.startswith("TransferUnit7/") for t in unit.subscribed_topics)

    def test_ramp_rate_is_configurable(self):
        """#83: ramp_rate is a constructor argument with a sensible default."""
        unit_default = TransferUnit()
        assert unit_default.ramp_rate == DEFAULT_RAMP_RATE

        unit_custom = TransferUnit(ramp_rate=2.5)
        assert unit_custom.ramp_rate == 2.5


@pytest.mark.asyncio
class TestLiveBehaviour:
    async def test_publishes_all_four_values(self, mqtt_broker):
        host, port = mqtt_broker.split(":")
        async with TransferUnit(broker=host, port=int(port), publish_interval=0.1) as unit:
            received = await _collect(mqtt_broker, unit.published_topics, count=4)

        assert {topic for topic, _ in received} == set(unit.published_topics)

    async def test_a_setpoint_moves_the_published_speed(self, mqtt_broker):
        """#40's acceptance: a setpoint on speed_set moves the published speed."""
        host, port = mqtt_broker.split(":")
        async with TransferUnit(
            broker=host, port=int(port), publish_interval=0.1
        ) as unit:
            assert unit.speeds["left"] == 0.0

            async with aiomqtt.Client(host, port=int(port)) as publisher:
                await publisher.publish(
                    "TransferUnit1/ConveyorBelt/left/speed_set", json.dumps(1.75).encode()
                )
                await unit.wait_for_setpoint()

            # The setpoint ramps the belt rather than snapping it (#83) -- wait for the
            # ramp to converge before asserting the exact value.
            await unit.wait_for_convergence("left", timeout=5.0)
            assert unit.speeds["left"] == 1.75

            # And the new value reaches the read topic, which is what a middleware sees.
            speeds = await _collect(
                mqtt_broker, ["TransferUnit1/ConveyorBelt/left/speed"], count=1
            )
            assert speeds[0][1] == 1.75

    async def test_a_setpoint_does_not_disturb_the_other_belt(self, mqtt_broker):
        host, port = mqtt_broker.split(":")
        async with TransferUnit(
            broker=host, port=int(port), publish_interval=0.1
        ) as unit:
            async with aiomqtt.Client(host, port=int(port)) as publisher:
                await publisher.publish(
                    "TransferUnit1/ConveyorBelt/right/speed_set", json.dumps(2.5).encode()
                )
                await unit.wait_for_setpoint()

            await unit.wait_for_convergence("right", timeout=5.0)
            assert unit.speeds["right"] == 2.5
            assert unit.speeds["left"] == 0.0

    async def test_a_light_barrier_reports_occupancy(self, mqtt_broker):
        host, port = mqtt_broker.split(":")
        async with TransferUnit(
            broker=host, port=int(port), publish_interval=5.0
        ) as unit:
            await unit.set_occupied("front", True)

            received = await _collect(
                mqtt_broker, ["TransferUnit1/LightBarrier/front/occupied"], count=1
            )

        assert received[0][1] is True

    async def test_an_unparseable_setpoint_is_ignored_not_fatal(self, mqtt_broker):
        """A malformed payload from a device must not take the mock down."""
        host, port = mqtt_broker.split(":")
        async with TransferUnit(
            broker=host, port=int(port), publish_interval=0.1
        ) as unit:
            async with aiomqtt.Client(host, port=int(port)) as publisher:
                await publisher.publish(
                    "TransferUnit1/ConveyorBelt/left/speed_set", b"not-a-number"
                )
                await asyncio.sleep(0.3)
                await publisher.publish(
                    "TransferUnit1/ConveyorBelt/left/speed_set", json.dumps(3.0).encode()
                )
                await unit.wait_for_setpoint()

            await unit.wait_for_convergence("left", timeout=5.0)
            assert unit.speeds["left"] == 3.0


@pytest.mark.asyncio
class TestRamping:
    """#83: setpoints ramp over time, not instantly; reverse works through zero."""

    async def test_a_setpoint_does_not_move_the_speed_instantly(self, mqtt_broker):
        host, port = mqtt_broker.split(":")
        async with TransferUnit(
            broker=host, port=int(port), publish_interval=0.1, ramp_rate=0.5
        ) as unit:
            async with aiomqtt.Client(host, port=int(port)) as publisher:
                await publisher.publish(
                    "TransferUnit1/ConveyorBelt/left/speed_set", json.dumps(2.0).encode()
                )
                await unit.wait_for_setpoint()

            # Still ramping, not yet converged — proof the setpoint moves speed over time.
            assert unit.speeds["left"] != 2.0

            await unit.wait_for_convergence("left", timeout=10.0)
            assert unit.speeds["left"] == 2.0

    async def test_a_negative_setpoint_ramps_the_belt_backwards(self, mqtt_broker):
        """#83: ramp passes through zero into reverse with no special-casing."""
        host, port = mqtt_broker.split(":")
        async with TransferUnit(
            broker=host, port=int(port), publish_interval=0.1
        ) as unit:
            async with aiomqtt.Client(host, port=int(port)) as publisher:
                await publisher.publish(
                    "TransferUnit1/ConveyorBelt/left/speed_set", json.dumps(-1.5).encode()
                )
                await unit.wait_for_setpoint()

            await unit.wait_for_convergence("left", timeout=10.0)
            assert unit.speeds["left"] == -1.5

    async def test_set_speed_sets_the_target_not_the_actual_value(self, mqtt_broker):
        host, port = mqtt_broker.split(":")
        async with TransferUnit(
            broker=host, port=int(port), publish_interval=0.1
        ) as unit:
            await unit.set_speed("left", 3.0)

            # Target moved, actual value has not caught up yet.
            assert unit.setpoints["left"] == 3.0
            assert unit.speeds["left"] != 3.0

            await unit.wait_for_convergence("left", timeout=10.0)
            assert unit.speeds["left"] == 3.0


@pytest.mark.asyncio
class TestThroughputSimulation:
    """#83: throughput simulation drives plc.set_occupied only (read-only northbound)."""

    async def test_no_cycling_while_stopped(self, mqtt_broker):
        host, port = mqtt_broker.split(":")
        async with TransferUnit(
            broker=host, port=int(port), publish_interval=0.1
        ) as unit:
            # Both belts at default 0.0 — no cycling should start.
            await unit.set_throughput_simulation(True)
            await asyncio.sleep(0.3)  # Three THROUGHPUT_POLL_SECONDS.
            assert unit.occupied == {"front": False, "back": False}

    async def test_cycles_while_running(self, mqtt_broker):
        host, port = mqtt_broker.split(":")
        async with TransferUnit(
            broker=host, port=int(port), publish_interval=0.1
        ) as unit:
            # Set belt running directly — legitimate test setup, not a production write.
            unit.speeds["left"] = 5.0
            await unit.set_throughput_simulation(True)

            # Poll until front barrier observed occupied at least once.
            async def wait_for_front_occupied():
                while True:
                    if unit.occupied["front"] is True:
                        return
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(wait_for_front_occupied(), timeout=3.0)

    async def test_stops_when_belts_stop(self, mqtt_broker):
        host, port = mqtt_broker.split(":")
        async with TransferUnit(
            broker=host, port=int(port), publish_interval=0.1
        ) as unit:
            unit.speeds["left"] = 5.0
            await unit.set_throughput_simulation(True)

            # Wait for front occupied once (as in test_cycles_while_running).
            async def wait_for_front_occupied():
                while True:
                    if unit.occupied["front"] is True:
                        return
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(wait_for_front_occupied(), timeout=3.0)

            # Stop the belt — cycling should halt and clear.
            unit.speeds["left"] = 0.0

            # Poll until both barriers clear (at most one poll inside in-flight half-cycle).
            async def wait_for_all_clear():
                while True:
                    if unit.occupied == {"front": False, "back": False}:
                        return
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(wait_for_all_clear(), timeout=2.0)

    async def test_disabling_clears_any_open_barrier(self, mqtt_broker):
        host, port = mqtt_broker.split(":")
        async with TransferUnit(
            broker=host, port=int(port), publish_interval=0.1
        ) as unit:
            unit.speeds["left"] = 5.0
            await unit.set_throughput_simulation(True)

            # Wait for front occupied once.
            async def wait_for_front_occupied():
                while True:
                    if unit.occupied["front"] is True:
                        return
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(wait_for_front_occupied(), timeout=3.0)

            # Disabling clears synchronously.
            await unit.set_throughput_simulation(False)
            assert unit.occupied == {"front": False, "back": False}


class TestSurvivesAStartupRace:
    """#79's acceptance: the PLC survives being started before its broker exists.

    The middleware brings up a unit's broker concurrently with the PLC now (ADR 0029 as
    amended), rather than the launcher starting it first, so refusal on the first
    connection attempts is the ordinary startup race and ``start()`` must retry through it.
    """

    # Distinct from conftest.py's MQTT_TEST_PORT (18831): nothing may already listen here
    # when the test begins, and the mqtt_broker fixture owns that one.
    _PORT = 18841

    @pytest.mark.asyncio
    async def test_start_retries_until_the_broker_appears(self):
        unit = TransferUnit(broker="127.0.0.1", port=self._PORT, publish_interval=0.1)
        start_task = asyncio.create_task(unit.start())
        try:
            await asyncio.sleep(0.5)
            assert not start_task.done(), "start() must not die on the first refusal"

            # The real production path (#79, ADR 0034): the unit's own middleware brings its
            # broker up through this same hook. Reusing it here, rather than hand-rolling
            # another amqtt Broker(...), is both less duplication and a more faithful stand-in
            # for what actually races the PLC's retry loop in the running demo.
            ensure_transport("127.0.0.1", self._PORT)

            await asyncio.wait_for(start_task, timeout=10.0)
        finally:
            # No broker teardown here, on purpose (ADR 0034: a caller owns no lifetime it
            # didn't start) -- ensure_transport's daemon thread dies with the test process.
            if start_task.done() and not start_task.cancelled() and start_task.exception() is None:
                await unit.stop()
            else:
                start_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await start_task
