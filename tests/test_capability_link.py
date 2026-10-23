"""Registration writes each Capability together with the link from the resource that provides it.

A Capability does not exist without its resource. So the Capability, its own link to its
Workflow or StateProperty, and the resource's link (`cfc:hasCapability`, Resource -> Capability)
go into one `OGM.commit`: a `Create` of the Capability, or an `Update` of the one an earlier run
left, and one `Append` of the link. No commit holds a Capability without its link. The link is a
multi-valued property of the resource, and the `Append` adds it and removes none: the OGM reads
the resource's class to check the property, and none of the property's stored values; and a
sibling's concurrent link is kept.

The unit tests run a real OGM over a stand-in of the store contract, which answers the reads
from the shipped ontologies in memory and records the writes, or a recording OGM. The
integration tests need a live store and observe the graph.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from _shapes import restore_shipped_shapes, with_shipped_shapes
from kapps_triplestore_interface import IRI, SPARQLQuery, TripleStore, XSDToPythonTypes
from kapps_triplestore_interface.exceptions import PreconditionFailedError, ShaclValidationError
from kapps_triplestore_interface.utils.utils import convert_multi_bindings_to_python_type
from rdflib import RDF, Graph, Literal, URIRef

from kapps_ogm import OGM, Append, Create, Update
from kapps_semantic_middleware.commit import _scope_naming
from kapps_semantic_middleware.credentials import triplestore_for
from kapps_semantic_middleware.registration import (
    _create,
    _register_capability,
    deregister_service,
    mint_capability_iri,
    mint_service_iri,
    mint_state_property_iri,
    mint_workflow_iri,
    register_service,
    register_state_property,
    register_workflow,
)
from kapps_semantic_middleware.vocabulary import CFC, CORE_NS, SVC

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

ADDR_A = "http://127.0.0.1:8973"
ADDR_B = "http://127.0.0.1:8974"

RESOURCE = IRI(f"{seed.DEMO_NS}test_resource")
CAP_A = IRI(f"{seed.DEMO_NS}test_cap_a")
CAP_B = IRI(f"{seed.DEMO_NS}test_cap_b")
CAP_NEW = IRI(f"{seed.DEMO_NS}test_cap_new")
WORKFLOW = IRI(f"{seed.DEMO_NS}test_workflow")
GRAPH = IRI(f"{seed.DEMO_NS}test_graph")

SHARED_ONTOLOGIES = sorted(
    (Path(__file__).resolve().parents[1] / "src" / "kapps_semantic_middleware" / "ontology").glob(
        "*.ttl"
    )
)

# The form in which the triple store interface asks for explicit statements only. The stand-in
# holds explicit statements only, so it reads a query without it.
DATASET_CLAUSE = re.compile(r"^FROM <[^>]*>$", re.MULTILINE)


class _ShapeRefusal(ShaclValidationError):
    """A SHACL refusal built without the interface's own arguments."""

    def __init__(self) -> None:
        Exception.__init__(self, "the store's shapes refused the write")


class _Refused(PreconditionFailedError):
    """The store's refusal of a create whose instance it holds already, built without the
    interface's own arguments."""

    def __init__(self) -> None:
        Exception.__init__(self, "the precondition of the write was false")


def _python_value(term):
    """An object of `triples_get` as the store gives it: a literal as its python value, a
    datatype as its python type."""
    if isinstance(term, URIRef):
        iri = IRI(str(term))
        return XSDToPythonTypes.get(iri, iri)
    return term.toPython() if isinstance(term, Literal) else term


