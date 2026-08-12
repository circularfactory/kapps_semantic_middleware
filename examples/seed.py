"""Self-contained seeding for the example scenarios (ADR 0010).

Every example scenario runs against a dedicated, clearable GraphDB repository.
These helpers clear it. They load exactly the ground-truth ontology the
scenario needs (the ``svc:`` module plus the demo ontology of the scenario).
They then create the starting instance data. A scenario notebook or
integration test is therefore a complete, reproducible specification of its
own prerequisites, and never depends on the production state.

The system writes everything into the default graph of the repository, which
``clear_repository`` wipes at the start of each run.
"""

from __future__ import annotations

from pathlib import Path

from kapps_triplestore_interface import IRI
from kapps_ogm.utils.class_scope import ClassScope
from rdflib.namespace import RDF

from kapps_semantic_middleware.registration import create_possession
from kapps_semantic_middleware.seeding import (
    # Re-exported, not used here. A scenario asserts against the named graphs it
    # seeded, and reaches them through this module rather than through the library
    # (``tests/test_scenario3_seed_integration.py`` reads ``seed.CORE_GRAPH``).
    CORE_GRAPH,  # noqa: F401
    MES_GRAPH,  # noqa: F401
    SERVICE_GRAPH,  # noqa: F401
    _read_mes_ontology,
    clear_repository,
    load_shared_ontologies,
)
from kapps_semantic_middleware.vocabulary import MES


DEMO_NS = "https://example.org/kapps-demo#"

# --- Scenario 1 class IRIs (ground-truth, declared in demo_ontology.ttl) ---
HELLO_RESOURCE_CLASS = IRI(f"{DEMO_NS}DemoResource")
HELLO_SERVICE_CLASS = IRI(f"{DEMO_NS}HelloWorldService")
HELLO_CAPABILITY_CLASS = IRI(f"{DEMO_NS}HelloWorldCapability")
HELLO_WORKFLOW_CLASS = IRI(f"{DEMO_NS}HelloWorldWorkflow")
PLANNER_RESOURCE_CLASS = IRI(f"{DEMO_NS}PlannerResource")
PLANNER_SERVICE_CLASS = IRI(f"{DEMO_NS}PlannerService")

# --- Scenario 1 instance IRIs ---
HELLO_RESOURCE = IRI(f"{DEMO_NS}hello_resource")
PLANNER_RESOURCE = IRI(f"{DEMO_NS}planner_resource")

# --- Scenario 2 (door) class IRIs ---
DOOR_RESOURCE_CLASS = IRI(f"{DEMO_NS}DoorResource")
DOOR_SERVICE_CLASS = IRI(f"{DEMO_NS}DoorControllerService")
DOOR_OPEN_CAPABILITY_CLASS = IRI(f"{DEMO_NS}DoorOpenCapability")
DOOR_CLOSE_CAPABILITY_CLASS = IRI(f"{DEMO_NS}DoorCloseCapability")
DOOR_STATUS_CAPABILITY_CLASS = IRI(f"{DEMO_NS}DoorStatusCapability")
DOOR_OPEN_WORKFLOW_CLASS = IRI(f"{DEMO_NS}DoorOpenWorkflow")
DOOR_CLOSE_WORKFLOW_CLASS = IRI(f"{DEMO_NS}DoorCloseWorkflow")
DOOR_STATUS_STATE_CLASS = IRI(f"{DEMO_NS}DoorStatusStateProperty")

# --- Scenario 2 instance IRIs ---
DOOR_RESOURCE = IRI(f"{DEMO_NS}door_042")

# Mobile robot (scenario 2 consumer): discovers and drives the door through the graph.
MOBILE_ROBOT_RESOURCE_CLASS = IRI(f"{DEMO_NS}MobileRobotResource")
MOBILE_ROBOT_SERVICE_CLASS = IRI(f"{DEMO_NS}MobileRobotControllerService")
MOBILE_ROBOT = IRI(f"{DEMO_NS}mobile_robot_007")

# --- Handover scenario IRIs (Core reified possession + mes: handover ability) ---
TRANSFER_MODULE_CLASS = IRI(f"{DEMO_NS}TransferModule")
BOX_CLASS = IRI(f"{DEMO_NS}Box")
HANDOVER_SOURCE = IRI(f"{DEMO_NS}transfer_module_A")
HANDOVER_DEST = IRI(f"{DEMO_NS}transfer_module_B")
HANDOVER_BOX = IRI(f"{DEMO_NS}box_001")

