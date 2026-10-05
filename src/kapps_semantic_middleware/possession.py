"""Possession: one possession state per Resource, which the Workpieces it possesses point at.

A Resource gets one ``cfc:PossessionState`` with its first possession and keeps it for life. A
Workpiece points at the state of its one Possessor with ``cfc:hasPossessedWorkpiece``, so a
handover changes only that link of the Workpiece, and no Resource's ``cfc:hasPossessor`` link
changes. One state holds every Workpiece that its Resource possesses.

Possession is a topic of its own, and specific to the circular factory: this module speaks Core's
``cfc:`` vocabulary. It is built on the generic middleware, not inside it. It reads through the
OGM's access module and gives kapps_ogm ``Create`` and ``Update`` changes, which the caller
commits through ``mw.commit()``, or ``OGM.commit``, together with the write that needs them. Its
:func:`workpiece` scope brings the same rule to ``mw.create()``. So the generic core knows no
``cfc:`` term and stays portable to a domain that models possession another way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

from kapps_ogm import Create, Update
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware.commit import (
    Change,
    PredefinedScope,
    ScopeChanges,
    _as_reference,
    _require_iri,
)
from kapps_semantic_middleware.errors import PossessionSwitchConflict
from kapps_semantic_middleware.vocabulary import CFC

if TYPE_CHECKING:
    from kapps_ogm import OGM


@dataclass(frozen=True)
class PossessionChange:
    """The changes that make a Resource the Possessor of a Workpiece, for one commit."""

    #: The Possessor's one possession state, which the Workpiece points at.
    state: IRI
    #: The Workpiece's ``Update``, and the state's creation when the Possessor has none yet.
    changes: list[Change]
    #: Whether ``changes`` create the state. Another write may create it first; kapps_ogm then
    #: refuses the commit of these changes, and a second read finds the state.
    creates_state: bool


def mint_possession_state_iri(resource_iri: IRI) -> IRI:
    """The IRI ``{resource}_possession``, which a Resource's possession state gets when created.

    Only the commit that creates the state uses it. Every later write finds the state by query,
    because a seed may have given it another IRI. The fixed IRI keeps two first writes at the same
    moment from making two states: both name the same one.
    """
    return IRI(f"{resource_iri}_possession")


def possession_state(ogm: "OGM", resource_iri: IRI) -> tuple[IRI, list[Change]]:
    """A Resource's one possession state, and the changes that create it when it has none.

    The state is found by a query (``<resource> cfc:hasPossessor ?state``), never derived from an
    IRI. When the Resource has a state, there are no changes, and nothing writes its link. When it
    has none, the state is ``<resource>_possession``, and the changes are its ``Create`` (which
    writes its type ``cfc:PossessionState``) and an ``Update`` that links the Resource to it. Put
    them in the commit that points a Workpiece at the state, so the state comes into being with
    its first Workpiece.

    The query is a read of its own, before that commit. Two first writes for one Resource that
    truly coincide both write the same state triples, and one state results. When one lands
    between the other's read and its commit, the store refuses the later ``Create`` with
    ``PreconditionFailedError`` and writes nothing of its commit. After such a refusal, a second
    read finds the state, with no changes.

    Returns:
        The state's IRI, and the changes that create it (none when it exists).

    Raises:
        ValueError: If the Resource has more than one possession state. A Resource has one.
    """
    sparql = f"SELECT DISTINCT ?state WHERE {{ <{resource_iri}> <{CFC.hasPossessor}> ?state . }}"
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    states = sorted(str(b["state"]) for b in bindings)
    if len(states) > 1:
        raise ValueError(
            f"{resource_iri} has {len(states)} possession states ({', '.join(states)}); "
            "a Resource has one"
        )
    if states:
        return IRI(states[0]), []
    state = mint_possession_state_iri(resource_iri)
    return state, [
        Create(class_iri=CFC.PossessionState, data={}, instance_iri=state),
        Update(instance_iri=resource_iri, data={str(CFC.hasPossessor): [{"id": str(state)}]}),
    ]


def possession_change(
    ogm: "OGM",
    *,
    workpiece_iri: IRI,
    possessor_iri: IRI,
    workpiece_update: Optional[Mapping[str, Sequence[Any]]] = None,
) -> PossessionChange:
    """The changes that make ``possessor_iri`` the Possessor of ``workpiece_iri``: a handover's
    change of possession, for one commit.

    The Workpiece's ``cfc:hasPossessedWorkpiece`` points at the Possessor's one state, found with
    :func:`possession_state`, and ``workpiece_update`` changes more properties of the Workpiece in
    the same ``Update``. When the Possessor has no state yet, the changes also create it and link
    the Possessor to it; that new link is the only ``cfc:hasPossessor`` they write.

    ``workpiece_update`` is ``{property IRI: [values]}``; each listed property is replaced. A value
    that is an IRI (an ``IRI`` or an rdflib ``URIRef``), or ``{"id": iri}``, is a reference to that
    object. Any other value goes to the OGM as it is: a plain ``str`` is a literal, which the OGM
    refuses for an object property with ``ValueError``, before any write.

    Raises:
        ValueError: If ``workpiece_update`` names ``cfc:hasPossessedWorkpiece``, which this change
            sets itself, or the Possessor has more than one possession state.
    """
    update = {
        prop: [_as_reference(value) for value in values]
        for prop, values in (workpiece_update or {}).items()
    }
    if CFC.hasPossessedWorkpiece in {IRI(prop) for prop in update}:
        raise ValueError(
            f"workpiece_update must not set {CFC.hasPossessedWorkpiece}: the handover sets it"
        )
    state, create_state = possession_state(ogm, possessor_iri)
    moved = {str(CFC.hasPossessedWorkpiece): [{"id": str(state)}], **update}
    return PossessionChange(
        state=state,
        changes=[Update(instance_iri=workpiece_iri, data=moved), *create_state],
        creates_state=bool(create_state),
    )


def _switch_conflict(workpiece_iri: IRI) -> PossessionSwitchConflict:
    """The error for a change of possession of ``workpiece_iri`` whose commit the store refused
    because its precondition was false. Raise it ``from`` the store's refusal."""
    return PossessionSwitchConflict(
        f"the store refused the change of possession of {workpiece_iri}: the precondition of "
        "its commit was false, so nothing was written"
    )


