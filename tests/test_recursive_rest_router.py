"""Recursive REST routes terminate at interface-accessible parameters.

The framework generates CRUD routes for a data model, then recurses through complex properties
to expose terminal parameters — conveyor speed, barrier occupancy — as leaf endpoints. A naive
implementation makes two mistakes that this guard exists to catch:

1. **Closure capture in the loop.** Generating four parameter routes in one `for` binding the
   loop variable late means every route serves the *last* parameter's value. This test asserts
   that different paths return different values.

2. **Recursion does not terminate.** If the walker follows child references without tracking
   visited ids, a cyclic instance graph hangs forever. The recursion must stop at parameters
   and at already-seen ids.

3. **A write route reaches the device or the graph.** The served data model is read-only: a
   device changes only through a workflow of its service. The framework's generator mounts
   POST, PUT and DELETE beside every GET, and a parameter route once had a PUT of its own.
   The tests below run the framework's real generator against a real `DataModel` and find
   only GET, and a write verb answers 405 with nothing consumed and nothing persisted.

This is a **static** check of the generated routes plus **behavioural** checks via `TestClient`.
No store, no broker: the middleware is faked, the instance tree is plain pydantic models,
and the connectors are stubs that record what they would consume.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient
from kapps_triplestore_interface import IRI
from pydantic import BaseModel
from transitional_sync_middleware.model.data_model import DataModel

from kapps_semantic_middleware.connectors.semantic import AccessMode
from kapps_semantic_middleware.rest_router import generate_recursive_rest_api

from conftest import methods_at


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: plain pydantic models mirroring the OGM shape (research doc 0029)
# ─────────────────────────────────────────────────────────────────────────────


class Speed(BaseModel):
    """Terminal blanknode for conveyor speed — no `id` field."""

    model_config = {"extra": "forbid"}

    hasValue: list[float] = []
    hasUnit: list[str] = []


class Occupancy(BaseModel):
    """Terminal blanknode for light-barrier occupancy — no `id` field."""

    model_config = {"extra": "forbid"}

    hasValue: list[bool] = []


class Belt(BaseModel):
    """A belt carries a speed parameter."""

    model_config = {"extra": "forbid"}

    id: str
    TU_hasConveyorSpeed: list[Speed] = []


class Barrier(BaseModel):
    """A barrier carries an occupancy parameter."""

    model_config = {"extra": "forbid"}

    id: str
    TU_isOccupied: list[Occupancy] = []


class TransferUnit(BaseModel):
    """Root model: belts and barriers hang off this."""

    model_config = {"extra": "forbid"}

    id: str
    TU_hasConveyorBelt: list[Belt] = []
    TU_hasLightBarrier: list[Barrier] = []


@dataclass(frozen=True)
class _BindingStub:
    """Minimal ParameterBinding-like object carrying what the router reads."""

    resource_iri: IRI
    field_id: str
    access_mode: str


def _make_unit() -> TransferUnit:
    """A TransferUnit with two belts and two barriers, using IRI-looking ids."""
    return TransferUnit(
        id="https://example.org/tui#TransferUnit1",
        TU_hasConveyorBelt=[
            Belt(
                id="https://example.org/tui#ConveyorBelt1_left",
                TU_hasConveyorSpeed=[Speed(hasValue=[1.5], hasUnit=["m/s"])],
            ),
            Belt(
                id="https://example.org/tui#ConveyorBelt1_right",
                TU_hasConveyorSpeed=[Speed(hasValue=[2.0], hasUnit=["m/s"])],
            ),
        ],
        TU_hasLightBarrier=[
            Barrier(
                id="https://example.org/tui#LightBarrier1_front",
                TU_isOccupied=[Occupancy(hasValue=[False])],
            ),
            Barrier(
                id="https://example.org/tui#LightBarrier1_back",
                TU_isOccupied=[Occupancy(hasValue=[True])],
            ),
        ],
    )


def _make_bindings() -> list[_BindingStub]:
    """Four bindings: two belts read-write, two barriers read-only."""
    return [
        _BindingStub(
            resource_iri=IRI("https://example.org/tui#ConveyorBelt1_left"),
            field_id="TU_hasConveyorSpeed",
            access_mode=AccessMode.READWRITE,
        ),
        _BindingStub(
            resource_iri=IRI("https://example.org/tui#ConveyorBelt1_right"),
            field_id="TU_hasConveyorSpeed",
            access_mode=AccessMode.READWRITE,
        ),
        _BindingStub(
            resource_iri=IRI("https://example.org/tui#LightBarrier1_front"),
            field_id="TU_isOccupied",
            access_mode=AccessMode.READ,
        ),
        _BindingStub(
            resource_iri=IRI("https://example.org/tui#LightBarrier1_back"),
            field_id="TU_isOccupied",
            access_mode=AccessMode.READ,
        ),
    ]


class _StubConnector:
    """Records what the framework asks it to provide/consume.

    Stands in for a ``PersistedConnector``, so it carries that class's signature. A served
    data model mounts no write route, so ``consumed`` stays ``None`` whatever a request asks:
    the write tests below assert exactly that.
    """

    def __init__(self) -> None:
        self.provided: Optional[Any] = None
        self.consumed: Optional[Any] = None
        self.changed: Optional[Any] = None
        self.origin: Optional[Any] = None

    async def provide(self) -> Any:
        return self.provided

    async def consume(
        self, value: Any, origin: Any = None, changed: Any = None
    ) -> None:
        self.consumed = value
        self.origin = origin
        self.changed = changed


class _FakeMiddleware:
    """A middleware fake with a real FastAPI app, a real data model and stub persistence.

    ``data_models`` holds a real ``DataModel`` built from the instance, as
    ``load_model_instances`` builds it, so the framework's own generator runs against it and the
    route tests see every verb that generator would mount. ``persisted`` records a write to the
    graph: the framework's POST on the model persists through ``persist``.
    """

    def __init__(self, *instances: Any) -> None:
        self.app = FastAPI()
        self._connector = _StubConnector()
        self.persisted: list[Any] = []
        model = DataModel.from_models(*instances) if instances else DataModel()
        # The router looks the model up by `data_model_name`; one model serves every name here.
        self.data_models: dict[str, DataModel] = defaultdict(lambda: model)

        class _Registry:
            def get_connection(_self, connection_info: Any) -> _StubConnector:
                return self._connector

        self._registry = _Registry()

    async def persist(self, data_model_name: str, model: Any) -> None:
        self.persisted.append(model)

    @property
    def persistence_registry(self) -> Any:
        return self._registry


def _path(root_id: str, field_name: str, child_id: str, field_id: str) -> str:
    """Build the expected path shape with mangled id segments."""
    return f"/TransferUnit/{IRI(root_id).lined}/{field_name}/{IRI(child_id).lined}/{field_id}"


def _prime_connector(mw: _FakeMiddleware, unit: TransferUnit) -> None:
    """Prime the stub connector's `provided` with the unit so handlers can navigate it.

    Every test that drives requests through TestClient needs this, or `await provide()` returns
    `None` and the handler crashes. Centralising it here prevents forgetting in a new test.
    """
    mw.persistence_registry.get_connection(None).provided = unit


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────




def test_routes_are_generated_at_the_right_structural_paths():
    """The left belt's speed appears at its exact mangled path among app.routes.

    Path shape is `/{Model}/{lined_root}/{field}/{lined_child}/{field_id}`. A mistake in any
    segment — forgetting to mangle an id, using the wrong field name — breaks client code
    that constructs URLs from the ontology.
    """
    unit = _make_unit()
    mw = _FakeMiddleware(unit)
    bindings = _make_bindings()

    generate_recursive_rest_api(
        middleware=mw,
        data_model_name="TransferUnit",
        root_id=unit.id,
        instance=unit,
        bindings=bindings,
    )

    expected = _path(
        unit.id,
        "TU_hasConveyorBelt",
        "https://example.org/tui#ConveyorBelt1_left",
        "TU_hasConveyorSpeed",
    )

    route_paths = {r.path for r in mw.app.routes}
    assert expected in route_paths
    # The framework's top-level reads mount beside the parameter routes. Only the no-id test
    # pinned them once, so a refactor that dropped them on the normal path went unnoticed.
    assert "GET" in methods_at(mw.app, "/TransferUnit/{item_id}")


def test_a_read_only_parameter_gets_get_and_no_put():
    """A barrier's occupancy path exists with GET and explicitly lacks PUT.

    Read-only parameters must not expose mutation endpoints. The router gates PUT on
    `binding.access_mode`, so a monitor instance cannot accidentally drive a device.
    """
    unit = _make_unit()
    mw = _FakeMiddleware(unit)
    bindings = _make_bindings()

    generate_recursive_rest_api(
        middleware=mw,
        data_model_name="TransferUnit",
        root_id=unit.id,
        instance=unit,
        bindings=bindings,
    )

    path = _path(
        unit.id,
        "TU_hasLightBarrier",
        "https://example.org/tui#LightBarrier1_front",
        "TU_isOccupied",
    )

    methods = methods_at(mw.app, path)
    assert "GET" in methods
    assert "PUT" not in methods


def test_a_read_write_parameter_gets_get_and_no_put():
    """A settable belt's speed path exposes GET and no PUT.

    ``readwrite`` says the device accepts a setpoint, not that a peer may send one over the
    served data model. A PUT here would set the device with no commit and no admission: a
    device changes only through a workflow of its service.
    """
    unit = _make_unit()
    mw = _FakeMiddleware(unit)
    bindings = _make_bindings()

    generate_recursive_rest_api(
        middleware=mw,
        data_model_name="TransferUnit",
        root_id=unit.id,
        instance=unit,
        bindings=bindings,
    )

    path = _path(
        unit.id,
        "TU_hasConveyorBelt",
        "https://example.org/tui#ConveyorBelt1_left",
        "TU_hasConveyorSpeed",
    )

    assert methods_at(mw.app, path) == {"GET"}


def test_no_route_is_generated_below_a_complex_property():
    """Recursion terminates at parameters — no path starts with a parameter path plus `/`.

    The walker must not descend into the blanknode fields of a parameter (hasValue, hasUnit).
    Those are persistence details, not addressable resources. A generic check ensures this
    holds for every generated route, not just one named path.
    """
    unit = _make_unit()
    mw = _FakeMiddleware(unit)
    bindings = _make_bindings()

    param_paths = generate_recursive_rest_api(
        middleware=mw,
        data_model_name="TransferUnit",
        root_id=unit.id,
        instance=unit,
        bindings=bindings,
    )

    all_paths = {r.path for r in mw.app.routes}

    for param_path in param_paths:
        deeper = [p for p in all_paths if p.startswith(param_path + "/")]
        assert not deeper, f"Found routes below parameter {param_path}: {deeper}"


def test_closure_capture_is_correct():
    """GET-ing two different parameter paths returns two different values.

    The most likely bug: generating four routes in one loop binds the loop variable late, so
    every route serves the last parameter's value. This test fetches two distinct paths and
    asserts they return distinct data. Values are lists because the field is list[Speed].
    """
    unit = _make_unit()
    mw = _FakeMiddleware(unit)
    bindings = _make_bindings()
    _prime_connector(mw, unit)

    generate_recursive_rest_api(
        middleware=mw,
        data_model_name="TransferUnit",
        root_id=unit.id,
        instance=unit,
        bindings=bindings,
    )

    left_path = _path(
        unit.id,
        "TU_hasConveyorBelt",
        "https://example.org/tui#ConveyorBelt1_left",
        "TU_hasConveyorSpeed",
    )
    right_path = _path(
        unit.id,
        "TU_hasConveyorBelt",
        "https://example.org/tui#ConveyorBelt1_right",
        "TU_hasConveyorSpeed",
    )

    client = TestClient(mw.app)

    left_response = client.get(left_path)
    right_response = client.get(right_path)

    assert left_response.status_code == 200
    assert right_response.status_code == 200
    # Index the list first: the field is list[Speed]
    assert left_response.json()[0]["hasValue"] == [1.5]
    assert right_response.json()[0]["hasValue"] == [2.0]
    assert left_response.json() != right_response.json()


def test_cyclic_instance_graph_terminates():
    """A model whose child list refers back to an ancestor id returns rather than hanging.

    Build a real cycle: two nodes that reference each other through their children lists.
    Pydantic v2 allows post-construction mutation, so create the loop after construction.
    The recursion tracks visited ids and stops — the returned path list is finite and contains
    each node's parameter exactly once.
    """

    class Node(BaseModel):
        """Recursive model for testing cycle detection."""

        model_config = {"extra": "forbid"}

        id: str
        TU_hasConveyorSpeed: list[Speed] = []
        children: list[Node] = []

    mw = _FakeMiddleware()

    # Build two nodes, then create the cycle by assignment
    a = Node(id="https://example.org/tui#NodeA", TU_hasConveyorSpeed=[Speed(hasValue=[1.0])])
    b = Node(id="https://example.org/tui#NodeB", TU_hasConveyorSpeed=[Speed(hasValue=[2.0])])
    a.children = [b]
    b.children = [a]

    bindings = [
        _BindingStub(
            resource_iri=IRI("https://example.org/tui#NodeA"),
            field_id="TU_hasConveyorSpeed",
            access_mode=AccessMode.READWRITE,
        ),
        _BindingStub(
            resource_iri=IRI("https://example.org/tui#NodeB"),
            field_id="TU_hasConveyorSpeed",
            access_mode=AccessMode.READWRITE,
        ),
    ]

    # This must not hang
    paths = generate_recursive_rest_api(
        middleware=mw,
        data_model_name="Node",
        root_id=a.id,
        instance=a,
        bindings=bindings,
    )

    assert isinstance(paths, list)
    assert len(paths) == 2  # Two parameters, no infinite expansion


def test_instance_with_no_id_returns_empty_list_and_still_mounts_the_reads():
    """An instance lacking an `id` field yields `[]` but the top-level reads still mount.

    The recursion cannot proceed without a root id to mangle into paths, but the data model's
    top-level reads must still be served. This guards against a silent early-return that skips
    everything.
    """

    class NoIdModel(BaseModel):
        model_config = {"extra": "forbid"}
        TU_hasConveyorBelt: list[Belt] = []

    mw = _FakeMiddleware(_make_unit())
    instance = NoIdModel()
    bindings: list[_BindingStub] = []

    paths = generate_recursive_rest_api(
        middleware=mw,
        data_model_name="NoIdModel",
        root_id="",
        instance=instance,
        bindings=bindings,
    )

    assert paths == []
    assert "GET" in methods_at(mw.app, "/TransferUnit/{item_id}")


def test_a_binding_whose_owner_is_absent_generates_no_route():
    """A binding whose owner IRI is missing from the instance tree yields no route, not an error.

    Route generation is best-effort and additive — `_load_resource_datamodel` is documented as
    never fatal to startup — so a binding that cannot be placed costs its route, not the whole
    REST surface. The loud failure belongs at request time, where the router raises a 500 naming
    the missing individual, rather than at generation time where it would take down every other
    parameter with it.
    """
    unit = _make_unit()
    mw = _FakeMiddleware(unit)
    bindings = _make_bindings()
    bindings.append(
        _BindingStub(
            resource_iri=IRI("https://example.org/tui#ConveyorBelt99_ghost"),
            field_id="TU_hasConveyorSpeed",
            access_mode=AccessMode.READWRITE,
        )
    )

    paths = generate_recursive_rest_api(
        middleware=mw,
        data_model_name="TransferUnit",
        root_id=unit.id,
        instance=unit,
        bindings=bindings,
    )

    assert len(paths) == 4
    ghost_segment = IRI("https://example.org/tui#ConveyorBelt99_ghost").lined
    assert not any(ghost_segment in p for p in paths)


_WRITE_VERBS = {"POST", "PUT", "PATCH", "DELETE"}


def _data_model_routes(app: FastAPI) -> list[Any]:
    """Every route the served data model mounts: those under the root model's prefix."""
    return [r for r in app.routes if getattr(r, "path", "").startswith("/TransferUnit/")]


