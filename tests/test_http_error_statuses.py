"""KSM's own routes answer a typed error with the HTTP status of its action.

The error map gives each action one status: Repair 500, Retry
503, Refresh 409 (or 404 when the addressed instance does not exist), Refused 422, Fix the call
400. Every error answer has the same body: ``error`` (the type name), ``detail`` (the original
message) and ``report`` (the raw store report, or ``null``). Only typed errors are mapped; a bare
built-in answers 500. Request validation answers 400 for the whole app.

The map sits on KSM's own routers, the parameter routes and the dashboard routes. The framework's
workflow routes answer as they did before.

No store and no network: the routes run in a ``TestClient``, and the errors are stubs raised by
a route or by a stub connector.
"""

from __future__ import annotations

import functools
from collections import defaultdict
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from kapps_ogm.utils.errors import (
    AnonymousNodeFetchError,
    OGMDataError,
    OGMNotFoundError,
    OGMOntologyError,
    OGMUsageError,
    OGMUsageTypeError,
    PropertyOutsideScopeError,
)
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import (
    AuthenticationError,
    InvalidQueryError,
    PreconditionFailedError,
    ShaclValidationError,
    TripleStoreAccessError,
    TripleStoreResponseError,
    TripleStoreUnavailableError,
)
from pydantic import BaseModel
from transitional_sync_middleware.connect.workflows.workflow import Workflow
from transitional_sync_middleware.middleware.workflow_router import (
    generate_workflow_endpoint,
)
from transitional_sync_middleware.model.data_model import DataModel

from kapps_semantic_middleware.dashboard import enable_dashboard
from kapps_semantic_middleware.errors import (
    CapabilityNotRealizedError,
    DeploymentRuntimeError,
    DeploymentValueError,
    NotificationReceiverAmbiguousError,
    NotificationReceiverUnavailableError,
    OperationReceiverUnavailableError,
)
from kapps_semantic_middleware.http_errors import (
    TypedErrorRoute,
    install_validation_handler,
)
from kapps_semantic_middleware.rest_router import generate_recursive_rest_api

BODY_FIELDS = {"error", "detail", "report"}


def _app_raising(error: BaseException) -> TestClient:
    """An app with one KSM route, ``GET /boom``, that raises ``error``."""
    app = FastAPI()
    install_validation_handler(app)
    router = APIRouter(route_class=TypedErrorRoute)

    @router.get("/boom")
    async def boom() -> None:
        raise error

    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


# One row per mapped type, and one per leaf whose bases could pull it to another status.
_TYPED = [
    # Repair
    (OGMOntologyError("no range"), 500),
    (DeploymentValueError("TRIPLESTORE_URL is not set"), 500),
    (DeploymentRuntimeError("no notification handler"), 500),
    (CapabilityNotRealizedError("no workflow realizes it"), 500),
    (NotificationReceiverAmbiguousError("two live services"), 500),
    (AuthenticationError("401 from the store", status=401), 500),
    (TripleStoreAccessError("403 from the store", status=403), 500),
    # Retry
    (TripleStoreUnavailableError("store unreachable"), 503),
    (OperationReceiverUnavailableError("no live service"), 503),
    (NotificationReceiverUnavailableError("post failed"), 503),
    # Refresh
    (PreconditionFailedError("another writer changed it"), 409),
    (OGMNotFoundError("no such instance"), 404),
    (AnonymousNodeFetchError("no such node"), 404),
    # Refused
    (ShaclValidationError("refused", report="the report"), 422),
    (OGMDataError("a required property is missing"), 422),
    (PropertyOutsideScopeError("outside the scope"), 422),
    # Fix the call
    (OGMUsageError("two changes name the same instance"), 400),
    (OGMUsageTypeError("a wrong argument type"), 400),
]


@pytest.mark.parametrize(
    ("error", "status"), _TYPED, ids=[type(e).__name__ for e, _ in _TYPED]
)
def test_a_typed_error_answers_the_status_of_its_action(
    error: BaseException, status: int
) -> None:
    """Each typed error answers its action's status, with the three body fields."""
    response = _app_raising(error).get("/boom")

    assert response.status_code == status
    body = response.json()
    assert set(body) == BODY_FIELDS
    assert body["error"] == type(error).__name__
    assert body["detail"] == str(error)
    expected_report = error.report if isinstance(error, ShaclValidationError) else None
    assert body["report"] == expected_report
    assert "retry-after" not in response.headers


