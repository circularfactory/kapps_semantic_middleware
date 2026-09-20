"""Recursive REST router integration: route generation from the real materialized tree.

``tests/test_recursive_rest_router.py`` proves the router logic against hand-built pydantic
models. That cannot prove the router works on the tree the **OGM materializes**. The
shape it walks is what a hand-built fixture assumes. Test the shape. Which nodes
carry an ``id``. What the field annotations are. How the parameter blanknodes come
back. This file closes that gap. Generate routes from a real seeded TransferUnit.
Assert the paths that come out.

Scope honestly: this covers **route generation from the real materialized tree**.
Write semantics are covered by the unit tests. The persistence connector is
observable there. Drive a live PUT here. Reach connectors that are registered but
not connected. No ASGI lifespan ran. Test the framework failure handling. Not this
router.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from kapps_triplestore_interface import IRI

from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection
from kapps_ogm import OGM
from kapps_ogm.utils.class_scope import ClassScope

from kapps_semantic_middleware import Mode, SemanticMiddleware

from conftest import methods_at, requires_graphdb  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402


@pytest.fixture
def scenario3_ogm(graphdb):
    """A seeded scenario 3 and a fresh OGM over it.

    Function-scoped. Load-bearing. Tempt the ~80 round trips a seed costs. Several
    tests here add interface metadata with a raw SPARQL INSERT. Prove a declared
    term survives the read. Share one seed across the module. Writes leak forward.
    An envelope path inserted by one test puts a later test formatter into envelope
    mode. It reads a raw scalar as an unobserved payload.
    """
    seed.seed_scenario3(graphdb, OGM(db=graphdb))
    # A *fresh* OGM. Deliberately not the one that seeded. The seeding client carries
    # state from its own writes. A test plans a wiring. Read the graph the way a
    # cold middleware reads it.
    return graphdb, OGM(db=graphdb)


def _unit_view() -> ClassScope:
    """The consumer view of a TransferUnit: the unit, its components, their parameters.

    Two levels. A TransferUnit parameters hang off its belts and barriers. A view
    belongs to its consumer. Configure here in embedding code. Not in the ontology.
    The ontology cannot know how deep a consumer cares to look.
    """
    return ClassScope.from_property_chains(
        [
            [seed.TU_HAS_CONVEYOR_BELT, seed.TU_HAS_CONVEYOR_SPEED],
            [seed.TU_HAS_LIGHT_BARRIER, seed.TU_IS_OCCUPIED],
        ]
    )


def _model_name(mw: SemanticMiddleware, ogm: OGM) -> str:
    """Derive the materialized model class name the way production does.

    ``SemanticMiddleware`` never stores the materialized instance.
    ``_load_resource_datamodel`` fetches it into a local. Let it go. Re-fetch via
    the wiring northbound kwargs. Get the class name. Build expected route paths.
    """
    node = ogm.fetch(instance_iri=seed.TRANSFER_UNIT_1, **mw._wiring.northbound_fetch_kwargs())
    return type(node.instance).__name__




@requires_graphdb
class TestParameterRoutes:
    """The deep parameter routes exist. Verbs are right per access mode."""

    @pytest.mark.asyncio
    async def test_the_left_belts_speed_is_addressable(self, scenario3_ogm):
        """The deep path for ConveyorBelt/left/hasConveyorSpeed exists. Serve GET.

        This is the route the whole ticket exists to create. A parameter nests two
        levels beneath the root instance. Reach through a list of belts. Then the
        speed property on a specific belt. This path does not exist. The recursive
        router failed to walk the materialized tree.
        """
        graphdb, ogm = scenario3_ogm

        mw = SemanticMiddleware(
            mode=Mode.RESOURCE,
            resource_iri=seed.TRANSFER_UNIT_1,
            service_class=f"{seed.TU_NS}TransferUnitService",
            ogm=ogm,
            host="127.0.0.1",
            port=8996,
            class_scope=_unit_view(),
            connector_sync_direction=SyncDirection.BIDIRECTIONAL,
            heartbeat_interval=None,
        )
        await mw._load_resource_datamodel()

        model_name = _model_name(mw, ogm)
        expected_path = (
            f"/{model_name}"
            f"/{IRI(str(seed.TRANSFER_UNIT_1)).lined}"
            f"/{seed.TU_HAS_CONVEYOR_BELT.lined}"
            f"/{IRI(str(seed.CONVEYOR_BELT_LEFT)).lined}"
            f"/{seed.TU_HAS_CONVEYOR_SPEED.lined}"
        )

        assert expected_path in mw._parameter_routes
        assert "GET" in methods_at(mw.app, expected_path)

    @pytest.mark.asyncio
    async def test_a_settable_parameter_gets_a_put(self, scenario3_ogm):
        """The conveyor speed path also serves PUT. The seeded parameter is readwrite.

        Access mode is read from the graph. ``inf:accessMode`` on the parameter node.
        Verb gating is per individual. One belt may be ``readwrite``. A barrier on
        the same unit is ``read``. A PUT to a read-only parameter returns 405. The
        route does not exist. FastAPI handles that automatically. Assert the
        readwrite case.
        """
        graphdb, ogm = scenario3_ogm

        mw = SemanticMiddleware(
            mode=Mode.RESOURCE,
            resource_iri=seed.TRANSFER_UNIT_1,
            service_class=f"{seed.TU_NS}TransferUnitService",
            ogm=ogm,
            host="127.0.0.1",
            port=8996,
            class_scope=_unit_view(),
            connector_sync_direction=SyncDirection.BIDIRECTIONAL,
            heartbeat_interval=None,
        )
        await mw._load_resource_datamodel()

        model_name = _model_name(mw, ogm)
        expected_path = (
            f"/{model_name}"
            f"/{IRI(str(seed.TRANSFER_UNIT_1)).lined}"
            f"/{seed.TU_HAS_CONVEYOR_BELT.lined}"
            f"/{IRI(str(seed.CONVEYOR_BELT_LEFT)).lined}"
            f"/{seed.TU_HAS_CONVEYOR_SPEED.lined}"
        )

        assert "PUT" in methods_at(mw.app, expected_path)

    @pytest.mark.asyncio
    async def test_a_read_only_sensor_gets_no_put(self, scenario3_ogm):
        """The light barrier path serves GET. **Not** PUT.

        A write to a light barrier must be a 405 from a route that does not exist.
        The seeded barrier ``inf:accessMode`` is ``READ``. The router must not
        generate a PUT handler for it. Guard against the regression. Access mode
        was ignored. Every parameter became writable.
        """
        graphdb, ogm = scenario3_ogm

        mw = SemanticMiddleware(
            mode=Mode.RESOURCE,
            resource_iri=seed.TRANSFER_UNIT_1,
            service_class=f"{seed.TU_NS}TransferUnitService",
            ogm=ogm,
            host="127.0.0.1",
            port=8996,
            class_scope=_unit_view(),
            connector_sync_direction=SyncDirection.BIDIRECTIONAL,
            heartbeat_interval=None,
        )
        await mw._load_resource_datamodel()

        model_name = _model_name(mw, ogm)
        expected_path = (
            f"/{model_name}"
            f"/{IRI(str(seed.TRANSFER_UNIT_1)).lined}"
            f"/{seed.TU_HAS_LIGHT_BARRIER.lined}"
            f"/{IRI(str(seed.LIGHT_BARRIER_FRONT)).lined}"
            f"/{seed.TU_IS_OCCUPIED.lined}"
        )

        assert expected_path in mw._parameter_routes
        assert "GET" in methods_at(mw.app, expected_path)
        assert "PUT" not in methods_at(mw.app, expected_path)


@requires_graphdb
class TestRecursionTermination:
    """The walk stops at parameters. Do not descend further."""

    @pytest.mark.asyncio
    async def test_no_route_is_generated_below_a_parameter(self, scenario3_ogm):
        """No generated route path starts with another generated route path plus ``/``.

        Recursion terminates at COMPLEX properties. RDF has no properties-about-properties.
        Metadata about a conveyor speed is modelled as a blanknode. Hang it off the
        parameter property. Its unit. Its MQTT topic. That blanknode materializes as
        an ``AnonymousClass``. No ``id``. Not ``Identifiable``. Never routable. Treat
        the blanknode dict as the atomic element. The id-less problem disappears. Assert
        that no route descends below a parameter.
        """
        graphdb, ogm = scenario3_ogm

        mw = SemanticMiddleware(
            mode=Mode.RESOURCE,
            resource_iri=seed.TRANSFER_UNIT_1,
            service_class=f"{seed.TU_NS}TransferUnitService",
            ogm=ogm,
            host="127.0.0.1",
            port=8996,
            class_scope=_unit_view(),
            connector_sync_direction=SyncDirection.BIDIRECTIONAL,
            heartbeat_interval=None,
        )
        await mw._load_resource_datamodel()

        param_paths = mw._parameter_routes

        for i, path_a in enumerate(param_paths):
            for j, path_b in enumerate(param_paths):
                if i != j:
                    assert not path_b.startswith(path_a + "/"), (
                        f"Route {path_b} descends below parameter route {path_a}"
                    )


@requires_graphdb
class TestCoverage:
    """The walk reaches everything recognition found."""

    @pytest.mark.asyncio
    async def test_every_recognised_parameter_is_addressable(self, scenario3_ogm):
        """The number of generated parameter routes equals the number of bindings. Every
        binding ``field_id`` appears as the last segment of some generated path.

        This is the claim. The tree walk reaches everything recognition found. The
        failure mode exists. The walk silently misses a branch. Recognition happens
        from the graph. Routing happens from the materialized instance. The instance
        tree differs from what recognition expects. A parameter is recognised but
        unreachable via REST.
        """
        graphdb, ogm = scenario3_ogm

        mw = SemanticMiddleware(
            mode=Mode.RESOURCE,
            resource_iri=seed.TRANSFER_UNIT_1,
            service_class=f"{seed.TU_NS}TransferUnitService",
            ogm=ogm,
            host="127.0.0.1",
            port=8996,
            class_scope=_unit_view(),
            connector_sync_direction=SyncDirection.BIDIRECTIONAL,
            heartbeat_interval=None,
        )
        await mw._load_resource_datamodel()

        param_routes = mw._parameter_routes

        assert len(param_routes) == len(mw._wiring.bindings)

        binding_field_ids = {b.field_id for b in mw._wiring.bindings}
        route_last_segments = {r.split("/")[-1] for r in param_routes}

        assert binding_field_ids == route_last_segments

    @pytest.mark.asyncio
    async def test_the_top_level_crud_still_exists(self, scenario3_ogm):
        """The framework top-level route for the TransferUnit model is still present.

        The local router calls the framework generator. Add its own routes after. Guard
        the "scenarios 1 and 2 are unaffected" criterion at the route level. Add
        recursive parameter routes. Do not break the existing top-level CRUD. Other
        consumers rely on it.
        """
        graphdb, ogm = scenario3_ogm

        mw = SemanticMiddleware(
            mode=Mode.RESOURCE,
            resource_iri=seed.TRANSFER_UNIT_1,
            service_class=f"{seed.TU_NS}TransferUnitService",
            ogm=ogm,
            host="127.0.0.1",
            port=8996,
            class_scope=_unit_view(),
            connector_sync_direction=SyncDirection.BIDIRECTIONAL,
            heartbeat_interval=None,
        )
        await mw._load_resource_datamodel()

        model_name = _model_name(mw, ogm)
        # The framework parameterises the item: `/{Model}/{item_id}`. Match at request time.
        # The parameter routes below it are literal. The verb gate is per individual.
        # The two levels legitimately encode ids differently. A consumer
        # derives the deep paths structurally from a GET. Not build them by hand.
        top_level_path = f"/{model_name}/{{item_id}}"

        assert any(r.path == top_level_path for r in mw.app.routes)