@pytest.fixture
def stand_in() -> Mock:
    """A stand-in of the store contract. It holds the shipped ontologies, a Resource and two
    Capabilities, answers each read from them, and records each write without applying it."""
    graph = Graph()
    for path in SHARED_ONTOLOGIES:
        graph.parse(path, format="turtle")
    graph.add((URIRef(str(RESOURCE)), RDF.type, URIRef(str(CFC.Resource))))
    for capability in (CAP_A, CAP_B):
        graph.add((URIRef(str(capability)), RDF.type, URIRef(str(CFC.Capability))))

    def triples_get(sub=None, pred=None, obj=None, **_):
        pattern = tuple(None if term is None else IRI(term) for term in (sub, pred, obj))
        return [(IRI(str(s)), IRI(str(p)), _python_value(o)) for s, p, o in graph.triples(pattern)]

    def classes_of(instance, **_):
        return {IRI(str(o)) for o in graph.objects(URIRef(str(instance)), RDF.type)}

    def query(query, update=False, convert_bindings=False, **_):
        assert not update, "the stand-in takes no update"
        text = query.to_string() if isinstance(query, SPARQLQuery) else query
        answer = json.loads(graph.query(DATASET_CLAUSE.sub("", text)).serialize(format="json"))
        if convert_bindings and "results" in answer:
            answer["results"]["bindings"] = convert_multi_bindings_to_python_type(
                answer["results"]["bindings"]
            )
        return answer

    store = Mock(spec=TripleStore)
    store.triples_get.side_effect = triples_get
    store.owl_get_classes_of_individual.side_effect = classes_of
    store.query.side_effect = query
    return store


def _updates(store: Mock) -> list:
    """The update requests that the store received, each a call with keyword arguments."""
    return store.triples_update.call_args_list


def _strings(triples) -> set[tuple[str, str, str]]:
    return {(str(s), str(p), str(o)) for s, p, o in triples}


def _writes_other_than_the_update(store: Mock) -> list[str]:
    mutations = ("triple_add", "triples_add", "triple_delete", "triples_delete", "graph_add")
    written = [name for name in mutations if getattr(store, name).called]
    written += ["query(update)" for c in store.query.call_args_list if c.kwargs.get("update")]
    return written


def _capability_of_workflow(ogm: OGM, capability: IRI, named_graph=None) -> None:
    _register_capability(
        ogm,
        resource_iri=RESOURCE,
        capability_iri=capability,
        capability_class=CFC.Capability,
        data={str(SVC.realizedByWorkflow): [{"id": str(WORKFLOW)}]},
        named_graph=named_graph,
    )


class TestOneCommitUnit:
    """What the Capability's commit writes, read and sent through a real OGM."""

    def test_a_new_capability_and_its_link_are_one_update_request(self, stand_in):
        _capability_of_workflow(OGM(db=stand_in), CAP_NEW, GRAPH)

        (update,) = _updates(stand_in)
        assert update.kwargs["absent_subjects"] == [CAP_NEW]
        assert update.kwargs["named_graph"] == GRAPH
        assert not update.kwargs["old_triples"]
        # The OGM writes the type with its prefixed name, which the store expands.
        assert {
            (str(CAP_NEW), "rdf:type", str(CFC.Capability)),
            (str(CAP_NEW), str(SVC.realizedByWorkflow), str(WORKFLOW)),
            (str(RESOURCE), str(CFC.hasCapability), str(CAP_NEW)),
        } <= _strings(update.kwargs["new_triples"])
        assert _writes_other_than_the_update(stand_in) == []

    def test_a_capability_an_earlier_run_left_is_adopted_in_the_commit_of_its_link(
        self, stand_in
    ):
        """The store refuses the create, because it holds the Capability. The adoption that
        follows is one request again: the Capability's data and the link."""

        def update(**kwargs):
            if CAP_A in kwargs["absent_subjects"]:
                raise _Refused()

        stand_in.triples_update.side_effect = update

        _capability_of_workflow(OGM(db=stand_in), CAP_A)

        refused, adopted = _updates(stand_in)
        assert refused.kwargs["absent_subjects"] == [CAP_A]
        assert not adopted.kwargs["absent_subjects"]
        assert {
            (str(CAP_A), str(SVC.realizedByWorkflow), str(WORKFLOW)),
            (str(RESOURCE), str(CFC.hasCapability), str(CAP_A)),
        } <= _strings(adopted.kwargs["new_triples"])
        assert _writes_other_than_the_update(stand_in) == []

    def test_no_stored_value_of_the_resources_property_is_read(self, stand_in):
        """The OGM reads the resource's class to check the property, and nothing the resource
        holds: "has capability" has no maximum, so there is no count to read."""
        _capability_of_workflow(OGM(db=stand_in), CAP_NEW)

        subjects = {str(c.kwargs.get("sub")) for c in stand_in.triples_get.call_args_list}
        assert str(RESOURCE) not in subjects
        queries = [str(c) for c in stand_in.query.call_args_list]
        assert not [text for text in queries if str(RESOURCE) in text]

    def test_a_refusal_by_the_store_propagates_unchanged_and_is_not_retried(self, stand_in):
        refusal = _ShapeRefusal()
        stand_in.triples_update.side_effect = refusal

        with pytest.raises(ShaclValidationError) as raised:
            _capability_of_workflow(OGM(db=stand_in), CAP_NEW)

        assert raised.value is refusal
        stand_in.triples_update.assert_called_once()
        assert _writes_other_than_the_update(stand_in) == []


