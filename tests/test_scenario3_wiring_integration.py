"""Scenario 3 wiring: graph -> recognition -> connectors -> served payload.

Live GraphDB, plus a live MQTT broker that runs in-process. This is the ticket real acceptance
surface. The middleware reads the seeded TransferUnit out of the graph. Recognize
which properties are interface-accessible parameters. Build the right number of
connectors in the right directions. The regression matters. Serve a payload that
carries no connection metadata, regardless of the connector wiring.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection
from kapps_triplestore_interface import IRI
from kapps_ogm import OGM

from kapps_semantic_middleware.connectors.mqtt_binding import MQTTBinding
from kapps_semantic_middleware.connectors.rest_binding import RESTBinding, build_parameter_path
from kapps_semantic_middleware.connectors.semantic import SemanticConnectorRegistry
from kapps_semantic_middleware.connectors.wiring import plan_wiring
from kapps_semantic_middleware.projection import (
    carries_southbound,
    southbound_properties,
)
from kapps_semantic_middleware.vocabulary import INF, SVC, AccessMode

from conftest import requires_graphdb  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402


@pytest.fixture
def scenario3(graphdb):
    """A seeded scenario 3 and an OGM over it.

    Function-scoped. Load-bearing. Tempt the ~80 round trips a seed costs. Several
    tests here add interface metadata with a raw SPARQL INSERT (`hasMQTTValuePath`,
    an OPC-UA endpoint). Prove a declared term survives the read. Share one seed
    across the module. Writes leak forward. An envelope path inserted by one test
    puts a later test formatter into envelope mode. It reads a raw scalar as an
    unobserved payload.
    """
    seed.seed_scenario3(graphdb, OGM(db=graphdb))
    # A *fresh* OGM. Deliberately not the one that seeded. The seeding client carries
    # state from its own writes. A test plans a wiring. Read the graph the way a
    # cold middleware reads it.
    return graphdb, OGM(db=graphdb)


def _plan(ogm, unit_scope, **kwargs):
    kwargs.setdefault("registry", SemanticConnectorRegistry([MQTTBinding]))
    return plan_wiring(
        ogm=ogm,
        resource_iri=seed.TRANSFER_UNIT_1,
        class_scope=unit_scope,
        **kwargs,
    )


SERVICE_ADDRESS = "http://10.0.0.5:8010"


def _publish_service(graphdb, resource_iri, address=SERVICE_ADDRESS):
    """Give ``resource_iri`` a live Service, the way ``_register_service`` would at runtime.

    Plain ``INSERT DATA``, not the OGM write path -- the same pattern
    ``test_an_envelope_path_reaches_the_binding_from_the_graph`` already relies on to prove a
    raw insert lands in the explicit graph that ``wiring.py``'s ``EXPLICIT_GRAPH``-scoped
    queries read from.
    """
    service_iri = f"{resource_iri}Service"
    graphdb.query(
        f"""
        INSERT DATA {{
          <{service_iri}> a <{SVC.Service}> ;
              <{SVC.isServiceOf}> <{resource_iri}> ;
              <{SVC.address}> "{address}" .
        }}
        """,
        update=True,
    )
    return address


@requires_graphdb
class TestRecognition:
    """Four parameters. Recognised from the graph by their interface property."""

    def test_recognises_all_four_parameters(self, scenario3, unit_scope):
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope)

        assert len(plan.bindings) == 4
        assert {str(b.resource_iri) for b in plan.bindings} == {
            str(seed.CONVEYOR_BELT_LEFT),
            str(seed.CONVEYOR_BELT_RIGHT),
            str(seed.LIGHT_BARRIER_FRONT),
            str(seed.LIGHT_BARRIER_BACK),
        }

    def test_reads_each_parameter_access_mode_from_the_graph(self, scenario3, unit_scope):
        """Belts are settable control variables. Barriers are read-only sensors."""
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope)
        modes = {str(b.resource_iri): b.access_mode for b in plan.bindings}

        assert modes[str(seed.CONVEYOR_BELT_LEFT)] == AccessMode.READWRITE
        assert modes[str(seed.LIGHT_BARRIER_FRONT)] == AccessMode.READ

    def test_reads_the_connection_metadata_off_the_parameter_node(
        self, scenario3, unit_scope
    ):
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope)
        left = next(
            b for b in plan.bindings if str(b.resource_iri) == str(seed.CONVEYOR_BELT_LEFT)
        )

        assert left.get(INF.hasMQTTTopic) == "TransferUnit1/ConveyorBelt/left/speed"
        assert left.get(INF.hasMQTTSetTopic) == "TransferUnit1/ConveyorBelt/left/speed_set"
        assert left.get(INF.hasMQTTBrokerIP) == seed.MQTT_BROKER_IP

    def test_an_envelope_path_reaches_the_binding_from_the_graph(
        self, scenario3, unit_scope
    ):
        """inf:hasMQTTValuePath must be *declared* to survive the write and the read.

        `_parameter_metadata` keeps only properties the effective shape declares. A term
        missing from the range restriction is filtered out. Envelope mode could never
        activate from real data. The formatter is correct and unreachable. Pin the
        declaration. Not the formatter logic.
        """
        graphdb, ogm = scenario3
        graphdb.query(
            f'INSERT {{ ?n <{INF.hasMQTTValuePath}> "payload.speed" }} '
            f"WHERE {{ <{seed.CONVEYOR_BELT_LEFT}> <{seed.TU_HAS_CONVEYOR_SPEED}> ?n }}",
            update=True,
        )

        plan = _plan(ogm, unit_scope)
        left = next(
            b for b in plan.bindings if str(b.resource_iri) == str(seed.CONVEYOR_BELT_LEFT)
        )

        assert left.get(INF.hasMQTTValuePath) == "payload.speed"

    def test_a_declared_port_round_trips_as_an_integer_not_a_string(
        self, scenario3, unit_scope
    ):
        """The broker port is the first non-string literal any seed here writes.

        write 18831 -> graph -> ClassSpec -> ``binding.get()`` must hand back the integer
        18831, not the string "18831" -- `_parameter_metadata` used to force every value
        through `str()`, which was a no-op for every property so far because they were all
        `xsd:string`. A connector handed a string port fails at the socket layer, not at
        construction, so this is load-bearing rather than a type-purity nicety.
        """
        graphdb, ogm = scenario3
        graphdb.query(
            f'INSERT {{ ?n <{INF.hasMQTTBrokerPort}> '
            f'"18831"^^<http://www.w3.org/2001/XMLSchema#integer> }} '
            f"WHERE {{ <{seed.CONVEYOR_BELT_LEFT}> <{seed.TU_HAS_CONVEYOR_SPEED}> ?n }}",
            update=True,
        )

        plan = _plan(ogm, unit_scope)
        left = next(
            b for b in plan.bindings if str(b.resource_iri) == str(seed.CONVEYOR_BELT_LEFT)
        )

        port = left.get(INF.hasMQTTBrokerPort)
        assert port == 18831
        assert isinstance(port, int)

    def test_an_undeclared_port_defaults_to_1883_on_the_connector(
        self, scenario3, unit_scope
    ):
        """No scenario-3 ABox declares a port -- every existing seed keeps its meaning."""
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope)
        speed = next(
            r for _, r in plan.registrations if r.connector.topic.endswith("left/speed")
        )

        assert speed.connector.mqtt_broker_port == 1883

    def test_the_value_is_parsed_per_the_ontology_datatype(self, scenario3, unit_scope):
        """"Raw scalar. Parsed per the parameter ontology datatype."

        The parsing is not done by hand. Not what ``Registration.model_type`` is for.
        That is the persistence type of the bound field. It is a list. It falls out
        of the node model generated from the effective shape. `tu:hasConveyorSpeed`
        restricts `inf:hasValue` to `xsd:float`. `tu:isOccupied` to `xsd:boolean`.
        Pydantic coerces on construction. A device publishes a quoted number. It
        lands as a number.
        """
        _, ogm = scenario3
        plan = _plan(ogm, unit_scope)

        speed = next(
            r for _, r in plan.registrations if r.connector.topic.endswith("left/speed")
        )
        occupied = next(
            r for _, r in plan.registrations if "occupied" in r.connector.topic
        )

        [speed_node] = speed.formatter.deserialize("12.5")
        [occupied_node] = occupied.formatter.deserialize("true")

        assert getattr(speed_node, INF.hasValue.lined) == [12.5]
        assert getattr(occupied_node, INF.hasValue.lined) == [True]

    def test_an_envelope_path_is_still_southbound(self, scenario3, unit_scope):
        """Declare it. Must not let it reach a peer."""
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope)

        assert str(INF.hasMQTTValuePath) in plan.southbound_properties

    def test_binds_to_the_complex_property_not_hasvalue(self, scenario3, unit_scope):
        """ConnectionInfo has three levels. field_id is a plain getattr. The parameter
        node is the deepest addressable thing."""
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope)

        assert {b.field_id for b in plan.bindings} == {
            seed.TU_HAS_CONVEYOR_SPEED.lined,
            seed.TU_IS_OCCUPIED.lined,
        }
        assert INF.hasValue.lined not in {b.field_id for b in plan.bindings}


@requires_graphdb
class TestRegistrationCount:
    """4 parameters -> 4 bindings -> 6 connectors -> 6 topics."""

    def test_a_controller_builds_six_connectors(self, scenario3, unit_scope):
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope, flavour=SyncDirection.BIDIRECTIONAL)

        assert len(plan.registrations) == 6
        topics = {r.connector.topic for _, r in plan.registrations}
        assert topics == {
            "TransferUnit1/ConveyorBelt/left/speed",
            "TransferUnit1/ConveyorBelt/left/speed_set",
            "TransferUnit1/ConveyorBelt/right/speed",
            "TransferUnit1/ConveyorBelt/right/speed_set",
            "TransferUnit1/LightBarrier/front/occupied",
            "TransferUnit1/LightBarrier/back/occupied",
        }

    def test_a_monitor_builds_four_and_can_drive_nothing(self, scenario3, unit_scope):
        """TO_PERSISTENCE: live values. Structurally unable to write."""
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope, flavour=SyncDirection.TO_PERSISTENCE)

        assert len(plan.registrations) == 4
        assert all(
            r.sync_direction is SyncDirection.TO_PERSISTENCE for _, r in plan.registrations
        )
        assert not any("_set" in r.connector.topic for _, r in plan.registrations)

    def test_an_inspector_builds_none(self, scenario3, unit_scope):
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope, autoregister=False)

        assert plan.registrations == []

    def test_an_inspector_still_recognises_every_parameter(self, scenario3, unit_scope):
        """The flag gates wiring. Never recognition. Skipping recognition makes every
        parameter node ordinary data. An inspector serves broker addresses northbound."""
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope, autoregister=False)

        assert len(plan.bindings) == 4


@requires_graphdb
class TestNorthboundProjection:
    """The regression matters. No flavour ever serves connection metadata."""

    def _served(self, ogm, plan):
        node = ogm.fetch(
            instance_iri=seed.TRANSFER_UNIT_1, **plan.northbound_fetch_kwargs()
        )
        return node.instance.model_dump()

    def test_a_protocol_with_no_registered_binding_is_still_hidden(
        self, scenario3, unit_scope
    ):
        """The regression the ontology-derived projection prevents.

        A belt made reachable over MQTT *and* OPC-UA. No OPC-UA binding registered
        anywhere. The earlier registry-derived prune set removed the MQTT metadata.
        Served `inf:hasOPCUAEndpoint` with its address. A set built from registered
        descriptors knows only the protocols this middleware code has.

        Two protocols on one parameter is not hypothetical: own-built hardware. Protocol is not known when the ontology is authored.
        """
        graphdb, ogm = scenario3
        opcua_marker = IRI(f"{seed.INF_NS}isInterfaceAccessibleOPCUAParameter")
        opcua_endpoint = IRI(f"{seed.INF_NS}hasOPCUAEndpoint")
        owl, rdfs, xsd = (
            "http://www.w3.org/2002/07/owl#",
            "http://www.w3.org/2000/01/rdf-schema#",
            "http://www.w3.org/2001/XMLSchema#",
        )

        graphdb.query(
            f"""
            INSERT DATA {{
              <{opcua_marker}> a <{owl}ObjectProperty> ;
                  <{rdfs}subPropertyOf> <{seed.INF_NS}isInterfaceAccessibleParameter> ;
                  <{rdfs}range> [ a <{owl}Class> ; <{owl}intersectionOf> (
                      [ a <{owl}Restriction> ; <{owl}onProperty> <{opcua_endpoint}> ;
                        <{owl}allValuesFrom> <{xsd}string> ] ) ] .
              <{opcua_endpoint}> a <{owl}DatatypeProperty> .
              <{seed.TU_HAS_CONVEYOR_SPEED}> <{rdfs}subPropertyOf> <{opcua_marker}> .
            }}
            """,
            update=True,
        )
        graphdb.query(
            f'INSERT {{ ?n <{opcua_endpoint}> "opc.tcp://10.0.0.5:4840/belt" }} '
            f"WHERE {{ <{seed.CONVEYOR_BELT_LEFT}> <{seed.TU_HAS_CONVEYOR_SPEED}> ?n }}",
            update=True,
        )

        plan = _plan(ogm, unit_scope)
        served = str(self._served(ogm, plan))

        assert "opc.tcp://10.0.0.5:4840/belt" not in served
        assert opcua_endpoint.lined not in served
        # And the northbound content still survives. This is not passing by serving nothing.
        assert INF.accessMode.lined in served
        assert seed.TU_HAS_UNIT.lined in served

    def test_the_unpruned_spec_would_have_leaked(self, scenario3, unit_scope):
        """Guards the premise. Stop the leak. The OGM gained a merge-depth knob. The
        projection's prune can retire."""
        _, ogm = scenario3
        full = ogm.get_class_spec(
            class_iri=seed.TRANSFER_UNIT_CLASS, class_scope=unit_scope
        )

        leaked = ogm.fetch(
            instance_iri=seed.TRANSFER_UNIT_1,
            class_spec=full,
            class_scope=unit_scope,
            materialize=True,
        )

        assert carries_southbound(
            leaked.instance.model_dump(),
            southbound_properties(ogm, seed.TU_HAS_CONVEYOR_SPEED),
        )

    @pytest.mark.parametrize(
        "flavour, autoregister",
        [
            (SyncDirection.BIDIRECTIONAL, True),  # controller
            (SyncDirection.TO_PERSISTENCE, True),  # monitor
            (SyncDirection.BIDIRECTIONAL, False),  # inspector
        ],
    )
    def test_no_flavour_serves_connection_metadata(
        self, scenario3, unit_scope, flavour, autoregister
    ):
        _, ogm = scenario3
        plan = _plan(ogm, unit_scope, flavour=flavour, autoregister=autoregister)

        served = self._served(ogm, plan)

        assert carries_southbound(served, plan.southbound_properties) == set()
        assert "127.0.0.1" not in str(served)

    def test_all_three_flavours_serve_identical_payloads(self, scenario3, unit_scope):
        """The three wirings serve byte-identical northbound payloads."""
        _, ogm = scenario3

        controller = self._served(ogm, _plan(ogm, unit_scope))
        monitor = self._served(
            ogm, _plan(ogm, unit_scope, flavour=SyncDirection.TO_PERSISTENCE)
        )
        inspector = self._served(ogm, _plan(ogm, unit_scope, autoregister=False))

        assert controller == monitor == inspector

    def test_the_projection_keeps_northbound_content(self, scenario3, unit_scope):
        """An empty projection passes the leak test. Be useless."""
        _, ogm = scenario3
        plan = _plan(ogm, unit_scope)

        served = str(self._served(ogm, plan))

        assert INF.accessMode.lined in served
        assert seed.TU_HAS_UNIT.lined in served
        assert INF.hasValue.lined in served

    def test_a_declared_broker_port_never_reaches_the_northbound_payload(
        self, scenario3, unit_scope
    ):
        """The port is a restriction on the MQTT protocol marker's range, so the
        projection prunes it with the rest of the connection metadata -- no projection code
        change needed, just this proof that the claim holds now that the term exists."""
        graphdb, ogm = scenario3
        graphdb.query(
            f'INSERT {{ ?n <{INF.hasMQTTBrokerPort}> '
            f'"18831"^^<http://www.w3.org/2001/XMLSchema#integer> }} '
            f"WHERE {{ <{seed.CONVEYOR_BELT_LEFT}> <{seed.TU_HAS_CONVEYOR_SPEED}> ?n }}",
            update=True,
        )

        plan = _plan(ogm, unit_scope)
        served = str(self._served(ogm, plan))

        assert "18831" not in served
        assert INF.hasMQTTBrokerPort.lined not in served
        assert str(INF.hasMQTTBrokerPort) in plan.southbound_properties


