"""Provisioning: the middleware writes each declared parameter's connection metadata at first setup.

A cold start finds a resource whose parameter nodes carry no connection metadata. The embedding
code's declaration (a ``Provision``) is then the only source of the interface, and the
middleware writes the metadata onto the instance through one ``OGM.commit`` before it
recognizes and wires the parameters. From then on it works from the
graph alone: a second start finds the metadata and writes nothing.

The unit tests run a real OGM and a real middleware over a stand-in of the store contract. The
stand-in holds the shipped ontologies and the TransferUnit classes in memory, answers each read
from them, and applies each update request after it checks the request's precondition. The
integration test needs a live store and restarts scenario 3 on it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from kapps_triplestore_interface import IRI, SPARQLQuery, TripleStore, XSDToPythonTypes
from kapps_triplestore_interface.exceptions import PreconditionFailedError
from kapps_triplestore_interface.utils.utils import convert_multi_bindings_to_python_type
from rdflib import RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.term import Node as RDFNode

from kapps_ogm import OGM
from kapps_ogm.utils.class_scope import ClassScope
from kapps_ogm.utils.skolem import mint_skolem_iri
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.connectors.mqtt_binding import MQTTBinding
from kapps_semantic_middleware.connectors.provisioning import Provision
from kapps_semantic_middleware.connectors.semantic import SemanticConnectorRegistry
from kapps_semantic_middleware.errors import DeploymentValueError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

SERVICE_CLASS = f"{seed.TU_NS}TransferUnitService"
GRAPH = IRI(f"{seed.DEMO_NS}provisioning_graph")
INF_MQTT = IRI(f"{seed.INF_NS}isInterfaceAccessibleMQTTParameter")
INF_HAS_MQTT_BROKER_PORT = IRI(f"{seed.INF_NS}hasMQTTBrokerPort")

# A second protocol, known to the test only: its metadata on a node is what an exclusive
# MQTT declaration makes stale.
TEST_NS = "http://example.org/provisioning-test#"
OTHER_INTERFACE = IRI(f"{TEST_NS}isInterfaceAccessibleOtherParameter")
OTHER_ENDPOINT = IRI(f"{TEST_NS}hasOtherEndpoint")

ONTOLOGIES = sorted(
    (Path(__file__).resolve().parents[1] / "src" / "kapps_semantic_middleware" / "ontology").glob(
        "*.ttl"
    )
) + [Path(__file__).resolve().parents[1] / "examples" / "transferunit.ttl"]

OTHER_INTERFACE_TTL = f"""
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
@prefix inf:  <{seed.INF_NS}> .

<{OTHER_INTERFACE}> a owl:ObjectProperty ;
    rdfs:subPropertyOf inf:isInterfaceAccessibleParameter ;
    rdfs:range [ a owl:Restriction ; owl:onProperty <{OTHER_ENDPOINT}> ;
                 owl:allValuesFrom xsd:string ] .
<{OTHER_ENDPOINT}> a owl:DatatypeProperty .
"""

# The form in which the triple store interface asks for explicit statements only. The stand-in
# holds explicit statements only, so it reads a query without it.
DATASET_CLAUSE = re.compile(r"^FROM <[^>]*>$", re.MULTILINE)

BELTS = dict(seed.SCENARIO3_BELTS)
BARRIERS = dict(seed.SCENARIO3_BARRIERS)
_belt_metadata = seed.belt_metadata
_barrier_metadata = seed.barrier_metadata


def provisions(exclusive: bool = False) -> list:
    """The embedding code's declaration of every parameter of the TransferUnit."""
    return seed.scenario3_provisions(exclusive=exclusive)


class _Refused(PreconditionFailedError):
    """The store's refusal of a removal it does not hold, built without the interface's own
    arguments."""

    def __init__(self) -> None:
        Exception.__init__(self, "the precondition of the write was false")


def _python_value(term):
    """An object of `triples_get` as the store gives it: a literal as its python value, a
    datatype as its python type."""
    if isinstance(term, URIRef):
        iri = IRI(str(term))
        return XSDToPythonTypes.get(iri, iri)
    return term.toPython() if isinstance(term, Literal) else term


# The prefixed names in which the OGM writes a create's structural types.
PREFIXES = {
    "rdf:": str(RDF),
    "rdfs:": str(RDFS),
    "owl:": "http://www.w3.org/2002/07/owl#",
    "xsd:": "http://www.w3.org/2001/XMLSchema#",
}


