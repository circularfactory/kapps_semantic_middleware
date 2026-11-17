"""Graph-write core of the KAPPS Semantic Middleware.

Every knowledge-graph **write** goes through the OGM (`OGM.create` / `OGM.commit`). This is the
architecture's single validated write path. No write issues raw SPARQL UPDATE.
**Reads** may use the triple-store access module (`ogm.db`) directly. The architecture permits
this.

Only one direction of each inverse relation receives materialization. The direction belongs to
the newly-created instance (e.g. a workflow ``svc:isWorkflowOf``, a capability
``svc:realizedByWorkflow``). The container-side inverses (``svc:hasWorkflow`` etc.) are
OWL-inferable. They receive no write. Each registered individual is written as a whole, a
``Create`` or an ``Update`` of the properties it owns, rather than a read-modify-write append onto
a growing multi-valued property of its container. Queries in this
module therefore use the materialized (instance-owned) direction. The one exception is the
resource's ``cfc:hasCapability``, which Core gives the resource: it is written through
``OGM.commit`` as an ``Append``, which adds the link and removes none. A Workflow or StateProperty
with its endpoint, its Capability, and the resource's link arrive in one commit, so no reader sees
one of them without the others (see ``_write_exposure``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from kapps_triplestore_interface import IRI
from rdflib import BNode
from kapps_triplestore_interface.exceptions import (
    InvalidIRIError,
    PreconditionFailedError,
    TripleStoreResponseError,
    TripleStoreUnavailableError,
)
from kapps_ogm import Append, Create, Update

from kapps_semantic_middleware.commit import _scope_naming
from kapps_semantic_middleware.errors import (  # noqa: F401 - their earlier home, kept importable
    OntologyGroundTruthError,
    OperationQueueEmpty,
    OperationResolutionError,
)
from kapps_semantic_middleware.errors import (
    CapabilityAlreadyRealizedError,
    CapabilityNotRealizedError,
    DeploymentValueError,
    OperationReceiverUnavailableError,
)
from kapps_semantic_middleware import _deprecation
from kapps_semantic_middleware.operation import _implements
from kapps_semantic_middleware.vocabulary import (
    CFC,
    OperationStatus,
    SVC,
)

if TYPE_CHECKING:
    from kapps_ogm import OGM


RDF_TYPE = IRI("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")


# --------------------------------------------------------------------------- #
# Deterministic IRI minting (so a restarting resource reuses its IRIs).
# --------------------------------------------------------------------------- #


logger = logging.getLogger(__name__)

#: The store's errors that the liveness paths (the heartbeat, the watchdog's sweep) log and survive,
#: trying again at the next interval: the store unreachable or the connection dropped (Retry), an
#: error answer of the store (logged at every interval, so one that needs a repair stays visible),
#: and a value another write changed after the read (Refresh: the next attempt reads again).
_LIVENESS_STORE_ERRORS = (
    TripleStoreUnavailableError,
    TripleStoreResponseError,
    PreconditionFailedError,
)


def __getattr__(name: str) -> Any:
    # The deprecated names of the handover's helpers and errors, which left this module. Each use
    # of one comes here and warns, and only then imports the new home, so this module does not
    # import it. The release after the one that moved them removes them.
    extension = "kapps_semantic_middleware.possession"
    errors = "kapps_semantic_middleware.errors"
    homes = {
        "create_possession": extension,
        "switch_possession": extension,
        "find_possession_state": extension,
        "counterpart_has_complementary_ability": extension,
        "mint_possession_state_iri": extension,
        "HandoverPreconditionError": errors,
        "PossessionSwitchConflict": errors,
    }
    return _deprecation.moved(__name__, homes, name)


def normalize_address(address: str) -> str:
    """Normalize an instance address. Trivially different spellings become one deployment.

    Lowercase the scheme and the netloc (host names are case-insensitive, paths are not). Strip
    a trailing ``/``. Without this, ``http://Host:8000/`` and ``http://host:8000`` would mint
    two Service nodes for one process. This discriminator prevents exactly that orphaning.
    """
    parts = urlsplit(address)
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            parts.query,
            parts.fragment,
        )
    )


def mint_service_iri(resource_iri: IRI, address: str) -> IRI:
    """Mint the Service IRI for one middleware *instance*: ``{resource_iri}_service_{address}``.

    The discriminator is the instance normalized address mangled with ``IRI.lined``. This
    satisfies two requirements at once. It is stable across restarts of the same
    deployment (a returning instance re-adopts its node). It is distinct between concurrent
    instances (a controller and a monitor on one resource do not overwrite each other
    ``svc:hasAddress`` and heartbeat). ``lined`` rather than a hash. Production IRIs remain
    back-resolvable. ``IRI.from_lined`` reads the address off the node IRI.

    ``address`` is required. A default would silently reinstate the shared node.

    Raises:
        ValueError: If ``address`` is not an absolute http(s) URL.
    """
    try:
        discriminator = IRI(normalize_address(address)).lined
    except InvalidIRIError as exc:
        raise ValueError(
            "The Service discriminator is derived from the instance address, which must be an "
            f"absolute http(s) URL; got {address!r}"
        ) from exc
    return IRI(f"{resource_iri}_service_{discriminator}")


def mint_workflow_iri(service_iri: IRI, name: str) -> IRI:
    """Mint the deterministic Workflow IRI: ``{service_iri}_workflow_{name}``."""
    return IRI(f"{service_iri}_workflow_{name}")


def mint_capability_iri(resource_iri: IRI, name: str) -> IRI:
    """Mint the deterministic Capability IRI: ``{resource_iri}_capability_{name}``."""
    return IRI(f"{resource_iri}_capability_{name}")


def mint_state_property_iri(service_iri: IRI, name: str) -> IRI:
    """Mint the deterministic StateProperty IRI: ``{service_iri}_state_{name}``."""
    return IRI(f"{service_iri}_state_{name}")


def build_workflow_endpoint(address: str, workflow_name: str) -> str:
    """Build the callable endpoint URL for a workflow (``POST /workflows/{name}/execute``)."""
    return f"{address.rstrip('/')}/workflows/{workflow_name}/execute"


def build_state_endpoint(address: str, state_name: str) -> str:
    """Build the GET endpoint URL for a state property (``GET /state/{name}``)."""
    return f"{address.rstrip('/')}/state/{state_name}"


# --------------------------------------------------------------------------- #
# Ground-truth assertion and OGM write helpers.
# --------------------------------------------------------------------------- #


def assert_class_registered(
    ogm: "OGM", class_iri: IRI, base_iri: IRI, named_graph: Optional[IRI] = None
) -> None:
    """Assert a class pre-exists as the required kind.

    Valid iff ``class_iri == base_iri`` or ``class_iri`` is a subclass of ``base_iri``. A read
    against the access module. A non-existent class makes the subclass check False. This also
    catches a missing class.

    Raises:
        OntologyGroundTruthError: If the class is missing or not the required subclass.
    """
    if class_iri != base_iri and not ogm.db.is_subclass(
        class_iri, base_iri, named_graph=named_graph
    ):
        raise OntologyGroundTruthError(
            f"Class {class_iri} must be {base_iri} or a subclass thereof, "
            "but it is not registered as such in the knowledge graph. "
            "Types are ontology-ground-truth: author the class first."
        )


def _ref(iri: IRI) -> dict:
    """Wrap an IRI as an object-property reference for OGM write data.

    Object-property values must be node references (the OGM materializes them as nested nodes),
    not literals. A ``{"id": iri}`` dict becomes a reference node by the OGM data formatter.
    Literals pass as plain values.
    """
    return {"id": str(iri)}


def _create(
    ogm: "OGM",
    class_iri: IRI,
    instance_iri: IRI,
    data: dict,
    named_graph: Optional[IRI] = None,
) -> None:
    """Create a typed individual with its instance-owned properties via the OGM.

    ``data`` maps property-IRI strings to lists of values (object references as IRI strings,
    literals as their Python/str value). The IRI must be new: if the graph already holds
    statements about it, the OGM writes nothing and raises ``PreconditionFailedError``.
    """
    scope = _scope_naming(None, data)
    ogm.create(
        class_iri=class_iri,
        instance_iri=instance_iri,
        data=data,
        class_scope=scope,
        persist=True,
        named_graph=named_graph,
    )


def _create_or_adopt(
    ogm: "OGM",
    class_iri: IRI,
    instance_iri: IRI,
    data: dict,
    named_graph: Optional[IRI] = None,
) -> None:
    """Create a registration individual, or adopt the one an earlier run left in the graph.

    Registration IRIs are stable across restarts, and deregistration keeps the structural
    triples and ``rdf:type``. So a returning instance finds its individuals in place, and the OGM
    refuses to create them again. Such an individual is adopted: ``data`` is set on it through
    ``OGM.commit``. An unchanged individual gives an empty diff, and nothing is written. A
    structural link that changed is set to the new value.

    Only for the deterministic registration IRIs. An Operation IRI is fresh for each dispatch,
    so an Operation that already exists is an error, and ``_create`` raises it.
    """
    try:
        _create(ogm, class_iri, instance_iri, data, named_graph)
    except PreconditionFailedError:
        _set(ogm, instance_iri, data, named_graph)


def _set(
    ogm: "OGM",
    instance_iri: IRI,
    data: dict,
    named_graph: Optional[IRI] = None,
) -> None:
    """Set/replace/remove mutable properties on an existing individual via OGM.commit.

    A value of ``[]`` removes the property. The commit is a single atomic DELETE/INSERT
    transaction. Cardinality-constrained properties are never transiently absent (SHACL-safe).
    """
    ogm.commit(instance_iri=instance_iri, data=data, named_graph=named_graph)


# --------------------------------------------------------------------------- #
# Registration (writes via OGM).
# --------------------------------------------------------------------------- #


def register_service(
    ogm: "OGM",
    *,
    resource_iri: IRI,
    service_iri: IRI,
    service_class: IRI,
    address: str,
    named_graph: Optional[IRI] = None,
) -> None:
    """Register a Service instance for a resource. Set its base address and a first heartbeat.

    The address and ``svc:hasLastHeartbeat`` (now) are written in one commit. A Service with an
    address is stale to the watchdog when its heartbeat is missing or old, so an address written
    alone would let a sweep that runs before the heartbeat loop's first beat deregister a Service
    that is starting, or one that restarts after its old heartbeat aged past the threshold.

    Raises OntologyGroundTruthError if ``service_class`` is not ``svc:Service`` or a subclass.
    """
    assert_class_registered(ogm, service_class, SVC.Service, named_graph)
    _create_or_adopt(
        ogm, service_class, service_iri, {str(SVC.isServiceOf): [_ref(resource_iri)]}, named_graph
    )
    _set(
        ogm,
        service_iri,
        {
            str(SVC.hasAddress): [str(address)],
            str(SVC.hasLastHeartbeat): [datetime.now(timezone.utc).isoformat()],
        },
        named_graph,
    )


def register_workflow(
    ogm: "OGM",
    *,
    resource_iri: IRI,
    service_iri: IRI,
    workflow_iri: IRI,
    workflow_class: IRI,
    capability_iri: IRI,
    capability_class: IRI,
    endpoint: str,
    named_graph: Optional[IRI] = None,
) -> None:
    """Register a Workflow instance, its Capability instance, and their links, in one commit.

    The Workflow owns ``svc:isWorkflowOf`` (-> service) and ``svc:hasEndpoint``. The Capability
    owns ``svc:realizedByWorkflow`` (-> workflow). The resource links to the Capability it
    *provides* (``cfc:hasCapability``, Resource -> Capability). The Workflow with its link and its
    endpoint, the Capability with its link, and the resource's link arrive in one ``OGM.commit``
    (:func:`_write_exposure`), so no reader sees the Workflow without its Capability. Raises
    OntologyGroundTruthError if the classes are not the required subclasses; nothing is written
    then.

    This writer reads no holder of the Capability: when the Capability is there, its link is set
    to this Workflow, whichever instance it led to. A starting instance registers through
    :func:`register_exposures` instead, which refuses a Capability that another live instance
    realizes.
    """
    item = Exposure(
        capability_iri, capability_class, WORKFLOW, workflow_iri, workflow_class, endpoint
    )
    _register_unchecked(ogm, resource_iri, service_iri, item, named_graph)


def register_state_property(
    ogm: "OGM",
    *,
    resource_iri: IRI,
    service_iri: IRI,
    state_property_iri: IRI,
    state_property_class: IRI,
    capability_iri: IRI,
    capability_class: IRI,
    endpoint: str,
    named_graph: Optional[IRI] = None,
) -> None:
    """Register a StateProperty instance, its Capability instance, and their links, in one commit.

    The StateProperty owns ``svc:isStatePropertyOf`` (-> service) and ``svc:hasEndpoint``. The
    Capability owns ``svc:providedByStateProperty`` (-> state property). The resource links to
    the Capability it *provides* (``cfc:hasCapability``). All of them arrive in one
    ``OGM.commit``, as in :func:`register_workflow`. Only the stable endpoint receives write. The
    live value receives no persist.

    Like :func:`register_workflow`, this writer reads no holder of the Capability; a starting
    instance registers through :func:`register_exposures`.
    """
    item = Exposure(
        capability_iri,
        capability_class,
        STATE_PROPERTY,
        state_property_iri,
        state_property_class,
        endpoint,
    )
    _register_unchecked(ogm, resource_iri, service_iri, item, named_graph)


def _register_unchecked(
    ogm: "OGM",
    resource_iri: IRI,
    service_iri: IRI,
    item: "Exposure",
    named_graph: Optional[IRI],
) -> None:
    """Write one exposure in one commit, with no read of the Capability's holder.

    A ``PreconditionFailedError`` (an individual that another write created or changed after the
    read) leads to one read again and one more commit; a second one propagates unchanged. Any
    other refusal of the store propagates at once, and nothing of the commit is written.
    """
    _assert_classes(ogm, [item], named_graph)
    iris = [item.iri, item.capability]
    for attempt in (1, 2):
        try:
            _write_exposure(
                ogm,
                resource_iri=resource_iri,
                service_iri=service_iri,
                capability_iri=item.capability,
                items=[item],
                stored=_stored(ogm, iris, named_graph),
                named_graph=named_graph,
            )
        except PreconditionFailedError:
            if attempt == 2:
                raise
            continue
        return


# --------------------------------------------------------------------------- #
# The startup's registration of what an instance exposes: one Capability, one live realizer.
# --------------------------------------------------------------------------- #


WORKFLOW = "workflow"
STATE_PROPERTY = "state"


class _Kind(NamedTuple):
    """What a kind of exposure is in the service ontology."""

    #: The class that its class must be or specialize.
    base: IRI
    #: Its own link to its Service.
    owner: IRI
    #: The Capability's link to it.
    link: IRI


_KINDS = {
    WORKFLOW: _Kind(SVC.Workflow, SVC.isWorkflowOf, SVC.realizedByWorkflow),
    STATE_PROPERTY: _Kind(SVC.StateProperty, SVC.isStatePropertyOf, SVC.providedByStateProperty),
}


class Exposure(NamedTuple):
    """One Workflow or StateProperty that an instance exposes, and the Capability it realizes."""

    #: The Capability's IRI (:func:`mint_capability_iri`).
    capability: IRI
    #: The Capability's class.
    capability_class: IRI
    #: :data:`WORKFLOW` or :data:`STATE_PROPERTY`.
    kind: str
    #: The IRI of the Workflow or the StateProperty.
    iri: IRI
    #: Its class.
    class_iri: IRI
    #: Its endpoint.
    endpoint: str


class _Holder(NamedTuple):
    """A Service that a Capability leads to now, through a Workflow or a StateProperty."""

    service: IRI
    address: Optional[str]
    #: Its last ``svc:hasLastHeartbeat``, as the store gives it, or None.
    heartbeat: Optional[datetime]
    live: bool
    #: The Capability's links through which it leads to this Service.
    links: frozenset[IRI]


def _cutoff(now: datetime, max_age_seconds: float) -> str:
    """The oldest heartbeat that is live at ``now``, as a SPARQL ``xsd:dateTime`` literal."""
    return (
        f'"{(now - timedelta(seconds=max_age_seconds)).isoformat()}"'
        "^^<http://www.w3.org/2001/XMLSchema#dateTime>"
    )


def _holders(
    ogm: "OGM",
    capability_iri: IRI,
    staleness_threshold: float,
    now: datetime,
) -> list[_Holder]:
    """The Services that ``capability_iri`` leads to now, each with its liveness, in one read.

    A holder is the Service of a Workflow that realizes the Capability (``svc:realizedByWorkflow``)
    or of a StateProperty that provides it (``svc:providedByStateProperty``). Live is the rule of
    the watchdog's sweep and of the startup reconstruction: an ``svc:hasAddress`` and an
    ``svc:hasLastHeartbeat`` no older than ``staleness_threshold`` before ``now``.
    """
    branches = "\n        UNION\n        ".join(
        f"{{ <{capability_iri}> <{kind.link}> ?item . ?item <{kind.owner}> ?svc . "
        f"BIND(<{kind.link}> AS ?link) }}"
        for kind in _KINDS.values()
    )
    sparql = f"""
    SELECT ?svc ?link ?addr ?hb ?live WHERE {{
        {branches}
        OPTIONAL {{ ?svc <{SVC.hasAddress}> ?addr . }}
        OPTIONAL {{ ?svc <{SVC.hasLastHeartbeat}> ?hb . }}
        BIND(BOUND(?addr) && BOUND(?hb) && ?hb >= {_cutoff(now, staleness_threshold)} AS ?live)
    }}
    """
    found: dict[IRI, _Holder] = {}
    for row in _bindings(ogm.db.query(sparql, convert_bindings=True)):
        service = IRI(str(row["svc"]))
        address = row.get("addr")
        heartbeat = row.get("hb")
        holder = _Holder(
            service,
            str(address) if address is not None else None,
            heartbeat if isinstance(heartbeat, datetime) else None,
            row.get("live") is True,
            frozenset({IRI(str(row["link"]))}),
        )
        earlier = found.get(service)
        if earlier is not None:
            # Several rows of one Service (two links, or two heartbeats): a live row wins, and
            # the links add up.
            best = holder if holder.live and not earlier.live else earlier
            holder = best._replace(links=earlier.links | holder.links)
        found[service] = holder
    return list(found.values())


def _live_other(holders: Sequence[_Holder], own_service: IRI) -> Optional[_Holder]:
    """The first live holder that is not ``own_service``, or None."""
    return next((h for h in holders if h.live and h.service != own_service), None)


def _refusal(
    *,
    resource_iri: IRI,
    capability_iri: IRI,
    holder: _Holder,
    staleness_threshold: float,
) -> CapabilityAlreadyRealizedError:
    """The refusal: ``holder``, a live Service of another instance, realizes the Capability."""
    heartbeat = holder.heartbeat
    stale_from = (
        heartbeat + timedelta(seconds=staleness_threshold) if heartbeat is not None else None
    )

    def shown(value: Optional[datetime]) -> str:
        return value.isoformat() if value is not None else "unknown"

    message = (
        f"This instance does not start: the Capability {capability_iri} of the Resource "
        f"{resource_iri} is already realized by the live instance at {holder.address} "
        f"(Service {holder.service}), whose last heartbeat is {shown(heartbeat)}. One Capability "
        "has at most one live realizer. This instance counts that registration as stale from "
        f"{shown(stale_from)} (its last heartbeat plus this instance's staleness threshold of "
        f"{staleness_threshold:g} s). Either stop one of the two instances; or, if the instance "
        f"at {holder.address} has crashed, start this one again after {shown(stale_from)}, and "
        "it takes the Capability over."
    )
    return CapabilityAlreadyRealizedError(
        message,
        resource=str(resource_iri),
        capability=str(capability_iri),
        holder=str(holder.service),
        holder_address=str(holder.address),
        holder_heartbeat=heartbeat,
        stale_from=stale_from,
    )


def _grouped(exposures: Sequence[Exposure]) -> list[tuple[IRI, list[Exposure]]]:
    """``exposures`` by Capability, in one fixed order: by Capability IRI, then kind, then IRI.

    Two instances that start together contest their shared Capabilities in this order, so one
    cannot win the first while the other wins the second.
    """
    groups: dict[IRI, list[Exposure]] = {}
    for exposure in sorted(exposures, key=lambda e: (str(e.capability), e.kind, str(e.iri))):
        groups.setdefault(exposure.capability, []).append(exposure)
    return list(groups.items())


def check_capabilities(
    ogm: "OGM",
    *,
    resource_iri: IRI,
    service_iri: IRI,
    exposures: Sequence[Exposure],
    staleness_threshold: float,
    now: Optional[datetime] = None,
) -> dict[IRI, list[_Holder]]:
    """Read the holder of each Capability that ``exposures`` realize, before the first write.

    Returns each Capability's holders, which :func:`register_exposures` takes as its first
    decision. Nothing is written.

    Raises:
        CapabilityAlreadyRealizedError: If a live Service other than ``service_iri`` realizes
            one of them.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    read: dict[IRI, list[_Holder]] = {}
    for capability, _ in _grouped(exposures):
        holders = _holders(ogm, capability, staleness_threshold, now)
        other = _live_other(holders, service_iri)
        if other is not None:
            raise _refusal(
                resource_iri=resource_iri,
                capability_iri=capability,
                holder=other,
                staleness_threshold=staleness_threshold,
            )
        read[capability] = holders
    return read


