"""Measure one control lap, so #82's default tick can be set from data rather than reasoning.

A lap is PUT -> unit middleware -> MQTT -> PLC -> MQTT back -> connector read: the time from
commanding a value to the middleware having *observed* the device actually at it.

**It cannot be measured on the ADR 0017 route.** Under ADR 0024's locator pattern a parameter
has one value slot, and the PUT writes the commanded value straight into it -- so the route
reports the target the instant the PUT returns, whatever the belt is doing. Timing that
measures the HTTP handler (~0.02 s) and nothing else. The lap has to be timed against the
device's own state and against the inbound publish that carries it back.

This could not be measured at all before #94: the belt froze short of its setpoint, so a lap
never completed. That is why ``DEFAULT_TICK_SECONDS`` shipped reasoned from
``rest_binding.DEFAULT_POLL_INTERVAL_SECONDS`` rather than measured.

Run on demand -- marked ``lap`` and deselected by default, because it measures rather than
asserts and its timings are machine-dependent:

    pytest tests/test_lap_measurement.py -m lap -s
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import pytest

from conftest import requires_graphdb
from test_southbound_echo_integration import (  # noqa: F401  (fixtures)
    PORT,
    _await_true,
    _put,
    _read,
    _route_ending,
    _speed_route_suffix,
    _wait_for_value,
    running_unit,
)

import examples.seed as seed
from demo.transferunits.plc.transfer_unit import DEFAULT_RAMP_RATE, TransferUnit
from kapps_semantic_middleware.vocabulary import INF

DISTANCES = (0.05, 0.5, 1.5, 3.0)
"""One ramp step, then a short hop, a typical setpoint change, and a near-full-range move.
The first isolates transport latency with essentially no ramp in it; the rest show how much
of a lap is #83's momentum. The tick must exceed the slowest lap the algorithm can provoke,
not the average one."""

ACTUAL_TOPIC = "TransferUnit1/ConveyorBelt/left/speed"


class _ActualSpy:
    """Timestamped inbound publishes on the belt's *actual* speed topic."""

    def __init__(self):
        self.seen: list[tuple[float, float]] = []
        self._ready = asyncio.Event()
        self._task: asyncio.Task | None = None

    def first_at_or_after(self, value: float, since: float) -> float | None:
        for stamp, seen in self.seen:
            if stamp >= since and abs(seen - value) < 1e-9:
                return stamp
        return None

    async def _run(self, host: str, port: int):
        import aiomqtt

        async with aiomqtt.Client(hostname=host, port=port) as client:
            await client.subscribe(ACTUAL_TOPIC)
            self._ready.set()
            async for message in client.messages:
                self.seen.append(
                    (time.monotonic(), json.loads(message.payload.decode()))
                )


@contextlib.asynccontextmanager
async def _spying_on_actuals(host: str, port: int):
    spy = _ActualSpy()
    spy._task = asyncio.create_task(spy._run(host, port))
    try:
        await asyncio.wait_for(spy._ready.wait(), timeout=10.0)
        yield spy
    finally:
        spy._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await spy._task


@requires_graphdb
@pytest.mark.lap
@pytest.mark.asyncio
async def test_measure_one_lap(running_unit):  # noqa: F811
    mw, host, port = running_unit
    route = await _route_ending(mw, _speed_route_suffix(seed.CONVEYOR_BELT_LEFT))
    url = f"http://127.0.0.1:{PORT}{route}"

    rows = []
    async with TransferUnit(
        broker=host,
        port=port,
        publish_interval=0.5,
        initial_speeds={"left": 0.0, "right": 0.0},
    ) as unit:
        assert await _wait_for_value(url, 0.0), "startup publish never reached persistence"

        async with _spying_on_actuals(host, port) as spy:
            for distance in DISTANCES:
                target = round(unit.speeds["left"] + distance, 4)
                [node] = await _read(url)
                node[INF.hasValue.lined] = [target]

                started = time.monotonic()
                await _put(url, [node])

                # Half 1: the device actually gets there (#83's ramp lives here).
                await _await_true(
                    lambda t=target: abs(unit.speeds["left"] - t) < 1e-9,
                    40.0,
                    f"the belt never reached {target}",
                )
                settled = time.monotonic()

                # Half 2: that settled value comes back over MQTT and is observed.
                await _await_true(
                    lambda t=target, s=started: spy.first_at_or_after(t, s) is not None,
                    20.0,
                    f"the settled value {target} was never published back",
                )
                observed = spy.first_at_or_after(target, started)
                assert observed is not None

                rows.append(
                    (distance, target, settled - started, observed - started)
                )
                await asyncio.sleep(0.5)

    print("\n\n=== One control lap: PUT -> middleware -> MQTT -> PLC -> MQTT -> read ===")
    print(f"    ramp rate {DEFAULT_RAMP_RATE} m/s per second, publish interval 0.5 s\n")
    print(f"    {'ramp':>6}  {'target':>7}  {'device settled':>15}  {'observed back':>14}")
    for distance, target, settle, observe in rows:
        print(f"    {distance:>6}  {target:>7}  {settle:>13.2f} s  {observe:>12.2f} s")
    slowest = max(observe for _, _, _, observe in rows)
    print(f"\n    slowest full lap: {slowest:.2f} s")
    print("=========================================================================\n")
