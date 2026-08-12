"""Recursive REST routes terminate at interface-accessible parameters (#41).

The framework generates CRUD routes for a data model, then recurses through complex properties
to expose terminal parameters — conveyor speed, barrier occupancy — as leaf endpoints. A naive
implementation makes two mistakes that this guard exists to catch:

1. **Closure capture in the loop.** Generating four parameter routes in one `for` binding the
   loop variable late means every route serves the *last* parameter's value. This test asserts
   that different paths return different values.

2. **Recursion does not terminate.** If the walker follows child references without tracking
   visited ids, a cyclic instance graph hangs forever. The recursion must stop at parameters
   and at already-seen ids.

3. **PUT body misclassified as a query parameter (#87).** FastAPI infers body-vs-query from
   whether a field's annotation looks scalar. A COMPLEX property that isn't required has an
   `Optional[Annotated[conlist(...), BeforeValidator]]` annotation, and that `Optional` wrapper
   defeats the inference -- every real PUT route 422ed. The other fixtures below use a plain
   `list[Model]` shape that never exercises this; `RealisticBelt`/`RealisticTransferUnit` exist
   specifically to reproduce the real shape.

This is a **static** check of the generated routes plus **behavioural** checks via `TestClient`.
No GraphDB, no broker: the middleware is faked, the instance tree is plain pydantic models,
and the connectors are stubs that record what they consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient
from kapps_triplestore_interface import IRI
from pydantic import BaseModel, BeforeValidator, ConfigDict, conlist, create_model

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


def _coerce_speed(value: Any) -> Any:
    return value


# Built with `create_model`, exactly as `kapps_ogm.ClassSpec.to_pydantic_model` builds it,
# rather than a static class body -- a literal `conlist(...)` call cannot appear in a class-body
# annotation under strict static typing, but that is exactly the shape a COMPLEX property's
# field actually has at runtime: `Optional` because the property isn't required, `Annotated`
# because of the coercion validator, `conlist` rather than plain `list` for the min/max bound.
# Every fixture above this point uses the simpler `list[Speed]` shape instead, which FastAPI's
# scalar check always classifies as a body regardless of how the route wires the parameter --
# that shape would not have caught issue #87.
_realistic_speed_field: Any = Optional[
    Annotated[conlist(Speed, min_length=0), BeforeValidator(_coerce_speed)]
]


RealisticBelt: Any = create_model(
    "RealisticBelt",
    __config__=ConfigDict(extra="forbid"),
    id=(str, ...),
    TU_hasConveyorSpeed=(_realistic_speed_field, None),
)


RealisticTransferUnit: Any = create_model(
    "RealisticTransferUnit",
    __config__=ConfigDict(extra="forbid"),
    id=(str, ...),
    TU_hasConveyorBelt=(list[RealisticBelt], []),
)


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

    Stands in for a ``PersistedConnector``, so it must carry that class's signature:
    ``origin`` and ``changed`` are how a caller tells the fan-out what it attributed the
    write to and which region of the model actually moved (#92, #94). Recording
    ``changed`` rather than swallowing it makes this stub the fast, GraphDB-free place to
    assert that the PUT handler scopes its write to one parameter.
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
    """A middleware fake with a real FastAPI app and stub persistence."""

    def __init__(self) -> None:
        self.app = FastAPI()
        self._generate_called = False
        self._connector = _StubConnector()

        class _Registry:
            def get_connection(_self, connection_info: Any) -> _StubConnector:
                return self._connector

        self._registry = _Registry()

    def generate_rest_api_for_data_model(self, name: str) -> None:
        self._generate_called = True

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
    mw = _FakeMiddleware()
    unit = _make_unit()
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
    # Load-bearing ordering: the framework's top-level CRUD must be generated before the
    # parameter routes mount beneath it. Only the no-id test pinned this call, so a refactor
    # that dropped it on the normal path would have gone unnoticed.
    assert mw._generate_called


def test_read_only_parameter_gets_get_but_no_put():
    """A barrier's occupancy path exists with GET and explicitly lacks PUT.

    Read-only parameters must not expose mutation endpoints. The router gates PUT on
    `binding.access_mode`, so a monitor instance cannot accidentally drive a device.
    """
    mw = _FakeMiddleware()
    unit = _make_unit()
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


def test_read_write_parameter_gets_both_get_and_put():
    """A belt's speed path exposes both GET and PUT.

    A controller instance may drive settable parameters. Both verbs must be present, or the
    instance cannot fulfil its role.
    """
    mw = _FakeMiddleware()
    unit = _make_unit()
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

    methods = methods_at(mw.app, path)
    assert "GET" in methods
    assert "PUT" in methods


def test_no_route_is_generated_below_a_complex_property():
    """Recursion terminates at parameters — no path starts with a parameter path plus `/`.

    The walker must not descend into the blanknode fields of a parameter (hasValue, hasUnit).
    Those are persistence details, not addressable resources. A generic check ensures this
    holds for every generated route, not just one named path.
    """
    mw = _FakeMiddleware()
    unit = _make_unit()
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


def test_put_to_one_belt_mutates_only_that_belt():
    """Driving the PUT handler changes the left belt's speed and leaves everything else untouched.

    This is the headline criterion: recursive routes must isolate their target. A bug in the
    closure or the traversal would mutate the wrong child, or overwrite siblings. The stub
    connector records exactly what was consumed — assertions target `consumed`, not `unit`,
    because `consumed` is what actually reached persistence.
    """
    mw = _FakeMiddleware()
    unit = _make_unit()
    bindings = _make_bindings()
    _prime_connector(mw, unit)

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

    client = TestClient(mw.app)

    # PUT new value to left belt — payload is a list because the field is list[Speed]
    payload = [{"hasValue": [9.9], "hasUnit": ["m/s"]}]
    response = client.put(path, json=payload)
    assert response.status_code == 200

    # Assert on consumed, which is what reached persistence
    consumed = mw.persistence_registry.get_connection(None).consumed

    # Left belt's speed changed
    left_belt = next(b for b in consumed.TU_hasConveyorBelt if b.id.endswith("_left"))
    assert left_belt.TU_hasConveyorSpeed[0].hasValue == [9.9]

    # Siblings unchanged
    right_belt = next(b for b in consumed.TU_hasConveyorBelt if b.id.endswith("_right"))
    assert right_belt.TU_hasConveyorSpeed[0].hasValue == [2.0]

    front_barrier = next(b for b in consumed.TU_hasLightBarrier if b.id.endswith("_front"))
    assert front_barrier.TU_isOccupied[0].hasValue == [False]

    back_barrier = next(b for b in consumed.TU_hasLightBarrier if b.id.endswith("_back"))
    assert back_barrier.TU_isOccupied[0].hasValue == [True]


def test_put_tells_the_fan_out_which_parameter_moved():
    """#94: mutating one field is not enough -- the ``consume`` call has to say so.

    The whole model goes to persistence either way, because a persistence connector is
    keyed by *(data_model_name, model_id)* and holds the resource entire. Without
    ``changed``, the fan-out asks every write leg on that resource to re-derive its slice,
    and a sibling parameter's write leg publishes its last *observed* value onto its *set*
    topic -- an unbidden command. The integration test in
    ``test_southbound_echo_integration.py`` proves the effect against a real broker; this
    proves the cause, with no GraphDB and no network.
    """
    mw = _FakeMiddleware()
    unit = _make_unit()
    _prime_connector(mw, unit)

    generate_recursive_rest_api(
        middleware=mw,
        data_model_name="TransferUnit",
        root_id=unit.id,
        instance=unit,
        bindings=_make_bindings(),
    )

    path = _path(
        unit.id,
        "TU_hasConveyorBelt",
        "https://example.org/tui#ConveyorBelt1_left",
        "TU_hasConveyorSpeed",
    )
    response = TestClient(mw.app).put(path, json=[{"hasValue": [9.9], "hasUnit": ["m/s"]}])
    assert response.status_code == 200

    changed = mw.persistence_registry.get_connection(None).changed
    assert changed is not None, "the PUT handler told the fan-out nothing about what moved"

    # All four levels, and the deepest two are the point: `contained_model_id` names the
    # belt and `field_id` the parameter, so `_covers` reaches this belt's write leg and no
    # other. A `changed` that stopped at `model_id` would cover the whole unit and restore
    # the defect while still looking like a fix.
    assert changed.data_model_name == "TransferUnit"
    assert changed.model_id == unit.id
    assert changed.contained_model_id == "https://example.org/tui#ConveyorBelt1_left"
    assert changed.field_id == "TU_hasConveyorSpeed"


def test_get_and_put_are_symmetric():
    """What GET returns is accepted by PUT unchanged, and doing so is not an error.

    Symmetry matters for clients that read-modify-write. If the shapes diverge, a round-trip
    fails even though nothing changed.
    """
    mw = _FakeMiddleware()
    unit = _make_unit()
    bindings = _make_bindings()
    _prime_connector(mw, unit)

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

    client = TestClient(mw.app)

    get_response = client.get(path)
    assert get_response.status_code == 200
    payload = get_response.json()

    put_response = client.put(path, json=payload)
    assert put_response.status_code == 200


def test_put_body_is_bound_as_a_json_body_not_a_query_parameter():
    """A PUT whose field type has the shape the OGM actually produces is accepted as JSON.

    ``Belt.TU_hasConveyorSpeed`` above is typed ``list[Speed]`` -- a plain list, which FastAPI's
    scalar check always classifies as a body regardless of how the route wires the parameter.
    That shape is not what ``ClassSpec.to_pydantic_field`` (kapps_ogm) actually emits for a
    COMPLEX property: a real field is ``Optional[Annotated[conlist(Model, ...), BeforeValidator]]``
    -- optional because the property isn't required, `Annotated` because of the coercion
    validator, `conlist` rather than `list` for the min/max bound. FastAPI's implicit body-vs-query
    inference (`field_annotation_is_scalar`) does not see through that `Optional` wrapper the same
    way, and silently classifies the parameter as a query param instead -- issue #87, reproduced
    against the real installed FastAPI in isolation before this test was written. Every other test
    in this file uses the simpler shape and would not have caught this.
    """
    mw = _FakeMiddleware()
    unit = RealisticTransferUnit(
        id="https://example.org/tui#TransferUnit1",
        TU_hasConveyorBelt=[
            RealisticBelt(
                id="https://example.org/tui#ConveyorBelt1_left",
                TU_hasConveyorSpeed=[Speed(hasValue=[1.5], hasUnit=["m/s"])],
            )
        ],
    )
    bindings = [
        _BindingStub(
            resource_iri=IRI("https://example.org/tui#ConveyorBelt1_left"),
            field_id="TU_hasConveyorSpeed",
            access_mode=AccessMode.READWRITE,
        )
    ]
    _prime_connector(mw, unit)

    # `type(instance).__name__` becomes the route's leading segment, so it is
    # "RealisticTransferUnit" here rather than the "TransferUnit" `_path()` assumes.
    generated_paths = generate_recursive_rest_api(
        middleware=mw,
        data_model_name="TransferUnit",
        root_id=unit.id,
        instance=unit,
        bindings=bindings,
    )
    assert len(generated_paths) == 1
    path = generated_paths[0]

    client = TestClient(mw.app)
    payload = [{"hasValue": [9.9], "hasUnit": ["m/s"]}]
    response = client.put(path, json=payload)

    assert response.status_code == 200, response.json()


def test_closure_capture_is_correct():
    """GET-ing two different parameter paths returns two different values.

    The most likely bug: generating four routes in one loop binds the loop variable late, so
    every route serves the last parameter's value. This test fetches two distinct paths and
    asserts they return distinct data. Values are lists because the field is list[Speed].
    """
    mw = _FakeMiddleware()
    unit = _make_unit()
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


def test_instance_with_no_id_returns_empty_list_and_still_calls_generate():
    """An instance lacking an `id` field yields `[]` but still triggers the base CRUD generation.

    The recursion cannot proceed without a root id to mangle into paths, but the middleware's
    top-level CRUD must still be wired. This guards against a silent early-return that skips
    everything.
    """

    class NoIdModel(BaseModel):
        model_config = {"extra": "forbid"}
        TU_hasConveyorBelt: list[Belt] = []

    mw = _FakeMiddleware()
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
    assert mw._generate_called


def test_a_binding_whose_owner_is_absent_generates_no_route():
    """A binding whose owner IRI is missing from the instance tree yields no route, not an error.

    Route generation is best-effort and additive — `_load_resource_datamodel` is documented as
    never fatal to startup — so a binding that cannot be placed costs its route, not the whole
    REST surface. The loud failure belongs at request time, where the router raises a 500 naming
    the missing individual, rather than at generation time where it would take down every other
    parameter with it.
    """
    mw = _FakeMiddleware()
    unit = _make_unit()
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