def test_only_get_is_mounted_on_the_served_data_model():
    """The framework's generator and the parameter routes together mount no write verb.

    The framework mounts POST on the model, PUT and DELETE on an item, and PUT, DELETE and an
    optional POST on each contained model. Any of them would set the device or the graph with no
    commit around it. The GET assertions keep this from passing on an empty route table.
    """
    unit = _make_unit()
    mw = _FakeMiddleware(unit)

    param_paths = generate_recursive_rest_api(
        middleware=mw,
        data_model_name="TransferUnit",
        root_id=unit.id,
        instance=unit,
        bindings=_make_bindings(),
    )

    routes = _data_model_routes(mw.app)
    served = {method for route in routes for method in route.methods}
    assert served <= {"GET", "HEAD", "OPTIONS"}, served

    # Not vacuous: the reads are there, on the model, on a contained model, and on every
    # parameter, the settable belts included.
    assert "GET" in methods_at(mw.app, "/TransferUnit/")
    assert "GET" in methods_at(mw.app, "/TransferUnit/{item_id}")
    assert "GET" in methods_at(mw.app, "/TransferUnit/{item_id}/TU_hasConveyorBelt/")
    assert len(param_paths) == 4
    for path in param_paths:
        assert methods_at(mw.app, path) == {"GET"}, path