# --- Scenario 3 (TransferUnit) ----------------------------------------------------
TU_NS = "https://www.sfb1574.kit.edu/ontologies/TransferUnit#"
TUI_NS = "https://www.sfb1574.kit.edu/ontologies/TransferUnitInstances#"
INF_NS = "https://www.sfb1574.kit.edu/ontologies/CrcInterfaces#"

TRANSFER_UNIT_CLASS = IRI(f"{TU_NS}TransferUnit")
CONVEYOR_BELT_CLASS = IRI(f"{TU_NS}ConveyorBelt")
LIGHT_BARRIER_CLASS = IRI(f"{TU_NS}LightBarrier")

TU_HAS_CONVEYOR_BELT = IRI(f"{TU_NS}hasConveyorBelt")
TU_HAS_LIGHT_BARRIER = IRI(f"{TU_NS}hasLightBarrier")
TU_HAS_CONVEYOR_SPEED = IRI(f"{TU_NS}hasConveyorSpeed")
TU_IS_OCCUPIED = IRI(f"{TU_NS}isOccupied")
TU_HAS_UNIT = IRI(f"{TU_NS}hasUnit")

INF_HAS_VALUE = IRI(f"{INF_NS}hasValue")
INF_ACCESS_MODE = IRI(f"{INF_NS}accessMode")
INF_HAS_MQTT_TOPIC = IRI(f"{INF_NS}hasMQTTTopic")
INF_HAS_MQTT_SET_TOPIC = IRI(f"{INF_NS}hasMQTTSetTopic")
INF_HAS_MQTT_BROKER_IP = IRI(f"{INF_NS}hasMQTTBrokerIP")

TRANSFER_UNIT_1 = IRI(f"{TUI_NS}TransferUnit1")
CONVEYOR_BELT_LEFT = IRI(f"{TUI_NS}ConveyorBelt1_left")
CONVEYOR_BELT_RIGHT = IRI(f"{TUI_NS}ConveyorBelt1_right")
LIGHT_BARRIER_FRONT = IRI(f"{TUI_NS}LightBarrier1_front")
LIGHT_BARRIER_BACK = IRI(f"{TUI_NS}LightBarrier1_back")

MQTT_BROKER_IP = "127.0.0.1"


def _read_demo_ontology(filename: str) -> str:
    """Return a scenario demo ontology Turtle file (sibling of this file)."""
    return (Path(__file__).parent / filename).read_text(encoding="utf-8")


def load_scenario1_ontologies(db) -> None:
    """Load the shared modules plus ONLY the scenario 1 demo classes."""
    load_shared_ontologies(db)
    db.import_statements(
        _read_demo_ontology("demo_scenario1.ttl"), content_type="application/x-turtle"
    )


def load_scenario2_ontologies(db) -> None:
    """Load the shared modules plus ONLY the scenario 2 (door) demo classes."""
    load_shared_ontologies(db)
    db.import_statements(
        _read_demo_ontology("demo_scenario2.ttl"), content_type="application/x-turtle"
    )


def load_scenario3_ontologies(db) -> None:
    """Load the shared modules plus the scenario 3 TransferUnit classes.

    ``transferunit.ttl`` is classes only — the ``inf:`` interface vocabulary and the
    ``tu:`` domain terms. Its instances are created through the OGM by
    :func:`seed_scenario3`, and its ``cfc:`` superclasses come from the Core graph rather
    than from local stubs.
    """
    load_shared_ontologies(db)
    db.import_statements(
        _read_demo_ontology("transferunit.ttl"), content_type="application/x-turtle"
    )


def create_resource(db, resource_iri: IRI, resource_class: IRI) -> None:
    """Instantiate a resource individual of the given class."""
    db.triple_add((resource_iri, RDF.type, resource_class))


def seed_scenario1(db) -> None:
    """Full scenario 1 seed: clear, load ontologies, and create the hello + planner resources."""
    clear_repository(db)
    load_scenario1_ontologies(db)
    create_resource(db, HELLO_RESOURCE, HELLO_RESOURCE_CLASS)
    create_resource(db, PLANNER_RESOURCE, PLANNER_RESOURCE_CLASS)


