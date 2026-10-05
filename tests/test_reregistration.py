"""A returning instance adopts the registration that an earlier run left in the graph.

Registration IRIs are stable across restarts, and deregistration keeps the structural triples
and `rdf:type`. The OGM refuses to create an instance that the graph already holds, and the
triple store interface reports every write whose precondition fails with
`PreconditionFailedError`. Registration adopts such an instance: it sets the same data through
`OGM.commit`, which writes nothing for an unchanged instance and replaces a link that changed.
An Operation IRI is fresh for each dispatch, so an Operation that already exists stays an error.

The same error reaches the possession switch, which reports it as `PossessionSwitchConflict`.

The unit tests are pure logic. The integration tests need a live GraphDB.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import PreconditionFailedError

from kapps_ogm import OGM
from kapps_semantic_middleware.registration import (
    PossessionSwitchConflict,
    _create_or_adopt,
    create_operation,
    deregister_service,
    mint_capability_iri,
    mint_operation_iri,
    mint_service_iri,
    mint_state_property_iri,
    mint_workflow_iri,
    register_service,
    register_state_property,
    register_workflow,
    switch_possession,
)
from kapps_semantic_middleware.vocabulary import CFC, SVC

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

# No server is started here. The address only decides the Service IRI.
ADDR_A = "http://127.0.0.1:8963"
ADDR_B = "http://127.0.0.1:8964"


class _Refused(PreconditionFailedError):
    """A refusal built without the interface's own arguments, which these tests do not need."""

    def __init__(self) -> None:
        Exception.__init__(self, "the precondition of the write was false")


def _explicit_about(db, *subjects: IRI) -> set:
    """The explicit statements about ``subjects``, as tuples of strings."""
    return {
        tuple(str(term) for term in triple)
        for subject in subjects
        for triple in db.triples_get(sub=subject, include_implicit=False)
    }


def _register_hello(ogm: OGM, address: str) -> tuple[IRI, IRI, IRI]:
    """Register the hello resource's Service and Workflow at ``address``. Return their IRIs and
    the Capability's, which depends on the resource and the workflow name only."""
    service_iri = mint_service_iri(seed.HELLO_RESOURCE, address)
    workflow_iri = mint_workflow_iri(service_iri, "hello_world")
    capability_iri = mint_capability_iri(seed.HELLO_RESOURCE, "hello_world")
    register_service(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=service_iri,
        service_class=seed.HELLO_SERVICE_CLASS,
        address=address,
    )
    register_workflow(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=service_iri,
        workflow_iri=workflow_iri,
        workflow_class=seed.HELLO_WORKFLOW_CLASS,
        capability_iri=capability_iri,
        capability_class=seed.HELLO_CAPABILITY_CLASS,
        endpoint=f"{address}/workflows/hello_world/execute",
    )
    return service_iri, workflow_iri, capability_iri


def _register_door(ogm: OGM, address: str) -> tuple[IRI, ...]:
    """Register the door's Service, one Workflow and one StateProperty. Return every IRI that
    registration writes about, the Service's first."""
    service_iri = mint_service_iri(seed.DOOR_RESOURCE, address)
    workflow_iri = mint_workflow_iri(service_iri, "open")
    open_iri = mint_capability_iri(seed.DOOR_RESOURCE, "open")
    state_property_iri = mint_state_property_iri(service_iri, "status")
    status_iri = mint_capability_iri(seed.DOOR_RESOURCE, "status")
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
        workflow_iri=workflow_iri,
        workflow_class=seed.DOOR_OPEN_WORKFLOW_CLASS,
        capability_iri=open_iri,
        capability_class=seed.DOOR_OPEN_CAPABILITY_CLASS,
        endpoint=f"{address}/workflows/open/execute",
    )
    register_state_property(
        ogm,
        resource_iri=seed.DOOR_RESOURCE,
        service_iri=service_iri,
        state_property_iri=state_property_iri,
        state_property_class=seed.DOOR_STATUS_STATE_CLASS,
        capability_iri=status_iri,
        capability_class=seed.DOOR_STATUS_CAPABILITY_CLASS,
        endpoint=f"{address}/state/status",
    )
    return service_iri, workflow_iri, open_iri, state_property_iri, status_iri