def test_a_write_verb_answers_405_and_reaches_neither_the_device_nor_the_graph():
    """A write on a parameter, an item or a contained model is refused, and nothing moves.

    The stub connector stands in for the persistence connector, whose ``consume`` fans out to
    the device's write legs; ``persisted`` stands in for the graph. Both stay untouched.
    """
    unit = _make_unit()
    mw = _FakeMiddleware(unit)
    _prime_connector(mw, unit)

    generate_recursive_rest_api(
        middleware=mw,
        data_model_name="TransferUnit",
        root_id=unit.id,
        instance=unit,
        bindings=_make_bindings(),
    )

    belt = _path(
        unit.id,
        "TU_hasConveyorBelt",
        "https://example.org/tui#ConveyorBelt1_left",
        "TU_hasConveyorSpeed",
    )
    item = f"/TransferUnit/{IRI(unit.id).lined}"
    contained = f"/TransferUnit/{IRI(unit.id).lined}/TU_hasConveyorBelt/"
    client = TestClient(mw.app)
    body = [{"hasValue": [9.9], "hasUnit": ["m/s"]}]

    for path in (belt, item, contained, "/TransferUnit/"):
        for verb in sorted(_WRITE_VERBS):
            response = client.request(verb, path, json=body)
            # 405, not 404: each path matches a GET route, so a 404 would mean the read
            # vanished rather than that the write was refused.
            assert response.status_code == 405, (verb, path, response.status_code)

    # The read still answers, so the refusals above are not a dead app.
    assert client.get(belt).json()[0]["hasValue"] == [1.5]
    assert mw.persistence_registry.get_connection(None).consumed is None
    assert mw.persisted == []