def _term(value) -> RDFNode:
    if isinstance(value, RDFNode):
        return value
    if isinstance(value, str):
        for prefix, namespace in PREFIXES.items():
            if value.startswith(prefix):
                return URIRef(namespace + value[len(prefix):])
    return Literal(value)


def _stored(graph: Graph, triple) -> list:
    """The stored triples that `triple` names. A literal written from python matches the stored
    literal of the same python value, whatever its datatype."""
    s, p, o = (_term(t) for t in triple)
    if isinstance(o, Literal):
        return [
            (s, p, stored)
            for stored in graph.objects(s, p)
            if isinstance(stored, Literal) and stored.toPython() == o.toPython()
        ]
    return [(s, p, o)] if (s, p, o) in graph else []


class StandIn:
    """A stand-in of the store contract over an rdflib graph that applies each update."""

    def __init__(self, *, marked: bool = True) -> None:
        self.graph = Graph()
        for path in ONTOLOGIES:
            self.graph.parse(path, format="turtle")
        self.graph.parse(data=OTHER_INTERFACE_TTL, format="turtle")
        if not marked:
            # The parameter properties lose their TBox marker: only a declaration gives them
            # an interface. The interface vocabulary and its ranges stay.
            for prop in (seed.TU_HAS_CONVEYOR_SPEED, seed.TU_IS_OCCUPIED):
                self.graph.remove((URIRef(str(prop)), RDFS.subPropertyOf, None))

        self.store = Mock(spec=TripleStore)
        self.store.triples_get.side_effect = self._triples_get
        self.store.owl_get_classes_of_individual.side_effect = self._classes_of
        self.store.query.side_effect = self._query
        self.store.triples_update.side_effect = self._triples_update
        self.store.triples_add.side_effect = self._triples_add

        # The seed's cold start: the TransferUnit, its belts and its barriers, without any
        # parameter node, written through the OGM. The tests count the writes after it.
        seed.create_scenario3_instances(OGM(db=self.store), metadata=False)
        self.store.reset_mock(return_value=False, side_effect=False)

    def _triples_get(self, sub=None, pred=None, obj=None, **_):
        pattern = tuple(None if term is None else IRI(term) for term in (sub, pred, obj))
        return [
            (IRI(str(s)), IRI(str(p)), _python_value(o))
            for s, p, o in self.graph.triples(pattern)
        ]

    def _classes_of(self, instance, **_):
        """The instance's classes without `owl:NamedIndividual`, as the interface leaves out the
        `owl:` and `rdfs:` namespaces."""
        return {
            IRI(str(o))
            for o in self.graph.objects(URIRef(str(instance)), RDF.type)
            if not str(o).startswith((PREFIXES["owl:"], PREFIXES["rdfs:"]))
        }

    def _query(self, query, update=False, convert_bindings=False, **_):
        assert not update, "the stand-in takes no raw update"
        text = query.to_string() if isinstance(query, SPARQLQuery) else query
        answer = json.loads(self.graph.query(DATASET_CLAUSE.sub("", text)).serialize(format="json"))
        if convert_bindings and "results" in answer:
            answer["results"]["bindings"] = convert_multi_bindings_to_python_type(
                answer["results"]["bindings"]
            )
        return answer

    def _triples_update(self, *, old_triples, new_triples, named_graph=None, absent_subjects=()):
        removed = []
        for triple in old_triples:
            stored = _stored(self.graph, triple)
            if not stored:
                raise _Refused()
            removed.extend(stored)
        for subject in absent_subjects:
            assert (URIRef(str(subject)), None, None) not in self.graph
        for triple in removed:
            self.graph.remove(triple)
        for triple in new_triples:
            self.graph.add(tuple(_term(t) for t in triple))

    def _triples_add(self, triples, **_):
        for triple in triples:
            self.graph.add(tuple(_term(t) for t in triple))

    # --- what the tests read ---

    def nodes(self, holder: IRI, prop: IRI) -> list:
        return list(self.graph.objects(URIRef(str(holder)), URIRef(str(prop))))

    def values(self, holder: IRI, prop: IRI) -> dict:
        """The properties of the one parameter node of `holder`, as strings."""
        (node,) = self.nodes(holder, prop)
        values: dict = {}
        for p, o in self.graph.predicate_objects(node):
            if p != RDF.type:
                values.setdefault(str(p), set()).add(str(o))
        return values

    def put_node(
        self, holder: IRI, prop: IRI, content: dict, *, node: URIRef | BNode | None = None
    ) -> URIRef:
        """A parameter node that an earlier run, or the seed, left on `holder`; a named one
        unless `node` gives its address."""
        node = node or URIRef(f"{TEST_NS}node_{IRI(str(holder)).lined}")
        self.graph.add((URIRef(str(holder)), URIRef(str(prop)), node))
        for p, values in content.items():
            for value in values:
                self.graph.add((node, URIRef(str(p)), Literal(value)))
        return node

    def snapshot(self) -> set:
        return set(self.graph)