@requires_graphdb
class TestAConsumerLoadsThePrunedShape:
    """Prune-on-load: a consumer fetching a peer's datamodel gets the prune.

    These four tests used to drive ``projection.load_northbound``, a second entry point
    nothing in the product called. It was deleted and the tests moved onto the path the
    controller really takes: ``plan_wiring`` builds the plan, and the fetch runs with
    ``WiringPlan.northbound_fetch_kwargs()`` (``demo/transferunits/controller.py:636``).

    One of the four is gone rather than moved. It asserted that a protocol declared only
    in the graph is pruned with no code change, which is exactly
    ``TestNorthboundProjection.test_a_protocol_with_no_registered_binding_is_still_hidden``
    on this same path -- keeping both would have been the same claim written twice.
    """

    def _loaded(self, ogm, unit_scope):
        """What a consumer holds after loading a peer's datamodel."""
        plan = _plan(ogm, unit_scope, autoregister=False)
        node = ogm.fetch(instance_iri=seed.TRANSFER_UNIT_1, **plan.northbound_fetch_kwargs())
        return plan, node.instance.model_dump()

    def test_a_loaded_transferunit_carries_no_mqtt_property(self, scenario3, unit_scope):
        _, ogm = scenario3

        _, dumped = self._loaded(ogm, unit_scope)

        assert "hasMQTT" not in str(dumped)
        assert not carries_southbound(
            dumped, southbound_properties(ogm, seed.TU_HAS_CONVEYOR_SPEED)
        )
        # Northbound content survives -- not passing by serving nothing.
        assert INF.accessMode.lined in str(dumped)
        assert seed.TU_HAS_UNIT.lined in str(dumped)

    def test_what_was_pruned_is_visible_per_parameter(self, scenario3, unit_scope):
        """The consumer can say what it did not fetch, parameter by parameter.

        This replaces an INFO line per parameter that only the deleted function
        emitted. The breakdown itself outlived it as ``southbound_by_property``, which
        is what the station board renders under each row.
        A union across the whole resource would not do: the point is that a viewer sees
        which properties went from *this* parameter.
        """
        _, ogm = scenario3

        plan, _ = self._loaded(ogm, unit_scope)
        per_parameter = plan.southbound_by_property

        # Both interface-accessible properties of the scenario-3 seed appear.
        assert str(seed.TU_HAS_CONVEYOR_SPEED) in per_parameter
        assert len([props for props in per_parameter.values() if props]) >= 2
        assert str(INF.hasMQTTTopic) in per_parameter[str(seed.TU_HAS_CONVEYOR_SPEED)]

    def test_a_consumer_keeps_only_the_pruned_shape(self, scenario3, unit_scope):
        """Unlike the serving path, nothing here ever holds a field to carry a broker address."""
        _, ogm = scenario3

        _, served = self._loaded(ogm, unit_scope)

        belt = served[seed.TU_HAS_CONVEYOR_BELT.lined][0]
        parameter = belt[seed.TU_HAS_CONVEYOR_SPEED.lined][0]

        assert INF.hasMQTTBrokerIP.lined not in parameter