@pytest.mark.parametrize(
    "error",
    [
        ValueError("a bare value error"),
        TypeError("a bare type error"),
        RuntimeError("a bare runtime error"),
        KeyError("unmapped"),
        TripleStoreResponseError("an error answer no subtype covers", status=500),
        InvalidQueryError("a malformed query"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_an_unmapped_error_answers_500_with_the_same_body(error: BaseException) -> None:
    """A bare built-in, or a type the map does not name, answers 500; its type tells no action."""
    response = _app_raising(error).get("/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": type(error).__name__,
        "detail": str(error),
        "report": None,
    }
    assert "retry-after" not in response.headers


# ─────────────────────────────────────────────────────────────────────────────
# The parameter routes
# ─────────────────────────────────────────────────────────────────────────────


class _Speed(BaseModel):
    model_config = {"extra": "forbid"}

    hasValue: list[float] = []


class _Belt(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    hasSpeed: list[_Speed] = []


class _Unit(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    hasBelt: list[_Belt] = []


class _Binding:
    def __init__(self, resource_iri: str, field_id: str) -> None:
        self.resource_iri = IRI(resource_iri)
        self.field_id = field_id


ROOT = "http://example.org/u#Unit1"
BELT = "http://example.org/u#Belt1"


class _Connector:
    """Provides ``provided``, or raises ``error`` when one is set."""

    def __init__(self) -> None:
        self.provided: Any = None
        self.error: BaseException | None = None

    async def provide(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.provided


class _ParameterMiddleware:
    """The least the parameter routes need: an app, a data model and a persistence registry."""

    def __init__(self, unit: _Unit) -> None:
        self.app = FastAPI()
        install_validation_handler(self.app)
        self.connector = _Connector()
        self.connector.provided = unit
        model = DataModel.from_models(unit)
        self.data_models: dict[str, DataModel] = defaultdict(lambda: model)
        connector = self.connector

        class _Registry:
            def get_connection(self, connection_info: Any) -> _Connector:
                return connector

        self.persistence_registry = _Registry()


def _unit() -> _Unit:
    return _Unit(id=ROOT, hasBelt=[_Belt(id=BELT, hasSpeed=[_Speed(hasValue=[1.5])])])


def _parameter_route() -> tuple[_ParameterMiddleware, str]:
    unit = _unit()
    mw = _ParameterMiddleware(unit)
    (path,) = generate_recursive_rest_api(
        mw,
        "unit",
        root_id=ROOT,
        instance=unit,
        bindings=[_Binding(BELT, "hasSpeed")],  # type: ignore[list-item]
    )
    return mw, path


def test_a_shacl_refusal_on_a_parameter_route_answers_422_with_the_report_byte_for_byte() -> None:
    """The report crosses the route raw: newlines, quotes, markup and non-ASCII unchanged."""
    report = (
        '@prefix sh: <http://www.w3.org/ns/shacl#> .\n'
        '[] a sh:ValidationReport ; sh:conforms false ;\n'
        '   sh:result [ sh:resultMessage "Größe < 0 \\"nicht\\" erlaubt" ] .\r\n'
        "\t<tail> ünïcödé ✓\n"
    )
    mw, path = _parameter_route()
    mw.connector.error = ShaclValidationError("400: refused\n" + report, report=report)

    response = TestClient(mw.app, raise_server_exceptions=False).get(path)

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "ShaclValidationError"
    assert body["detail"] == "400: refused\n" + report
    assert body["report"] == report


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (OGMNotFoundError("the belt is gone"), 404),
        (PreconditionFailedError("another writer changed it"), 409),
        (TripleStoreUnavailableError("store unreachable"), 503),
        (ValueError("a bare value error"), 500),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else str(v),
)
def test_a_parameter_route_answers_the_status_of_its_action(
    error: BaseException, status: int
) -> None:
    """The parameter routes no longer flatten every error into a 500."""
    mw, path = _parameter_route()
    mw.connector.error = error

    response = TestClient(mw.app, raise_server_exceptions=False).get(path)

    assert response.status_code == status
    assert response.json() == {
        "error": type(error).__name__,
        "detail": str(error),
        "report": None,
    }


def test_an_absent_owner_answers_500_with_the_reason_phrase() -> None:
    """The owner left the materialized tree after the route was generated: KSM's own 500."""
    mw, path = _parameter_route()
    mw.connector.provided = _Unit(id=ROOT)  # the belt is gone

    response = TestClient(mw.app, raise_server_exceptions=False).get(path)

    assert response.status_code == 500
    body = response.json()
    assert set(body) == BODY_FIELDS
    assert body["error"] == "Internal Server Error"
    assert BELT in body["detail"] and "absent from the materialized tree" in body["detail"]
    assert body["report"] is None


def test_a_parameter_route_still_serves_its_value() -> None:
    mw, path = _parameter_route()

    response = TestClient(mw.app).get(path)

    assert response.status_code == 200
    assert response.json() == [{"hasValue": [1.5]}]


# ─────────────────────────────────────────────────────────────────────────────
# The dashboard routes
# ─────────────────────────────────────────────────────────────────────────────


class _DashboardMiddleware:
    def __init__(self) -> None:
        self.app = FastAPI()


def _dashboard() -> TestClient:
    mw = _DashboardMiddleware()
    enable_dashboard(mw)
    return TestClient(mw.app, raise_server_exceptions=False)


def test_an_unknown_dashboard_box_answers_404_with_the_reason_phrase() -> None:
    response = _dashboard().get("/dashboard/box/no-such-box")

    assert response.status_code == 404
    assert response.json() == {
        "error": "Not Found",
        "detail": "Unknown box: no-such-box",
        "report": None,
    }


def test_a_validation_error_answers_400_with_fastapis_list() -> None:
    """A malformed body answers 400, not FastAPI's 422, and ``detail`` is FastAPI's list."""
    response = _dashboard().post("/dashboard/logs/level", json={"logger": "x"})

    assert response.status_code == 400
    body = response.json()
    assert set(body) == BODY_FIELDS
    assert body["error"] == "RequestValidationError"
    assert isinstance(body["detail"], list) and body["detail"]
    assert body["detail"][0]["loc"] == ["body", "level"]
    assert body["report"] is None


def test_a_body_that_is_not_json_answers_400() -> None:
    response = _dashboard().post(
        "/dashboard/logs/level",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "RequestValidationError"


@pytest.mark.parametrize(
    ("logger", "level"), [("tests.http_errors.level", "VERBOSE"), (" ", "DEBUG")]
)
def test_a_json_call_with_a_wrong_level_or_an_empty_logger_answers_400(
    logger: str, level: str
) -> None:
    response = _dashboard().post(
        "/dashboard/logs/level", json={"logger": logger, "level": level}
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "Bad Request"
    assert body["report"] is None
    assert isinstance(body["detail"], str) and body["detail"]


@pytest.mark.parametrize(
    ("logger", "level"), [("tests.http_errors.form", "VERBOSE"), (" ", "DEBUG")]
)
def test_the_same_refusal_from_the_form_answers_200_with_a_fragment(
    logger: str, level: str
) -> None:
    response = _dashboard().post(
        "/dashboard/logs/level",
        json={"logger": logger, "level": level},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Not set." in response.text


@pytest.mark.parametrize("from_the_form", [False, True])
def test_a_valid_level_still_answers_200(from_the_form: bool) -> None:
    import logging

    name = "tests.http_errors.valid"
    try:
        response = _dashboard().post(
            "/dashboard/logs/level",
            json={"logger": name, "level": "WARNING"},
            headers={"HX-Request": "true"} if from_the_form else {},
        )
    finally:
        logging.getLogger(name).setLevel(logging.NOTSET)

    assert response.status_code == 200
    if from_the_form:
        assert response.headers["content-type"].startswith("text/html")
        assert "Level in force" in response.text
    else:
        assert response.json() == {"logger": name, "level": "WARNING"}


# ─────────────────────────────────────────────────────────────────────────────
# The framework's workflow routes answer as before
# ─────────────────────────────────────────────────────────────────────────────


def _workflow_app(error: BaseException) -> TestClient:
    async def failing() -> None:
        raise error

    app = FastAPI()
    install_validation_handler(app)
    app.include_router(
        generate_workflow_endpoint(Workflow(functools.partial(failing), interval=None))
    )
    return TestClient(app, raise_server_exceptions=False)


def test_a_runtime_error_in_a_framework_workflow_route_still_answers_409() -> None:
    """The framework's own mapping: 409 with its ``detail`` alone, not KSM's body."""
    response = _workflow_app(DeploymentRuntimeError("no handler")).post(
        "/workflows/failing/execute"
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "no handler"}


def test_a_typed_error_in_a_framework_workflow_route_still_answers_a_plain_500() -> None:
    """KSM's map does not reach the framework's workflow routes: no 404, no KSM body."""
    response = _workflow_app(OGMNotFoundError("gone")).post("/workflows/failing/execute")

    assert response.status_code == 500
    assert response.text == "Internal Server Error"


# ─────────────────────────────────────────────────────────────────────────────
# The middleware's own app
# ─────────────────────────────────────────────────────────────────────────────


def test_the_middlewares_app_answers_a_validation_error_with_400() -> None:
    """``SemanticMiddleware.app`` installs the handler, so it covers every route of the app."""
    from kapps_semantic_middleware import Mode, SemanticMiddleware

    mw = SemanticMiddleware(
        mode=Mode.RESOURCE,
        resource_iri="http://example.org/HttpErrorsTestResource",
        service_class="http://example.org/HttpErrorsTestService",
        ogm=object(),  # Never dereferenced before the app's lifespan runs.
        host="127.0.0.1",
        port=0,
        heartbeat_interval=None,
    )

    @mw.app.post("/probe")
    async def probe(count: int) -> int:
        return count

    response = TestClient(mw.app).post("/probe", params={"count": "not a number"})

    assert response.status_code == 400
    assert response.json()["error"] == "RequestValidationError"
    assert isinstance(response.json()["detail"], list)
