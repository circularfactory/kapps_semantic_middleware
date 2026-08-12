"""Factory seeding for the multi-process TransferUnit demo.

This module mints IRIs and creates instance data for N TransferUnits plus one control station.
The IRI scheme is index-derived (ADR 0030): unit 1 keeps the IRIs from
``examples/seed.py``, and higher indices follow the same pattern. This module
does not import from ``examples/`` — constants are duplicated on purpose so the
demo package is self-contained (ADR 0030: "the duplication ends in its own commit").
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from kapps_triplestore_interface import IRI
from kapps_ogm.utils.class_scope import ClassScope
from rdflib import Literal
from rdflib.namespace import RDF, RDFS

from kapps_semantic_middleware.seeding import clear_repository, load_shared_ontologies
from kapps_semantic_middleware.vocabulary import INF, SVC


# --- Namespaces ------------------------------------------------------------- #

TU_NS = "https://www.sfb1574.kit.edu/ontologies/TransferUnit#"
TUI_NS = "https://www.sfb1574.kit.edu/ontologies/TransferUnitInstances#"
FAC_NS = "https://www.sfb1574.kit.edu/ontologies/Factory#"
FACI_NS = "https://www.sfb1574.kit.edu/ontologies/FactoryInstances#"

# --- Domain class and property IRIs (tu:) ------------------------------------ #

TRANSFER_UNIT_CLASS = IRI(f"{TU_NS}TransferUnit")
CONVEYOR_BELT_CLASS = IRI(f"{TU_NS}ConveyorBelt")
LIGHT_BARRIER_CLASS = IRI(f"{TU_NS}LightBarrier")
# Already declared in transferunit.ttl, alongside the domain classes above — the
# middleware runner registers as this, not the generic svc:Service (ADR 0018's
# per-domain Service subclass, same convention factory.ttl repeats for fac:).
TRANSFER_UNIT_SERVICE_CLASS = IRI(f"{TU_NS}TransferUnitService")

TU_HAS_CONVEYOR_BELT = IRI(f"{TU_NS}hasConveyorBelt")
TU_HAS_LIGHT_BARRIER = IRI(f"{TU_NS}hasLightBarrier")
TU_HAS_CONVEYOR_SPEED = IRI(f"{TU_NS}hasConveyorSpeed")
TU_IS_OCCUPIED = IRI(f"{TU_NS}isOccupied")
TU_HAS_UNIT = IRI(f"{TU_NS}hasUnit")

# --- Interface property IRIs (inf:) ------------------------------------------ #
#
# Reached through `vocabulary.INF`, never minted here. ADR 0021 puts every ontology IRI in
# one place, and `inf:` is *library* vocabulary: the connectors match on these very terms,
# so a demo that spelled them itself could drift out of step with the code that reads them.
# The `tu:` and `fac:` IRIs above are different -- those are domain terms this demo owns,
# and ADR 0030 keeps them here on purpose.

# --- Control station ---------------------------------------------------------- #

CONTROL_STATION = IRI(f"{FACI_NS}ControlStation1")
CONTROL_STATION_CLASS = IRI(f"{FAC_NS}ControlStation")
CONTROL_STATION_SERVICE_CLASS = IRI(f"{FAC_NS}ControlStationService")

MQTT_BROKER_IP = "127.0.0.1"

# Every unit's own broker (ADR 0029 as amended, ADR 0030 as amended). Not 1883 + n: 1900 is
# SSDP/UPnP and is live on most Linux desktops, so --units 17 would break on a
# machine-specific collision that presents as a broker fault. 18830 is a quiet, unassigned
# stretch of the registered range that never touches 1883 itself.
BROKER_PORT_BASE = 18830


def broker_port(n: int) -> int:
    """The port of unit n's own MQTT broker. Unit 1 -> 18831, unit 2 -> 18832, ...

    Both readers -- the middleware, off inf:hasMQTTBrokerPort on the graph, and the PLC, off
    the launcher's --broker-port flag -- must know this before either process starts, so it is
    a pure function of the unit index, exactly like every IRI and every topic (ADR 0030).

    Unit 1's port, 18831, coincides with tests/conftest.py's MQTT_TEST_PORT -- picked there,
    separately, as an unlikely-to-collide high port. Harmless: the demo's own launcher and the
    test suite's live-broker fixtures never run in the same process at once. Noted here so the
    match reads as coincidence, not a shared constant someone forgot to factor out.
    """
    return BROKER_PORT_BASE + n


def _mint_transfer_unit_iri(n: int) -> IRI:
    """Mint the IRI for TransferUnit<n>."""
    return IRI(f"{TUI_NS}TransferUnit{n}")


def _mint_conveyor_belt_iri(n: int, position: str) -> IRI:
    """Mint the IRI for ConveyorBelt<n>_{position}."""
    return IRI(f"{TUI_NS}ConveyorBelt{n}_{position}")


def _mint_light_barrier_iri(n: int, position: str) -> IRI:
    """Mint the IRI for LightBarrier<n>_{position}."""
    return IRI(f"{TUI_NS}LightBarrier{n}_{position}")


def _mqtt_topic(n: int, component: str, position: str, param: str) -> str:
    """Build the MQTT topic for a parameter (ADR 0023 scheme).

    Topic shape: TransferUnit<n>/<component>/<position>/<param>. A setpoint
    appends _set to the param segment.
    """
    return f"TransferUnit{n}/{component}/{position}/{param}"


def _create_belt(ogm, n: int, position: str) -> IRI:
    """Create a conveyor belt instance for unit n at the given position."""
    belt_iri = _mint_conveyor_belt_iri(n, position)
    ogm.create(
        class_iri=CONVEYOR_BELT_CLASS,
        instance_iri=belt_iri,
        class_scope=ClassScope.from_property_chains([[TU_HAS_CONVEYOR_SPEED]]),
        data={
            TU_HAS_CONVEYOR_SPEED: [
                {
                    TU_HAS_UNIT: ["m/s"],
                    INF.accessMode: ["readwrite"],
                    INF.hasMQTTTopic: [
                        _mqtt_topic(n, "ConveyorBelt", position, "speed")
                    ],
                    INF.hasMQTTSetTopic: [
                        _mqtt_topic(n, "ConveyorBelt", position, "speed_set")
                    ],
                    INF.hasMQTTBrokerIP: [MQTT_BROKER_IP],
                    INF.hasMQTTBrokerPort: [broker_port(n)],
                }
            ]
        },
    )
    return belt_iri


def _create_barrier(ogm, n: int, position: str) -> IRI:
    """Create a light barrier instance for unit n at the given position."""
    barrier_iri = _mint_light_barrier_iri(n, position)
    ogm.create(
        class_iri=LIGHT_BARRIER_CLASS,
        instance_iri=barrier_iri,
        class_scope=ClassScope.from_property_chains([[TU_IS_OCCUPIED]]),
        data={
            TU_IS_OCCUPIED: [
                {
                    INF.accessMode: ["read"],
                    INF.hasMQTTTopic: [
                        _mqtt_topic(n, "LightBarrier", position, "occupied")
                    ],
                    INF.hasMQTTBrokerIP: [MQTT_BROKER_IP],
                    INF.hasMQTTBrokerPort: [broker_port(n)],
                }
            ]
        },
    )
    return barrier_iri


def _create_transfer_unit(ogm, n: int) -> IRI:
    """Create a TransferUnit instance with its two conveyor belts and two light barriers."""
    unit_iri = _mint_transfer_unit_iri(n)

    belt_left = _create_belt(ogm, n, "left")
    belt_right = _create_belt(ogm, n, "right")
    barrier_front = _create_barrier(ogm, n, "front")
    barrier_back = _create_barrier(ogm, n, "back")

    ogm.create(
        class_iri=TRANSFER_UNIT_CLASS,
        instance_iri=unit_iri,
        class_scope=ClassScope.from_property_chains(
            [[TU_HAS_CONVEYOR_BELT], [TU_HAS_LIGHT_BARRIER]]
        ),
        data={
            TU_HAS_CONVEYOR_BELT: [
                {"id": belt_left},
                {"id": belt_right},
            ],
            TU_HAS_LIGHT_BARRIER: [
                {"id": barrier_front},
                {"id": barrier_back},
            ],
        },
    )

    # A human-readable name (#89 item 5). discover_resources binds ?label off exactly
    # this predicate and returned label=None for every unit before this line existed --
    # the graph had no rdfs:label on a TransferUnit individual for the SPARQL OPTIONAL to
    # find. #82 (the control station board) renders unit identity on every card, so this
    # is seeded rather than dropped from ResourceInfo: a real name for a real card, not an
    # empty field nothing downstream can build on. A plain triple_add, the same way the
    # control station's own label goes on below -- ogm.create's class_scope only covers
    # domain properties, and rdfs:label is not one.
    ogm.db.triple_add((unit_iri, RDFS.label, Literal(f"TransferUnit {n}")))
    return unit_iri


def _load_demo_ontologies(db) -> None:
    """Load the TransferUnit domain ontology and the factory classes into the default graph."""
    transferunit_ttl = (Path(__file__).parent / "transferunit.ttl").read_text(
        encoding="utf-8"
    )
    db.import_statements(transferunit_ttl, content_type="application/x-turtle")

    factory_ttl = (Path(__file__).parent / "factory.ttl").read_text(encoding="utf-8")
    db.import_statements(factory_ttl, content_type="application/x-turtle")


def seed_factory(db, ogm, units: int) -> None:
    """Seed the factory: clear the default graph, load ontologies, create N units plus one control station.

    Args:
        db: The GraphDB client.
        ogm: The OGM instance for validated writes.
        units: Number of TransferUnits to create (indices 1..units).
    """
    clear_repository(db)
    load_shared_ontologies(db)
    _load_demo_ontologies(db)

    for n in range(1, units + 1):
        _create_transfer_unit(ogm, n)

    db.triple_add((CONTROL_STATION, RDF.type, CONTROL_STATION_CLASS))
    # Same reasoning as a unit's label above (#89 item 5): a real name for the one card
    # that isn't a TransferUnit.
    db.triple_add((CONTROL_STATION, RDFS.label, Literal("Control Station")))


def factory_is_live(db, max_age_seconds: float = 90.0) -> list[IRI]:
    """Check whether a factory is currently live in the graph.

    A factory is live when any Service node carries both svc:address and a
    svc:lastHeartbeat within the staleness window. This inverts the query
    shape from registration.find_stale_services (live is bound(?hb) and
    fresh, not stale or absent).

    Args:
        db: The GraphDB client.
        max_age_seconds: Maximum age of a heartbeat to count as fresh.

    Returns:
        The live Service IRIs. An empty list means the factory is not live.
    """
    now = datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(seconds=max_age_seconds)).isoformat()

    sparql = f"""
    SELECT DISTINCT ?s WHERE {{
        ?s <{SVC.address}> ?addr .
        ?s <{SVC.lastHeartbeat}> ?hb .
        FILTER (?hb >= "{cutoff_iso}"^^<http://www.w3.org/2001/XMLSchema#dateTime>)
    }}
    """
    result = db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    return [IRI(str(b["s"])) for b in bindings]