def _register(kind: str, ogm) -> tuple[IRI, IRI, dict]:
    """Register the door's Workflow ``open`` or its StateProperty ``status`` through ``ogm``.
    Return the Capability's IRI, its class and the data it owns."""
    service_iri = mint_service_iri(seed.DOOR_RESOURCE, ADDR_A)
    if kind == "workflow":
        workflow_iri = mint_workflow_iri(service_iri, "open")
        capability_iri = mint_capability_iri(seed.DOOR_RESOURCE, "open")
        register_workflow(
            ogm,
            resource_iri=seed.DOOR_RESOURCE,
            service_iri=service_iri,
            workflow_iri=workflow_iri,
            workflow_class=seed.DOOR_OPEN_WORKFLOW_CLASS,
            capability_iri=capability_iri,
            capability_class=seed.DOOR_OPEN_CAPABILITY_CLASS,
            endpoint=f"{ADDR_A}/workflows/open/execute",
        )
        data = {str(SVC.realizedByWorkflow): [{"id": str(workflow_iri)}]}
        return capability_iri, seed.DOOR_OPEN_CAPABILITY_CLASS, data
    state_property_iri = mint_state_property_iri(service_iri, "status")
    capability_iri = mint_capability_iri(seed.DOOR_RESOURCE, "status")
    register_state_property(
        ogm,
        resource_iri=seed.DOOR_RESOURCE,
        service_iri=service_iri,
        state_property_iri=state_property_iri,
        state_property_class=seed.DOOR_STATUS_STATE_CLASS,
        capability_iri=capability_iri,
        capability_class=seed.DOOR_STATUS_CAPABILITY_CLASS,
        endpoint=f"{ADDR_A}/state/status",
    )
    data = {str(SVC.providedByStateProperty): [{"id": str(state_property_iri)}]}
    return capability_iri, seed.DOOR_STATUS_CAPABILITY_CLASS, data


def _writes_about(ogm: Mock, capability: IRI) -> list:
    """The calls of ``ogm`` that write about ``capability`` or link to it, in their order."""

    def names(c) -> bool:
        if c.kwargs.get("instance_iri") == capability:
            return True
        return any(
            change.instance_iri == capability
            or (isinstance(change, Append) and change.value == capability)
            for change in c.kwargs.get("changes") or []
        )

    return [c for c in ogm.method_calls if names(c)]


def _create_and_link(capability: IRI, capability_class: IRI, data: dict):
    return call.commit(
        changes=[
            Create(
                class_iri=capability_class,
                data=data,
                instance_iri=capability,
                class_scope=_scope_naming(None, data),
            ),
            Append(
                instance_iri=seed.DOOR_RESOURCE, property_iri=CFC.hasCapability, value=capability
            ),
        ],
        named_graph=None,
    )


class TestRegistrationOneCommitUnit:
    """At no commit of a registration, a first one or a restart, does the Capability go without
    the link from its resource."""

    @pytest.mark.parametrize("kind", ["workflow", "state property"])
    def test_a_first_registration_writes_the_capability_and_its_link_in_one_commit(self, kind):
        ogm = Mock()

        capability, capability_class, data = _register(kind, ogm)

        assert _writes_about(ogm, capability) == [
            _create_and_link(capability, capability_class, data)
        ]

    @pytest.mark.parametrize("kind", ["workflow", "state property"])
    def test_a_restart_adopts_the_capability_in_the_commit_of_its_link(self, kind):
        """The store holds every individual of the earlier run, so it refuses each create."""
        ogm = Mock()
        ogm.create.side_effect = _Refused()

        def commit(**kwargs):
            if any(isinstance(change, Create) for change in kwargs.get("changes") or []):
                raise _Refused()

        ogm.commit.side_effect = commit

        capability, capability_class, data = _register(kind, ogm)

        assert _writes_about(ogm, capability) == [
            _create_and_link(capability, capability_class, data),
            call.commit(
                changes=[
                    Update(instance_iri=capability, data=data),
                    Append(
                        instance_iri=seed.DOOR_RESOURCE,
                        property_iri=CFC.hasCapability,
                        value=capability,
                    ),
                ],
                named_graph=None,
            ),
        ]


