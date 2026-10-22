"""Every writer that passes a class scope names every property it sets.

kapps_ogm refuses a value outside a ``Create``'s class scope (``OGMDataError``), so a writer whose
scope is narrower than its data fails at the commit. These tests read the scope that each such
writer hands to the OGM and check that it names each key of the data. They need no triple store.

The writers that take their scope from the OGM are the ``Update`` and ``Append`` ones: the status
transitions, the terminal record, the possession handover and the capability link. They pass no
class scope, so they cannot fall short of one; the last test pins that for the first three, and
the capability's test for its link, which goes into the commit of the capability's ``Create``.
"""

from __future__ import annotations

from types import SimpleNamespace

from kapps_ogm import Append, Create, Update
from kapps_ogm.utils.class_scope import ClassScope
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.operation import operation
from kapps_semantic_middleware.possession import possession_change, workpiece
from kapps_semantic_middleware.registration import (
    _create,
    _register_capability,
    _operation_status_update,
    _terminal_status_update,
    create_operation,
)
from kapps_semantic_middleware.vocabulary import CFC, SVC

EX = "http://example.org/test#"
CLASS = IRI(f"{EX}Thing")
THING = IRI(f"{EX}thing_1")
CAPABILITY = IRI(f"{EX}capability")
WORKFLOW = IRI(f"{EX}workflow")
OP = IRI(f"{EX}op_1")
FREE = f"{EX}freeProperty"
OTHER = f"{EX}otherProperty"


class _Recorder:
    """An OGM that keeps what it is handed, and answers the reads a possession state needs."""

    def __init__(self) -> None:
        self.changes: list = []
        self.created: list[dict] = []
        self.db = SimpleNamespace(query=lambda sparql, convert_bindings=False: {})

    def commit(self, *, changes=None, **kwargs):
        self.changes += list(changes or [])
        return []

    def create(self, **kwargs):
        self.created.append(kwargs)

    def fetch(self, *, instance_iri, **kwargs):
        return instance_iri


def _middleware(ogm) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=f"{EX}station",
        service_class=f"{EX}StationService",
        ogm=ogm,
        heartbeat_interval=None,
    )


def _named(scope: ClassScope) -> set[str]:
    return {str(chain[0]) for chain in scope.to_property_chains()}


def _assert_names_every_key(class_scope, data) -> None:
    assert class_scope is not None
    assert {str(key) for key in data} <= _named(class_scope)


def test_the_registration_create_names_every_key_of_its_data():
    ogm = _Recorder()
    _create(ogm, CLASS, THING, {FREE: ["a"], OTHER: [{"id": str(THING)}]})
    (call,) = ogm.created
    _assert_names_every_key(call["class_scope"], call["data"])


def test_the_capability_create_names_every_key_of_its_data_and_the_link_is_an_append():
    ogm = _Recorder()
    _register_capability(
        ogm,
        resource_iri=IRI(f"{EX}resource"),
        capability_iri=CAPABILITY,
        capability_class=CLASS,
        data={str(SVC.realizedByWorkflow): [{"id": str(WORKFLOW)}]},
    )
    create, link = ogm.changes
    assert isinstance(create, Create)
    _assert_names_every_key(create.class_scope, create.data)
    assert isinstance(link, Append)


def test_the_operation_writer_names_every_key_of_its_data_and_its_own_properties():
    ogm = _Recorder()
    create_operation(
        ogm,
        operation_iri=OP,
        operation_class=CLASS,
        capability_iri=CAPABILITY,
        data={FREE: ["a"]},
    )
    (create,) = ogm.changes
    assert isinstance(create, Create)
    _assert_names_every_key(create.class_scope, create.data)
    assert {str(CFC.implementsCapability), str(SVC.hasOperationStatus), FREE} <= _named(
        create.class_scope
    )


def test_mw_create_names_every_key_the_body_sets():
    ogm = _Recorder()
    with _middleware(ogm).create(CLASS, ClassScope()) as thing:
        thing.update({FREE: ["a"], OTHER: ["b"]})
    (create,) = ogm.changes
    _assert_names_every_key(create.class_scope, create.data)


def test_mw_create_names_what_the_operation_scope_sets():
    ogm = _Recorder()
    with _middleware(ogm).create(CLASS, operation(ClassScope(), capability=CAPABILITY)) as thing:
        thing.update({FREE: ["a"]})
    (create,) = ogm.changes
    assert str(CFC.implementsCapability) in {str(key) for key in create.data}
    _assert_names_every_key(create.class_scope, create.data)


def test_mw_create_names_what_the_workpiece_scope_sets():
    ogm = _Recorder()
    with _middleware(ogm).create(CLASS, workpiece(ClassScope(), possessor=IRI(f"{EX}owner"))) as box:
        box.update({FREE: ["a"]})
    creates = [change for change in ogm.changes if isinstance(change, Create)]
    box_create = next(change for change in creates if change.instance_iri == box.iri)
    assert str(CFC.hasPossessedWorkpiece) in {str(key) for key in box_create.data}
    for create in creates:
        if create.data:
            _assert_names_every_key(create.class_scope, create.data)


def test_an_update_folded_into_a_create_adds_its_keys_to_the_creates_scope():
    ogm = _Recorder()
    first = Create(
        class_iri=CLASS,
        instance_iri=THING,
        data={FREE: ["a"]},
        class_scope=ClassScope.from_property_chains([[IRI(FREE)]]),
    )
    later = Update(instance_iri=THING, data={OTHER: ["b"]})
    with _middleware(ogm).commit(exit=[first, later]):
        pass
    (merged,) = ogm.changes
    assert isinstance(merged, Create)
    assert OTHER in {str(key) for key in merged.data}
    _assert_names_every_key(merged.class_scope, merged.data)


def test_the_update_and_append_writers_pass_no_class_scope():
    status = _operation_status_update(OP, "running")
    terminal = _terminal_status_update(
        operation_iri=OP, workflow_iri=WORKFLOW, status="failed", result="r", failure_state="s"
    )
    handover = possession_change(
        _Recorder(),
        workpiece_iri=THING,
        possessor_iri=IRI(f"{EX}owner"),
        workpiece_update={FREE: ["a"]},
    )
    for change in (status, terminal, handover.changes[0]):
        assert isinstance(change, Update)
        assert getattr(change, "class_scope", None) is None
    state = handover.changes[1]
    assert isinstance(state, Create) and not state.data


def test_mw_create_names_the_properties_of_a_nested_instance_below_its_property():
    """kapps_ogm checks a nested node against the nested scope; a bare reference needs none."""
    ogm = _Recorder()
    nested = {"id": str(THING), OTHER: ["b"]}
    with _middleware(ogm).create(CLASS, ClassScope()) as thing:
        thing.update({FREE: [nested], f"{EX}link": [{"id": str(THING)}]})
    (create,) = ogm.changes
    chains = [[str(prop) for prop in chain] for chain in create.class_scope.to_property_chains()]
    assert [FREE, OTHER] in chains
    assert [f"{EX}link"] in chains
    assert [f"{EX}link", "id"] not in chains
