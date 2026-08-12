"""Scenario 3 seeding integration tests against a live GraphDB.

The TransferUnit ontology is classes only; its instances are created through the OGM by
`seed.seed_scenario3` rather than authored as Turtle, so this exercises the same validated
write path a running middleware uses (root ADR 0008). Skipped when GRAPHDB_* env vars are
absent (see conftest).

Scenario 3 is a **locator** (ADR 0024): the graph records where each value lives, never the
value itself, so a parameter node carries a unit and its connection metadata and no
`inf:hasValue`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kapps_ogm import OGM
from kapps_semantic_middleware.credentials import graphdb_env_present

requires_graphdb = pytest.mark.skipif(
    not graphdb_env_present(),
    reason="GRAPHDB_* environment variables not set; skipping live-GraphDB integration test",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

EXPLICIT = "FROM <http://www.ontotext.com/explicit>"


def _bindings(db, query: str):
    return db.query(query, convert_bindings=True)["results"]["bindings"]


def _parameter_properties(db, resource_iri, parameter_property) -> set[str]:
    """The local names of every property on `resource_iri`'s parameter node."""
    rows = _bindings(
        db,
        f"SELECT ?p {EXPLICIT} WHERE {{ <{resource_iri}> <{parameter_property}> ?n . ?n ?p ?v }}",
    )
    return {str(r["p"]).split("#")[-1] for r in rows}


@pytest.fixture
def seeded(graphdb):
    """Seed scenario 3 and hand back the client.

    Deliberately function-scoped, unlike the read-only scenario-3 fixtures elsewhere:
    `test_seeding_twice_is_idempotent` re-seeds inside the test body, so sharing one seed
    across the module would let that re-seed change what a later test sees.
    """
    seed.seed_scenario3(graphdb, OGM(db=graphdb))
    return graphdb


@requires_graphdb
def test_shared_ontologies_live_in_their_own_named_graphs(seeded):
    """Core, svc: and mes: are published/general modules: one named graph each."""
    for graph in (seed.CORE_GRAPH, seed.SERVICE_GRAPH, seed.MES_GRAPH):
        assert seeded.query(f"ASK {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}")["boolean"], graph

    # The scenario's own classes and instances stay in the default graph, so
    # clear_repository wipes them without touching the shared modules.
    assert not _bindings(
        seeded,
        f"SELECT ?g WHERE {{ GRAPH ?g {{ <{seed.TRANSFER_UNIT_1}> ?p ?o }} }}",
    )


@requires_graphdb
def test_transfer_unit_is_composed_of_its_belts_and_barriers(seeded):
    belts = {
        str(r["b"])
        for r in _bindings(
            seeded,
            f"SELECT ?b {EXPLICIT} WHERE {{ <{seed.TRANSFER_UNIT_1}> <{seed.TU_HAS_CONVEYOR_BELT}> ?b }}",
        )
    }
    barriers = {
        str(r["b"])
        for r in _bindings(
            seeded,
            f"SELECT ?b {EXPLICIT} WHERE {{ <{seed.TRANSFER_UNIT_1}> <{seed.TU_HAS_LIGHT_BARRIER}> ?b }}",
        )
    }
    assert belts == {str(seed.CONVEYOR_BELT_LEFT), str(seed.CONVEYOR_BELT_RIGHT)}
    assert barriers == {str(seed.LIGHT_BARRIER_FRONT), str(seed.LIGHT_BARRIER_BACK)}


@requires_graphdb
def test_a_settable_parameter_carries_a_set_topic(seeded):
    """A readwrite parameter needs two topics: the connector publishes to the one it
    subscribes to, so read and write need separate topics (ADR 0023)."""
    properties = _parameter_properties(
        seeded, seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED
    )
    assert properties == {
        "hasUnit",
        "accessMode",
        "hasMQTTTopic",
        "hasMQTTSetTopic",
        "hasMQTTBrokerIP",
    }


@requires_graphdb
def test_a_read_only_parameter_has_no_set_topic(seeded):
    """A light barrier is an occupancy sensor: read-only, and it has no unit."""
    properties = _parameter_properties(
        seeded, seed.LIGHT_BARRIER_FRONT, seed.TU_IS_OCCUPIED
    )
    assert properties == {"accessMode", "hasMQTTTopic", "hasMQTTBrokerIP"}
    assert "hasMQTTSetTopic" not in properties


@requires_graphdb
def test_no_value_is_seeded_because_scenario_three_is_a_locator(seeded):
    """ADR 0024: the graph says where the value lives, never what it is. A parameter that
    has not been observed yet simply has no value triple."""
    assert not _bindings(
        seeded, f"SELECT ?s {EXPLICIT} WHERE {{ ?s <{seed.INF_HAS_VALUE}> ?o }}"
    )


@requires_graphdb
def test_seeding_twice_is_idempotent(seeded):
    """A re-seed clears the default graph first, so it must not accumulate duplicates or
    leave a second parameter node behind."""
    seed.seed_scenario3(seeded, OGM(db=seeded))

    rows = _bindings(
        seeded,
        f"SELECT ?n {EXPLICIT} WHERE {{ <{seed.CONVEYOR_BELT_LEFT}> <{seed.TU_HAS_CONVEYOR_SPEED}> ?n }}",
    )
    assert len(rows) == 1
    assert _parameter_properties(
        seeded, seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED
    ) == {
        "hasUnit",
        "accessMode",
        "hasMQTTTopic",
        "hasMQTTSetTopic",
        "hasMQTTBrokerIP",
    }