def _middleware(standin: StandIn, **kwargs) -> SemanticMiddleware:
    kwargs.setdefault("class_scope", _unit_scope())
    return SemanticMiddleware(
        mode="resource",
        resource_iri=str(seed.TRANSFER_UNIT_1),
        service_class=SERVICE_CLASS,
        ogm=OGM(db=standin.store),
        heartbeat_interval=None,
        connector_registry=SemanticConnectorRegistry([MQTTBinding]),
        **kwargs,
    )


def _unit_scope():
    return ClassScope.from_property_chains(
        [
            [seed.TU_HAS_CONVEYOR_BELT, seed.TU_HAS_CONVEYOR_SPEED],
            [seed.TU_HAS_LIGHT_BARRIER, seed.TU_IS_OCCUPIED],
        ]
    )


def _expected(metadata: dict) -> dict:
    return {str(p): {str(v) for v in values} for p, values in metadata.items()}


class TestColdStartUnit:
    """Box 1, box 3 and box 6: a cold start provisions through one OGM commit, and wires."""

    def test_a_cold_start_writes_the_declared_metadata_onto_every_parameter(self):
        standin = StandIn()

        _middleware(standin, provisions=provisions())

        for belt, position in BELTS.items():
            assert standin.values(belt, seed.TU_HAS_CONVEYOR_SPEED) == _expected(
                _belt_metadata(position)
            )
        for barrier, position in BARRIERS.items():
            assert standin.values(barrier, seed.TU_IS_OCCUPIED) == _expected(
                _barrier_metadata(position)
            )

    def test_the_writes_are_one_update_request_and_no_raw_write(self):
        standin = StandIn()

        _middleware(standin, provisions=provisions())

        assert standin.store.triples_update.call_count == 1
        mutations = ("triple_add", "triples_add", "triple_delete", "triples_delete", "graph_add")
        assert [name for name in mutations if getattr(standin.store, name).called] == []

    def test_the_cold_start_wires_every_parameter(self):
        standin = StandIn()

        middleware = _middleware(standin, provisions=provisions())

        assert middleware._wiring is not None
        assert {str(b.resource_iri) for b in middleware._wiring.bindings} == {
            str(iri) for iri in (*BELTS, *BARRIERS)
        }
        assert middleware._wiring.registrations

    def test_the_write_goes_to_the_middlewares_named_graph(self):
        standin = StandIn()

        _middleware(standin, provisions=provisions(), named_graph=str(GRAPH))

        assert standin.store.triples_update.call_args.kwargs["named_graph"] == GRAPH

    def test_without_a_declaration_a_cold_start_writes_nothing_and_wires_nothing(self):
        standin = StandIn()

        middleware = _middleware(standin)

        standin.store.triples_update.assert_not_called()
        assert middleware._wiring is not None and middleware._wiring.bindings == []