def register_exposures(
    ogm: "OGM",
    *,
    resource_iri: IRI,
    service_iri: IRI,
    exposures: Sequence[Exposure],
    staleness_threshold: float,
    holders: Optional[dict[IRI, list[_Holder]]] = None,
    now: Optional[Callable[[], datetime]] = None,
    named_graph: Optional[IRI] = None,
) -> None:
    """Register what an instance exposes, so that one Capability has at most one live realizer.

    The startup calls it once, directly after :func:`register_service`. It checks the classes of
    every exposure first, and then takes the Capabilities in one fixed order (:func:`_grouped`).
    Each Capability arrives in one commit with the instance's own Workflows and StateProperties
    of it (:func:`_write_exposure`): each of them with its link to the Service and its endpoint,
    the Capability with its links to them, and the resource's ``cfc:hasCapability`` link. So no
    reader sees a Workflow or StateProperty of this instance without its Capability. Whether
    the commit is written at all is decided by this table of the Capability's holder:

    - none: it is written;
    - this instance's own Service, or a Service that is not live: it is written, with an
      ``Update`` of the Capability (a take-back, or a take-over, which logs one WARNING that names
      the Services it took over from);
    - another live Service: a refusal.

    Each individual of the commit is a ``Create`` if the store holds no statement about it, and an
    ``Update`` otherwise, read before the commit. So a restart, and a start that finds only part
    of an exposure from an earlier run, complete each exposure in one commit as well.

    ``holders`` is :func:`check_capabilities`'s read; without it, each holder is read here first.
    After an ``Update`` of a Capability, the holder is read once more: a take-over of another
    instance that landed after it leaves this instance refusing itself. When the store refuses a
    commit with ``PreconditionFailedError``, the holder and the individuals are read again and the
    table decides once more; a second refusal is not a race, and propagates.

    Each refusal after the first write deregisters this instance's own Service the way a clean
    stop does (:func:`deregister_service`, endpoints first, address last) and then raises.

    Raises:
        OntologyGroundTruthError: If a class is not the required subclass. No exposure is
            written.
        CapabilityAlreadyRealizedError: If a live Service of another instance realizes one of
            the Capabilities.
        PreconditionFailedError: The second refusal of one Capability's commit, unchanged.
    """
    clock = now if now is not None else (lambda: datetime.now(timezone.utc))
    _assert_classes(ogm, exposures, named_graph)
    try:
        for capability, items in _grouped(exposures):
            known = holders.get(capability) if holders is not None else None
            if known is None:
                known = _holders(ogm, capability, staleness_threshold, clock())
            _claim_capability(
                ogm,
                resource_iri=resource_iri,
                service_iri=service_iri,
                capability_iri=capability,
                items=items,
                holders=known,
                staleness_threshold=staleness_threshold,
                clock=clock,
                named_graph=named_graph,
            )
    except (CapabilityAlreadyRealizedError, PreconditionFailedError):
        try:
            deregister_service(ogm, service_iri, named_graph=named_graph)
        except Exception:
            logger.error(
                "The refused instance %s did not finish deregistering; its address stays until "
                "a watchdog sweeps it",
                service_iri,
                exc_info=True,
            )
        raise