class TestAdoptionUnit:
    """Which writes adopt an existing instance, and which report it."""

    def test_a_refused_create_is_adopted_with_the_same_data(self):
        ogm = Mock()
        ogm.create.side_effect = _Refused()
        data = {str(SVC.isServiceOf): [{"id": str(seed.HELLO_RESOURCE)}]}

        _create_or_adopt(ogm, seed.HELLO_SERVICE_CLASS, IRI(f"{seed.DEMO_NS}test_service"), data)

        ogm.commit.assert_called_once_with(
            instance_iri=IRI(f"{seed.DEMO_NS}test_service"), data=data, named_graph=None
        )

    def test_an_operation_that_already_exists_is_reported_not_adopted(self):
        ogm = Mock()
        ogm.commit.side_effect = _Refused()

        with pytest.raises(PreconditionFailedError):
            create_operation(
                ogm,
                operation_iri=IRI(f"{seed.DEMO_NS}test_op"),
                operation_class=CFC.Operation,
                capability_iri=IRI(f"{seed.DEMO_NS}test_cap"),
            )
        # The refused creation is the only write: nothing adopts the Operation afterwards.
        ogm.commit.assert_called_once()
        ogm.create.assert_not_called()

    def test_a_refused_switch_raises_the_conflict_and_keeps_the_refusal_as_its_cause(self):
        ogm = Mock()
        ogm.db.query.return_value = {"results": {"bindings": []}}
        refusal = _Refused()
        ogm.commit.side_effect = refusal

        with pytest.raises(PossessionSwitchConflict) as raised:
            switch_possession(
                ogm, workpiece_iri=IRI(f"{seed.DEMO_NS}test_box"), new_possessor_iri=IRI(f"{seed.DEMO_NS}test_module")
            )
        assert raised.value.__cause__ is refusal


class TestRestartIntegration:
    """A restart against the graph that holds the earlier run's registration."""

    def test_a_restart_on_the_same_address_restores_the_earlier_registration_exactly(
        self, graphdb
    ):
        seed.seed_scenario2(graphdb)
        ogm = OGM(db=graphdb)
        written = (seed.DOOR_RESOURCE, *_register_door(ogm, ADDR_A))
        registered = _explicit_about(graphdb, *written)

        deregister_service(ogm, written[1])
        _register_door(ogm, ADDR_A)

        # The address and the endpoints come back; nothing else is written.
        assert _explicit_about(graphdb, *written) == registered

    def test_a_restart_on_another_address_repoints_the_capability_and_writes_nothing_else(
        self, graphdb
    ):
        seed.seed_scenario1(graphdb)
        ogm = OGM(db=graphdb)
        service_a, workflow_a, capability = _register_hello(ogm, ADDR_A)
        deregister_service(ogm, service_a)
        service_b = mint_service_iri(seed.HELLO_RESOURCE, ADDR_B)
        workflow_b = mint_workflow_iri(service_b, "hello_world")
        written = (seed.HELLO_RESOURCE, service_a, workflow_a, capability, service_b, workflow_b)
        before = _explicit_about(graphdb, *written)

        _, _, same_capability = _register_hello(ogm, ADDR_B)

        assert same_capability == capability
        realized_by = graphdb.triples_get(
            sub=capability, pred=SVC.realizedByWorkflow, include_implicit=False
        )
        assert [str(t[2]) for t in realized_by] == [str(workflow_b)]
        after = _explicit_about(graphdb, *written)
        assert before - after == {(str(capability), str(SVC.realizedByWorkflow), str(workflow_a))}
        # Everything new describes the new Service, its Workflow, or the repointed link.
        assert {t[0] for t in after - before} == {str(service_b), str(workflow_b), str(capability)}
        assert {t for t in after - before if t[0] == str(capability)} == {
            (str(capability), str(SVC.realizedByWorkflow), str(workflow_b))
        }

    def test_an_operation_iri_that_exists_is_refused(self, graphdb):
        seed.seed_scenario1(graphdb)
        ogm = OGM(db=graphdb)
        _, _, capability = _register_hello(ogm, ADDR_A)
        op_iri = mint_operation_iri(CFC.Operation)
        create_operation(
            ogm, operation_iri=op_iri, operation_class=CFC.Operation, capability_iri=capability
        )

        with pytest.raises(PreconditionFailedError):
            create_operation(
                ogm,
                operation_iri=op_iri,
                operation_class=CFC.Operation,
                capability_iri=capability,
            )
