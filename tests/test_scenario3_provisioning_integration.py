"""Scenario 3 from a cold start: the middleware provisions the metadata, and a restart keeps it.

Live store. The seed creates the TransferUnit, its belts and its barriers without any parameter
node (``seed_scenario3(metadata=False)``). A middleware constructed with the scenario's
declaration provisions every parameter and wires it. A second middleware on the same graph,
the restart, writes nothing: the metadata is still on the same node, and each holder has one.

The other tests cover the other boxes against the real store: a node with domain content only
is provisioned in place, a monitor writes nothing, and a contradiction raises unless the
declaration is exclusive, which replaces the stored metadata on the same node.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from kapps_ogm import OGM

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.connectors.mqtt_binding import MQTTBinding
from kapps_semantic_middleware.connectors.semantic import SemanticConnectorRegistry
from kapps_semantic_middleware.errors import DeploymentValueError

from _explicit import explicit_select
from conftest import requires_store  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

SERVICE_CLASS = f"{seed.TU_NS}TransferUnitService"

PARAMETERS = [(belt, seed.TU_HAS_CONVEYOR_SPEED) for belt, _ in seed.SCENARIO3_BELTS] + [
    (barrier, seed.TU_IS_OCCUPIED) for barrier, _ in seed.SCENARIO3_BARRIERS
]


@pytest.fixture
def cold_unit(store):
    seed.seed_scenario3(store, OGM(db=store), metadata=False)
    return store


def _start(store, unit_scope, *, exclusive=False, **kwargs) -> SemanticMiddleware:
    """A middleware's construction on a fresh OGM, the way a process start reads the graph."""
    return SemanticMiddleware(
        mode="resource",
        resource_iri=str(seed.TRANSFER_UNIT_1),
        service_class=SERVICE_CLASS,
        ogm=OGM(db=store),
        heartbeat_interval=None,
        class_scope=unit_scope,
        connector_registry=SemanticConnectorRegistry([MQTTBinding]),
        provisions=seed.scenario3_provisions(exclusive=exclusive),
        **kwargs,
    )


def _parameter_nodes(store) -> dict:
    """Each holder's parameter nodes and what they carry, as strings, from asserted triples."""
    nodes: dict = {}
    for holder, prop in PARAMETERS:
        rows = store.query(
            explicit_select(["?node", "?p", "?v"], f"<{holder}> <{prop}> ?node . ?node ?p ?v"),
            convert_bindings=True,
        )["results"]["bindings"]
        for row in rows:
            content = nodes.setdefault(str(holder), {}).setdefault(str(row["node"]), {})
            content.setdefault(str(row["p"]), set()).add(str(row["v"]))
    return nodes


def _carries(content: dict, metadata: dict) -> bool:
    return all(
        content.get(str(p)) == {str(v) for v in values} for p, values in metadata.items()
    )


@requires_store
class TestScenario3ProvisioningIntegration:
    def test_a_cold_start_provisions_and_a_restart_keeps_the_same_nodes(
        self, cold_unit, unit_scope
    ):
        assert _parameter_nodes(cold_unit) == {}

        first = _start(cold_unit, unit_scope)

        provisioned = _parameter_nodes(cold_unit)
        assert len(first._wiring.bindings) == 4
        assert first._wiring.registrations
        for belt, position in seed.SCENARIO3_BELTS:
            (content,) = provisioned[str(belt)].values()
            assert _carries(content, seed.belt_metadata(position))
        for barrier, position in seed.SCENARIO3_BARRIERS:
            (content,) = provisioned[str(barrier)].values()
            assert _carries(content, seed.barrier_metadata(position))

        restarted = _start(cold_unit, unit_scope)

        assert _parameter_nodes(cold_unit) == provisioned
        assert len(restarted._wiring.bindings) == 4

    def test_a_node_with_domain_content_only_is_provisioned_in_place(self, cold_unit, unit_scope):
        belt, position = seed.SCENARIO3_BELTS[0]
        OGM(db=cold_unit).commit(
            instance_iri=belt,
            data={seed.TU_HAS_CONVEYOR_SPEED: [{seed.TU_HAS_UNIT: ["m/s"]}]},
        )
        (node,) = _parameter_nodes(cold_unit)[str(belt)]

        _start(cold_unit, unit_scope)

        ((after, content),) = _parameter_nodes(cold_unit)[str(belt)].items()
        assert after == node
        assert _carries(content, seed.belt_metadata(position))

    def test_a_monitor_provisions_nothing(self, cold_unit, unit_scope):
        monitor = _start(cold_unit, unit_scope, autoregister_connectors=False)

        assert _parameter_nodes(cold_unit) == {}
        assert monitor._wiring.registrations == []

    def test_a_contradiction_raises_and_an_exclusive_declaration_replaces_it(
        self, cold_unit, unit_scope
    ):
        belt, position = seed.SCENARIO3_BELTS[0]
        stale = {**seed.belt_metadata(position), seed.INF_HAS_MQTT_TOPIC: ["an/old/topic"]}
        OGM(db=cold_unit).commit(instance_iri=belt, data={seed.TU_HAS_CONVEYOR_SPEED: [stale]})
        (node,) = _parameter_nodes(cold_unit)[str(belt)]

        with pytest.raises(DeploymentValueError, match="an/old/topic"):
            _start(cold_unit, unit_scope)

        _start(cold_unit, unit_scope, exclusive=True)

        ((replaced, content),) = _parameter_nodes(cold_unit)[str(belt)].items()
        assert replaced == node
        assert _carries(content, seed.belt_metadata(position))