class TestRestartUnit:
    """Box 2: a second start changes no triples and creates no second parameter node."""

    def test_a_second_start_writes_nothing(self):
        standin = StandIn()
        _middleware(standin, provisions=provisions())
        before = standin.snapshot()
        standin.store.triples_update.reset_mock()

        middleware = _middleware(standin, provisions=provisions())

        standin.store.triples_update.assert_not_called()
        assert standin.snapshot() == before
        assert len(middleware._wiring.bindings) == 4

    def test_a_node_that_carries_only_domain_content_keeps_its_iri(self):
        """The seed's node before the metadata: provisioning adds to it, never beside it."""
        standin = StandIn()
        node = standin.put_node(
            seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED, {seed.TU_HAS_UNIT: ["m/s"]}
        )

        _middleware(standin, provisions=provisions())

        assert standin.nodes(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED) == [node]
        assert standin.values(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED) == {
            str(seed.TU_HAS_UNIT): {"m/s"},
            **_expected(_belt_metadata("left")),
        }

    def test_an_anonymous_node_that_carries_only_domain_content_keeps_its_iri(self):
        """A node that an earlier OGM write left has a Skolem IRI: provisioning adds to it too."""
        standin = StandIn()
        node = standin.put_node(
            seed.CONVEYOR_BELT_LEFT,
            seed.TU_HAS_CONVEYOR_SPEED,
            {seed.TU_HAS_UNIT: ["m/s"]},
            node=URIRef(str(mint_skolem_iri())),
        )

        _middleware(standin, provisions=provisions())

        assert standin.nodes(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED) == [node]
        assert standin.values(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED) == {
            str(seed.TU_HAS_UNIT): {"m/s"},
            **_expected(_belt_metadata("left")),
        }

    def test_a_blank_parameter_node_keeps_its_place(self):
        """A blank node is anonymous too: provisioning adds to it and adds no second node."""
        standin = StandIn()
        standin.put_node(
            seed.CONVEYOR_BELT_LEFT,
            seed.TU_HAS_CONVEYOR_SPEED,
            {seed.TU_HAS_UNIT: ["m/s"]},
            node=BNode(),
        )

        _middleware(standin, provisions=provisions())

        assert len(standin.nodes(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED)) == 1
        assert standin.values(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED) == {
            str(seed.TU_HAS_UNIT): {"m/s"},
            **_expected(_belt_metadata("left")),
        }

    def test_a_node_that_carries_the_declared_metadata_is_left_as_it_is(self):
        standin = StandIn()
        for belt, position in BELTS.items():
            standin.put_node(belt, seed.TU_HAS_CONVEYOR_SPEED, _belt_metadata(position))
        for barrier, position in BARRIERS.items():
            standin.put_node(barrier, seed.TU_IS_OCCUPIED, _barrier_metadata(position))

        _middleware(standin, provisions=provisions())

        standin.store.triples_update.assert_not_called()

    def test_an_integer_port_matches_its_stored_literal(self):
        standin = StandIn()
        declared = [
            Provision(
                holder_iri=seed.CONVEYOR_BELT_LEFT,
                property_iri=seed.TU_HAS_CONVEYOR_SPEED,
                interface_iri=INF_MQTT,
                metadata={**_belt_metadata("left"), INF_HAS_MQTT_BROKER_PORT: [18831]},
            )
        ]
        _middleware(standin, provisions=declared)
        standin.store.triples_update.reset_mock()

        _middleware(standin, provisions=declared)

        standin.store.triples_update.assert_not_called()


class TestMonitorUnit:
    """Box 4: a monitor instance provisions nothing."""

    def test_a_monitor_writes_nothing(self):
        standin = StandIn()

        middleware = _middleware(standin, provisions=provisions(), autoregister_connectors=False)

        standin.store.triples_update.assert_not_called()
        assert middleware._wiring is not None and middleware._wiring.registrations == []