def seed_scenario3(db, ogm) -> None:
    """Full scenario 3 seed.

    Clears the graph, loads the shared modules and the TransferUnit classes,
    and creates one TransferUnit with two belts and two light barriers,
    **through the OGM**.

    This function creates instances rather than authored as Turtle. So the seed
    exercises the same validated write path a running middleware uses (root
    ADR 0008), and the ontology file stays classes-only.

    No ``inf:hasValue`` literals: scenario 3 is a locator (ADR 0024). The
    graph records where each value lives. The live value exists only in the
    datamodel and over REST.
    """
    clear_repository(db)
    load_scenario3_ontologies(db)

    for belt, position in (
        (CONVEYOR_BELT_LEFT, "left"),
        (CONVEYOR_BELT_RIGHT, "right"),
    ):
        ogm.create(
            class_iri=CONVEYOR_BELT_CLASS,
            instance_iri=belt,
            class_scope=ClassScope.from_property_chains([[TU_HAS_CONVEYOR_SPEED]]),
            data={
                TU_HAS_CONVEYOR_SPEED: [
                    {
                        TU_HAS_UNIT: ["m/s"],
                        INF_ACCESS_MODE: ["readwrite"],
                        INF_HAS_MQTT_TOPIC: [
                            f"TransferUnit1/ConveyorBelt/{position}/speed"
                        ],
                        INF_HAS_MQTT_SET_TOPIC: [
                            f"TransferUnit1/ConveyorBelt/{position}/speed_set"
                        ],
                        INF_HAS_MQTT_BROKER_IP: [MQTT_BROKER_IP],
                    }
                ]
            },
        )

    for barrier, position in (
        (LIGHT_BARRIER_FRONT, "front"),
        (LIGHT_BARRIER_BACK, "back"),
    ):
        ogm.create(
            class_iri=LIGHT_BARRIER_CLASS,
            instance_iri=barrier,
            class_scope=ClassScope.from_property_chains([[TU_IS_OCCUPIED]]),
            data={
                TU_IS_OCCUPIED: [
                    {
                        INF_ACCESS_MODE: ["read"],
                        INF_HAS_MQTT_TOPIC: [
                            f"TransferUnit1/LightBarrier/{position}/occupied"
                        ],
                        INF_HAS_MQTT_BROKER_IP: [MQTT_BROKER_IP],
                    }
                ]
            },
        )

    ogm.create(
        class_iri=TRANSFER_UNIT_CLASS,
        instance_iri=TRANSFER_UNIT_1,
        class_scope=ClassScope.from_property_chains(
            [[TU_HAS_CONVEYOR_BELT], [TU_HAS_LIGHT_BARRIER]]
        ),
        data={
            TU_HAS_CONVEYOR_BELT: [
                {"id": CONVEYOR_BELT_LEFT},
                {"id": CONVEYOR_BELT_RIGHT},
            ],
            TU_HAS_LIGHT_BARRIER: [
                {"id": LIGHT_BARRIER_FRONT},
                {"id": LIGHT_BARRIER_BACK},
            ],
        },
    )


def seed_scenario2(db) -> None:
    """Full scenario 2 (door) seed: clear, load ontologies, create the door + robot resources."""
    clear_repository(db)
    load_scenario2_ontologies(db)
    create_resource(db, DOOR_RESOURCE, DOOR_RESOURCE_CLASS)
    create_resource(db, MOBILE_ROBOT, MOBILE_ROBOT_RESOURCE_CLASS)


def seed_handover(db, ogm) -> None:
    """Full handover seed.

    Clears the graph, loads the mes: and handover ontologies, and creates
    two transfer modules and a box. It gives the destination the ability
    complementary to Pass (Retrieve). Makes the source currently possess the
    box (Core reified possession, written through the OGM).
    """
    clear_repository(db)
    db.import_statements(_read_mes_ontology(), content_type="application/x-turtle")
    db.import_statements(
        _read_demo_ontology("demo_handover.ttl"), content_type="application/x-turtle"
    )
    create_resource(db, HANDOVER_SOURCE, TRANSFER_MODULE_CLASS)
    create_resource(db, HANDOVER_DEST, TRANSFER_MODULE_CLASS)
    create_resource(db, HANDOVER_BOX, BOX_CLASS)
    # The destination carries the ability complementary to Pass (Retrieve).
    db.triple_add((HANDOVER_DEST, MES.hasHandoverAbility, MES.Retrieve))
    # The source currently possesses the box.
    create_possession(ogm, workpiece_iri=HANDOVER_BOX, possessor_iri=HANDOVER_SOURCE)


# Resource *class* IRIs (the code types the instances above with these classes).
HELLO_RESOURCE_CLASS = IRI(f"{DEMO_NS}DemoResource")
PLANNER_RESOURCE_CLASS = IRI(f"{DEMO_NS}PlannerResource")
