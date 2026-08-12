"""SemanticMiddleware's connector flavours, through the constructor (#40, ADR 0022).

The seam is tested directly in ``test_semantic_connectors.py`` and against live data in
``test_scenario3_wiring_integration.py``. What is left is that the constructor parameters
actually reach it, and — the part most likely to break silently — that wiring happens at
**construction** rather than on startup.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection
from kapps_ogm import OGM

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.connectors.mqtt_binding import MQTTBinding
from kapps_semantic_middleware.connectors.semantic import SemanticConnectorRegistry

from conftest import requires_graphdb  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

SERVICE_CLASS = f"{seed.TU_NS}TransferUnitService"


@pytest.fixture(scope="module")
def seeded_unit(graphdb):
    """Module-scoped: these tests construct middlewares and assert on the connectors they
    wire, which is a read of the seeded graph. Registration only writes from the startup
    callbacks, and no test here starts a middleware."""
    seed.seed_scenario3(graphdb, OGM(db=graphdb))
    return graphdb


def _middleware(graphdb, **kwargs):
    return SemanticMiddleware(
        mode="resource",
        resource_iri=str(seed.TRANSFER_UNIT_1),
        service_class=SERVICE_CLASS,
        ogm=OGM(db=graphdb),
        heartbeat_interval=None,
        connector_registry=SemanticConnectorRegistry([MQTTBinding]),
        **kwargs,
    )


class TestWithoutAClassScope:
    """Scenarios 1 and 2 pass no class_scope, and must be entirely unaffected."""

    @requires_graphdb
    def test_no_class_scope_means_no_wiring(self, seeded_unit):
        middleware = _middleware(seeded_unit)

        assert middleware._wiring is None

    @requires_graphdb
    def test_no_class_scope_registers_no_connectors(self, seeded_unit):
        middleware = _middleware(seeded_unit)

        assert not middleware.connection_registry.connections


@requires_graphdb
class TestFlavours:
    """Three flavours of one library, differing only in what they connect (ADR 0022)."""

    def test_a_controller_wires_both_directions(self, seeded_unit, unit_scope):
        middleware = _middleware(seeded_unit, class_scope=unit_scope)

        directions = {r.sync_direction for _, r in middleware._wiring.registrations}
        assert directions == {
            SyncDirection.TO_PERSISTENCE,
            SyncDirection.FROM_PERSISTENCE,
        }

    def test_a_monitor_wires_nothing_that_writes(self, seeded_unit, unit_scope):
        middleware = _middleware(
            seeded_unit,
            class_scope=unit_scope,
            connector_sync_direction=SyncDirection.TO_PERSISTENCE,
        )

        assert all(
            r.sync_direction is SyncDirection.TO_PERSISTENCE
            for _, r in middleware._wiring.registrations
        )

    def test_an_inspector_wires_nothing_at_all(self, seeded_unit, unit_scope):
        middleware = _middleware(
            seeded_unit, class_scope=unit_scope, autoregister_connectors=False
        )

        assert middleware._wiring.registrations == []
        assert not middleware.connection_registry.connections

    def test_an_inspector_still_projects(self, seeded_unit, unit_scope):
        """The flag gates wiring, never recognition or the projection (ADR 0028)."""
        middleware = _middleware(
            seeded_unit, class_scope=unit_scope, autoregister_connectors=False
        )

        assert len(middleware._wiring.bindings) == 4
        assert middleware._wiring.southbound_properties


@requires_graphdb
class TestRegistrationTiming:
    def test_connectors_are_registered_before_startup(self, seeded_unit, unit_scope):
        """lifespan connects everything in the registry *before* on_start_up, and
        initiate_sync never calls connect(). A connector registered later never connects
        and its inbound direction dies silently (ADR 0023) — so registration must already
        have happened by the time the constructor returns.
        """
        middleware = _middleware(seeded_unit, class_scope=unit_scope)

        assert middleware.connection_registry.connections

    def test_defaults_are_the_controller_flavour(self, seeded_unit, unit_scope):
        """Defaults preserve today's behaviour, so nothing existing changes meaning."""
        middleware = _middleware(seeded_unit, class_scope=unit_scope)

        assert middleware.autoregister_connectors is True
        assert middleware.connector_sync_direction is SyncDirection.BIDIRECTIONAL


@requires_graphdb
class TestEnsureTransport:
    """``ensure_transport`` (ADR 0034) reaches the wiring plan through the constructor."""

    def test_the_hook_reaches_plan_wiring(self, seeded_unit, unit_scope):
        calls = []
        _middleware(
            seeded_unit,
            class_scope=unit_scope,
            ensure_transport=lambda host, port: calls.append((host, port)),
        )

        # Scenario 3's four parameters share one declared address, no port declared.
        assert calls == [(seed.MQTT_BROKER_IP, 1883)]

    def test_no_hook_is_the_default_and_calls_nothing(self, seeded_unit, unit_scope):
        middleware = _middleware(seeded_unit, class_scope=unit_scope)

        assert middleware.ensure_transport is None