def _assert_classes(
    ogm: "OGM", exposures: Sequence[Exposure], named_graph: Optional[IRI]
) -> None:
    """Check the class of each Workflow or StateProperty and of its Capability."""
    for item in exposures:
        assert_class_registered(ogm, item.class_iri, _KINDS[item.kind].base, named_graph)
        assert_class_registered(ogm, item.capability_class, CFC.Capability, named_graph)


def _stored(ogm: "OGM", iris: Sequence[IRI], named_graph: Optional[IRI]) -> set[IRI]:
    """The IRIs of ``iris`` about which the store holds an explicit statement.

    This is the rule of the store's refusal of a ``Create``: an IRI with any statement about it
    is not new. One with statements and no class gets an ``Update``, which the OGM refuses with
    ``OGMNotFoundError``: broken data in the store, which a repair of the store fixes.
    """
    return {
        iri
        for iri in iris
        if ogm.db.triples_get(sub=iri, include_implicit=False, named_graph=named_graph)
    }


def _write_exposure(
    ogm: "OGM",
    *,
    resource_iri: IRI,
    service_iri: IRI,
    capability_iri: IRI,
    items: Sequence[Exposure],
    stored: set[IRI],
    named_graph: Optional[IRI],
) -> None:
    """Write ``items`` and their Capability in one ``OGM.commit``.

    The commit carries, for each Workflow or StateProperty of ``items``, its link to the Service
    and its endpoint; the Capability with its links to them; and one ``Append`` of the resource's
    ``cfc:hasCapability`` link. An individual in ``stored`` gets an ``Update`` of these
    properties, any other a ``Create``. The store admits the commit or refuses it as a whole, so
    no reader sees one of them without the others.

    The resource's link is a multi-valued property, declared in Core (domain ``cfc:Resource``,
    range ``cfc:Capability``). The ``Append`` adds it and removes none, so a sibling instance's
    concurrent registration on the same resource keeps its link. The OGM reads the resource's
    class to check the property, and none of the property's stored values: the property has no
    maximum. The resource must be in the store already.

    Raises:
        OGMNotFoundError: If the store does not hold the resource (from ``kapps_ogm.utils.errors``).
            Nothing is written.
        PreconditionFailedError: If the store holds an individual to create, or another write
            changed one to update after the read. Nothing is written.
        TripleStoreResponseError: If the store refuses the commit, a SHACL refusal included. It
            propagates unchanged, and nothing of the commit is written.
    """

    def change(class_iri: IRI, iri: IRI, data: dict) -> Any:
        if iri in stored:
            return Update(instance_iri=iri, data=data)
        return Create(
            class_iri=class_iri,
            data=data,
            instance_iri=iri,
            class_scope=_scope_naming(None, data),
        )

    changes = []
    for item in items:
        data = {
            str(_KINDS[item.kind].owner): [_ref(service_iri)],
            str(SVC.hasEndpoint): [str(item.endpoint)],
        }
        changes.append(change(item.class_iri, item.iri, data))
    changes.append(change(items[0].capability_class, capability_iri, _link_data(items)))
    changes.append(
        Append(instance_iri=resource_iri, property_iri=CFC.hasCapability, value=capability_iri)
    )
    ogm.commit(changes=changes, named_graph=named_graph)


