"""No HTTP request reaches a connector's ``consume``: a device changes only through a workflow.

The seam is the FastAPI app of a ``SemanticMiddleware``, through a test client, with stub
connectors that record ``consume``. The read-only test of the served data model looks only under
the model's prefix; this one looks at the whole app.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from transitional_sync_middleware.middleware.sync.synced_connector import SyncRole

from kapps_semantic_middleware import Mode, SemanticMiddleware

READ = {"GET", "HEAD", "OPTIONS"}
IRI_ID = "http://example.org/r#field#mqtt"


class Reading(BaseModel):
    value: int = 0


class StubConnector:
    """Provides and consumes; records every ``consume``."""

    def __init__(self) -> None:
        self.consumed: list[object] = []

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def provide(self) -> Reading:
        return Reading()

    async def consume(self, value: object) -> None:
        self.consumed.append(value)


def _middleware() -> SemanticMiddleware:
    return SemanticMiddleware(
        mode=Mode.RESOURCE,
        resource_iri="http://example.org/NoWriteResource",
        service_class="http://example.org/NoWriteService",
        ogm=object(),
        host="127.0.0.1",
        port=0,
        heartbeat_interval=None,
    )


def _start_synced(mw: SemanticMiddleware, connector_id: str, role: SyncRole) -> StubConnector:
    """Register a synced connector and run the start-up callback that mounts its routes."""
    stub = StubConnector()
    # The framework looks a connector's persistence up by data model name and model id.
    mw.persistence_registry.add_connector(f"unused{connector_id}_persistence", StubConnector(), Reading)
    mw.add_synced_connector(
        connector_id=connector_id,
        connector=stub,
        model_type=Reading,
        data_model_name="unused",
        model_id=connector_id,
        sync_role=role,
    )
    for callback in list(mw.on_start_up_callbacks):
        if getattr(getattr(callback, "func", None), "__name__", "") == "initiate_sync":
            asyncio.run(callback())
    return stub


@pytest.fixture
def started():
    mw = _middleware()
    plain = StubConnector()
    mw.add_connector("plain", plain, Reading)
    stubs = {"plain": plain}
    for cid, role in (
        ("rw", SyncRole.READ_WRITE),
        ("ro", SyncRole.READ_ONLY),
        # The shape KSM's wiring gives a connector: resource IRI, field, suffix.
        (IRI_ID, SyncRole.READ_WRITE),
    ):
        stubs[cid] = _start_synced(mw, cid, role)
    return mw, stubs


def _non_read(app):
    return [
        (route.path, sorted(set(route.methods) - READ))
        for route in app.routes
        if getattr(route, "methods", None) and set(route.methods) - READ
    ]


def test_every_non_read_route_is_a_workflow_route(started):
    mw, _ = started
    for path, _methods in _non_read(mw.app):
        assert path.startswith("/workflows/") and path.endswith(
            ("/execute", "/execute_background")
        ), path


def test_openapi_lists_no_write_under_connectors(started):
    mw, _ = started
    paths = TestClient(mw.app).get("/openapi.json").json()["paths"]
    for path, item in paths.items():
        if path.startswith("/connectors/"):
            assert not set(item) & {"post", "put", "patch", "delete"}, path


def test_description_still_answers(started):
    mw, _ = started
    assert TestClient(mw.app).get("/connectors/plain/description").status_code == 200


@pytest.mark.parametrize("connector_id", ["plain", "rw", "ro", IRI_ID.replace("#", "%23")])
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("body", [{"json": {"value": 1}}, {}])
def test_no_write_reaches_consume(started, connector_id, method, body):
    mw, stubs = started
    response = TestClient(mw.app).request(method.upper(), f"/connectors/{connector_id}/value", **body)
    assert response.status_code == 405
    assert all(not stub.consumed for stub in stubs.values())


def test_the_iri_shaped_connector_is_mounted_and_readable(started):
    """Guards the case above: a connector with this id exists, so its refusal means something."""
    mw, _ = started
    response = TestClient(mw.app).get(f"/connectors/{IRI_ID.replace('#', '%23')}/description")
    assert response.status_code == 200


def test_a_connector_added_after_start_up_gets_no_write_route(started):
    mw, _ = started
    mw._startup_complete = True
    late = StubConnector()
    mw.add_connector("late", late, Reading)
    response = TestClient(mw.app).post("/connectors/late/value", json={"value": 1})
    assert response.status_code == 405
    assert late.consumed == []
    assert TestClient(mw.app).get("/connectors/late/description").status_code == 200