class TestContradictionUnit:
    """Box 5: a contradiction is loud; an exclusive declaration overrides it."""

    def _stale(self, standin: StandIn) -> URIRef:
        return standin.put_node(
            seed.CONVEYOR_BELT_LEFT,
            seed.TU_HAS_CONVEYOR_SPEED,
            {
                seed.INF_ACCESS_MODE: ["readwrite"],
                seed.INF_HAS_MQTT_TOPIC: ["an/old/topic"],
                OTHER_ENDPOINT: ["opc.tcp://old-endpoint"],
            },
        )

    def test_a_stored_value_that_differs_from_the_declaration_raises(self):
        standin = StandIn()
        self._stale(standin)
        before = standin.snapshot()

        with pytest.raises(DeploymentValueError, match="an/old/topic"):
            _middleware(standin, provisions=provisions())

        standin.store.triples_update.assert_not_called()
        assert standin.snapshot() == before

    def test_a_plain_declaration_merges_with_the_metadata_of_another_interface(self):
        """The node is also reached over another protocol: a dual-homed device. The declared
        metadata is written beside the other interface's, which stays."""
        standin = StandIn()
        node = standin.put_node(
            seed.CONVEYOR_BELT_LEFT,
            seed.TU_HAS_CONVEYOR_SPEED,
            {OTHER_ENDPOINT: ["opc.tcp://other-endpoint"]},
        )

        _middleware(standin, provisions=provisions())

        assert standin.nodes(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED) == [node]
        assert standin.values(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED) == {
            str(OTHER_ENDPOINT): {"opc.tcp://other-endpoint"},
            **_expected(_belt_metadata("left")),
        }

    def test_a_plain_write_keeps_a_comment_on_the_node(self):
        """A declared property is missing, so the node is written; its comment stays."""
        standin = StandIn()
        standin.put_node(
            seed.CONVEYOR_BELT_LEFT,
            seed.TU_HAS_CONVEYOR_SPEED,
            {RDFS.comment: ["the left drive"]},
        )

        _middleware(standin, provisions=provisions())

        assert standin.values(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED) == {
            str(RDFS.comment): {"the left drive"},
            **_expected(_belt_metadata("left")),
        }

    def test_a_node_with_its_metadata_other_metadata_and_a_comment_restarts_quietly(self):
        standin = StandIn()
        for belt, position in BELTS.items():
            standin.put_node(
                belt,
                seed.TU_HAS_CONVEYOR_SPEED,
                {
                    **_belt_metadata(position),
                    OTHER_ENDPOINT: ["opc.tcp://other-endpoint"],
                    RDFS.comment: ["the left drive"],
                },
            )
        for barrier, position in BARRIERS.items():
            standin.put_node(barrier, seed.TU_IS_OCCUPIED, _barrier_metadata(position))

        _middleware(standin, provisions=provisions())

        standin.store.triples_update.assert_not_called()

    def test_an_exclusive_declaration_replaces_the_stale_metadata_in_one_commit(self):
        standin = StandIn()
        node = self._stale(standin)

        _middleware(standin, provisions=provisions(exclusive=True))

        assert standin.store.triples_update.call_count == 1
        assert standin.nodes(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED) == [node]
        assert standin.values(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED) == _expected(
            _belt_metadata("left")
        )

    def test_an_exclusive_declaration_removes_another_interface_but_keeps_a_comment(self):
        """The declared values are right, so only the other interface's metadata makes the
        write. A second start finds nothing stale and writes nothing."""
        standin = StandIn()
        standin.put_node(
            seed.CONVEYOR_BELT_LEFT,
            seed.TU_HAS_CONVEYOR_SPEED,
            {
                **_belt_metadata("left"),
                OTHER_ENDPOINT: ["opc.tcp://old-endpoint"],
                RDFS.comment: ["the left drive"],
            },
        )

        _middleware(standin, provisions=provisions(exclusive=True))
        assert standin.store.triples_update.call_count == 1
        standin.store.triples_update.reset_mock()
        _middleware(standin, provisions=provisions(exclusive=True))

        standin.store.triples_update.assert_not_called()
        assert standin.values(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED) == {
            str(RDFS.comment): {"the left drive"},
            **_expected(_belt_metadata("left")),
        }


class TestUnmarkedPropertyUnit:
    """A parameter property without a TBox marker: the declaration alone gives its interface,
    to the write, to recognition and to the northbound projection."""

    def test_a_cold_start_provisions_and_wires_an_unmarked_property(self):
        standin = StandIn(marked=False)

        middleware = _middleware(standin, provisions=provisions())

        assert standin.values(seed.CONVEYOR_BELT_LEFT, seed.TU_HAS_CONVEYOR_SPEED) == _expected(
            _belt_metadata("left")
        )
        assert len(middleware._wiring.bindings) == 4
        assert middleware._wiring.registrations

    def test_the_projection_hides_the_declared_interfaces_metadata(self):
        standin = StandIn(marked=False)

        middleware = _middleware(standin, provisions=provisions())

        southbound = middleware._wiring.southbound_by_property[str(seed.TU_HAS_CONVEYOR_SPEED)]
        assert str(seed.INF_HAS_MQTT_BROKER_IP) in southbound
        assert str(seed.INF_HAS_MQTT_TOPIC) in southbound
        assert str(seed.INF_ACCESS_MODE) not in southbound
        belt = middleware._wiring.northbound_spec.properties[seed.TU_HAS_CONVEYOR_BELT].nested
        speed = belt.properties[seed.TU_HAS_CONVEYOR_SPEED].nested
        assert {str(p) for p in speed.properties}.isdisjoint(
            {str(seed.INF_HAS_MQTT_BROKER_IP), str(seed.INF_HAS_MQTT_TOPIC)}
        )

    def test_a_monitor_still_hides_the_declared_interfaces_metadata(self):
        standin = StandIn(marked=False)
        _middleware(standin, provisions=provisions())

        monitor = _middleware(standin, provisions=provisions(), autoregister_connectors=False)

        assert len(monitor._wiring.bindings) == 4
        southbound = monitor._wiring.southbound_by_property[str(seed.TU_HAS_CONVEYOR_SPEED)]
        assert str(seed.INF_HAS_MQTT_BROKER_IP) in southbound