@requires_graphdb
class TestServiceJoinRecognition:
    """The Service-join amendment of 2026-08-03: evidence may sit on the resource's Service.

    ``_recognise`` looks the address up once per resource and folds it into every binding's
    metadata, regardless of which protocol ends up matching. These tests exercise that against
    MQTT bindings -- the metadata plumbing is protocol-agnostic even though only REST reads it.
    """

    def test_a_live_resource_s_address_reaches_every_binding(self, scenario3, unit_scope):
        graphdb, ogm = scenario3
        address = _publish_service(graphdb, seed.TRANSFER_UNIT_1)

        plan = _plan(ogm, unit_scope)

        assert all(b.get(SVC.address) == address for b in plan.bindings)

    def test_a_resource_with_no_service_carries_no_address(self, scenario3, unit_scope):
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope)

        assert all(b.get(SVC.address) is None for b in plan.bindings)

    def test_bindings_carry_the_root_and_the_path_to_their_own_holder(self, scenario3, unit_scope):
        """The left belt's speed is one hop from the unit; the structural path reflects it."""
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope)
        left = next(
            b for b in plan.bindings if str(b.resource_iri) == str(seed.CONVEYOR_BELT_LEFT)
        )

        assert str(left.root_iri) == str(seed.TRANSFER_UNIT_1)
        # Mangled whole (IRI(...).lined), not the bare fragment: this must equal
        # `type(instance).__name__` for the materialized root, which is what
        # `ClassSpec.to_pydantic_model` (kapps_ogm) actually names the class -- the
        # {Model} segment `rest_router.py` mounts routes under.
        assert left.root_class_local_name == IRI(str(seed.TRANSFER_UNIT_CLASS)).lined
        assert left.path_steps == (
            (seed.TU_HAS_CONVEYOR_BELT.lined, str(seed.CONVEYOR_BELT_LEFT)),
        )


