"""Graph-write core of the KAPPS Semantic Middleware.

Every knowledge-graph **write** goes through the OGM (`OGM.create` / `OGM.commit`). This is the
architecture single validated write path (paper §4.4.2). No write issues raw SPARQL UPDATE.
**Reads** may use the triple-store access module (`ogm.db`) directly. The architecture permits
this.

Only one direction of each inverse relation receives materialization. The direction belongs to
the newly-created instance (e.g. a workflow ``svc:isWorkflowOf``, a capability
``svc:realizedByWorkflow``). The container-side inverses (``svc:hasWorkflow`` etc.) are
OWL-inferable. They receive no write. Each registration is a clean single-instance ``OGM.create``
rather than a read-modify-write append onto a growing multi-valued property. Queries in this
module therefore use the materialized (instance-owned) direction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from kapps_triplestore_interface import IRI
from rdflib import BNode
from kapps_triplestore_interface.exceptions import InvalidIRIError, PreconditionFailedError
from kapps_ogm.utils.class_scope import ClassScope
from kapps_ogm import Create, Update

from kapps_semantic_middleware.errors import (  # noqa: F401 - their earlier home, kept importable
    HandoverPreconditionError,
    OntologyGroundTruthError,
    OperationQueueEmpty,
    OperationResolutionError,
    PossessionSwitchConflict,
)
from kapps_semantic_middleware.errors import (
    CapabilityNotRealizedError,
    DeploymentValueError,
    OperationReceiverUnavailableError,
)
from kapps_semantic_middleware.operation import _implements
from kapps_semantic_middleware.possession import _switch_conflict, possession_change
# jms_kapps's seed imports mint_possession_state_iri from here.
from kapps_semantic_middleware.possession import mint_possession_state_iri  # noqa: F401
from kapps_semantic_middleware.vocabulary import (
    CFC,
    MES,
    OperationStatus,
    SVC,
)

if TYPE_CHECKING:
    from kapps_ogm import OGM


RDF_TYPE = IRI("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")


# --------------------------------------------------------------------------- #
# Deterministic IRI minting (so a restarting resource reuses its IRIs).
# --------------------------------------------------------------------------- #


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
    ``svc:address`` and heartbeat). ``lined`` rather than a hash. Production IRIs remain
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
    scope = ClassScope.from_property_chains([[IRI(prop)] for prop in data])
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
    structural link that changed is replaced.

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
    """Register a Service instance for a resource. Set its base address.

    Raises OntologyGroundTruthError if ``service_class`` is not ``svc:Service`` or a subclass.
    """
    assert_class_registered(ogm, service_class, SVC.Service, named_graph)
    _create_or_adopt(
        ogm, service_class, service_iri, {str(SVC.isServiceOf): [_ref(resource_iri)]}, named_graph
    )
    _set(ogm, service_iri, {str(SVC.address): [str(address)]}, named_graph)


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
    """Register a Workflow instance, its Capability instance, and their links.

    The Workflow owns ``svc:isWorkflowOf`` (-> service) and ``svc:endpoint``. The Capability
    owns ``svc:realizedByWorkflow`` (-> workflow). The resource links to the Capability it
    *provides* (``cfc:hasCapability``, Resource -> Capability). The resource *provides* the
    Capability. Raises OntologyGroundTruthError if the classes are not the required subclasses.
    """
    assert_class_registered(ogm, workflow_class, SVC.Workflow, named_graph)
    assert_class_registered(ogm, capability_class, CFC.Capability, named_graph)

    _create_or_adopt(
        ogm, workflow_class, workflow_iri, {str(SVC.isWorkflowOf): [_ref(service_iri)]}, named_graph
    )
    _set(ogm, workflow_iri, {str(SVC.endpoint): [str(endpoint)]}, named_graph)
    _create_or_adopt(
        ogm,
        capability_class,
        capability_iri,
        {str(SVC.realizedByWorkflow): [_ref(workflow_iri)]},
        named_graph,
    )
    # The resource *provides* the Capability (cfc:hasCapability, Resource -> Capability). This
    # Core link is not declared in the loaded Core subset. Write through the low-level triple
    # API (as create_operation does for cfc: links). It lets a restarting resource (or the
    # watchdog) find its own Operations by ontology traversal rather than by match of IRI names.
    # It persists across a restart.
    ogm.db.triple_add((resource_iri, CFC.hasCapability, capability_iri), named_graph=named_graph)


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
    """Register a StateProperty instance, its Capability instance, and their links.

    The StateProperty owns ``svc:isStatePropertyOf`` (-> service) and ``svc:endpoint``. The
    Capability owns ``svc:providedByStateProperty`` (-> state property). The resource links to
    the Capability it *provides* (``cfc:hasCapability``). Only the stable endpoint receives
    write. The live value receives no persist.
    """
    assert_class_registered(ogm, state_property_class, SVC.StateProperty, named_graph)
    assert_class_registered(ogm, capability_class, CFC.Capability, named_graph)

    _create_or_adopt(
        ogm,
        state_property_class,
        state_property_iri,
        {str(SVC.isStatePropertyOf): [_ref(service_iri)]},
        named_graph,
    )
    _set(ogm, state_property_iri, {str(SVC.endpoint): [str(endpoint)]}, named_graph)
    _create_or_adopt(
        ogm,
        capability_class,
        capability_iri,
        {str(SVC.providedByStateProperty): [_ref(state_property_iri)]},
        named_graph,
    )
    # The resource provides this Capability too (cfc:hasCapability). See register_workflow.
    ogm.db.triple_add((resource_iri, CFC.hasCapability, capability_iri), named_graph=named_graph)


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
    """
    db = ogm.db
    _set(ogm, service_iri, {str(SVC.address): []}, named_graph)

    for wf_node, _, _ in db.triples_get(
        pred=SVC.isWorkflowOf, obj=service_iri, named_graph=named_graph
    ):
        _set(ogm, _named(wf_node, "Workflow", service_iri), {str(SVC.endpoint): []}, named_graph)

    for sp_node, _, _ in db.triples_get(
        pred=SVC.isStatePropertyOf, obj=service_iri, named_graph=named_graph
    ):
        _set(
            ogm, _named(sp_node, "StateProperty", service_iri), {str(SVC.endpoint): []}, named_graph
        )


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
    """Refresh a Service ``svc:lastHeartbeat`` via OGM.commit (atomic replace)."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    _set(ogm, service_iri, {str(SVC.lastHeartbeat): [timestamp.isoformat()]}, named_graph)


def find_stale_services(
    ogm: "OGM",
    max_age_seconds: float,
    *,
    now: Optional[datetime] = None,
    named_graph: Optional[IRI] = None,
) -> list[IRI]:
    """Find reachable-but-silent Services whose heartbeat has gone stale (a read).

    A service is stale if it currently has an ``svc:address`` but either has no
    ``svc:lastHeartbeat`` or its latest heartbeat is older than ``max_age_seconds``. Services
    are identified by ``svc:address`` presence (real services are subclass-typed, so
    ``rdf:type svc:Service`` would not match).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(seconds=max_age_seconds)).isoformat()
    sparql = f"""
    SELECT DISTINCT ?s WHERE {{
        ?s <{SVC.address}> ?addr .
        OPTIONAL {{ ?s <{SVC.lastHeartbeat}> ?hb . }}
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
    """Deregister every stale Service. Fail its resource stranded Operations.

    Alongside removal of a dead resource reachability, the watchdog marks that resource stranded
    Operations (``queued``/``running``) ``failed``. Work addressed to a resource that will never
    return does not hang forever. Returns the swept Service IRIs.

    A resource may carry several Services. This is no longer quite right. A stale
    monitor drags its live sibling stranded Operations down with it. Fail them only when no
    sibling survives is *also* wrong. A surviving monitor realizes no Workflow. It would shield
    a dead controller queue forever. The correct predicate is per-Operation ("does any surviving
    Service realize this Operation Capability"). ``_reconstruct_queue`` needs the same
    treatment. Neither is done yet.
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
    marked ``failed``, after that commit, so the watchdog notifies the Operation's issuer."""
    stale = find_stale_services(ogm, max_age_seconds, now=now, named_graph=named_graph)
    for service_iri in stale:
        resource_iri = _resource_of_service(ogm, service_iri, named_graph=named_graph)
        if resource_iri is not None:
            for op_iri in find_resource_operations(
                ogm,
                resource_iri,
                [OperationStatus.QUEUED, OperationStatus.RUNNING],
                named_graph=named_graph,
            ):
                mark_operation_failed(
                    ogm,
                    op_iri,
                    "stranded operation swept by watchdog: the resource is stale/dead",
                    named_graph=named_graph,
                )
                failed(op_iri)
        deregister_service(ogm, service_iri, named_graph=named_graph)
    return stale


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
    ``cfc:implementsCapability`` link, ``svc:operationStatus``, and ``data``, whose properties
    the Operation's class must declare. Its class scope names every property it sets, because
    kapps_ogm drops a property that the scope does not name, with only a warning.
    ``issue_operation()`` does not call this: it creates the Operation with ``mw.create()`` under the
    operation scope. This writer is kept for existing callers, such as tests that seed an
    Operation. The IRI must be new: the store must hold no statement about it yet, or the
    store's refusal ``PreconditionFailedError`` is raised, and nothing is written. The
    Resource->Capability ``cfc:hasCapability`` link written at *registration* is a separate,
    multi-valued append. It stays on the low-level path until
    ``kapps_ogm`` grows a validated single-triple append.
    """
    create_data: dict = {
        **_implements(IRI(capability_iri)),
        str(SVC.operationStatus): [status],
        **(data or {}),
    }
    creation = Create(
        class_iri=IRI(operation_class),
        instance_iri=IRI(operation_iri),
        data=create_data,
        class_scope=ClassScope.from_property_chains([[IRI(prop)] for prop in create_data]),
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
    has a live ``svc:address``: ``Capability --realizedByWorkflow--> Workflow --isWorkflowOf-->
    Service --address``. If ``target_resource`` is given, the Service is pinned to that resource
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
        ?svc <{SVC.address}> ?addr .{resource_filter}
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
            "has a current svc:address)."
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
    pull-and-run so `executedByWorkflow` receives record even if the workflow `svc:endpoint` was
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
    SELECT ?wf ?issuer WHERE {{
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
    return _ClaimRead(IRI(str(binding["wf"])), IRI(str(issuer)) if issuer is not None else None)


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
    """Every ``svc:operationStatus`` of an Operation that the store holds, in one read. One, as
    a rule; none when the store holds no status for it."""
    sparql = f"""
    SELECT ?status WHERE {{
        <{operation_iri}> <{SVC.operationStatus}> ?status .
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
        OPTIONAL {{ <{operation_iri}> <{SVC.operationStatus}> ?status . }}
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
    """The kapps_ogm ``Update`` that sets `svc:operationStatus` on an Operation.

    This covers the queued->running transition and any other single-status transition. Nothing
    is written here: ``claim_next()`` commits it as the entry of ``mw.commit()``, its claim.
    """
    return Update(instance_iri=IRI(operation_iri), data={str(SVC.operationStatus): [status]})


def set_operation_status(
    ogm: "OGM",
    *,
    operation_iri: IRI,
    status: str,
    named_graph: Optional[IRI] = None,
) -> None:
    """Set `svc:operationStatus` on an Operation via the OGM atomic commit path.

    It writes :func:`_operation_status_update` in one ``OGM.commit``. ``claim_next()`` no longer
    calls this: it commits the same change through ``mw.commit()``. This writer is kept for
    existing callers.
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
    resource datamodel) goes into ``svc:failureState`` in the same change. The failed status and
    the state that produced it are never separable. Nothing is written here: ``claim_next()``
    commits it as the exit or the error transition of ``mw.commit()``.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    data: dict = {
        str(SVC.operationStatus): [status],
        str(SVC.executedByWorkflow): [_ref(workflow_iri)],
        str(SVC.executionTimestamp): [timestamp.isoformat()],
    }
    if result is not None:
        data[str(SVC.executionResult)] = [str(result)]
    if failure_state is not None:
        data[str(SVC.failureState)] = [failure_state]
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
    atomically, and on failure the dumped state with them. ``claim_next()`` no longer calls
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
    """Find a resource own Operations whose ``svc:operationStatus`` is one of ``statuses``.

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
        ?op <{SVC.operationStatus}> ?st .
        FILTER(?st IN ({status_filter}))
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    return [IRI(str(b["op"])) for b in bindings]


def services_of_resource(
    ogm: "OGM",
    resource_iri: IRI,
    *,
    reachable_only: bool = False,
    named_graph: Optional[IRI] = None,
) -> list[IRI]:
    """Every Service bound to a resource, via the instance-owned ``svc:isServiceOf``.

    A Service IRI carries an instance discriminator. It can no longer receive reconstruction from
    a resource IRI alone. This read replaces that reconstruction. ``svc:isServiceOf`` has always
    been many-to-one. Consumers must no longer assume the answer has exactly one element. A
    controller and a read-only monitor on one resource are two Services.

    Args:
        ogm: The OGM instance.
        resource_iri: The resource whose services to find.
        reachable_only: Keep only Services that currently carry an ``svc:address``. A
            deregistered instance keeps its individual but loses its address. This is
            the reachable-now set rather than the ever-registered set.
        named_graph: Optional named graph for the read.

    Returns:
        List of Service IRIs.
    """
    reachability = f"\n        ?svc <{SVC.address}> ?addr ." if reachable_only else ""
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
            str(SVC.operationStatus): [OperationStatus.FAILED],
            str(SVC.executionResult): [reason],
        },
        named_graph,
    )


# ---- Possession + handover (Core reified possession, one state per Resource) ----


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

    ``mes:complements`` is symmetric. This is a read (``mode_ability mes:complements ?a . counterpart
    mes:hasHandoverAbility ?a``).
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


def switch_possession(
    ogm: "OGM",
    *,
    workpiece_iri: IRI,
    new_possessor_iri: IRI,
    workpiece_update: Optional[Mapping[str, Sequence[Any]]] = None,
    named_graph: Optional[IRI] = None,
) -> IRI:
    """Hand a workpiece to a new possessor in one OGM commit, with no move around it.

    This is the possession change of ``SemanticMiddleware.handover()`` on its own. The workpiece's
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

    A participant hands over with ``SemanticMiddleware.handover()``. This module-level helper,
    and ``create_possession``, serve callers that hold no middleware; they write through
    ``OGM.commit``, the validated write, not through ``mw.commit()``.

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


def reachable_service_addresses(ogm: "OGM", resource_iri: IRI) -> list[Tuple[IRI, str]]:
    """Every reachable Service of a resource, with its ``svc:address``.

    The reachability rule of ``services_of_resource(..., reachable_only=True)``: a Service is
    reachable while it carries an ``svc:address``. The address is read in the same query, so a
    Service cannot lose it between two reads. A resource may have several reachable Services
    (one per middleware instance); the caller decides what that means.
    """
    sparql = f"""
    SELECT DISTINCT ?svc ?addr WHERE {{
        ?svc <{SVC.isServiceOf}> <{resource_iri}> .
        ?svc <{SVC.address}> ?addr .
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    return [(IRI(str(b["svc"])), str(b["addr"])) for b in bindings]