def workpiece(scope: Mapping, *, possessor: Optional[str]) -> PredefinedScope:
    """The scope of a new Workpiece for ``mw.create()``: it starts in its possessor's possession.

    Usage::

        with mw.create(BOX, workpiece(box_scope, possessor=mw.resource)) as box:
            ...

    ``possessor`` is any Resource, the middleware's own (``mw.resource``) or another one, such as
    the station that scanned the Workpiece. It is required: without one, ``mw.create()`` raises
    when it is entered, before the body runs and before any write. The Workpiece's
    ``cfc:hasPossessedWorkpiece`` points at the possessor's one possession state, found as
    :func:`possession_state` finds it, when the creation's changes are built at its clean exit.
    When the possessor has no state yet, the same commit creates ``<possessor>_possession`` and
    links the possessor to it. ``scope`` is the Workpiece class's own ClassScope; the body must not
    set ``cfc:hasPossessedWorkpiece`` itself.

    A first possession can race another write that creates the same state. The store then
    refuses this commit with ``PreconditionFailedError``, and nothing of it is written. Unlike
    ``handover()``, the creation does not try again.

    Raises:
        ValueError: At entry, if ``possessor`` is missing or no IRI. At the clean exit, if the
            possessor has more than one possession state.
    """

    def check() -> None:
        _require_iri(possessor, scope=workpiece, keyword="possessor")

    def changes(ogm: "OGM", instance_iri: IRI) -> ScopeChanges:
        state, create_state = possession_state(ogm, IRI(str(possessor)))
        return ScopeChanges({str(CFC.hasPossessedWorkpiece): [{"id": str(state)}]}, create_state)

    return PredefinedScope(scope, check=check, changes=changes)