@requires_graphdb
class TestRESTRecognition:
    """A live resource, generically interface-accessible, binds a REST connector."""

    def test_mqtt_still_wins_when_both_bindings_are_registered(self, scenario3, unit_scope):
        """The acceptance criterion: MQTT recognition is unchanged. A parameter carrying an
        MQTT marker keeps resolving to MQTTBinding even with RESTBinding in the registry and
        a live Service present -- wiring.py's existing most-specific-match tie-break, exercised
        against a new competitor."""
        graphdb, ogm = scenario3
        _publish_service(graphdb, seed.TRANSFER_UNIT_1)

        plan = _plan(ogm, unit_scope, registry=SemanticConnectorRegistry([MQTTBinding, RESTBinding]))

        assert {b.descriptor for b in plan.bindings} == {MQTTBinding}
        assert len(plan.registrations) == 6  # unchanged from TestRegistrationCount

    def test_rest_binds_when_the_resource_is_live(self, scenario3, unit_scope):
        """With MQTTBinding out of the registry, the same seeded (MQTT-marked) parameters
        still recognise -- through the generic interface root, per the Service-join amendment --
        and build a REST connector addressed at the live Service."""
        graphdb, ogm = scenario3
        address = _publish_service(graphdb, seed.TRANSFER_UNIT_1)

        plan = _plan(ogm, unit_scope, registry=SemanticConnectorRegistry([RESTBinding]))

        assert len(plan.bindings) == 4
        assert {b.descriptor for b in plan.bindings} == {RESTBinding}
        assert len(plan.registrations) == 6  # 2 settable belts (rw) + 2 read-only barriers

        left_speed = next(
            r
            for b, r in plan.registrations
            if str(b.resource_iri) == str(seed.CONVEYOR_BELT_LEFT)
            and r.sync_direction is SyncDirection.TO_PERSISTENCE
        )
        expected_path = build_parameter_path(
            # Mangled whole (IRI(...).lined), not the bare fragment -- must equal
            # `type(instance).__name__` for the materialized root, kapps_ogm's own
            # `ClassSpec.to_pydantic_model` naming.
            IRI(str(seed.TRANSFER_UNIT_CLASS)).lined,
            seed.TRANSFER_UNIT_1,
            [(seed.TU_HAS_CONVEYOR_BELT.lined, str(seed.CONVEYOR_BELT_LEFT))],
            seed.TU_HAS_CONVEYOR_SPEED.lined,
        )
        assert left_speed.connector.base_url == address
        assert left_speed.connector.path == expected_path

    def test_a_resource_with_no_live_service_binds_nothing_and_says_so(
        self, scenario3, unit_scope, caplog
    ):
        """The acceptance criterion this ticket names explicitly: recognition still runs,
        but no connector is built and the reason is logged."""
        _, ogm = scenario3

        with caplog.at_level(logging.WARNING):
            plan = _plan(ogm, unit_scope, registry=SemanticConnectorRegistry([RESTBinding]))

        assert len(plan.bindings) == 4
        assert plan.registrations == []
        assert "no live" in caplog.text

    def test_a_monitor_builds_read_only_rest_connectors(self, scenario3, unit_scope):
        """readwrite + observing binds read-only, the same rule as MQTT."""
        graphdb, ogm = scenario3
        _publish_service(graphdb, seed.TRANSFER_UNIT_1)

        plan = _plan(
            ogm,
            unit_scope,
            registry=SemanticConnectorRegistry([RESTBinding]),
            flavour=SyncDirection.TO_PERSISTENCE,
        )

        assert len(plan.registrations) == 4
        assert all(
            r.sync_direction is SyncDirection.TO_PERSISTENCE for _, r in plan.registrations
        )