def _link_data(items: Sequence[Exposure]) -> dict:
    """The Capability's own links to the Workflows and StateProperties in ``items``."""
    data: dict = {}
    for item in items:
        data.setdefault(str(_KINDS[item.kind].link), []).append(_ref(item.iri))
    return data


def _claim_capability(
    ogm: "OGM",
    *,
    resource_iri: IRI,
    service_iri: IRI,
    capability_iri: IRI,
    items: Sequence[Exposure],
    holders: list[_Holder],
    staleness_threshold: float,
    clock: Callable[[], datetime],
    named_graph: Optional[IRI],
) -> None:
    """Write one Capability with its exposures by the table of :func:`register_exposures`."""

    def refuse_if_held(read: list[_Holder]) -> None:
        other = _live_other(read, service_iri)
        if other is not None:
            raise _refusal(
                resource_iri=resource_iri,
                capability_iri=capability_iri,
                holder=other,
                staleness_threshold=staleness_threshold,
            )

    refuse_if_held(holders)
    iris = [capability_iri, *(item.iri for item in items)]
    stored = _stored(ogm, iris, named_graph)
    for attempt in (1, 2):
        try:
            _write_exposure(
                ogm,
                resource_iri=resource_iri,
                service_iri=service_iri,
                capability_iri=capability_iri,
                items=items,
                stored=stored,
                named_graph=named_graph,
            )
        except PreconditionFailedError:
            if attempt == 2:
                raise
            # Another write created or changed one of the individuals after the read: read the
            # holder and the individuals again, and decide once more.
            holders = _holders(ogm, capability_iri, staleness_threshold, clock())
            refuse_if_held(holders)
            stored = _stored(ogm, iris, named_graph)
            continue
        break
    if capability_iri not in stored:
        return
    # The OGM checks an Update against its own read inside the commit only, so a take-over by
    # another instance can land right before or after it. The read after the commit sees it.
    refuse_if_held(_holders(ogm, capability_iri, staleness_threshold, clock()))
    # Only a Service whose link this write replaced was taken over from. A stale link of the
    # other kind stays in place, as it was, and is no take-over.
    written = {IRI(link) for link in _link_data(items)}
    previous = sorted(
        str(h.service) for h in holders if h.service != service_iri and h.links & written
    )
    if previous:
        logger.warning(
            "The Capability %s of the Resource %s was realized by %s, which is not live; this "
            "instance (%s) takes it over",
            capability_iri,
            resource_iri,
            ", ".join(previous),
            service_iri,
        )


# --------------------------------------------------------------------------- #
# Deregistration (remove reachability via OGM, find targets via reads).
# --------------------------------------------------------------------------- #


def deregister_service(
    ogm: "OGM", service_iri: IRI, named_graph: Optional[IRI] = None
) -> None:
    """Remove a Service reachability (address + all workflow/state endpoints).

    Endpoints receive removal via ``OGM.commit`` (the validated write path). The workflows/state-
    properties to clear are found via a read on the instance-owned inverse (``svc:isWorkflowOf``
    / ``svc:isStatePropertyOf``). Structural triples and rdf:type are preserved.

    The endpoints go first and the address last, each in its own commit. A deregistration that
    stops halfway (a store error) leaves the address, so the Service is still found by its address
    (as stale, by the watchdog) and the next deregistration finishes it; the reverse order would
    leave endpoints that nothing finds again.
    """
    db = ogm.db
    for wf_node, _, _ in db.triples_get(
        pred=SVC.isWorkflowOf, obj=service_iri, named_graph=named_graph
    ):
        _set(ogm, _named(wf_node, "Workflow", service_iri), {str(SVC.hasEndpoint): []}, named_graph)

    for sp_node, _, _ in db.triples_get(
        pred=SVC.isStatePropertyOf, obj=service_iri, named_graph=named_graph
    ):
        _set(
            ogm, _named(sp_node, "StateProperty", service_iri), {str(SVC.hasEndpoint): []}, named_graph
        )

    _set(ogm, service_iri, {str(SVC.hasAddress): []}, named_graph)


