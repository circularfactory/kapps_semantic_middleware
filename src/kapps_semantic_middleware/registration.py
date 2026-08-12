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
# ADR: root 0002, 0003, 0004, 0006, 0007

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import InvalidIRIError
from kapps_ogm.utils.class_scope import ClassScope

from kapps_semantic_middleware.vocabulary import CFC, MES, OperationStatus, SVC

if TYPE_CHECKING:
    from kapps_ogm import OGM


class OntologyGroundTruthError(Exception):
    """A referenced class does not pre-exist as the required kind."""

    # ADR: 0003


class OperationResolutionError(Exception):
    """An Operation cannot resolve to a reachable Workflow endpoint."""

    # ADR: 0002


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
    # ADR: 0021, 0022
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
    # ADR: 0003
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
    literals as their Python/str value). Idempotent for unchanged structure (RDF set semantics
    on the OGM underlying add).
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
    _create(
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
    # ADR: 0002
    assert_class_registered(ogm, workflow_class, SVC.Workflow, named_graph)
    assert_class_registered(ogm, capability_class, CFC.Capability, named_graph)

    _create(
        ogm, workflow_class, workflow_iri, {str(SVC.isWorkflowOf): [_ref(service_iri)]}, named_graph
    )
    _set(ogm, workflow_iri, {str(SVC.endpoint): [str(endpoint)]}, named_graph)
    _create(
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

    _create(
        ogm,
        state_property_class,
        state_property_iri,
        {str(SVC.isStatePropertyOf): [_ref(service_iri)]},
        named_graph,
    )
    _set(ogm, state_property_iri, {str(SVC.endpoint): [str(endpoint)]}, named_graph)
    _create(
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
    # ADR: 0007
    db = ogm.db
    _set(ogm, service_iri, {str(SVC.address): []}, named_graph)

    for wf_iri, _, _ in db.triples_get(
        pred=SVC.isWorkflowOf, obj=service_iri, named_graph=named_graph
    ):
        _set(ogm, wf_iri, {str(SVC.endpoint): []}, named_graph)

    for sp_iri, _, _ in db.triples_get(
        pred=SVC.isStatePropertyOf, obj=service_iri, named_graph=named_graph
    ):
        _set(ogm, sp_iri, {str(SVC.endpoint): []}, named_graph)


# --------------------------------------------------------------------------- #
# Liveness: heartbeat (per-service) and watchdog sweep (centralized). ADR 0007.
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
    treatment. Tracked as #63. It is out of scope for #47.
    """
    # ADR: 0002, 0007, 0009, 0022
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
        deregister_service(ogm, service_iri, named_graph=named_graph)
    return stale


# --------------------------------------------------------------------------- #
# Event-trigger dispatch & operation queue (ADR 0009 / 0010).
# --------------------------------------------------------------------------- #


EVENT_TRIGGER_WORKFLOW_NAME = "event_trigger"
"""Reserved built-in Workflow name for the receiver event-trigger REST endpoint."""


def build_event_trigger_url(address: str) -> str:
    """Build the receiver event-trigger endpoint (``POST /workflows/event_trigger/execute``)."""
    # ADR: 0009
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

    The whole Operation receives creation in ONE ``OGM.create``. This includes its ``rdf:type``,
    the single-valued ``cfc:implementsCapability`` link, and ``svc:operationStatus`` (plus any
    svc:-domain ``data``). This is the single validated write path (paper §4.3/4.4.2,
    "every write originates as an OGM commit"). This requires the loaded ontology to declare the
    Core Operation-property domains (``cfc:implementsCapability`` with ``rdfs:domain
    cfc:Operation``). The scenario/demo ontologies do. The Resource->Capability
    ``cfc:hasCapability`` link written at *registration* is a separate, multi-valued append. It
    stays on the low-level path until ``kapps_ogm`` grows a validated single-triple append.
    """
    # ADR: 0008, 0009, 0010, 0011
    create_data: dict = {
        str(CFC.implementsCapability): [_ref(capability_iri)],
        str(SVC.operationStatus): [status],
    }
    if data:
        create_data.update(data)
    _create(ogm, operation_class, operation_iri, create_data, named_graph)


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
        OperationResolutionError: If no reachable Service realizes the capability class.
    """
    # ADR: 0002, 0009
    resource_filter = ""
    if target_resource is not None:
        resource_filter = f"\n        ?svc <{SVC.isServiceOf}> <{target_resource}> ."
    sparql = f"""
    SELECT ?cap ?svc ?addr WHERE {{
        ?cap a <{capability_class}> .
        ?cap <{SVC.realizedByWorkflow}> ?wf .
        ?wf <{SVC.isWorkflowOf}> ?svc .
        ?svc <{SVC.address}> ?addr .{resource_filter}
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    if not bindings:
        raise OperationResolutionError(
            f"Capability class {capability_class} cannot be resolved to any reachable "
            "Service (no Capability of that class is realized by a Workflow whose Service "
            "has a current svc:address)."
        )
    binding = bindings[0]
    return IRI(str(binding["cap"])), IRI(str(binding["svc"])), str(binding["addr"])


def revert_operation(
    ogm: "OGM",
    operation_iri: IRI,
    *,
    operation_class: IRI,
    capability_iri: IRI,
    data: Optional[dict] = None,
    named_graph: Optional[IRI] = None,
) -> None:
    """Remove a just-created Operation whose event trigger failed to deliver.

    Atomic create-and-notify: a failed notify reverts the created Operation. ``OGM.delete`` is
    not implemented in ``kapps_ogm`` yet. This removes exactly what ``create_operation`` wrote.
    Literal properties receive clear through the OGM commit path first (commit needs the
    ``rdf:type`` triple present to resolve the class). The ``cfc:implementsCapability`` and
    ``rdf:type`` triples then receive removal via the low-level ``ogm.db.triple_delete`` (a
    sanctioned kapps_triplestore_interface write).
    """
    # ADR: 0008, 0010, 0011
    clear_literals: dict = {str(SVC.operationStatus): []}
    if data:
        clear_literals.update({k: [] for k in data})
    _set(ogm, operation_iri, clear_literals, named_graph)
    ogm.db.triple_delete(
        (operation_iri, CFC.implementsCapability, capability_iri), named_graph=named_graph
    )
    ogm.db.triple_delete((operation_iri, RDF_TYPE, operation_class), named_graph=named_graph)


# ---- Pull-and-run: status transitions + terminal provenance (ADR 0009 / 0010) ----


class OperationQueueEmpty(Exception):
    """Raised when `claim_next` finds no `queued` Operation to pull."""


def resolve_operation_workflow(
    ogm: "OGM", operation_iri: IRI, named_graph: Optional[IRI] = None
) -> IRI:
    """Resolve an Operation to the Workflow that realizes its Capability, WITHOUT require of a
    live endpoint. This is the provenance resolver used by
    pull-and-run so `executedByWorkflow` receives record even if the workflow `svc:endpoint` was
    deregistered mid-run.

    Raises:
        OperationResolutionError: If no Workflow realizes the Operation Capability.
    """
    # ADR: 0002
    sparql = f"""
    SELECT ?wf WHERE {{
        <{operation_iri}> <{CFC.implementsCapability}> ?cap .
        ?cap <{SVC.realizedByWorkflow}> ?wf .
    }}
    """
    result = ogm.db.query(sparql, convert_bindings=True)
    bindings = (
        result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
    )
    if not bindings:
        raise OperationResolutionError(
            f"Operation {operation_iri} cannot be resolved to any Workflow "
            "(no Capability realized by a Workflow)."
        )
    binding = bindings[0]
    return IRI(str(binding["wf"]))


def set_operation_status(
    ogm: "OGM",
    *,
    operation_iri: IRI,
    status: str,
    named_graph: Optional[IRI] = None,
) -> None:
    """Set `svc:operationStatus` on an Operation via the OGM atomic commit path.

    This covers the queued->running transition and any other single-status transition.
    """
    # ADR: 0009
    _set(ogm, operation_iri, {str(SVC.operationStatus): [status]}, named_graph)


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

    Write status + provenance atomically (single commit). An Operation is never
    terminal-without-provenance. On failure,
    ``failure_state`` (a JSON snapshot of the resource datamodel) receives write into
    ``svc:failureState`` in the same commit. The failed status and the state that produced it
    are never separable.
    """
    # ADR: 0009
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
    _set(ogm, operation_iri, data, named_graph)


# ---- Queue durability + recovery (ADR 0009) ----


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
    # ADR: 0009
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
    # ADR: 0007, 0022
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
    # ADR: 0009
    _set(
        ogm,
        operation_iri,
        {
            str(SVC.operationStatus): [OperationStatus.FAILED],
            str(SVC.executionResult): [reason],
        },
        named_graph,
    )


# ---- Possession + handover (Core reified possession, ADR 0011) ----


class HandoverPreconditionError(Exception):
    """A handover precondition failed (caller lacks possession, or counterpart lacks the
    complementary handover ability). Rejection occurs before any physical work begins."""
    # ADR: 0011


def mint_possession_state_iri(workpiece_iri: IRI) -> IRI:
    """Mint a fresh PossessionState IRI. Each change of possession makes a new state.
    The previous one is kept as implicit history."""
    return IRI(f"{workpiece_iri}_possession_{uuid.uuid4().hex[:12]}")


def create_possession(
    ogm: "OGM",
    *,
    workpiece_iri: IRI,
    possessor_iri: IRI,
    named_graph: Optional[IRI] = None,
) -> IRI:
    """Establish an initial possession over Core reified model (verified vs Core 0.9.0).

    A ``cfc:PossessionState`` the possessor holds (``cfc:hasPossessor``, appended as an atomic
    insertion since a resource may possess several workpieces) and the workpiece is possessed by
    (``cfc:hasPossessedWorkpiece``, set through the OGM commit/update path). The
    scenario ontology must declare these Core terms domains so the OGM commit validates.

    Returns:
        The minted PossessionState IRI.
    """
    # ADR: 0008, 0011
    ps_iri = mint_possession_state_iri(workpiece_iri)
    # A resource may possess several workpieces at once (ADR 0011 — possession is not
    # universally maxCount 1), so `cfc:hasPossessor` is APPENDED via the kapps_triplestore_interface
    # atomic insertion. A workpiece has exactly one possession, so `cfc:hasPossessedWorkpiece`
    # is SET through the OGM commit path (an atomic replace). The PossessionState node comes
    # into being as their shared object (inferably a cfc:PossessionState via rdfs:range).
    ogm.db.triple_add((possessor_iri, CFC.hasPossessor, ps_iri), named_graph=named_graph)
    _set(ogm, workpiece_iri, {str(CFC.hasPossessedWorkpiece): [_ref(ps_iri)]}, named_graph)
    return ps_iri


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
    named_graph: Optional[IRI] = None,
) -> IRI:
    """Atomically change possession of a workpiece to a new possessor.

    A fresh ``cfc:PossessionState`` receives mint for the new possessor. The new possessor
    ``cfc:hasPossessor`` receives APPEND as an atomic insertion (kapps_triplestore_interface). A resource
    may possess several workpieces. This must not disturb its existing possessions, because
    possession is not universally maxCount 1. The workpiece ``cfc:hasPossessedWorkpiece`` then
    receives re-point via a single ``OGM.commit``. This is the update path, an atomic
    DELETE/INSERT. It keeps the workpiece pointed to exactly one PossessionState throughout.
    Core Workpiece cardinality-1 (the commit-time SHACL backstop) is never transiently violated.
    Append the possessor link BEFORE the re-point. A failed re-point leaves the prior possession
    fully intact. The previous PossessionState is kept as implicit history.

    Returns:
        The new PossessionState IRI.
    """
    new_ps = mint_possession_state_iri(workpiece_iri)
    ogm.db.triple_add((new_possessor_iri, CFC.hasPossessor, new_ps), named_graph=named_graph)
    _set(ogm, workpiece_iri, {str(CFC.hasPossessedWorkpiece): [_ref(new_ps)]}, named_graph)
    return new_ps