def _capabilities(store, resource: IRI) -> set[str]:
    """The explicit ``cfc:hasCapability`` links of ``resource``, as IRI strings."""
    return {
        str(t[2])
        for t in store.triples_get(sub=resource, pred=CFC.hasCapability, include_implicit=False)
    }


def _register_open(ogm: OGM, address: str) -> IRI:
    """Register the door's Service at ``address`` with the Workflow ``open``. Return the
    Capability's IRI."""
    service_iri = mint_service_iri(seed.DOOR_RESOURCE, address)
    capability_iri = mint_capability_iri(seed.DOOR_RESOURCE, "open")
    register_service(
        ogm,
        resource_iri=seed.DOOR_RESOURCE,
        service_iri=service_iri,
        service_class=seed.DOOR_SERVICE_CLASS,
        address=address,
    )
    register_workflow(
        ogm,
        resource_iri=seed.DOOR_RESOURCE,
        service_iri=service_iri,
        workflow_iri=mint_workflow_iri(service_iri, "open"),
        workflow_class=seed.DOOR_OPEN_WORKFLOW_CLASS,
        capability_iri=capability_iri,
        capability_class=seed.DOOR_OPEN_CAPABILITY_CLASS,
        endpoint=f"{address}/workflows/open/execute",
    )
    return capability_iri


def _register_status(ogm: OGM, address: str) -> IRI:
    """Register the door's Service at ``address`` with the StateProperty ``status``. Return the
    Capability's IRI."""
    service_iri = mint_service_iri(seed.DOOR_RESOURCE, address)
    capability_iri = mint_capability_iri(seed.DOOR_RESOURCE, "status")
    register_service(
        ogm,
        resource_iri=seed.DOOR_RESOURCE,
        service_iri=service_iri,
        service_class=seed.DOOR_SERVICE_CLASS,
        address=address,
    )
    register_state_property(
        ogm,
        resource_iri=seed.DOOR_RESOURCE,
        service_iri=service_iri,
        state_property_iri=mint_state_property_iri(service_iri, "status"),
        state_property_class=seed.DOOR_STATUS_STATE_CLASS,
        capability_iri=capability_iri,
        capability_class=seed.DOOR_STATUS_CAPABILITY_CLASS,
        endpoint=f"{address}/state/status",
    )
    return capability_iri