class TestDeclarationWithoutAViewUnit:
    def test_provisions_without_a_class_scope_refuse_construction(self):
        standin = StandIn()

        with pytest.raises(DeploymentValueError, match="class_scope"):
            _middleware(standin, provisions=provisions(), class_scope=None)


def _belt_provision(**kwargs) -> Provision:
    kwargs.setdefault("holder_iri", seed.CONVEYOR_BELT_LEFT)
    kwargs.setdefault("metadata", _belt_metadata("left"))
    return Provision(
        property_iri=seed.TU_HAS_CONVEYOR_SPEED, interface_iri=INF_MQTT, **kwargs
    )


class TestRefusedDeclarationUnit:
    """A declaration that the middleware cannot honour fails construction, and nothing is
    written."""

    def test_a_bare_string_value_is_refused_and_names_its_property(self):
        with pytest.raises(DeploymentValueError, match="accessMode.*'readwrite'"):
            _belt_provision(metadata={seed.INF_ACCESS_MODE: "readwrite"})

    def test_a_single_value_that_is_no_list_is_refused(self):
        with pytest.raises(DeploymentValueError, match="hasMQTTBrokerPort"):
            _belt_provision(metadata={INF_HAS_MQTT_BROKER_PORT: 18831})

    def test_an_empty_value_list_is_refused(self):
        with pytest.raises(DeploymentValueError, match="hasMQTTTopic no value"):
            _belt_provision(metadata={seed.INF_HAS_MQTT_TOPIC: []})

    def test_a_parameter_declared_twice_is_refused(self):
        standin = StandIn()
        before = standin.snapshot()

        with pytest.raises(DeploymentValueError, match="more than once"):
            _middleware(standin, provisions=[*provisions(), _belt_provision()])

        standin.store.triples_update.assert_not_called()
        assert standin.snapshot() == before

    def test_a_parameter_outside_the_class_scope_is_refused(self):
        """The scope reaches the belts only: the barriers would be written, but never wired."""
        standin = StandIn()
        before = standin.snapshot()
        belts_only = ClassScope.from_property_chains(
            [[seed.TU_HAS_CONVEYOR_BELT, seed.TU_HAS_CONVEYOR_SPEED]]
        )

        with pytest.raises(DeploymentValueError, match="isOccupied"):
            _middleware(standin, provisions=provisions(), class_scope=belts_only)

        standin.store.triples_update.assert_not_called()
        assert standin.snapshot() == before

    def test_a_holder_that_the_resource_does_not_link_is_refused(self):
        """A belt of another unit: its class has the parameter, but this resource's scope
        never reaches it."""
        standin = StandIn()
        stray = IRI(f"{TEST_NS}a_belt_of_another_unit")
        standin.graph.add((URIRef(stray), RDF.type, URIRef(str(seed.CONVEYOR_BELT_CLASS))))

        with pytest.raises(DeploymentValueError, match="a_belt_of_another_unit"):
            _middleware(standin, provisions=[_belt_provision(holder_iri=stray)])

        standin.store.triples_update.assert_not_called()

    def test_a_monitor_refuses_a_parameter_outside_the_class_scope(self):
        standin = StandIn()
        stray = IRI(f"{TEST_NS}a_belt_of_another_unit")

        with pytest.raises(DeploymentValueError, match="never wired"):
            _middleware(
                standin,
                provisions=[_belt_provision(holder_iri=stray)],
                autoregister_connectors=False,
            )

    def test_an_ogm_without_interface_is_refused_with_the_documented_error(self, monkeypatch):
        """A kapps-ogm before 0.4.0 has no ``Interface``: the error says what to install."""
        import kapps_ogm

        monkeypatch.delattr(kapps_ogm, "Interface", raising=False)
        standin = StandIn()

        with pytest.raises(DeploymentValueError, match="kapps_ogm.Interface"):
            _middleware(standin, provisions=provisions())

        standin.store.triples_update.assert_not_called()


def test_the_package_imports_without_the_ogms_interface():
    """A kapps-ogm before 0.4.0 has no ``Interface``. The package imports on it, and a middleware
    without provisions declares no interfaces, so it never asks the OGM for one."""
    import subprocess

    script = (
        "import kapps_ogm\n"
        "kapps_ogm.__dict__.pop('Interface', None)\n"
        "import kapps_semantic_middleware\n"
        "from kapps_semantic_middleware.connectors.provisioning import declared_interfaces\n"
        "assert declared_interfaces(()) == ()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
