"""The Operation: the Capability that it implements.

An Operation is a piece of work that a caller addresses to a Capability, not to a Workflow: it
links to the Capability with ``cfc:implementsCapability``, and the receiver resolves the Workflow
that realizes the Capability. This module gives that rule to ``mw.create()`` as the
:func:`operation` scope. It also gives it, privately, to the intake of a middleware that receives an
Operation: the scope under which the intake reads one, and the Capabilities that it finds in that
read. Further rules of an Operation, such as the Resource that executes it, belong here when a use
case needs them.

It is specific to the circular factory: it speaks Core's ``cfc:`` vocabulary. It is built on the
generic middleware, not inside it, so the generic core knows no ``cfc:`` term.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from kapps_ogm.utils.class_scope import ClassScope
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware.commit import (
    PredefinedScope,
    ScopeChanges,
    _as_class_scope,
    _require_iri,
)
from kapps_semantic_middleware.vocabulary import CFC, SVC


def operation(scope: Mapping, *, capability: Optional[str]) -> PredefinedScope:
    """The scope of a new Operation for ``mw.create()``: it implements ``capability``.

    Usage::

        with mw.create(TRANSPORT, operation(transport_scope, capability=cap)) as transport:
            ...

    ``capability`` is the Capability instance that the Operation implements. It is required:
    without one, ``mw.create()`` raises when it is entered, before the body runs and before any
    write. The Operation's ``cfc:implementsCapability`` is set to it in the Operation's
    ``Create``; the body must not set that property itself. ``scope`` is the Operation class's own
    ClassScope. The scope writes nothing else: the Operation's status and its other properties
    are the body's.

    Raises:
        ValueError: At entry, if ``capability`` is missing or no IRI.
    """

    def check() -> None:
        _require_iri(capability, scope=operation, keyword="capability")

    def changes(ogm: Any, instance_iri: IRI) -> ScopeChanges:
        return ScopeChanges(_implements(IRI(str(capability))), [])

    return PredefinedScope(scope, check=check, changes=changes)


def _implements(capability: IRI) -> dict:
    """The data of an Operation that implements ``capability``: its one Capability link."""
    return {str(CFC.implementsCapability): [{"id": str(capability)}]}


def _received_scope(scope: Optional[Mapping] = None) -> ClassScope:
    """The scope under which a middleware reads an Operation that it is notified of.

    It is the least that a middleware needs to pass the Operation on: the Capability that the
    Operation implements (``cfc:implementsCapability``) and the Workflow that realizes it
    (``svc:realizedByWorkflow``), united with ``scope``, a subproject's own ClassScope of the
    Operation. So a subproject's scope adds to the read and never removes the Capability from it.
    """
    chains: list = [[CFC.implementsCapability, SVC.realizedByWorkflow]]
    if scope:
        chains += _as_class_scope(scope).to_property_chains()
    return ClassScope.from_property_chains(chains)


def _implemented_capabilities(operation: Any) -> list[IRI]:
    """The IRIs of the Capabilities that an Operation implements, as a read of it under
    :func:`_received_scope` holds them. No Capability when the read holds none."""
    data = getattr(operation, "data", None) or {}
    values = data.get(CFC.implementsCapability, [])
    return [IRI(str(getattr(value, "id", value))) for value in values]
