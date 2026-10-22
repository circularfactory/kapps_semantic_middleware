"""Possession: one possession state per Resource, which the Workpieces it possesses point at,
and the handover, which changes it.

A Resource gets one ``cfc:PossessionState`` with its first possession and keeps it for life. A
Workpiece points at the state of its one Possessor with ``cfc:hasPossessedWorkpiece``, so a
handover changes only that link of the Workpiece, and no Resource's ``cfc:hasPossessor`` link
changes. One state holds every Workpiece that its Resource possesses.

Possession is an extension that ships with the middleware, and specific to the circular factory:
this module speaks Core's ``cfc:`` vocabulary and the ``mes:`` handover abilities. It is built on
the generic middleware, not inside it. It reads through the OGM's access module and gives
kapps_ogm ``Create`` and ``Update`` changes, which the caller commits through ``mw.commit()``, or
``OGM.commit``, together with the write that needs them. :func:`handover` commits its change of
possession through ``mw.commit()`` and reads through the OGM that the middleware exposes
(``mw.ogm``), and its :func:`workpiece` scope brings the same rule to ``mw.create()``. The generic
core knows Core's Capability, Resource and Operation (registration writes ``cfc:hasCapability``),
but it knows no possession term, so it stays portable to a domain that models possession another
way.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, List, Mapping, Optional, Sequence

from kapps_ogm import Create, Update
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import PreconditionFailedError

from kapps_semantic_middleware.commit import (
    Change,
    PredefinedScope,
    ScopeChanges,
    _as_reference,
    _nothing_to_join,
    _require_iri,
)
from kapps_semantic_middleware.errors import (
    DeploymentValueError,
    HandoverPreconditionError,
    PossessionSwitchConflict,
)
from kapps_semantic_middleware.modes import Mode
from kapps_semantic_middleware.vocabulary import CFC, MES, SVC, OperationStatus

if TYPE_CHECKING:
    from kapps_ogm import OGM

    from kapps_semantic_middleware.middleware import SemanticMiddleware


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
        DeploymentValueError: If the Resource has more than one possession state (Repair). A
            Resource has one. It is a ``ValueError``.
    """
    sparql = f"SELECT DISTINCT ?state WHERE {{ <{resource_iri}> <{CFC.hasPossessor}> ?state . }}"
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    states = sorted(str(b["state"]) for b in bindings)
    if len(states) > 1:
        raise DeploymentValueError(
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
            sets itself.
        DeploymentValueError: If the Possessor has more than one possession state (Repair). It
            is a ``ValueError``.
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
    :func:`handover`, the creation does not try again.

    Raises:
        ValueError: At entry, if ``possessor`` is missing or no IRI.
        DeploymentValueError: At the clean exit, if the possessor has more than one possession
            state (Repair). It is a ``ValueError``.
    """

    def check() -> None:
        _require_iri(possessor, scope=workpiece, keyword="possessor")

    def changes(ogm: "OGM", instance_iri: IRI) -> ScopeChanges:
        state, create_state = possession_state(ogm, IRI(str(possessor)))
        return ScopeChanges({str(CFC.hasPossessedWorkpiece): [{"id": str(state)}]}, create_state)

    return PredefinedScope(scope, check=check, changes=changes)


# --------------------------------------------------------------------------- #
# The reads of a handover's preconditions, and the possession helpers that a seed uses.
# --------------------------------------------------------------------------- #


def find_possession_state(
    ogm: "OGM",
    workpiece_iri: IRI,
    possessor_iri: IRI,
    named_graph: Optional[IRI] = None,
) -> Optional[IRI]:
    """The current PossessionState in which ``possessor_iri`` holds ``workpiece_iri``, or None.

    An ontology-backed read (``workpiece cfc:hasPossessedWorkpiece ?ps . possessor
    cfc:hasPossessor ?ps``). Never match on IRI names.
    """
    sparql = f"""
    SELECT ?ps WHERE {{
        <{workpiece_iri}> <{CFC.hasPossessedWorkpiece}> ?ps .
        <{possessor_iri}> <{CFC.hasPossessor}> ?ps .
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    return IRI(str(bindings[0]["ps"])) if bindings else None


def counterpart_has_complementary_ability(
    ogm: "OGM",
    counterpart_iri: IRI,
    mode_ability_iri: IRI,
    named_graph: Optional[IRI] = None,
) -> bool:
    """True iff the counterpart carries the handover ability COMPLEMENTARY to ``mode_ability_iri``.

    ``mes:complements`` is symmetric. This is a read (``mode_ability mes:complements ?a .
    counterpart mes:hasHandoverAbility ?a``).
    """
    sparql = f"""
    SELECT ?a WHERE {{
        <{mode_ability_iri}> <{MES.complements}> ?a .
        <{counterpart_iri}> <{MES.hasHandoverAbility}> ?a .
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    return bool(bindings)


def create_possession(
    ogm: "OGM",
    *,
    workpiece_iri: IRI,
    possessor_iri: IRI,
    named_graph: Optional[IRI] = None,
) -> IRI:
    """Give a workpiece its first Possessor, in one OGM commit.

    The same write as :func:`switch_possession`, under the name that callers of a first
    possession use: the workpiece points at the possessor's one ``cfc:PossessionState``, and
    when the possessor has none yet, the same commit creates it as ``<possessor>_possession``
    (its ``Create`` writes its type) and links the possessor to it. A possessor that has a state
    keeps its link unchanged, however many workpieces it possesses.

    Returns:
        The possessor's PossessionState IRI.
    """
    return switch_possession(
        ogm, workpiece_iri=workpiece_iri, new_possessor_iri=possessor_iri, named_graph=named_graph
    )


def switch_possession(
    ogm: "OGM",
    *,
    workpiece_iri: IRI,
    new_possessor_iri: IRI,
    workpiece_update: Optional[Mapping[str, Sequence[Any]]] = None,
    named_graph: Optional[IRI] = None,
) -> IRI:
    """Hand a workpiece to a new possessor in one OGM commit, with no move around it.

    This is the possession change of :func:`handover` on its own. The workpiece's
    ``cfc:hasPossessedWorkpiece`` points at the new possessor's one state, and
    ``workpiece_update`` changes more of its properties in the same commit. When the new
    possessor has no state yet, the same commit creates it and links the possessor to it. The
    previous possessor keeps its state and its link, and so does a new possessor that has a
    state. The store validates the whole commit once; if it refuses, nothing of it stays.

    Another write that moves the workpiece at the same moment is refused only in part. Inside the
    commit, kapps_ogm compares the workpiece with a fresh read of it: a write that landed before
    that read is overwritten. One that lands between that read and the write makes the store
    refuse this commit and write nothing of it, and the switch raises
    ``PossessionSwitchConflict``.

    A participant hands over with :func:`handover`. This helper, and :func:`create_possession`,
    serve callers that hold no middleware; they write through ``OGM.commit``, the validated
    write, not through ``mw.commit()``.

    Args:
        workpiece_update: More properties of the workpiece to set in the same commit, as
            ``{property IRI: [values]}``; each listed property is replaced. An IRI value (an
            ``IRI`` or an rdflib ``URIRef``), or ``{"id": iri}``, is a reference to that object;
            any other value goes to the OGM as it is.

    Returns:
        The new possessor's PossessionState IRI.

    Raises:
        ValueError: If ``workpiece_update`` names ``cfc:hasPossessedWorkpiece``, which the
            change sets itself.
        DeploymentValueError: If the new possessor has more than one possession state
            (Repair). It is a ``ValueError``.
        PossessionSwitchConflict: If another write changed what the commit had read, so the
            store found its precondition false (``PreconditionFailedError``, the cause).
            Nothing is written.
    """
    change = possession_change(
        ogm,
        workpiece_iri=workpiece_iri,
        possessor_iri=new_possessor_iri,
        workpiece_update=workpiece_update,
    )
    try:
        ogm.commit(changes=change.changes, named_graph=named_graph)
    except PreconditionFailedError as error:
        raise _switch_conflict(workpiece_iri) from error
    return change.state


# --------------------------------------------------------------------------- #
# The handover.
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def handover(
    mw: "SemanticMiddleware",
    *,
    mode: Any,
    workpiece: Any,
    counterpart: Any,
    workpiece_update: Optional[Mapping[str, Sequence[Any]]] = None,
    operation: Any = None,
    entry: Optional[Sequence[Change]] = None,
) -> Iterator[None]:
    """Hand a workpiece from ``mw``'s Resource to a counterpart: the possession changes first,
    then the body moves it.

    Usage::

        from kapps_semantic_middleware.possession import handover

        with handover(mw, mode=MES.Pass, workpiece=workpiece, counterpart=peer, operation=op):
            machine.move_workpiece()  # the physical move, owned by the domain

    and, with more changes that must be admitted together with the possession change, such as
    a transport that the peer now executes::

        moved_on = Update(
            instance_iri=transport,
            data={
                str(CFC.executedOn): [{"id": peer}],
                str(CFC.implementsCapability): [{"id": peer_capability}],
            },
        )
        with handover(
            mw, mode=MES.Pass, workpiece=workpiece, counterpart=peer, entry=[moved_on]
        ):
            machine.move_workpiece()

    It is built on the middleware's public surface only: it reads through the OGM that the
    middleware exposes (``mw.ogm``), names the giver by ``mw.resource``, and writes with
    ``mw.commit()``.

    The entry checks two preconditions before anything is written: ``mw``'s Resource currently
    possesses the workpiece (the workpiece points at a `cfc:PossessionState` whose possessor
    is that Resource), and the counterpart carries the `mes:hasHandoverAbility` complementary
    to `mode`. There is deliberately no check that the counterpart is free: a resource may
    possess any number of workpieces unless a domain rule limits it.

    Then the entry commits the possession change through ``mw.commit()``, as one commit. The
    workpiece's `cfc:hasPossessedWorkpiece` points at the counterpart's one possession state;
    `workpiece_update` sets more properties of the workpiece that must change together with
    its possessor, such as its transport state; `operation`, the Operation that this
    handover completes, becomes `done`; and `entry`, the caller's own kapps_ogm changes, is
    written too. When the counterpart has no state yet, the same commit creates
    `<counterpart>_possession` and links the counterpart to it. Nothing writes the
    `cfc:hasPossessor` link of the giver, or of a counterpart that has a state.

    If the store refuses the commit, its error is raised (a SHACL refusal is
    `ShaclValidationError`, with the store's validation report as its `report`, and
    ``mw.refused_writes`` records it with the verb ``handover``), nothing is written, neither the
    possession change nor a change of `entry`, and the body does not run. The body is the move:
    the physical transport, and any coordination with the counterpart, for example through an
    ``mw.issue_operation(...)``. A clean exit writes nothing more, so a ``mw.create()`` in the
    body commits on its own. An error in the body is raised as it is and undoes nothing: the
    possession has already changed, so the caller records what really happened.

    Core says that a resource and a workpiece each have exactly one possession state. That is
    OWL: it licenses inferences and refuses no write. What refuses a handover at commit is the
    domain's SHACL shape, for example the one that allows one workpiece at a station.

    A first handover to a counterpart without a state can race another write that creates
    that state. Two commits that truly coincide both write the same state triples, and one
    state results. When the other write lands between this handover's read and its commit,
    the store refuses the second creation of the state (`PreconditionFailedError`), and
    writes nothing of it. The handover then checks both preconditions again and commits once
    more, with the state that exists now; if a precondition no longer holds, for example
    because the other write handed this same workpiece, it raises
    `HandoverPreconditionError`, and the body does not run.

    A second handover of the same workpiece at the same moment is refused only in part.
    Both pass the preconditions before either commits. Inside its commit, kapps_ogm compares
    the workpiece with a fresh read of it. If the other handover's commit landed between that
    read and this commit's write, the store refuses this commit and writes nothing of it, and
    the handover raises `PossessionSwitchConflict` before the body runs. Known limit: if the
    other handover's commit landed before that read, this commit overwrites its possession
    change, both succeed, and both bodies run.

    Args:
        mw: The resource-mode middleware whose Resource gives the workpiece away.
        mode: The giver's handover ability, such as `MES.Pass`. The counterpart must carry the
            complementary one.
        workpiece: The workpiece that ``mw``'s Resource possesses.
        counterpart: The resource that takes the workpiece over.
        workpiece_update: More properties of the workpiece to set in the same commit, as
            `{property IRI: [values]}`; each listed property is replaced. A value that is an
            IRI (an `IRI` or an rdflib `URIRef`), or `{"id": iri}`, is a reference to that
            object. Any other value goes to the OGM as it is: a plain `str` is a literal,
            which the OGM refuses for an object property with `ValueError`, before any write.
        operation: The Operation that this handover completes. Its `svc:hasOperationStatus`
            becomes `done` in the same commit.
        entry: More kapps_ogm `Create` and `Update` changes, built by the caller, that the
            store admits or refuses in the same commit as the possession change, before the
            body. A change of the workpiece or of `operation` belongs in `workpiece_update` or
            is made by `operation`: kapps_ogm refuses two changes of one instance in one
            commit, with `ValueError`.

    Raises:
        RuntimeError: If ``mw`` is not in resource mode.
        HandoverPreconditionError: If a precondition fails. Nothing is written.
        PossessionSwitchConflict: If the store found the precondition of the entry's
            commit false (`PreconditionFailedError`, the cause). Usually another write
            changed what the entry had read, such as a second handover of the workpiece.
            The store does not say which part of the commit failed, so the same error
            covers a change of `entry` that creates an instance that the store holds.
            Nothing is written, and the body does not run.
        ValueError: If `workpiece_update` names `cfc:hasPossessedWorkpiece`, which the
            handover sets itself, or a value that does not fit its property, or a change of
            `entry` does not fit its class or writes an instance that the commit already
            writes. Nothing is written.
        DeploymentValueError: If the counterpart has more than one possession state
            (Repair). It is a ``ValueError``. Nothing is written.
        kapps_triplestore_interface.exceptions.TripleStoreInterfaceError: If the store
            refuses the possession change; a SHACL refusal is `ShaclValidationError`.
            Nothing is written, and the body does not run.
    """
    if mw.mode != Mode.RESOURCE or mw.resource is None:
        raise RuntimeError(
            f"handover() is only valid in resource mode; current mode is {mw.mode!r}"
        )
    ogm = mw.ogm
    giver_iri = IRI(mw.resource)
    workpiece_iri = IRI(workpiece)
    counterpart_iri = IRI(counterpart)
    mode_ability_iri = IRI(mode)

    _check_handover_preconditions(ogm, giver_iri, workpiece_iri, counterpart_iri, mode_ability_iri)

    # What the commit carries beside the possession change: the Operation that the handover
    # completes, and the caller's own changes.
    alongside: List[Change] = []
    if operation is not None:
        done = {str(SVC.hasOperationStatus): [OperationStatus.DONE]}
        alongside.append(Update(instance_iri=IRI(operation), data=done))
    alongside.extend(entry or ())

    # The entry commits the possession change; the body is the move; the exit writes nothing.
    change = possession_change(
        ogm,
        workpiece_iri=workpiece_iri,
        possessor_iri=counterpart_iri,
        workpiece_update=workpiece_update,
    )
    with contextlib.ExitStack() as admitted:
        try:
            admitted.enter_context(
                mw.commit(entry=[*change.changes, *alongside], verb="handover")
            )
        except PreconditionFailedError as refusal:
            # The store wrote nothing of the entry. When the entry creates the counterpart's
            # state, another write may have created that state after it was read: then the
            # preconditions are checked again, and the entry is committed once more with
            # that state. Any other refusal is a conflict.
            again = None
            if change.creates_state:
                again = possession_change(
                    ogm,
                    workpiece_iri=workpiece_iri,
                    possessor_iri=counterpart_iri,
                    workpiece_update=workpiece_update,
                )
            if again is None or again.creates_state:
                raise _switch_conflict(workpiece_iri) from refusal
            _check_handover_preconditions(
                ogm, giver_iri, workpiece_iri, counterpart_iri, mode_ability_iri
            )
            try:
                admitted.enter_context(
                    mw.commit(entry=[*again.changes, *alongside], verb="handover")
                )
            except PreconditionFailedError as second_refusal:
                raise _switch_conflict(workpiece_iri) from second_refusal
        # The commit has happened, so a creation in the body commits on its own.
        admitted.enter_context(_nothing_to_join())
        yield


def _check_handover_preconditions(
    ogm: "OGM",
    giver_iri: IRI,
    workpiece_iri: IRI,
    counterpart_iri: IRI,
    mode_ability_iri: IRI,
) -> None:
    """The two preconditions of a handover, read before anything is written.

    Raises:
        HandoverPreconditionError: If the giver does not possess the workpiece, or the
            counterpart lacks the handover ability complementary to the mode.
    """
    if find_possession_state(ogm, workpiece_iri, giver_iri) is None:
        raise HandoverPreconditionError(f"{giver_iri} does not currently possess {workpiece_iri}")
    if not counterpart_has_complementary_ability(ogm, counterpart_iri, mode_ability_iri):
        raise HandoverPreconditionError(
            f"counterpart {counterpart_iri} lacks the handover ability complementary "
            f"to {mode_ability_iri}"
        )