def _named(node: IRI | BNode, kind: str, service_iri: IRI) -> IRI:
    """``node`` as the IRI that names a Service's Workflow or StateProperty.

    The store can answer a subject as a blank node, and the OGM's commit takes an IRI. A blank
    node there is broken data in the store.

    Raises:
        DeploymentValueError: If ``node`` is a blank node.
    """
    if isinstance(node, BNode):
        raise DeploymentValueError(
            f"The store holds a blank node as a {kind} of Service {service_iri}, where an IRI is "
            "required. Repair the data in the store."
        )
    return node


# --------------------------------------------------------------------------- #
# Liveness: heartbeat (per-service) and watchdog sweep (centralized).
# --------------------------------------------------------------------------- #


def update_heartbeat(
    ogm: "OGM",
    service_iri: IRI,
    timestamp: Optional[datetime] = None,
    named_graph: Optional[IRI] = None,
) -> None:
    """Refresh a Service ``svc:hasLastHeartbeat`` via OGM.commit (atomic replace)."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    _set(ogm, service_iri, {str(SVC.hasLastHeartbeat): [timestamp.isoformat()]}, named_graph)


def find_stale_services(
    ogm: "OGM",
    max_age_seconds: float,
    *,
    now: Optional[datetime] = None,
    named_graph: Optional[IRI] = None,
) -> list[IRI]:
    """Find reachable-but-silent Services whose heartbeat has gone stale (a read).

    A service is stale if it currently has an ``svc:hasAddress`` but either has no
    ``svc:hasLastHeartbeat`` or its latest heartbeat is older than ``max_age_seconds``. Services
    are identified by ``svc:hasAddress`` presence (real services are subclass-typed, so
    ``rdf:type svc:Service`` would not match).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(seconds=max_age_seconds)).isoformat()
    sparql = f"""
    SELECT DISTINCT ?s WHERE {{
        ?s <{SVC.hasAddress}> ?addr .
        OPTIONAL {{ ?s <{SVC.hasLastHeartbeat}> ?hb . }}
        FILTER ( !bound(?hb) || ?hb < "{cutoff_iso}"^^<http://www.w3.org/2001/XMLSchema#dateTime> )
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    return [IRI(str(b["s"])) for b in bindings]


def sweep_stale_services(
    ogm: "OGM",
    max_age_seconds: float,
    *,
    now: Optional[datetime] = None,
    named_graph: Optional[IRI] = None,
) -> list[IRI]:
    """Deregister every stale Service. Fail the Operations of its resource that are stranded.

    Alongside removal of a dead resource reachability, the watchdog marks ``failed`` the
    ``queued``/``running`` Operations of that resource that no surviving Service can take. Work
    addressed to a resource that will never return does not hang forever. Returns the Service IRIs
    it deregistered; one skipped on a store error is left for the next sweep and not returned.

    The decision is per Operation (:func:`find_stranded_operations`), live meaning a Service
    with an ``svc:hasAddress`` and an ``svc:hasLastHeartbeat`` within ``max_age_seconds`` of ``now``:
    a ``queued`` Operation is stranded when its Capability is realized by no live Service of
    its resource; a ``running`` one when the Service of the Workflow that claimed it
    (``svc:executedByWorkflow``) is not live, or no claimer is recorded. The stale Service is
    not live by definition. So a stale monitor does not fail the queue of a live controller
    sibling that realizes its Capability, a live monitor that realizes no Workflow does not
    shield the queue of a dead controller, and a dead claimer's running Operation is failed
    even when a live sibling realizes its Capability (that sibling would never run it). The
    stale Service is deregistered either way. The startup reconstruction of a queue
    (``SemanticMiddleware._reconstruct_queue``) decides its running Operations with the same
    predicate.

    A second pass follows the first, for a Resource whose Services all stopped cleanly (they
    removed their address, so the first pass never finds them): when the newest heartbeat of the
    Resource's Services is older than ``max_age_seconds``, its ``queued`` and ``running``
    Operations are failed as well (:func:`find_unserved_resources`). A Service that restarts
    within the threshold re-registers with a fresh heartbeat, so its queue survives; a Resource
    with a live Service is left to the first pass.
    """
    return _sweep_stale_services(ogm, max_age_seconds, now=now, named_graph=named_graph)


def _sweep_stale_services(
    ogm: "OGM",
    max_age_seconds: float,
    *,
    now: Optional[datetime] = None,
    named_graph: Optional[IRI] = None,
    failed: Callable[[IRI], None] = lambda operation_iri: None,
) -> list[IRI]:
    """The sweep of :func:`sweep_stale_services`. It calls ``failed`` with each Operation that it
    marked ``failed``, after that commit, so the watchdog notifies the Operation's issuer.

    A store error is isolated to the Service or the Operation it hit: it is logged as one warning
    that names them, and the sweep goes on with the next. A Service whose stranded Operations
    were not all failed keeps its address, so the next sweep finds it again and finishes; it is
    left out of the returned list. Only the first read (the stale Services) raises a store error.
    Any other error is raised.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    stale = find_stale_services(ogm, max_age_seconds, now=now, named_graph=named_graph)
    swept: list[IRI] = []
    for service_iri in stale:
        try:
            resource_iri = _resource_of_service(ogm, service_iri, named_graph=named_graph)
            stranded = (
                find_stranded_operations(
                    ogm,
                    resource_iri,
                    [OperationStatus.QUEUED, OperationStatus.RUNNING],
                    max_age_seconds,
                    now=now,
                    named_graph=named_graph,
                )
                if resource_iri is not None
                else []
            )
        except _LIVENESS_STORE_ERRORS as error:
            logger.warning(
                "The sweep skipped the stale Service %s, whose stranded Operations were not "
                "read; the next sweep tries again: %r",
                service_iri,
                error,
            )
            continue
        all_failed = True
        for op_iri in stranded:
            try:
                mark_operation_failed(
                    ogm,
                    op_iri,
                    "stranded operation swept by watchdog: no live service of the resource "
                    "will finish it",
                    named_graph=named_graph,
                )
            except _LIVENESS_STORE_ERRORS as error:
                all_failed = False
                logger.warning(
                    "The sweep did not fail the stranded Operation %s of the stale Service %s; "
                    "the next sweep tries again: %r",
                    op_iri,
                    service_iri,
                    error,
                )
                continue
            failed(op_iri)
        if not all_failed:
            continue
        try:
            deregister_service(ogm, service_iri, named_graph=named_graph)
        except _LIVENESS_STORE_ERRORS as error:
            logger.warning(
                "The sweep did not finish deregistering the stale Service %s; the next sweep "
                "tries again: %r",
                service_iri,
                error,
            )
            continue
        swept.append(service_iri)
    _fail_unserved_resources(ogm, max_age_seconds, now=now, named_graph=named_graph, failed=failed)
    return swept