@requires_graphdb
class TestEnsureTransport:
    """The deployment's transport hook, threaded through ``plan_wiring``.

    Scenario 3's four parameters (two belts, two barriers) all declare the same broker
    address, so this is exactly the shape the hook's "once per distinct address" rule names:
    the hook must fire once, not once per parameter and not once per connector.
    """

    def test_called_once_when_four_parameters_share_one_address(self, scenario3, unit_scope):
        _, ogm = scenario3
        calls = []

        _plan(ogm, unit_scope, ensure_transport=lambda host, port: calls.append((host, port)))

        assert calls == [(seed.MQTT_BROKER_IP, 1883)]

    def test_two_distinct_addresses_each_get_ensured(self, scenario3, unit_scope):
        graphdb, ogm = scenario3
        graphdb.query(
            f'DELETE {{ ?n <{INF.hasMQTTBrokerIP}> "{seed.MQTT_BROKER_IP}" }} '
            f'INSERT {{ ?n <{INF.hasMQTTBrokerIP}> "10.0.0.9" }} '
            f"WHERE {{ <{seed.CONVEYOR_BELT_LEFT}> <{seed.TU_HAS_CONVEYOR_SPEED}> ?n . "
            f'?n <{INF.hasMQTTBrokerIP}> "{seed.MQTT_BROKER_IP}" }}',
            update=True,
        )
        calls = []

        _plan(ogm, unit_scope, ensure_transport=lambda host, port: calls.append((host, port)))

        assert sorted(calls) == sorted(
            [(seed.MQTT_BROKER_IP, 1883), ("10.0.0.9", 1883)]
        )

    def test_no_hook_given_calls_nothing(self, scenario3, unit_scope):
        """An instance constructed without ensure_transport calls nothing -- there is
        nothing to assert on here beyond the plan succeeding with the default (None)."""
        _, ogm = scenario3

        plan = _plan(ogm, unit_scope)

        assert len(plan.registrations) == 6

    def test_an_inspector_calls_no_hook(self, scenario3, unit_scope):
        """autoregister=False builds no connector, so nothing needs transport ensured."""
        _, ogm = scenario3
        calls = []

        _plan(
            ogm,
            unit_scope,
            autoregister=False,
            ensure_transport=lambda host, port: calls.append((host, port)),
        )

        assert calls == []
