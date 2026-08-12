"""Index — FastAPI web interface for the Launcher's live topology page.

Routes and the template only. No subprocess and no seed here (ADR 0029) — the page only
reads state the launcher machinery in launcher.py already tracks.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .launcher import Factory

app = FastAPI()
factory: Factory | None = None
launcher_address: str = ""


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Answer the browser's automatic favicon request with a bare 204 (#89).

    The launcher ships no icon asset. Left unanswered, every page load logs a 404 for
    this request -- the only console error on an otherwise clean load. A 204 says
    "nothing here, and that's fine" without inventing an asset this demo has no
    branding to put in.
    """
    return Response(status_code=204)

# One entry for each box and each arrow on the page. `file` names a BACKEND source file,
# never a frontend one (ADR 0029). A test asserts that every path here exists on disk, so
# the page stays honest when a file moves.
TEACH = {
    "plc": {
        "title": "The mock PLC, and the panel it serves",
        "what": "It speaks MQTT, and it knows nothing about the graph, the ontology or the "
        "middleware. That asymmetry is the point of the demonstration. Its panel acts on "
        "the device directly.",
        "file": "demo/transferunits/plc/transfer_unit.py",
    },
    "middleware": {
        "title": "One middleware instance, for one unit",
        "what": "Every middleware carries a live activity feed of its own wiring and traffic "
        "(ADR 0029) -- the \"activity\" link beside its address opens it. The same instance "
        "also reads its unit out of the graph and builds its connectors from what it finds "
        "there; no topic and no address is written in its code.",
        "file": "src/kapps_semantic_middleware/activity.py",
    },
    "controller": {
        "title": "The control station",
        "what": "It lists every resource it finds in the graph, and it drives any of them. "
        "No endpoint is configured into it. It carries the same live activity feed as a "
        "middleware (ADR 0022) -- the \"activity\" link beside its address opens it.",
        "file": "src/kapps_semantic_middleware/rest_router.py",
    },
    "broker": {
        "title": "This unit's own MQTT broker",
        "what": "Its middleware brings this up itself, the moment it registers its first "
        "MQTT connector -- on a daemon thread, so it dies with the middleware process, no "
        "teardown code required. No two units share transport; the knowledge graph is the "
        "only thing any two units in this factory have in common.",
        "file": "demo/transferunits/middleware.py",
    },
    "graph": {
        "title": "The knowledge graph",
        "what": "It holds the units, their parameters, the address of every topic and the "
        "address of every service. It is the only thing the controller reads.",
        "file": "src/kapps_semantic_middleware/registration.py",
    },
    "launcher": {
        "title": "The Launcher",
        "what": "It seeds the graph and it starts every process on this page. It never "
        "appears in the graph, because nobody dispatches an Operation to bolt a conveyor to "
        "the floor (ADR 0029).",
        "file": "demo/transferunits/launcher.py",
    },
    "arrow-plc-broker": {
        "title": "The device publishes",
        "what": "The PLC publishes four values and subscribes to two setpoints. The unit "
        "index is the first segment of every topic.",
        "file": "demo/transferunits/plc/transfer_unit.py",
    },
    "arrow-broker-mw": {
        "title": "The middleware subscribes",
        "what": "The middleware subscribes to the same topics. It learned every one of them "
        "from the graph.",
        "file": "src/kapps_semantic_middleware/connectors/wiring.py",
    },
    "arrow-mw-graph": {
        "title": "The middleware reads and registers",
        "what": "It reads its unit from the graph at startup. It then registers its own "
        "Service and writes its address there.",
        "file": "src/kapps_semantic_middleware/registration.py",
    },
    "arrow-graph-ctrl": {
        "title": "The controller discovers",
        "what": "The controller finds the units here, and it finds their addresses here. "
        "This arrow is the reason no endpoint is configured into it.",
        "file": "src/kapps_semantic_middleware/registration.py",
    },
    "arrow-ctrl-mw": {
        "title": "The controller drives a unit",
        "what": "It sets a speed over REST, at an address it read from the graph. One "
        "parameter has one address.",
        "file": "src/kapps_semantic_middleware/rest_router.py",
    },
}

# Where the Launcher learned an address. The page marks every one of them.
SOURCES = {
    "pipe": "The Launcher read this address from one line of the process stdout. A PLC holds "
    "no graph credentials, so it cannot announce itself any other way.",
    "graph": "The Launcher read this address from svc:address in the knowledge graph. The "
    "process wrote it there itself, when it registered its Service.",
    "flag": "The Launcher chose this address, and it passed the address as a command-line "
    "flag.",
    "env": "This address comes from the GRAPHDB environment variables.",
}


def configure_factory(instance: Factory, address: str) -> None:
    """Configure the Factory instance and this launcher's own address for the page."""
    global factory, launcher_address
    factory = instance
    launcher_address = address


def get_factory() -> Factory:
    """Get the Factory instance, raising if not configured."""
    if factory is None:
        raise RuntimeError("Factory not configured. Call configure_factory() first.")
    return factory


@app.get("/api/state")
async def api_state() -> JSONResponse:
    """Return the current factory state snapshot."""
    return JSONResponse(get_factory().state_snapshot(launcher_address))


@app.post("/api/stop/{index}")
async def api_stop_unit(index: int) -> JSONResponse:
    """Stop one unit, and return the resulting state."""
    await asyncio.to_thread(get_factory().stop_unit, index)
    return JSONResponse(get_factory().state_snapshot(launcher_address))


@app.post("/api/stop")
async def api_stop_all() -> JSONResponse:
    """Stop the whole factory, and return the resulting state."""
    await asyncio.to_thread(get_factory().stop_all)
    return JSONResponse(get_factory().state_snapshot(launcher_address))


@app.get("/api/output/{index}/{part}")
async def api_output(index: int, part: str) -> JSONResponse:
    """The last 20 lines of output of one process. index=0 selects the controller."""
    return JSONResponse({"lines": get_factory().output(index, part)})


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the index HTML page."""
    template_path = Path(__file__).parent / "templates" / "index.html"
    page = template_path.read_text(encoding="utf-8").replace("__TEACH__", json.dumps(TEACH))
    return HTMLResponse(page.replace("__SOURCES__", json.dumps(SOURCES)))