def find_unserved_resources(
    ogm: "OGM",
    max_age_seconds: float,
    *,
    now: Optional[datetime] = None,
    named_graph: Optional[IRI] = None,
) -> list[IRI]:
    """Find the Resources that no Service serves any more, and that still hold unfinished work (a read).

    A Resource qualifies when it has a Service individual (``svc:isServiceOf``) with a heartbeat,
    none of its Services has a heartbeat within ``max_age_seconds`` before ``now`` (so none is
    live, and a cleanly stopped Service that left its address and endpoints is not mistaken for
    a restart), and at least one of its Operations is ``queued`` or ``running``. The newest
    heartbeat of the Resource counts. A Resource that never had a Service individual does not
    qualify.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    sparql = f"""
    SELECT DISTINCT ?r WHERE {{
        ?svc <{SVC.isServiceOf}> ?r .
        ?svc <{SVC.hasLastHeartbeat}> ?anyhb .
        FILTER NOT EXISTS {{
            ?sibling <{SVC.isServiceOf}> ?r .
            ?sibling <{SVC.hasLastHeartbeat}> ?freshhb .
            FILTER(?freshhb >= {_cutoff(now, max_age_seconds)})
        }}
        ?op <{CFC.implementsCapability}> ?cap .
        ?r <{CFC.hasCapability}> ?cap .
        ?op <{SVC.hasOperationStatus}> ?st .
        FILTER(?st IN ("{OperationStatus.QUEUED}", "{OperationStatus.RUNNING}"))
    }}
    """
    return [IRI(str(b["r"])) for b in _bindings(ogm.db.query(sparql, convert_bindings=True))]


def _fail_unserved_resources(
    ogm: "OGM",
    max_age_seconds: float,
    *,
    now: datetime,
    named_graph: Optional[IRI],
    failed: Callable[[IRI], None],
) -> None:
    """The second pass of the sweep: fail the unfinished Operations of each unserved Resource.

    The first pass starts from Services that still have an address. A Service that stopped
    cleanly removed it, so its Resource is found here instead (:func:`find_unserved_resources`).
    A store error is logged as one warning that names the Resource, and the pass goes on with the
    next; the next sweep tries the Resource again.
    """
    try:
        resources = find_unserved_resources(
            ogm, max_age_seconds, now=now, named_graph=named_graph
        )
    except _LIVENESS_STORE_ERRORS as error:
        logger.warning(
            "The sweep did not read the Resources that no Service serves any more; the next "
            "sweep tries again: %r",
            error,
        )
        return
    for resource_iri in resources:
        try:
            stranded = find_stranded_operations(
                ogm,
                resource_iri,
                [OperationStatus.QUEUED, OperationStatus.RUNNING],
                max_age_seconds,
                now=now,
                named_graph=named_graph,
            )
            for op_iri in stranded:
                mark_operation_failed(
                    ogm,
                    op_iri,
                    "stranded operation swept by watchdog: no live service of the resource "
                    "will finish it",
                    named_graph=named_graph,
                )
                failed(op_iri)
        except _LIVENESS_STORE_ERRORS as error:
            logger.warning(
                "The sweep did not fail the Operations of the Resource %s, which no Service "
                "serves any more; the next sweep tries again: %r",
                resource_iri,
                error,
            )


# --------------------------------------------------------------------------- #
# Event-trigger dispatch & operation queue.
# --------------------------------------------------------------------------- #


EVENT_TRIGGER_WORKFLOW_NAME = "event_trigger"
"""Reserved built-in Workflow name for the receiver event-trigger REST endpoint."""


def build_event_trigger_url(address: str) -> str:
    """Build the receiver event-trigger endpoint (``POST /workflows/event_trigger/execute``)."""
    return f"{address.rstrip('/')}/workflows/{EVENT_TRIGGER_WORKFLOW_NAME}/execute"


def mint_operation_iri(operation_class: IRI) -> IRI:
    """Mint a unique Operation instance IRI under the operation-class namespace.

    Operations are per-dispatch. Each dispatch mints a fresh IRI (unlike the deterministic
    registration IRIs).
    """
    return IRI(f"{operation_class}_op_{uuid.uuid4().hex[:12]}")


def create_operation(
    ogm: "OGM",
    *,
    operation_iri: IRI,
    operation_class: IRI,
    capability_iri: IRI,
    status: str = OperationStatus.QUEUED,
    data: Optional[dict] = None,
    named_graph: Optional[IRI] = None,
) -> None:
    """Create an Operation individual for dispatch. Address it to a target Capability.

    The whole Operation is written in ONE ``OGM.commit``: its ``rdf:type``, its single-valued
    ``cfc:implementsCapability`` link, ``svc:hasOperationStatus``, and ``data``, whose properties
    the Operation's class must declare: kapps_ogm refuses a property that the class does not
    declare, with ``OGMDataError``. Its class scope names every property it sets, so none lies
    outside the scope.
    ``issue_operation()`` does not call this: it creates the Operation with ``mw.create()`` under the
    operation scope. This writer is kept for existing callers, such as tests that seed an
    Operation. The IRI must be new: the store must hold no statement about it yet, or the
    store's refusal ``PreconditionFailedError`` is raised, and nothing is written. The
    Resource->Capability ``cfc:hasCapability`` link written at *registration* is a separate,
    multi-valued property of the resource, written through the OGM as an ``Append`` in the
    commit of the Capability and its Workflow by ``_write_exposure``.
    """
    create_data: dict = {
        **_implements(IRI(capability_iri)),
        str(SVC.hasOperationStatus): [status],
        **(data or {}),
    }
    creation = Create(
        class_iri=IRI(operation_class),
        instance_iri=IRI(operation_iri),
        data=create_data,
        class_scope=_scope_naming(None, create_data),
    )
    ogm.commit(changes=[creation], named_graph=named_graph)


def resolve_dispatch_target(
    ogm: "OGM",
    capability_class: IRI,
    target_resource: Optional[IRI] = None,
    named_graph: Optional[IRI] = None,
) -> Tuple[IRI, IRI, str]:
    """Resolve a reachable receiver for a Capability *class*.

    Bind a Capability *instance* of ``capability_class`` realized by a Workflow whose Service
    has a live ``svc:hasAddress``: ``Capability --realizedByWorkflow--> Workflow --isWorkflowOf-->
    Service --hasAddress``. If ``target_resource`` is given, the Service is pinned to that resource
    (``svc:isServiceOf``). The bound Capability instance is what the dispatched Operation will
    ``cfc:implementsCapability``.

    Returns:
        Tuple of (capability_instance_iri, service_iri, service_address).

    Raises:
        OperationReceiverUnavailableError: If a Workflow realizes a Capability of the class, but
            no Service of it has an address now (Retry). A second read, made only when the
            first finds no live Service, tells this case from the next.
        CapabilityNotRealizedError: If no Workflow realizes a Capability of the class (Repair).
    """
    resource_filter = ""
    if target_resource is not None:
        resource_filter = f"\n        ?svc <{SVC.isServiceOf}> <{target_resource}> ."
    realized = f"""
        ?cap a <{capability_class}> .
        ?cap <{SVC.realizedByWorkflow}> ?wf .
        ?wf <{SVC.isWorkflowOf}> ?svc ."""
    sparql = f"""
    SELECT ?cap ?svc ?addr WHERE {{{realized}
        ?svc <{SVC.hasAddress}> ?addr .{resource_filter}
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    if not bindings:
        message = (
            f"Capability class {capability_class} cannot be resolved to any reachable "
            "Service (no Capability of that class is realized by a Workflow whose Service "
            "has a current svc:hasAddress)."
        )
        answer = ogm.db.query(f"ASK {{{realized}{resource_filter}\n    }}")
        if isinstance(answer, dict) and answer.get("boolean", False):
            raise OperationReceiverUnavailableError(message)
        raise CapabilityNotRealizedError(message)
    binding = bindings[0]
    return IRI(str(binding["cap"])), IRI(str(binding["svc"])), str(binding["addr"])


# ---- Pull-and-run: status transitions + terminal provenance ----


def resolve_operation_workflow(
    ogm: "OGM", operation_iri: IRI, named_graph: Optional[IRI] = None
) -> IRI:
    """Resolve an Operation to the Workflow that realizes its Capability, WITHOUT require of a
    live endpoint. This is the provenance resolver used by
    pull-and-run so `executedByWorkflow` receives record even if the workflow `svc:hasEndpoint` was
    deregistered mid-run.

    Raises:
        CapabilityNotRealizedError: If no Workflow realizes the Operation Capability (Repair). It
            is an ``OperationResolutionError``.
    """
    return _resolve_claim(ogm, operation_iri, named_graph).workflow


class _ClaimRead(NamedTuple):
    """What the read before a claim finds about the Operation."""

    #: The Workflow that realizes the Operation's Capability, for the provenance.
    workflow: IRI
    #: The Operation's issuer (``svc:issuedBy``), or None when it has none.
    issuer: Optional[IRI]
    #: The Operation's Capability (``cfc:implementsCapability``), or None when not read.
    capability: Optional[IRI] = None


def _resolve_claim(
    ogm: "OGM", operation_iri: IRI, named_graph: Optional[IRI] = None
) -> _ClaimRead:
    """The read before a claim, in one query: the Workflow that realizes the Operation's
    Capability, as :func:`resolve_operation_workflow` finds it, and the Operation's issuer, whom
    the claimer notifies after the claim and after the end.

    Raises:
        CapabilityNotRealizedError: If no Workflow realizes the Operation Capability (Repair). It
            is an ``OperationResolutionError``.
    """
    sparql = f"""
    SELECT ?wf ?issuer ?cap WHERE {{
        <{operation_iri}> <{CFC.implementsCapability}> ?cap .
        ?cap <{SVC.realizedByWorkflow}> ?wf .
        OPTIONAL {{ <{operation_iri}> <{SVC.issuedBy}> ?issuer . }}
    }}
    """
    bindings = _bindings(ogm.db.query(sparql, convert_bindings=True))
    if not bindings:
        raise CapabilityNotRealizedError(
            f"Operation {operation_iri} cannot be resolved to any Workflow "
            "(no Capability realized by a Workflow)."
        )
    binding = bindings[0]
    issuer = binding.get("issuer")
    capability = binding.get("cap")
    return _ClaimRead(
        IRI(str(binding["wf"])),
        IRI(str(issuer)) if issuer is not None else None,
        IRI(str(capability)) if capability is not None else None,
    )


def _issuers(ogm: "OGM", operation_iris: Sequence[IRI]) -> dict[IRI, IRI]:
    """The issuer (``svc:issuedBy``) of each of ``operation_iris`` that has one, in one read."""
    if not operation_iris:
        return {}
    values = " ".join(f"<{iri}>" for iri in operation_iris)
    sparql = f"""
    SELECT ?op ?issuer WHERE {{
        VALUES ?op {{ {values} }}
        ?op <{SVC.issuedBy}> ?issuer .
    }}
    """
    bindings = _bindings(ogm.db.query(sparql, convert_bindings=True))
    return {IRI(str(b["op"])): IRI(str(b["issuer"])) for b in bindings}


def _operation_statuses(ogm: "OGM", operation_iri: IRI) -> frozenset[str]:
    """Every ``svc:hasOperationStatus`` of an Operation that the store holds, in one read. One, as
    a rule; none when the store holds no status for it."""
    sparql = f"""
    SELECT ?status WHERE {{
        <{operation_iri}> <{SVC.hasOperationStatus}> ?status .
    }}
    """
    bindings = _bindings(ogm.db.query(sparql, convert_bindings=True))
    return frozenset(str(b["status"]) for b in bindings)


class _NotifiedRead(NamedTuple):
    """What the middleware reads about a notified Operation to tell feedback from work: an
    instance without a notification handler reads it at its event trigger, and the intake reads
    it for an Operation that matches a workflow of the instance, to decide whether the workflow
    runs."""

    #: Every status of the Operation that the store holds.
    statuses: frozenset[str]
    #: The Operation's issuer, or None.
    issuer: Optional[IRI]


def _read_notified(ogm: "OGM", operation_iri: IRI) -> _NotifiedRead:
    """Read a notified Operation's status and issuer in one query, and check that the store
    holds it.

    Raises:
        ValueError: If the store holds no class for the Operation.
    """
    sparql = f"""
    SELECT ?type ?status ?issuer WHERE {{
        <{operation_iri}> a ?type .
        OPTIONAL {{ <{operation_iri}> <{SVC.hasOperationStatus}> ?status . }}
        OPTIONAL {{ <{operation_iri}> <{SVC.issuedBy}> ?issuer . }}
    }}
    """
    bindings = _bindings(ogm.db.query(sparql, convert_bindings=True))
    if not bindings:
        raise ValueError(f"Could not determine class IRI for instance {operation_iri}")
    statuses = frozenset(str(b["status"]) for b in bindings if b.get("status") is not None)
    issuers = [IRI(str(b["issuer"])) for b in bindings if b.get("issuer") is not None]
    return _NotifiedRead(statuses, issuers[0] if issuers else None)


def _bindings(result: Any) -> list:
    """The rows of a SELECT's answer."""
    return result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []


def _operation_status_update(operation_iri: IRI, status: str) -> Update:
    """The kapps_ogm ``Update`` that sets `svc:hasOperationStatus` on an Operation.

    It covers any single-status transition. Nothing is written here: :func:`set_operation_status`
    commits it. ``claim_next()`` commits :func:`_claim_update` as its claim instead.
    """
    return Update(instance_iri=IRI(operation_iri), data={str(SVC.hasOperationStatus): [status]})


def _claim_update(operation_iri: IRI, executor: Optional[IRI]) -> Update:
    """The kapps_ogm ``Update`` of a claim: `svc:hasOperationStatus` ``running`` and, when the
    claimer names its Workflow, that Workflow as the executor (`svc:executedByWorkflow`).

    One change, so the status and the executor are written in one commit.
    :func:`find_stranded_operations` reads the executor to decide whether a running Operation is
    stranded. Nothing is written here: ``claim_next()`` commits it as its entry.
    """
    data: dict = {str(SVC.hasOperationStatus): [OperationStatus.RUNNING]}
    if executor is not None:
        data[str(SVC.executedByWorkflow)] = [_ref(executor)]
    return Update(instance_iri=IRI(operation_iri), data=data)


def set_operation_status(
    ogm: "OGM",
    *,
    operation_iri: IRI,
    status: str,
    named_graph: Optional[IRI] = None,
) -> None:
    """Set `svc:hasOperationStatus` on an Operation via the OGM atomic commit path.

    It writes :func:`_operation_status_update` in one ``OGM.commit``. ``claim_next()`` does not
    call this: it commits its claim (:func:`_claim_update`) through ``mw.commit()``. This writer
    is kept for existing callers.
    """
    ogm.commit(changes=[_operation_status_update(operation_iri, status)], named_graph=named_graph)


def _terminal_status_update(
    *,
    operation_iri: IRI,
    workflow_iri: IRI,
    status: str,
    result: Optional[str] = None,
    failure_state: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> Update:
    """The kapps_ogm ``Update`` of a terminal status (`done`/`failed`) with execution provenance.

    Status and provenance are one change, so one commit writes them together. An Operation is
    never terminal-without-provenance. On failure, ``failure_state`` (a JSON snapshot of the
    resource datamodel) goes into ``svc:hasFailureState`` in the same change. The failed status and
    the state that produced it are never separable. Nothing is written here: ``claim_next()``
    commits it as the exit or the error transition of ``mw.commit()``.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    data: dict = {
        str(SVC.hasOperationStatus): [status],
        str(SVC.executedByWorkflow): [_ref(workflow_iri)],
        str(SVC.hasExecutionTimestamp): [timestamp.isoformat()],
    }
    if result is not None:
        data[str(SVC.hasExecutionResult)] = [str(result)]
    if failure_state is not None:
        data[str(SVC.hasFailureState)] = [failure_state]
    return Update(instance_iri=IRI(operation_iri), data=data)


def record_terminal_status(
    ogm: "OGM",
    *,
    operation_iri: IRI,
    workflow_iri: IRI,
    status: str,
    result: Optional[str] = None,
    failure_state: Optional[str] = None,
    timestamp: Optional[datetime] = None,
    named_graph: Optional[IRI] = None,
) -> None:
    """Record terminal status (`done`/`failed`) with execution provenance in ONE commit.

    The commit writes :func:`_terminal_status_update`, so status and provenance are written
    atomically, and on failure the dumped state with them. ``claim_next()`` does not call
    this: it commits the same change through ``mw.commit()``. This writer is kept for existing
    callers.
    """
    change = _terminal_status_update(
        operation_iri=operation_iri,
        workflow_iri=workflow_iri,
        status=status,
        result=result,
        failure_state=failure_state,
        timestamp=timestamp,
    )
    ogm.commit(changes=[change], named_graph=named_graph)


# ---- Queue durability + recovery ----


def find_resource_operations(
    ogm: "OGM",
    resource_iri: IRI,
    statuses: list[str],
    named_graph: Optional[IRI] = None,
) -> list[IRI]:
    """Find a resource own Operations whose ``svc:hasOperationStatus`` is one of ``statuses``.

    Ontology-backed traversal: an Operation implements a Capability
    (``cfc:implementsCapability``). The resource provides that Capability (``cfc:hasCapability``).
    Both links persist across a restart. This works at early startup before re-registration.
    Explicitly does NOT match on IRI names.

    Args:
        ogm: The OGM instance.
        resource_iri: The resource whose queue to inspect.
        statuses: Iterable of status strings to filter (e.g. ``["queued", "running"]``).
        named_graph: Optional named graph for the read.

    Returns:
        List of Operation IRIs matching the criteria.
    """
    status_filter = ", ".join(f'"{s}"' for s in statuses)
    sparql = f"""
    SELECT DISTINCT ?op WHERE {{
        ?op <{CFC.implementsCapability}> ?cap .
        <{resource_iri}> <{CFC.hasCapability}> ?cap .
        ?op <{SVC.hasOperationStatus}> ?st .
        FILTER(?st IN ({status_filter}))
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    return [IRI(str(b["op"])) for b in bindings]


def find_stranded_operations(
    ogm: "OGM",
    resource_iri: IRI,
    statuses: Sequence[str],
    max_age_seconds: float,
    now: Optional[datetime] = None,
    named_graph: Optional[IRI] = None,
    *,
    excluding: Sequence[IRI] = (),
) -> list[IRI]:
    """Find a resource own Operations, in one of ``statuses``, that no live Service will finish.

    A Service is live when it has an ``svc:hasAddress`` and an ``svc:hasLastHeartbeat`` no older than
    ``max_age_seconds`` before ``now``, and is not in ``excluding``. The Operations are the
    resource own, as :func:`find_resource_operations` finds them (``cfc:implementsCapability``
    and ``cfc:hasCapability``). Two rules, one read:

    - A ``running`` Operation is stranded when the Service of its executor is not live: the
      Workflow recorded at the claim (``svc:executedByWorkflow``) belongs (``svc:isWorkflowOf``)
      to no live Service. A running Operation without a recorded executor (written by a claim
      that names none) is stranded. No other Service takes up a
      running Operation, so only its claimer counts.
    - An Operation in any other status (``queued``) is stranded when its Capability is realized
      by no live Service of its resource: no Workflow that realizes the Capability
      (``svc:realizedByWorkflow``) belongs to a live Service of the resource
      (``svc:isServiceOf``). The joins are those of discovery (:func:`resolve_dispatch_target`),
      plus the heartbeat.

    The watchdog sweep and the startup reconstruction of a queue (for its running Operations)
    both decide with it. The reconstruction takes up its queued Operations without it. A
    restarting instance's re-registration writes a fresh heartbeat with its address
    (:func:`register_service`), so a sweep after it counts the instance as live.

    Args:
        ogm: The OGM instance.
        resource_iri: The resource whose Operations to inspect.
        statuses: The statuses to filter (e.g. ``["queued", "running"]``).
        max_age_seconds: The staleness threshold: a heartbeat older than this is not live.
        now: The time the threshold counts back from. Defaults to the current time.
        named_graph: Optional named graph for the read.
        excluding: Services that never count as live, whatever their heartbeat. A restarting
            instance names its own Service here: its heartbeat may still be fresh from before
            the crash, and the running Operations it claimed then are orphans.

    Returns:
        List of the stranded Operation IRIs.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = _cutoff(now, max_age_seconds)
    status_filter = ", ".join(f'"{s}"' for s in statuses)

    def live(service: str) -> str:
        """The pattern of a live ``service`` (a SPARQL variable)."""
        exclusion = ""
        if excluding:
            listed = ", ".join(f"<{iri}>" for iri in excluding)
            exclusion = f" FILTER({service} NOT IN ({listed}))"
        return (
            f"{service} <{SVC.hasAddress}> ?addr . {service} <{SVC.hasLastHeartbeat}> ?hb . "
            f"FILTER(?hb >= {cutoff}){exclusion}"
        )

    sparql = f"""
    SELECT DISTINCT ?op WHERE {{
        ?op <{CFC.implementsCapability}> ?cap .
        <{resource_iri}> <{CFC.hasCapability}> ?cap .
        ?op <{SVC.hasOperationStatus}> ?st .
        FILTER(?st IN ({status_filter}))
        FILTER(
            (?st = "{OperationStatus.RUNNING}" && NOT EXISTS {{
                ?op <{SVC.executedByWorkflow}> ?exwf .
                ?exwf <{SVC.isWorkflowOf}> ?exsvc .
                {live("?exsvc")}
            }})
            || (?st != "{OperationStatus.RUNNING}" && NOT EXISTS {{
                ?cap <{SVC.realizedByWorkflow}> ?wf .
                ?wf <{SVC.isWorkflowOf}> ?svc .
                ?svc <{SVC.isServiceOf}> <{resource_iri}> .
                {live("?svc")}
            }})
        )
    }}
    """
    return [IRI(str(b["op"])) for b in _bindings(ogm.db.query(sparql, convert_bindings=True))]


def services_of_resource(
    ogm: "OGM",
    resource_iri: IRI,
    *,
    reachable_only: bool = False,
    named_graph: Optional[IRI] = None,
) -> list[IRI]:
    """Every Service bound to a resource, via the instance-owned ``svc:isServiceOf``.

    A Service IRI carries an instance discriminator, so it cannot be reconstructed from a
    resource IRI alone; this read finds it instead. ``svc:isServiceOf`` is many-to-one.
    Consumers must not assume the answer has exactly one element. A controller and a read-only
    monitor on one resource are two Services.

    Args:
        ogm: The OGM instance.
        resource_iri: The resource whose services to find.
        reachable_only: Keep only Services that currently carry an ``svc:hasAddress``. A
            deregistered instance keeps its individual but loses its address. This is
            the reachable-now set rather than the ever-registered set.
        named_graph: Optional named graph for the read.

    Returns:
        List of Service IRIs.
    """
    reachability = f"\n        ?svc <{SVC.hasAddress}> ?addr ." if reachable_only else ""
    sparql = f"""
    SELECT DISTINCT ?svc WHERE {{
        ?svc <{SVC.isServiceOf}> <{resource_iri}> .{reachability}
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    return [IRI(str(b["svc"])) for b in bindings]


def _resource_of_service(
    ogm: "OGM", service_iri: IRI, named_graph: Optional[IRI] = None
) -> Optional[IRI]:
    """The resource a Service wraps (``svc:isServiceOf``), or None. A read (ontology hop)."""
    sparql = f"SELECT ?r WHERE {{ <{service_iri}> <{SVC.isServiceOf}> ?r . }}"
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    return IRI(str(bindings[0]["r"])) if bindings else None


def mark_operation_failed(
    ogm: "OGM",
    operation_iri: IRI,
    reason: str,
    named_graph: Optional[IRI] = None,
) -> None:
    """Transition an Operation to ``failed`` with a reason via the OGM atomic commit path.

    Use to reclaim an orphaned ``running`` Operation (a resource crashed mid-execution) or to
    sweep a dead resource stranded Operations. Recovery is never a silent physical replay. The
    Operation goes to ``failed``. A planner decides whether to re-dispatch.
    """
    _set(
        ogm,
        operation_iri,
        {
            str(SVC.hasOperationStatus): [OperationStatus.FAILED],
            str(SVC.hasExecutionResult): [reason],
        },
        named_graph,
    )


def reachable_service_addresses(ogm: "OGM", resource_iri: IRI) -> list[Tuple[IRI, str]]:
    """Every reachable Service of a resource, with its ``svc:hasAddress``.

    The reachability rule of ``services_of_resource(..., reachable_only=True)``: a Service is
    reachable while it carries an ``svc:hasAddress``. The address is read in the same query, so a
    Service cannot lose it between two reads. A resource may have several reachable Services
    (one for each middleware instance bound to it); the caller decides what that means.
    """
    sparql = f"""
    SELECT DISTINCT ?svc ?addr WHERE {{
        ?svc <{SVC.isServiceOf}> <{resource_iri}> .
        ?svc <{SVC.hasAddress}> ?addr .
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    return [(IRI(str(b["svc"])), str(b["addr"])) for b in bindings]