class TestCapabilityLinkIntegration:
    """The links the graph holds after registration."""

    def test_two_instances_on_one_resource_leave_both_links_and_a_restart_keeps_them(
        self, store
    ):
        seed.seed_scenario2(store)
        before = _capabilities(store, seed.DOOR_RESOURCE)

        open_cap = _register_open(OGM(db=store), ADDR_A)
        status_cap = _register_status(OGM(db=store), ADDR_B)

        expected = before | {str(open_cap), str(status_cap)}
        assert _capabilities(store, seed.DOOR_RESOURCE) == expected

        # A restart of the first instance registers again and drops nothing.
        _register_open(OGM(db=store), ADDR_A)
        assert _capabilities(store, seed.DOOR_RESOURCE) == expected

    def test_two_instances_registering_at_the_same_time_leave_every_link(self, store):
        """Each instance has its own client of the store and its own OGM, as two processes
        would."""
        seed.seed_scenario2(store)
        before = _capabilities(store, seed.DOOR_RESOURCE)
        barrier = threading.Barrier(2)
        results: dict[str, IRI] = {}
        errors: list[BaseException] = []

        def run(name: str, register, address: str) -> None:
            client = None
            try:
                client = triplestore_for(store.dataset)
                ogm = OGM(db=client)
                barrier.wait(timeout=30)
                results[name] = register(ogm, address)
            except BaseException as error:  # noqa: BLE001 - reported by the main thread
                errors.append(error)
            finally:
                if client is not None:
                    client.close()

        threads = [
            threading.Thread(target=run, args=("open", _register_open, ADDR_A)),
            threading.Thread(target=run, args=("status", _register_status, ADDR_B)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        assert not errors, errors
        assert _capabilities(store, seed.DOOR_RESOURCE) == before | {
            str(results["open"]),
            str(results["status"]),
        }


# Every Capability belongs to exactly one resource: the inverse of "has capability", at least one
# and at most one. A test fixture, the form a deployment's own shapes take.
ONE_RESOURCE_SHAPE = f"""\
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix cfc: <{CORE_NS}> .

<urn:test:capability-belongs-to-one-resource> a sh:NodeShape ;
    sh:targetClass cfc:Capability ;
    sh:property [
        sh:path [ sh:inversePath cfc:hasCapability ] ;
        sh:minCount 1 ;
        sh:maxCount 1
    ] .
"""


class TestOneResourceShapeIntegration:
    """Registration under a shape that refuses a Capability without its resource."""

    def test_a_service_registers_and_restarts_and_the_shape_admits_every_commit(self, store):
        seed.seed_scenario2(store)
        address = ADDR_A
        service_iri = mint_service_iri(seed.DOOR_RESOURCE, address)
        ogm = OGM(db=store)
        store.shapes_set(with_shipped_shapes(ONE_RESOURCE_SHAPE), "text/turtle")
        try:
            # The shape fires: a Capability created on its own is refused.
            with pytest.raises(ShaclValidationError):
                _create(
                    ogm,
                    seed.DOOR_OPEN_CAPABILITY_CLASS,
                    IRI(f"{seed.DEMO_NS}test_cap_alone"),
                    {str(SVC.realizedByWorkflow): [{"id": str(WORKFLOW)}]},
                )

            def register(ogm: OGM) -> set[str]:
                register_service(
                    ogm,
                    resource_iri=seed.DOOR_RESOURCE,
                    service_iri=service_iri,
                    service_class=seed.DOOR_SERVICE_CLASS,
                    address=address,
                )
                capabilities = set()
                for name, workflow_class, capability_class in (
                    ("open", seed.DOOR_OPEN_WORKFLOW_CLASS, seed.DOOR_OPEN_CAPABILITY_CLASS),
                    ("close", seed.DOOR_CLOSE_WORKFLOW_CLASS, seed.DOOR_CLOSE_CAPABILITY_CLASS),
                ):
                    capability = mint_capability_iri(seed.DOOR_RESOURCE, name)
                    register_workflow(
                        ogm,
                        resource_iri=seed.DOOR_RESOURCE,
                        service_iri=service_iri,
                        workflow_iri=mint_workflow_iri(service_iri, name),
                        workflow_class=workflow_class,
                        capability_iri=capability,
                        capability_class=capability_class,
                        endpoint=f"{address}/workflows/{name}/execute",
                    )
                    capabilities.add(str(capability))
                capability = mint_capability_iri(seed.DOOR_RESOURCE, "status")
                register_state_property(
                    ogm,
                    resource_iri=seed.DOOR_RESOURCE,
                    service_iri=service_iri,
                    state_property_iri=mint_state_property_iri(service_iri, "status"),
                    state_property_class=seed.DOOR_STATUS_STATE_CLASS,
                    capability_iri=capability,
                    capability_class=seed.DOOR_STATUS_CAPABILITY_CLASS,
                    endpoint=f"{address}/state/status",
                )
                capabilities.add(str(capability))
                return capabilities

            capabilities = register(ogm)
            assert capabilities <= _capabilities(store, seed.DOOR_RESOURCE)

            # A restart: the Service deregisters, and a new OGM, as a new process has, registers
            # again over the earlier run's individuals, which it adopts.
            deregister_service(ogm, service_iri)
            assert register(OGM(db=store)) == capabilities
            assert capabilities <= _capabilities(store, seed.DOOR_RESOURCE)
        finally:
            restore_shipped_shapes(store)
