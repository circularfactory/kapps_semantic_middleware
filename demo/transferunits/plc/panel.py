"""Panel — FastAPI web interface for the TransferUnit PLC.

This module provides the HTTP interface for operators to view and control the PLC.
It has no MQTT logic and no asyncio imports — those belong in transfer_unit.py.

The panel polls /api/state every 500ms and displays:
- Actual belt speeds (from PLC speeds)
- Commanded speeds (from PLC setpoints) — shown whenever the belt is short of its
  setpoint, which under #83's ramp is most of every set
- Whether a belt is still converging on its setpoint, or has stopped short of it
- Light barrier states (occupied/clear)

Tooltips name the backend files (transfer_unit.py set_speed(), etc.)
"""

import time
from typing import Callable, Dict, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .transfer_unit import TransferUnit

app = FastAPI()
plc: TransferUnit | None = None

DEFAULT_STILL_SECONDS = 6.0
"""How long a belt may sit unmoved, short of its setpoint, before it reads ``diverged``.

Deliberately the same number as ``controller.py``'s constant of the same name, and
deliberately *not* imported from it: ``controller.py`` pulls in ``SemanticMiddleware``, and
ADR 0029 keeps the PLC tier free of middleware knowledge -- the guard test in
``tests/test_plc_guard.py`` exists to hold exactly that line. Root ADR 0004 prefers
duplication to sharing in ``demo/`` for this reason.
"""


class ConvergenceTracker:
    """Whether each belt is still closing on its setpoint, or has stopped short of it.

    #81 defined ``diverged`` as **stopped converging**, not *unequal*, and amended #31 to
    say so. The panel had the older reading -- ``abs(cmd - speed) > 1e-9`` -- which #83's
    ramp made permanently true during every set, so a healthy belt rendered as a fault the
    whole way to its setpoint (#93, item 3).

    Judged on the clock, not on polls, for the reason #82 found the hard way: a poll is a
    browser asking. Counting polls lets two open tabs call a belt stuck in half the time,
    and a closed page never call it at all.

    Args:
        still_seconds: How long an unmoved belt short of its setpoint stays ``converging``.
        clock: Monotonic seconds source, injectable so a test can age a belt without
            sleeping through it.
    """

    def __init__(
        self,
        *,
        still_seconds: float = DEFAULT_STILL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._still_seconds = still_seconds
        self._clock = clock
        self._last_speed: Dict[str, float] = {}
        self._unchanged_since: Dict[str, float] = {}

    def status(self, position: str, speed: float, setpoint: Optional[float]) -> Optional[str]:
        """``settled`` | ``converging`` | ``diverged``, or ``None`` if nothing is commanded.

        Call once per belt per poll. Only *movement* resets the clock, so polling harder
        cannot make a belt diverge sooner.
        """
        if setpoint is None:
            return None

        now = self._clock()
        previous = self._last_speed.get(position)
        if previous is None or abs(speed - previous) > 1e-9:
            self._last_speed[position] = speed
            self._unchanged_since[position] = now

        if abs(speed - setpoint) <= 1e-9:
            return "settled"
        if now - self._unchanged_since[position] >= self._still_seconds:
            return "diverged"
        return "converging"


convergence = ConvergenceTracker()


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Answer the browser's automatic favicon request with a bare 204 (#89).

    The panel ships no icon asset. Left unanswered, every page load logs a 404 for
    this request -- the only console error on an otherwise clean load. A 204 says
    "nothing here, and that's fine" without inventing an asset this demo has no
    branding to put in.
    """
    return Response(status_code=204)


def get_plc() -> TransferUnit:
    """Get the PLC instance, raising if not configured."""
    if plc is None:
        raise RuntimeError("PLC not configured. Call configure_plc() first.")
    return plc


def configure_plc(instance: TransferUnit) -> None:
    """Configure the PLC instance for the panel."""
    global plc
    plc = instance


@app.get("/api/state")
async def state() -> JSONResponse:
    """Return the current PLC state snapshot, plus each belt's convergence status.

    The status is judged here rather than in the page's script, for the reason #82 gives
    for the station board: a verdict the browser reaches is a verdict no test can hold.
    ``transfer_unit.py`` stays out of it -- it reports what the belt is doing, and the
    panel decides what that means (ADR 0029's tier split).
    """
    snapshot = get_plc().snapshot()
    snapshot["belt_status"] = {
        position: convergence.status(
            position, speed, snapshot["setpoints"].get(position)
        )
        for position, speed in snapshot["speeds"].items()
    }
    return JSONResponse(snapshot)


@app.post("/api/speed/{position}")
async def set_speed(position: str, request: Request) -> JSONResponse:
    """Set the speed for a belt position."""
    if position not in ("left", "right"):
        raise HTTPException(status_code=404, detail=f"Unknown position: {position}")

    try:
        body = await request.json()
        value = float(body["value"])
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"Invalid value: {e}")

    await get_plc().set_speed(position, value)
    return JSONResponse(get_plc().snapshot())


@app.post("/api/barrier/{position}")
async def set_barrier(position: str, request: Request) -> JSONResponse:
    """Set the occupancy state for a light barrier."""
    if position not in ("front", "back"):
        raise HTTPException(status_code=404, detail=f"Unknown position: {position}")

    try:
        body = await request.json()
        occupied = bool(body["occupied"])
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"Invalid value: {e}")

    await get_plc().set_occupied(position, occupied)
    return JSONResponse(get_plc().snapshot())


@app.post("/api/throughput")
async def set_throughput(request: Request) -> JSONResponse:
    """Start or stop the throughput simulation."""
    try:
        body = await request.json()
        enabled = bool(body["enabled"])
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"Invalid value: {e}")

    await get_plc().set_throughput_simulation(enabled)
    return JSONResponse(get_plc().snapshot())


#: Marker in panel.html that the unit graphic is substituted into.
GRAPHIC_SLOT = "<!--UNIT-GRAPHIC-->"


@app.get("/", response_class=HTMLResponse)
async def panel() -> HTMLResponse:
    """Serve the panel HTML page, with the unit graphic inlined.

    The graphic is *inlined* rather than served from ``/static`` and referenced with an
    ``<img>``: the page's script addresses ids inside it -- it sets each chain's
    animation-duration and play-state from the published speed, swaps each barrier group
    between ``clear`` and ``occupied``, and writes live values into the labels. An image
    in an ``<img>`` has no reachable DOM, so none of that would work.

    It stays its own file rather than being pasted into the template so the drawing can be
    edited as a drawing, and so the id contract it documents lives beside the shapes.
    """
    from pathlib import Path
    here = Path(__file__).parent
    html_content = (here / "templates" / "panel.html").read_text(encoding="utf-8")
    graphic = (here / "static" / "transferunit.svg").read_text(encoding="utf-8")
    return HTMLResponse(html_content.replace(GRAPHIC_SLOT, graphic))
