"""TransferUnit — the edge-device PLC stand-in for scenario 3 (#40, ADR 0023).

Stands in for the decentralized PLC controlling one TransferUnit. It speaks only MQTT and
knows nothing about the middleware, the ontology or the graph: that asymmetry is the point of
the scenario. Everything semantic happens on the middleware side, and the device is exactly
as dumb as a real one.

**Publishes 4** — two conveyor speeds and two light-barrier occupancies.
**Subscribes to 2** — the two conveyor speed setpoints. A setpoint moves the speed the unit
publishes, which is what closes the loop end to end.

Topic scheme (an instance convention, never baked into the classes — ADR 0023)::

    TransferUnit<n>/<component>/<position>/<param>          # read
    TransferUnit<n>/ConveyorBelt/<position>/speed_set        # setpoint

Payloads are raw JSON scalars, matching the default the MQTT binding expects when a parameter
declares no ``inf:hasMQTTValuePath``.

It publishes **no** ``inf:hasValue`` into the graph, and indeed never touches the graph:
scenario 3 is a locator (ADR 0024). The graph records where a value lives; the live value
exists only in the datamodel and over REST.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Dict, Optional

import aiomqtt

logger = logging.getLogger(__name__)

DEFAULT_BROKER = "127.0.0.1"
# The plain MQTT default, not seed.broker_port(1) (#79) -- this is only the standalone
# fallback for a PLC started with no --broker-port at all. The launcher always passes one
# explicitly, and this module stays free of any import from seed.py or the middleware side
# on purpose: a PLC knows nothing about the graph or the unit-index port scheme (ADR 0029).
DEFAULT_PORT = 1883

CONNECT_RETRY_INITIAL_SECONDS = 0.2
CONNECT_RETRY_MAX_SECONDS = 5.0

DEFAULT_RAMP_RATE = 1.0
# speed-units per second^2 — the belt's actual speed moves toward its setpoint at up to this
# rate. 1.0 means a typical demo setpoint change (0-3 m/s) settles in a few seconds: slow enough
# for a human to watch on the panel, fast enough that a controller polling this unit does not
# wait unreasonably long for a write to converge.

RAMP_TICK_SECONDS = 0.05
# The ramp loop's own step interval. Independent of `publish_interval` (which governs the
# unconditional periodic republish loop that already exists) -- this is deliberately tighter, so
# a listener downstream (a test, a connector) sees the ramp's progress promptly rather than only
# picking it up on the next slow periodic publish.

THROUGHPUT_PERIOD_AT_UNIT_SPEED = 2.0
# Seconds for ONE light barrier's block-then-clear half of the throughput simulation's cycle,
# when the driving belt speed's magnitude is exactly 1.0. The actual half-cycle length is this
# constant divided by (2 * current speed magnitude) -- a faster belt means a faster cycle.

THROUGHPUT_POLL_SECONDS = 0.1
# How often the throughput loop re-checks belt speed: both while idle (waiting for a belt to
# start) and while holding mid-cycle (so a belt that stops is noticed within this granularity
# rather than only at the end of a stale, already-computed half-cycle).


class TransferUnit:
    """A PLC for one TransferUnit, publishing four values and taking two setpoints.

    Publishing is periodic rather than on-change alone. A middleware may start after the
    device. It must still converge. ``MqttClientConnector`` holds the latest message from
    its subscription. It has nothing to hold until one arrives. A device that published
    on change only would leave a fresh middleware blank. An operator would need to move
    something first.
    """

    def __init__(
        self,
        unit_index: int = 1,
        broker: str = DEFAULT_BROKER,
        port: int = DEFAULT_PORT,
        publish_interval: float = 0.5,
        initial_speeds: Optional[Dict[str, float]] = None,
        ramp_rate: float = DEFAULT_RAMP_RATE,
    ) -> None:
        """Initialize a TransferUnit PLC instance.

        Set the unit index, broker address, and port. Set the publish interval for periodic
        updates. Set the ramp rate for belt acceleration. Initialize speeds to zero or the
        given initial values. Initialize setpoints to None. Initialize barrier states to
        unoccupied.
        """
        self.unit_index = unit_index
        self.unit = f"TransferUnit{unit_index}"
        self.broker = broker
        self.port = port
        self.publish_interval = publish_interval
        self.ramp_rate = ramp_rate

        self.speeds: Dict[str, float] = dict(initial_speeds or {"left": 0.0, "right": 0.0})
        self.setpoints: Dict[str, Optional[float]] = {"left": None, "right": None}
        self.occupied: Dict[str, bool] = {"front": False, "back": False}

        self._client: Optional[aiomqtt.Client] = None
        self._tasks: list = []
        self._setpoints_seen = asyncio.Event()
        self._throughput_task: Optional[asyncio.Task] = None

    # --- Topics ------------------------------------------------------------------ #

    def speed_topic(self, position: str) -> str:
        """Return the MQTT topic for publishing a belt's actual speed.

        The PLC publishes to this topic. The middleware subscribes to it. Messages travel
        northbound from device to middleware.
        """
        return f"{self.unit}/ConveyorBelt/{position}/speed"

    def speed_set_topic(self, position: str) -> str:
        """Return the MQTT topic for receiving a belt's speed setpoint.

        The middleware publishes to this topic. The PLC subscribes to it. Messages travel
        southbound from middleware to device.
        """
        return f"{self.unit}/ConveyorBelt/{position}/speed_set"

    def occupied_topic(self, position: str) -> str:
        """Return the MQTT topic for publishing a light barrier's occupancy state.

        The PLC publishes to this topic. The middleware subscribes to it. Messages travel
        northbound from device to middleware.
        """
        return f"{self.unit}/LightBarrier/{position}/occupied"

    @property
    def published_topics(self) -> list:
        """The four topics this unit publishes."""
        return [self.speed_topic(p) for p in ("left", "right")] + [
            self.occupied_topic(p) for p in ("front", "back")
        ]

    @property
    def subscribed_topics(self) -> list:
        """The two setpoint topics this unit subscribes to."""
        return [self.speed_set_topic(p) for p in ("left", "right")]

    # --- Lifecycle ---------------------------------------------------------------- #

    async def start(self) -> None:
        """Connect, subscribe to both setpoint topics, and begin publishing.

        Retries the connection with a backoff, capped at ``CONNECT_RETRY_MAX_SECONDS``. The
        middleware brings up this unit's broker concurrently with the PLC. It does not start
        the broker first (ADR 0029 as amended). Refusal on the first attempts is the ordinary
        startup race. It is not a fault. A real machine must survive a broker restart the
        same way. This is a device concern. The device holds no knowledge of the middleware.
        It does not know why the broker was briefly unreachable. A fresh attempt is worth
        making.
        """
        self._client = aiomqtt.Client(self.broker, port=self.port)
        delay = CONNECT_RETRY_INITIAL_SECONDS
        while True:
            try:
                await self._client.__aenter__()
                break
            except aiomqtt.MqttError as exc:
                logger.info(
                    "%s broker at %s:%s not ready yet (%s), retrying in %.1fs",
                    self.unit,
                    self.broker,
                    self.port,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, CONNECT_RETRY_MAX_SECONDS)
        for topic in self.subscribed_topics:
            await self._client.subscribe(topic)
        self._tasks = [
            asyncio.create_task(self._listen()),
            asyncio.create_task(self._publish_loop()),
            asyncio.create_task(self._ramp_loop()),
        ]
        logger.info(
            "TransferUnit %s up on %s:%s — publishing %d, subscribed to %d",
            self.unit,
            self.broker,
            self.port,
            len(self.published_topics),
            len(self.subscribed_topics),
        )

    async def stop(self) -> None:
        """Cancel the loops and disconnect."""
        # Tear down the throughput simulation first, if it is running -- clearing its
        # barriers is a barrier write and must happen while the client is still connected.
        if self._throughput_task is not None:
            self._throughput_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._throughput_task
            self._throughput_task = None
            for pos in ("front", "back"):
                if self.occupied[pos]:
                    await self.set_occupied(pos, False)

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def __aenter__(self) -> "TransferUnit":
        """Enter the async context manager and start the PLC.

        Call ``start()`` to connect to the MQTT broker. Subscribe to setpoint topics. Start
        background tasks for listening, publishing, and ramping. Return self for use in the
        context block.
        """
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        """Exit the async context manager and stop the PLC.

        Call ``stop()`` to cancel all background tasks. Disconnect from the MQTT broker.
        Clear occupied barriers if the throughput simulation is running. Release resources.
        """
        await self.stop()

    # --- Behaviour ---------------------------------------------------------------- #

    async def publish_once(self) -> None:
        """Publish the current value of all four read topics."""
        for position, speed in self.speeds.items():
            await self._publish(self.speed_topic(position), speed)
        for position, occupied in self.occupied.items():
            await self._publish(self.occupied_topic(position), occupied)

    async def set_occupied(self, position: str, occupied: bool) -> None:
        """Move a light barrier and publish it immediately.

        The barriers are read-only northbound. This is the test's way in. A workpiece passing
        the sensor would do this. The throughput simulation calls this same method. It does
        not assign ``self.occupied`` directly. The panel's manual barrier buttons do the same.
        This method is the one door a barrier moves through.
        """
        self.occupied[position] = occupied
        await self._publish(self.occupied_topic(position), occupied)

    async def set_speed(self, position: str, value: float) -> None:
        """Set the commanded target for a belt position.

        This is the REST API entry point for the panel. It used to set the actual speed.
        It published immediately. Now it only sets the target. The belt ramps toward the
        target (#83). ``_ramp_loop`` is the single place that moves ``self.speeds``. It
        publishes the result. This write path works exactly as an MQTT setpoint.
        """
        self.setpoints[position] = value

    async def wait_for_setpoint(self, timeout: float = 5.0) -> None:
        """Block until at least one setpoint has been received. For tests."""
        await asyncio.wait_for(self._setpoints_seen.wait(), timeout=timeout)

    async def wait_for_convergence(self, position: str, timeout: float = 5.0) -> None:
        """Block until `position`'s actual speed reaches its setpoint. For tests.

        This method polls rather than using an event. Convergence is the ramp's last tick.
        No single moment exists before it happens. A listener cannot subscribe in advance.
        """

        async def _settled() -> None:
            """Wait until the belt speed equals its setpoint.

            Poll the speed at each ramp tick. Continue while the setpoint is None. Continue
            while the speed does not equal the setpoint. Exit when convergence is reached.
            """
            while (
                self.setpoints[position] is None
                or self.speeds[position] != self.setpoints[position]
            ):
                await asyncio.sleep(RAMP_TICK_SECONDS)

        await asyncio.wait_for(_settled(), timeout=timeout)

    async def set_throughput_simulation(self, enabled: bool) -> None:
        """Start or stop the barrier-cycling throughput simulation (#83).

        A crude stand-in for a workpiece traveling front-to-back while a belt runs. Cancelling
        is used rather than flagging. The loop can be mid-sleep inside a half cycle. A flag
        checked only between awaits would leave that sleep to finish on its own clock. This
        method is idempotent. Calling with the state already in effect is a no-op.
        """
        if enabled:
            if self._throughput_task is None:
                self._throughput_task = asyncio.create_task(self._throughput_loop())
        else:
            if self._throughput_task is not None:
                self._throughput_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._throughput_task
                self._throughput_task = None
                # A stop always leaves a clean, cleared state rather than whatever mid-cycle
                # state the loop happened to be in.
                for pos in ("front", "back"):
                    if self.occupied[pos]:
                        await self.set_occupied(pos, False)

    def snapshot(self) -> dict:
        """Return a snapshot of the current PLC state for the panel."""
        return {
            "unit": self.unit,
            "unit_index": self.unit_index,
            "speeds": dict(self.speeds),
            "setpoints": dict(self.setpoints),
            "occupied": dict(self.occupied),
            "throughput_simulation": self._throughput_task is not None,
        }

    async def _listen(self) -> None:
        """Record incoming setpoints. The ramp loop is what moves the reported speed."""
        assert self._client is not None
        async for message in self._client.messages:
            topic = str(message.topic)
            position = topic.split("/")[-2] if "/" in topic else ""
            try:
                value = float(json.loads(message.payload.decode()))
            except (ValueError, json.JSONDecodeError):
                logger.warning("Ignoring unparseable setpoint on %s", topic)
                continue

            # Record the setpoint. _ramp_loop picks it up on its own tick and moves
            # self.speeds toward it -- this is no longer an instant assignment (#83).
            self.setpoints[position] = value
            self._setpoints_seen.set()
            logger.info("%s setpoint -> %s", topic, value)

    async def _ramp_loop(self) -> None:
        """Move each belt's actual speed toward its setpoint, one tick at a time.

        A belt is a thing with momentum. It is not a number that snaps (#83). The setpoint
        moves the target. This loop is the "slow PID controller". It drives the reported
        speed there. It runs regardless of who last moved the target -- the MQTT
        path (``_listen``) and the panel's REST path (``set_speed``) both only ever set
        ``self.setpoints`` now; this is the single place ``self.speeds`` changes.

        The same arithmetic handles a negative target identically to a positive one. A
        setpoint that reverses a belt's direction ramps straight through zero. No special-
        casing exists anywhere in this method.
        """
        while True:
            await asyncio.sleep(RAMP_TICK_SECONDS)
            step = self.ramp_rate * RAMP_TICK_SECONDS
            for position, target in self.setpoints.items():
                if target is None:
                    continue  # no setpoint has ever arrived for this belt
                current = self.speeds[position]
                if current == target:
                    continue  # already converged, nothing to do this tick
                diff = target - current
                if abs(diff) <= step:
                    # Snap exactly onto the target -- this is what makes convergence
                    # float-exact at rest, never an off-by-epsilon overshoot or undershoot.
                    new_value = target
                else:
                    new_value = current + step if diff > 0 else current - step
                self.speeds[position] = new_value
                await self._publish(self.speed_topic(position), new_value)

    async def _throughput_loop(self) -> None:
        """Cycle both light barriers while a belt runs, at a rate that tracks its speed.

        A crude stand-in for a workpiece traveling front-to-back. "The belts are running"
        means EITHER belt has nonzero speed. The unit has one shared simulation. It does
        not have one per belt. A single moving belt is enough to move material through it.
        """
        while True:
            speed = max(abs(self.speeds["left"]), abs(self.speeds["right"]))
            if speed == 0:
                if self.occupied["front"]:
                    await self.set_occupied("front", False)
                if self.occupied["back"]:
                    await self.set_occupied("back", False)
                await asyncio.sleep(THROUGHPUT_POLL_SECONDS)
                continue
            half_cycle = THROUGHPUT_PERIOD_AT_UNIT_SPEED / (2 * speed)
            await self.set_occupied("front", True)
            if not await self._hold_while_running(half_cycle):
                continue
            await self.set_occupied("front", False)
            await self.set_occupied("back", True)
            if not await self._hold_while_running(half_cycle):
                continue
            await self.set_occupied("back", False)

    async def _hold_while_running(self, duration: float) -> bool:
        """Sleep up to `duration`, in THROUGHPUT_POLL_SECONDS slices, bailing out early.

        Returns False the moment both belts have stopped. A stopped belt is noticed within
        one poll interval. It is not noticed only at the end of a half-cycle. That half-
        cycle would be computed from a now-stale speed. Returns True if the full duration
        elapsed with a belt still running.
        """
        elapsed = 0.0
        while elapsed < duration:
            if self.speeds["left"] == 0 and self.speeds["right"] == 0:
                return False
            step = min(THROUGHPUT_POLL_SECONDS, duration - elapsed)
            await asyncio.sleep(step)
            elapsed += step
        return True

    async def _publish_loop(self) -> None:
        """Publish all four values periodically at the configured interval.

        Periodic publishing ensures middleware convergence. A middleware starting after the
        device must receive values. On-change publishing alone would leave it blank. Publish
        all speeds and barrier states. Wait for the next interval. Repeat forever.
        """
        while True:
            await self.publish_once()
            await asyncio.sleep(self.publish_interval)

    async def _publish(self, topic: str, value) -> None:
        """Publish a single value to an MQTT topic.

        Encode the value as JSON. Send it to the broker on the given topic. Assert the
        client is connected first. All published messages pass through this method.
        """
        assert self._client is not None
        await self._client.publish(topic, json.dumps(value).encode())


async def main() -> None:  # pragma: no cover - manual run
    """Run a TransferUnit until interrupted, against a local broker."""
    logging.basicConfig(level=logging.INFO)
    async with TransferUnit():
        await asyncio.Event().wait()


if __name__ == "__main__":  # pragma: no cover - manual run
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
