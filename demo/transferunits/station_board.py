"""The Control Expert's station board — FastAPI routes + API for the live view (#82, ADR 0029).

This module holds all the board's HTTP surface: the HTML template route and the JSON API
routes the page polls and posts to. It gets grafted onto an already-running Controller
instance's own FastAPI app (the same way index.py grafts onto the Launcher's app, and
panel.py onto a PLC's). The board reads controller.units and calls controller.push() /
controller.rebuild_view() in-process — no second server, no second event loop.

The split between this file (routes + template) and control_station.py (runner: argparse,
uvicorn, Controller construction) is enforced by a guard test (tests/test_station_board_guard.py),
mirroring the index.py/launcher.py split (ADR 0029). This file names no subprocess, no signal,
no SIGTERM — it is pure HTTP, not process management.

The view mechanism (ADR 0033, ticket #80) means the board never constructs its own SPARQL
or walks its own tree. Every row comes from WiringPlan/ParameterBinding generically — no
domain term (tu:, TransferUnit, ConveyorBelt, hasConveyorSpeed, etc.) appears anywhere in
this file, in code or in the teaching copy. algorithm.py remains the only file in this demo
allowed to name one. ``tests/test_station_board_guard.py`` holds this down, so the claim
fails a test rather than merely aging badly.

The `inf:` CURIEs below are the exception, and they are not domain terms: `inf:hasValue`
appears inside a string the page *displays*, to show a reader the Python expression a write
performs. It is library vocabulary, and it is illustrative text rather than a query.

Usage::

    # In control_station.py's main():
    controller = Controller(...)
    hits = controller.view(algorithm.build_view_query())
    controller.wire_view(hits, class_scope=algorithm.unit_class_scope())
    state = algorithm.AlgorithmState(tick_seconds=args.tick)
    
    # Graft the board onto the same app:
    station_board.mount_onto(
        controller.app,
        controller=controller,
        algorithm_state=state,
        default_query=algorithm.build_view_query(),
    )
    
    # Then run_server(controller.app, ...)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from kapps_triplestore_interface import IRI

from . import algorithm
from .controller import Controller

logger = logging.getLogger(__name__)

# One entry per taught element on the page, mirroring index.py's own TEACH dict (#68's
# pattern) -- deliberately this file's own dict, not a re-import of index.py's: that one
# describes the factory-overview page's boxes (plc/middleware/broker/graph/launcher),
# none of which appear on this page's DOM. `file` always names a BACKEND source file, and
# a guard test asserts every path here exists on disk, the same way
# test_launcher_index_guard.py::test_teach_files_exist_on_disk does for index.py's TEACH.
TEACH: Dict[str, Dict[str, str]] = {
    "view-box": {
        "title": "The editable view heuristic",
        "what": "The frame (class and the ?resource/liveness join) is fixed; the "
        "textarea is the Control Expert's own heuristic. \"run\" rebuilds immediately; "
        "\"reset\" restores the even-index default. A malformed query or a zero-hit "
        "one is reported here, in place -- neither ever 500s the page.",
        "file": "demo/transferunits/controller.py",
    },
    "algo-bar": {
        "title": "The demonstration algorithm",
        "what": "Two modes: timed (a slow, visible tick) and event-driven (fires once "
        "when a light barrier's reading changes, quiescent otherwise). One global "
        "pause -- while it runs, every set control below is disabled; pause it to "
        "drive a unit by hand.",
        "file": "demo/transferunits/algorithm.py",
    },
    "station-card": {
        "title": "A station card",
        "what": "One card per unit the view selected. Collapsed is display-only: every "
        "connector stays live and every value stays current behind a shut card -- "
        "unlike the monitor's collapsed row (ADR 0032), which holds no data until "
        "expanded. Click the header to expand or collapse.",
        "file": "demo/transferunits/controller.py",
    },
    "param-row": {
        "title": "A parameter row",
        "what": "A plain number box and a set button -- no slider, because the ontology "
        "declares no bounds for a parameter. A write goes converging -> settled | "
        "rejected | diverged. Commanded sits beside actual; diverged means it stopped "
        "converging, not merely unequal.",
        "file": "src/kapps_semantic_middleware/connectors/rest_binding.py",
    },
    "show-iris": {
        "title": "Show IRIs",
        "what": "One toggle, three jobs: the full IRI, the real assignment expression "
        "(ADR 0027's skolemized shape made visible), and what prune_southbound "
        "stripped for this exact parameter -- the surface #78 asked someone to pick.",
        "file": "src/kapps_semantic_middleware/projection.py",
    },
}


@dataclass
class _BoardState:
    """Mutable holder for the board's currently active heuristic text.
    
    Created once inside mount_onto and closed over by the route handlers.
    Initialized to default_query; updated by /api/view/run and /api/view/reset.
    Controller.rebuild_view sets controller._current_view_query as a side effect,
    but this file must not rely on that attribute existing before the first rebuild.
    """
    current_query: str


def _local_fragment(iri_string: str) -> str:
    """Extract the local name from an IRI string (after the last '#' or '/').
    
    Generic helper — no domain knowledge. Used for assignment_expression display
    and for rendering facet keys/pruned properties in a human-readable form.
    """
    for sep in ("#", "/"):
        if sep in iri_string:
            return iri_string.rsplit(sep, 1)[1]
    return iri_string


def _first_value(value: Any) -> Any:
    """Return the first element of a list/tuple, or the value itself if not a sequence.
    
    Matches semantic.first()'s behaviour — metadata values are lists per normalize_metadata,
    but facets should show the scalar.
    """
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value


def _get_label(ogm: Any, resource_iri: IRI) -> Optional[str]:
    """Query the graph for rdfs:label on a resource.
    
    Generic — rdfs:label is not a domain term. Same query-and-bindings-shape idiom
    Controller.discover_resources uses (controller.py).
    """
    from kapps_semantic_middleware.vocabulary import RDFS

    sparql = f"""
    SELECT ?label WHERE {{
        <{resource_iri}> <{RDFS.label}> ?label .
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    if bindings:
        return str(bindings[0]["label"])
    return None


def _get_class_iri(ogm: Any, resource_iri: IRI) -> Optional[str]:
    """The asserted domain class of a resource, or ``None`` if the graph does not say.

    Delegates to the library's ``class_of``. This used to repeat that query, and its own
    copy of the meta-type namespace list, here in the demo.

    The one difference is what happens when there is no answer. ``class_of`` raises, because
    a middleware that cannot resolve a ClassSpec cannot start. A board is a screen: an
    unreadable resource becomes a row with a blank class, and the other rows still render.
    """
    from kapps_semantic_middleware.connectors.wiring import class_of

    try:
        return str(class_of(ogm, resource_iri))
    except ValueError:
        return None


def _find_wiring_for_resource(
    controller: Controller, resource_iri_str: str
) -> Optional[Tuple[IRI, Any]]:
    """Find the (resource_iri, WiringPlan) tuple for a given resource IRI string.
    
    Scans controller._view_wirings — a List[Tuple[IRI, WiringPlan]].
    Returns None if not found (a rebuild race is possible).
    """
    for iri, wiring in controller._view_wirings:
        if str(iri) == resource_iri_str:
            return iri, wiring
    return None


def _walk_to_holder_node(instance: Any, path_steps: Tuple[Tuple[str, str], ...]) -> Optional[Any]:
    """Walk a materialized instance through binding.path_steps to find the live holder node.
    
    path_steps is a tuple of (field_name, child_iri) hops. For each hop:
    - getattr(node, field_name, None) or [] to get children
    - next((c for c in children if str(getattr(c, "id", "")) == child_id), None)
    
    Returns None if any hop fails (the live tree does not have this hop right now).
    """
    node = instance
    for field_name, child_id in path_steps:
        children = getattr(node, field_name, None) or []
        node = next((c for c in children if str(getattr(c, "id", "")) == child_id), None)
        if node is None:
            return None
    return node


def mount_onto(
    app: FastAPI,
    *,
    controller: Controller,
    algorithm_state: algorithm.AlgorithmState,
    default_query: str,
) -> None:
    """Graft the station board's routes and template onto an existing FastAPI app.
    
    Called exactly once, from control_station.py's main(), after controller.wire_view(...)
    and before run_server(...). The board reads controller.units and calls
    controller.push() / controller.rebuild_view() in-process — no second server, no
    second event loop.
    
    Root route replacement: app already has a GET / route (installed by
    SemanticMiddleware.app's property). Remove it and install our own, using the exact
    same technique that property itself uses (middleware.py): iterate list(app.routes),
    find the one whose path == "/" and "GET" in methods, app.routes.remove(route), then
    register our own @app.get("/", response_class=HTMLResponse). Also reset
    app.openapi_schema = None afterward. Do NOT touch the /favicon.ico route — it
    already exists and answers 204.
    
    Args:
        app: A FastAPI app, already controller.app (so favicon + root-welcome routes
            already exist).
        controller: demo.transferunits.controller.Controller instance, already serving.
        algorithm_state: demo.transferunits.algorithm.AlgorithmState instance (mutable,
            shared).
        default_query: The SPARQL query text to use as the initial/default heuristic.
    """
    # Mutable board state — single object all routes share.
    board_state = _BoardState(current_query=default_query)
    
    # Root route replacement — mirror SemanticMiddleware.app's technique (middleware.py).
    for route in list(app.routes):
        methods = getattr(route, "methods", None) or set()
        if getattr(route, "path", None) == "/" and "GET" in methods:
            app.routes.remove(route)
    
    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        """Serve the station board's HTML template, with this file's own TEACH dict
        injected the same way index.py injects its own (this page names none of
        index.py's boxes, so it carries its own dict rather than importing that one)."""
        template_path = Path(__file__).parent / "templates" / "station_board.html"
        page = template_path.read_text(encoding="utf-8")
        return HTMLResponse(page.replace("__TEACH__", json.dumps(TEACH)))
    
    # Reset OpenAPI schema so /docs and /openapi.json describe the routes that actually exist.
    app.openapi_schema = None
    
    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        """Return the current board state snapshot, including units and parameters.
        
        Re-runs the view on every poll (so the card set tracks the graph unattended).
        ViewDiff.error becomes the top-level view_error field — a malformed heuristic
        produces view_error with a message, never an HTTP 500.
        """
        from kapps_semantic_middleware.vocabulary import INF, SVC
        
        # Re-run the view on every poll.
        diff = await controller.rebuild_view(board_state.current_query)
        view_error = diff.error
        
        # Algorithm state.
        now = time.monotonic()
        seconds_since_last_tick = (
            (now - algorithm_state.last_tick_at) if algorithm_state.last_tick_at is not None else None
        )
        waiting_seconds = (
            (now - algorithm_state.waiting_since) if algorithm_state.waiting_since is not None else None
        )
        
        algorithm_data = {
            "mode": algorithm_state.mode.value,
            "paused": algorithm_state.paused,
            "tick_seconds": algorithm_state.tick_seconds,
            "seconds_since_last_tick": seconds_since_last_tick,
            "waiting_seconds": waiting_seconds,
        }
        
        # Units — one entry per controller.units item.
        units_data: List[Dict[str, Any]] = []
        for resource_iri_str, instance in controller.units.items():
            resource_iri = IRI(resource_iri_str)
            
            # Label and class_iri from the graph.
            label = _get_label(controller.ogm, resource_iri)
            class_iri = _get_class_iri(controller.ogm, resource_iri)
            
            # Liveness from controller.liveness_of().
            unreachable, age_seconds = controller.liveness_of(resource_iri)
            
            # Find the WiringPlan for this resource.
            wiring_tuple = _find_wiring_for_resource(controller, resource_iri_str)
            if wiring_tuple is None:
                # No wiring found — empty parameters list (rebuild race possible).
                units_data.append({
                    "resource_iri": resource_iri_str,
                    "label": label,
                    "class_iri": class_iri,
                    "unreachable": unreachable,
                    "age_seconds": age_seconds,
                    "parameters": [],
                })
                continue
            
            _, wiring = wiring_tuple
            
            # Parameters — one entry per binding in wiring.bindings.
            parameters_data: List[Dict[str, Any]] = []
            for binding in wiring.bindings:
                # Walk to the live holder node.
                holder_node = _walk_to_holder_node(instance, binding.path_steps)
                if holder_node is None:
                    continue
                
                # Get the parameter list.
                param_list = getattr(holder_node, binding.field_id, None) or []
                if not param_list:
                    continue
                
                param_node = param_list[0]
                
                # Value from inf:hasValue.
                raw_value = getattr(param_node, INF.hasValue.lined, None)
                value = raw_value[0] if raw_value else None
                
                # What pruning stripped for this exact parameter (#78's picked surface).
                # Computed before the facets below, because the facets are defined in
                # terms of it.
                pruned_set = wiring.southbound_by_property.get(str(binding.parameter_property), frozenset())
                pruned_list = sorted(_local_fragment(p) for p in pruned_set)

                # Facets -- the parameter's own descriptive properties, and only those.
                #
                # Excluded on two different grounds, which is why this is not one set.
                # `accessMode` and `address` are *rendered elsewhere* on the row, so
                # repeating them as facets would duplicate them. The southbound markers
                # are excluded because they are **not this consumer's to show**: the
                # controller reaches its peers over REST and has no MQTT connector, so a
                # broker address on this page is the ADR 0028 boundary leaking from the
                # consumer side (ticket #78).
                #
                # Derived from the ontology's own prune set rather than from a literal
                # list, so a protocol marker declared southbound later is excluded here
                # with no code change -- the same fail-closed property ADR 0028 gives the
                # serving path, and the reason this is not a hardcoded allow-list: #82
                # requires a parameter added to the seed to render with no code change,
                # which an allow-list would break.
                #
                # Note this does not make the binding stop *holding* them. The controller
                # still has the peer's broker address in `ParameterBinding.metadata` for
                # the life of the process. Fixing that needs kapps_ogm to fetch a
                # parameter node partially -- specified as SAWeindel/kapps_ogm#23, and
                # recorded as accepted debt on #78.
                rendered_elsewhere = {str(INF.accessMode), str(SVC.address)}
                facets: Dict[str, Any] = {}
                for key, val in binding.metadata.items():
                    if key in rendered_elsewhere or key in pruned_set:
                        continue
                    facets[_local_fragment(key)] = _first_value(val)

                # Commanded value.
                commanded_val = controller.writes.commanded_for(
                    binding.resource_iri, binding.field_id
                )
                if commanded_val is not None:
                    commanded_data = {
                        "value": commanded_val.value,
                        "origin": commanded_val.origin,
                        "age_seconds": now - commanded_val.at,
                    }
                else:
                    commanded_data = None
                
                # Assignment expression — literal "inf:hasValue" substring (illustrative CURIE).
                holder_fragment = _local_fragment(str(binding.resource_iri))
                assignment_expr = f'{holder_fragment}.{binding.field_id}[0]["inf:hasValue"][0] = {value!r}'
                
                # The write's status, judged server-side: this poll *is* the observation
                # that advances "has it stopped converging?". The page renders this
                # verdict rather than reaching its own, so the rule that `diverged` means
                # stopped converging (not merely unequal) is testable -- #82 requires
                # rejected and diverged to be distinguishable in a test.
                status = controller.writes.observe(binding.resource_iri, binding.field_id, value)

                parameters_data.append({
                    "holder_iri": str(binding.resource_iri),
                    "field_id": binding.field_id,
                    "field_iri": str(binding.parameter_property),
                    "label": binding.label,
                    "access_mode": binding.access_mode,
                    "value": value,
                    "facets": facets,
                    "commanded": commanded_data,
                    "status": status.value if status is not None else None,
                    "status_error": controller.writes.error_for(
                        binding.resource_iri, binding.field_id
                    ),
                    "pruned": pruned_list,
                    "assignment_expression": assignment_expr,
                })
            
            units_data.append({
                "resource_iri": resource_iri_str,
                "label": label,
                "class_iri": class_iri,
                "unreachable": unreachable,
                "age_seconds": age_seconds,
                "parameters": parameters_data,
            })
        
        return JSONResponse({
            "query": board_state.current_query,
            "default_query": default_query,
            "view_error": view_error,
            "algorithm": algorithm_data,
            "units": units_data,
        })
    
    @app.post("/api/view/run")
    async def api_view_run(request: Request) -> JSONResponse:
        """Run rebuild_view with a new query from the request body.
        
        Body: {"query": "<sparql text>"}. A malformed heuristic produces ok: false
        with the error message in the JSON body, never an HTTP 500.
        """
        try:
            body = await request.json()
            query = body["query"]
        except (KeyError, ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail=f"Invalid request body: {e}")
        
        board_state.current_query = query
        diff = await controller.rebuild_view(query)
        
        return JSONResponse({
            "ok": diff.error is None,
            "error": diff.error,
            "joiners": [str(x) for x in diff.joiners],
            "leavers": [str(x) for x in diff.leavers],
            "unchanged": [str(x) for x in diff.unchanged],
        })
    
    @app.post("/api/view/reset")
    async def api_view_reset() -> JSONResponse:
        """Reset to the default query.
        
        Same as /api/view/run but with default_query instead of a request body value.
        """
        board_state.current_query = default_query
        diff = await controller.rebuild_view(default_query)
        
        return JSONResponse({
            "ok": diff.error is None,
            "error": diff.error,
            "joiners": [str(x) for x in diff.joiners],
            "leavers": [str(x) for x in diff.leavers],
            "unchanged": [str(x) for x in diff.unchanged],
        })
    
    @app.post("/api/algorithm/pause")
    async def api_algorithm_pause(request: Request) -> JSONResponse:
        """Toggle the algorithm's pause state.
        
        Body: {"paused": true|false}. Sets algorithm_state.paused.
        """
        try:
            body = await request.json()
            paused = bool(body["paused"])
        except (KeyError, ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail=f"Invalid request body: {e}")
        
        algorithm_state.paused = paused
        return JSONResponse({"ok": True})
    
    @app.post("/api/algorithm/mode")
    async def api_algorithm_mode(request: Request) -> JSONResponse:
        """Change the algorithm's firing mode.
        
        Body: {"mode": "timed"|"event_driven"}. Validates against AlgorithmMode's values.
        """
        try:
            body = await request.json()
            mode_str = body["mode"]
        except (KeyError, ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail=f"Invalid request body: {e}")
        
        # Validate against AlgorithmMode's values.
        valid_modes = {m.value for m in algorithm.AlgorithmMode}
        if mode_str not in valid_modes:
            raise HTTPException(status_code=422, detail=f"Unrecognized mode: {mode_str}")
        
        algorithm_state.mode = algorithm.AlgorithmMode(mode_str)
        return JSONResponse({"ok": True})
    
    @app.post("/api/set")
    async def api_set(request: Request) -> JSONResponse:
        """Set a parameter value and push it to the unit.

        Body: {"resource_iri": "...", "holder_iri": "...", "field_id": "...", "value": <any>}.

        Steps:
        0. 409 while the algorithm is running -- the *server-side* half of "set controls
           are inert while the algorithm runs, and live while it is paused" (#82). The
           frontend's disabled buttons are the other half; neither is trusted alone.
        1. Look up instance = controller.units.get(resource_iri); 404 if absent.
        2. Find the WiringPlan for that resource_iri; 404 if not found.
        3. Find the matching binding; 404 if none matches.
        4. Re-walk instance through binding.path_steps; 404 if walk fails.
        5. Get param_list; 422 if empty.
        6. Coerce value into one-element list shape.
        7. setattr(param_list[0], INF.hasValue.lined, new_value).
        8. controller.writes.record_commanded(...) BEFORE the push.
        9. await controller.push(...); on exception, return {"ok": False, "error": ...} with status 200.
        10. On success, return {"ok": True}.
        """
        from kapps_semantic_middleware.vocabulary import INF

        if not algorithm_state.paused:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "The algorithm is running. Pause it to drive a unit by hand.",
                },
                status_code=409,
            )

        try:
            body = await request.json()
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail=f"Invalid JSON: {e}")

        # Extract required fields.
        try:
            resource_iri_str = body["resource_iri"]
            holder_iri_str = body["holder_iri"]
            field_id = body["field_id"]
            value = body["value"]
        except KeyError as e:
            raise HTTPException(status_code=422, detail=f"Missing required field: {e}")
        
        # 1. Look up instance.
        instance = controller.units.get(resource_iri_str)
        if instance is None:
            raise HTTPException(status_code=404, detail=f"Unit not loaded: {resource_iri_str}")
        
        # 2. Find the WiringPlan.
        wiring_tuple = _find_wiring_for_resource(controller, resource_iri_str)
        if wiring_tuple is None:
            raise HTTPException(status_code=404, detail=f"No wiring found for: {resource_iri_str}")
        
        _, wiring = wiring_tuple
        
        # 3. Find the matching binding.
        matching_binding = None
        for binding in wiring.bindings:
            if str(binding.resource_iri) == holder_iri_str and binding.field_id == field_id:
                matching_binding = binding
                break
        
        if matching_binding is None:
            raise HTTPException(status_code=404, detail=f"No binding found for {holder_iri_str}#{field_id}")
        
        # 4. Re-walk to holder node.
        holder_node = _walk_to_holder_node(instance, matching_binding.path_steps)
        if holder_node is None:
            raise HTTPException(status_code=404, detail=f"Cannot navigate to holder node for {holder_iri_str}")
        
        # 5. Get param_list.
        param_list = getattr(holder_node, matching_binding.field_id, None) or []
        if not param_list:
            raise HTTPException(status_code=422, detail=f"Empty parameter list for {matching_binding.field_id}")
        
        # 6. Coerce value.
        new_value = value if isinstance(value, list) else [value]
        
        # 7. Set the value.
        setattr(param_list[0], INF.hasValue.lined, new_value)
        
        # 8. Record commanded BEFORE push. The tracker normalizes the one-element
        # inf:hasValue list to the scalar /api/state reports, so both write paths can
        # hand over whatever shape they already hold.
        controller.writes.record_commanded(
            matching_binding.resource_iri,
            matching_binding.field_id,
            new_value,
            origin="operator",
        )
        
        # 9. Push -- controller.push_parameter, not controller.push(): push() re-consumes
        # the whole resource through the persistence fan-out, which the base framework's
        # own PersistedConnector._notify_synced_connectors catches and only logs a
        # sibling connector's failure from (by design -- one connector's failure must not
        # end every other one's sync). That means push() can never actually report a PUT
        # failure to us. push_parameter reaches the one write connector responsible for
        # this exact field directly, so a genuine failure propagates here instead of
        # being swallowed -- see its own docstring on Controller.
        try:
            await controller.push_parameter(
                resource_iri_str, holder_iri_str, matching_binding.field_id, param_list[0]
            )
        except Exception as exc:
            # Recorded on the controller, not just returned: `rejected` has to survive a
            # page reload and be visible to the next poll, and #82 requires it to be
            # distinguishable from `diverged` in a test as well as on screen.
            controller.writes.record_rejected(
                matching_binding.resource_iri, matching_binding.field_id, str(exc)
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)

        # 10. Success.
        return JSONResponse({"ok": True})
