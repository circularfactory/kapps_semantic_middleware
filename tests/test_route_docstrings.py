"""The routes of a workflow and of a state property say what the registered function does.

The framework generates a workflow's routes, and KSM mounts a state property's route. Each run
route of a workflow, and the route of a state property, takes the first line of the registered
function's docstring as its summary and the whole docstring as its description. The instance's
API description (`/openapi.json`, which `/docs` renders) carries them, and the dashboard's
Services box reads them from there. The workflow's description and interrupt routes keep the
framework's own summaries, and a function without a docstring keeps the default summary and no
description.

Offline: a resource-mode instance whose store is never reached, read through a test client.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable

from fastapi.testclient import TestClient

from kapps_semantic_middleware import Mode, SemanticMiddleware
from kapps_semantic_middleware.dashboard import enable_dashboard

RESOURCE = "http://example.org/test#Module1"
SERVICE_CLASS = "http://example.org/test#ModuleService"
CAPABILITY_CLASS = "http://example.org/test#SpawnCapability"
WORKFLOW_CLASS = "http://example.org/test#SpawnWorkflow"
STATE_PROPERTY_CLASS = "http://example.org/test#DoorState"


def _module() -> SemanticMiddleware:
    """A resource-mode instance. Its store is a bare object that nothing reads before startup."""
    return SemanticMiddleware(
        mode=Mode.RESOURCE,
        resource_iri=RESOURCE,
        service_class=SERVICE_CLASS,
        ogm=object(),
        host="127.0.0.1",
        port=0,
        heartbeat_interval=None,
    )


def _operations(mw: SemanticMiddleware) -> dict:
    """Every operation of the instance's API description, by (path, method)."""
    paths = TestClient(mw.app).get(mw.app.openapi_url).json()["paths"]
    return {
        (path, method): operation
        for path, methods in paths.items()
        for method, operation in methods.items()
    }


def _with_spawn(mw: SemanticMiddleware) -> Callable[[int], dict]:
    """Register the workflow `spawn`, with a docstring of several lines, and return it."""

    @mw.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    def spawn(count: int) -> dict:
        """Spawn new boxes at the module's entry.

        The count says how many, and each box gets a fresh id.
        A count of zero spawns nothing.
        """
        return {"spawned": str(count)}

    return spawn


def test_the_run_routes_carry_the_docstrings_first_line_and_the_whole_docstring() -> None:
    mw = _module()
    spawn = _with_spawn(mw)

    operations = _operations(mw)
    whole = inspect.cleandoc(spawn.__doc__ or "")
    for path in ("/workflows/spawn/execute", "/workflows/spawn/execute_background"):
        operation = operations[(path, "post")]
        assert operation["summary"] == "Spawn new boxes at the module's entry.", path
        assert operation["description"] == whole, path


def test_the_description_and_interrupt_routes_keep_their_own_summaries() -> None:
    mw = _module()
    _with_spawn(mw)

    operations = _operations(mw)
    description = operations[("/workflows/spawn/description", "get")]
    interrupt = operations[("/workflows/spawn/interrupt", "get")]
    assert description["summary"] == "Describe Workflow"
    assert interrupt["summary"] == "Interrupt Workflow"
    assert "description" not in description
    assert "description" not in interrupt


def test_each_state_property_carries_its_own_getters_docstring() -> None:
    mw = _module()

    @mw.state(capability_class=CAPABILITY_CLASS, state_property_class=STATE_PROPERTY_CLASS)
    def door() -> str:
        """Whether the module's door is open or closed.

        Read from the door's sensor at each call.
        """
        return "open"

    @mw.state(
        capability_class=CAPABILITY_CLASS,
        state_property_class=STATE_PROPERTY_CLASS,
        name="belt_speed",
    )
    def speed() -> float:
        """The speed of the belt, in metres per second."""
        return 0.25

    operations = _operations(mw)
    door_route = operations[("/state/door", "get")]
    speed_route = operations[("/state/belt_speed", "get")]
    assert door_route["summary"] == "Whether the module's door is open or closed."
    assert door_route["description"] == inspect.cleandoc(door.__doc__ or "")
    assert speed_route["summary"] == "The speed of the belt, in metres per second."
    assert speed_route["description"] == "The speed of the belt, in metres per second."


def test_without_a_docstring_the_routes_keep_the_default_summary_and_no_description() -> None:
    mw = _module()

    @mw.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)
    def silent(count: int) -> dict:
        return {}

    @mw.state(capability_class=CAPABILITY_CLASS, state_property_class=STATE_PROPERTY_CLASS)
    def quiet() -> str:
        return "quiet"

    operations = _operations(mw)
    expected = {
        ("/workflows/silent/execute", "post"): "Execute",
        ("/workflows/silent/execute_background", "post"): "Execute Background",
        ("/state/quiet", "get"): "Get State",
    }
    for key, summary in expected.items():
        assert operations[key]["summary"] == summary, key
        assert "description" not in operations[key], key


def test_the_services_box_shows_each_functions_docstring() -> None:
    mw = _module()
    _with_spawn(mw)

    @mw.state(capability_class=CAPABILITY_CLASS, state_property_class=STATE_PROPERTY_CLASS)
    def door() -> str:
        """Whether the module's door is open or closed.

        "open" & "closed" are the two values.
        """
        return "open"

    enable_dashboard(mw)
    fragment = TestClient(mw.app).get("/dashboard/box/services").text

    for path in ("/workflows/spawn/execute", "/workflows/spawn/execute_background"):
        block = fragment.split(f"<code>{path}</code>", 1)[1].split("<h4>", 1)[0]
        assert "<strong>Summary:</strong> Spawn new boxes at the module&#x27;s entry." in block
        assert "A count of zero spawns nothing." in block
    door_block = fragment.split("<code>/state/door</code>", 1)[1].split("<h4>", 1)[0]
    assert "<strong>Summary:</strong> Whether the module&#x27;s door is open or closed." in (
        door_block
    )
    # Escaped once, at the render boundary.
    assert "&quot;open&quot; &amp; &quot;closed&quot; are the two values." in door_block
    assert "Handle GET /state/" not in fragment, "the getter's text, not the handler's"


def test_the_event_triggers_run_routes_say_what_it_does() -> None:
    """The event trigger is a workflow that every resource-mode instance mounts, and its routes
    say what it does, as those of a registered workflow do."""
    operations = _operations(_module())

    for path in (
        "/workflows/event_trigger/execute",
        "/workflows/event_trigger/execute_background",
    ):
        operation = operations[(path, "post")]
        assert operation["summary"].startswith("Take a notification"), path
        assert operation["description"].startswith(operation["summary"] + "\n\n"), path
    assert operations[("/workflows/event_trigger/description", "get")]["summary"] == (
        "Describe Workflow"
    )
