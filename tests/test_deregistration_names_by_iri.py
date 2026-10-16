"""Deregistration names each Workflow and StateProperty of a Service by IRI.

`deregister_service` finds the endpoints to clear by a read of the store. The store can answer a
subject as a blank node, and the OGM's commit takes an IRI. A blank node there is broken data in
the store, so it raises the middleware's Repair type for a wrong value, and nothing is committed
for it.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from kapps_triplestore_interface import IRI
from rdflib import BNode

from kapps_semantic_middleware.errors import DeploymentValueError
from kapps_semantic_middleware.registration import deregister_service
from kapps_semantic_middleware.vocabulary import SVC

SERVICE = IRI("https://example.org/test#ServiceA")
WORKFLOW = IRI("https://example.org/test#WorkflowA")


def _ogm(workflows: list, state_properties: list) -> Mock:
    ogm = Mock()

    def triples_get(*, pred, obj, named_graph=None):
        rows = workflows if pred == SVC.isWorkflowOf else state_properties
        return [(subject, pred, obj) for subject in rows]

    ogm.db.triples_get.side_effect = triples_get
    return ogm


def test_the_endpoints_of_named_workflows_and_state_properties_are_cleared():
    ogm = _ogm([WORKFLOW], [])

    deregister_service(ogm, SERVICE)

    cleared = [call.kwargs["instance_iri"] for call in ogm.commit.call_args_list]
    assert cleared == [SERVICE, WORKFLOW]


@pytest.mark.parametrize("role", ["workflow", "state property"])
def test_a_blank_node_where_an_iri_is_required_raises_the_repair_type(role):
    blank = BNode()
    ogm = _ogm([blank], []) if role == "workflow" else _ogm([], [blank])

    with pytest.raises(DeploymentValueError, match="blank node"):
        deregister_service(ogm, SERVICE)

    cleared = [call.kwargs["instance_iri"] for call in ogm.commit.call_args_list]
    assert blank not in cleared
