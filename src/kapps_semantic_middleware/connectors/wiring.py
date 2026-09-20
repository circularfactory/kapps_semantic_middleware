"""Recognition and wiring: turn a resource's ClassSpec into connector registrations.

This is the step that registers from the knowledge graph. It runs at
**construction**, not during ``on_start_up``. The reason is sharp. ``lifespan`` calls
``connect()`` on everything in the connection registry *before* it runs ``on_start_up``
callbacks. ``initiate_sync`` (what ``add_synced_connector`` defers) starts
``run_receive()``, but never calls ``connect()``. A connector registered while the
datamodel materializes therefore never connects. The client stays ``None``. The listener
task never starts. Its queue is never fed. ``receive()`` blocks forever. Outbound would
limp along, because ``consume()`` reconnects on failure. The failure is one-directional
and silent.

Register at construction instead. This avoids the failure, with no out-of-band lifecycle
call. It is possible because everything a ``ConnectionInfo`` needs comes from the
ClassSpec and the graph.

Two shapes come out of here. Both are required.

- The **full** spec. The bindings read it to find broker addresses and topics.
- The **pruned** spec. The system serves this northbound.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware.connectors.semantic import (
    ParameterBinding,
    Registration,
    SemanticConnectorRegistry,
    normalize_metadata,
    resolve_direction,
)
from kapps_semantic_middleware.projection import cross_check, prune_southbound
from kapps_semantic_middleware.vocabulary import META_TYPE_NAMESPACES, RDFS, SVC

logger = logging.getLogger(__name__)

EXPLICIT_GRAPH = "FROM <http://www.ontotext.com/explicit>"
"""Restrict a query to asserted triples.

It is not an ontology term, so it does not belong in ``vocabulary.py``. It names a GraphDB
*feature*, the pseudo-graph of explicit statements. It is a constant rather than three inline
copies because every recognition query needs it for the same reason. The default graph
includes materialized inference, and a parameter node's only types are anonymous restriction
nodes that exist by inference alone. Count or read without this. This returns
inferred triples and inflates every result.
"""


@dataclass
class WiringPlan:
    """This describes what recognition found, and what the caller should do about it."""

    northbound_spec: Any
    """The pruned ClassSpec, safe to materialize and serve."""

    class_scope: Any
    """The scope where the system resolves the specs.

    Keep this because a fetch needs **both**. ``_fetch_object_property`` decides whether a nested
    individual is hydrated or fetched as a bare reference from the *class_scope*
    (``as_reference = nested_class_scope is None``), not from whether the spec it was handed
    has a ``nested``. Passing only the pruned spec yields belts and barriers with empty
    parameters. Use :func:`northbound_fetch_kwargs` rather than restate this."""

    bindings: List[ParameterBinding]
    """This lists every recognized interface-accessible parameter. Populate for every flavour,
    including one that wires nothing. The system never gates recognition."""

    registrations: List[Tuple[ParameterBinding, Registration]]
    """The connector registrations to perform. Empty when the flavour wires nothing."""

    southbound_properties: frozenset
    """The prune set that was applied, kept for assertions and diagnostics."""

    southbound_by_property: Dict[str, frozenset]
    """The same prune set, broken out per recognized parameter property rather than
    unioned across all of them. ``plan_wiring`` already computes this as a cache
    (``southbound_by_property``, one ontology round trip per distinct property rather
    than one per binding) and used to discard it once ``southbound_properties`` was
    built. Kept here instead: a consumer that shows what pruning stripped *per
    parameter* -- the demo's station board -- needs exactly this breakdown, and recomputing it a second time would cost
    the same ontology queries this cache exists to avoid paying twice."""

    def northbound_fetch_kwargs(self) -> Dict[str, Any]:
        """The arguments that materialize this resource's northbound view."""
        return {
            "class_spec": self.northbound_spec,
            "class_scope": self.class_scope,
            "materialize": True,
        }


def plan_wiring(
    *,
    ogm: Any,
    resource_iri: IRI,
    class_scope: Any,
    resource_class: Optional[IRI] = None,
    registry: SemanticConnectorRegistry,
    flavour: SyncDirection = SyncDirection.BIDIRECTIONAL,
    autoregister: bool = True,
    ensure_transport: Optional[Callable[[str, int], None]] = None,
) -> WiringPlan:
    """Resolve a resource's parameters and decide what to connect.

    ``autoregister`` gates only the registrations. Produce the spec, the projection, and
    the recognized bindings either way. An inspecting instance that skipped recognition
    would treat every parameter node as ordinary data, and serve its broker address. The
    least-privileged connector wiring would leak the most.

    ``ensure_transport`` is the deployment's transport hook. Wrapped once here,
    fresh per call, so it fires at most once per distinct ``(host, port)`` across every
    binding built below -- a binding descriptor calls it unconditionally and the wrapper
    absorbs the dedup, so no descriptor has to track addresses it has already seen across
    calls it cannot see each other from.
    """
    resource_class = resource_class or class_of(ogm, resource_iri)
    full_spec = ogm.get_class_spec(class_iri=resource_class, class_scope=class_scope)

    # The projection asks the *ontology* what is protocol metadata, not the registry. A
    # registry-derived set only knows the protocols this middleware has code for, and a
    # parameter reachable over an unregistered protocol would have its endpoint served
    # (measured). Recompute here on every construction, because the ontology may
    # have grown a protocol since this instance last started.
    # `cache` is shared with the loop below so the ontology is asked once per distinct property
    # rather than once per use. Every one of these is a live SPARQL round trip, and the store
    # this runs against refuses distinct/group-by queries under memory pressure. Redundant
    # queries are not merely slow. They raise the chance of a spurious startup failure.
    southbound_by_property: Dict[str, frozenset] = {}
    northbound_spec = prune_southbound(full_spec, ogm=ogm, cache=southbound_by_property)

    bindings = _recognise(
        ogm=ogm,
        resource_iri=resource_iri,
        resource_class=resource_class,
        spec=full_spec,
        registry=registry,
    )

    southbound = frozenset(
        prop
        for binding in bindings
        for prop in southbound_by_property.get(str(binding.parameter_property), ())
    )

    # Cross-check the two declarations against each other. The ontology governs what is hidden.
    # A descriptor's connection_metadata says what it reads. Disagreement is a real deployment
    # state rather than an error, so report it and do not raise.
    for binding in bindings:
        cross_check(
            binding.parameter_property,
            southbound_by_property.get(str(binding.parameter_property), ()),
            getattr(binding.descriptor, "connection_metadata", None),
        )

    deduped_ensure_transport = _dedupe_by_address(ensure_transport)

    registrations: List[Tuple[ParameterBinding, Registration]] = []
    if autoregister:
        for binding in bindings:
            direction = resolve_direction(binding.access_mode, flavour)
            for registration in binding.descriptor.build(
                binding, direction, ensure_transport=deduped_ensure_transport
            ):
                registrations.append((binding, registration))
    else:
        logger.info(
            "autoregister_connectors=False: recognized %d parameter(s) on %s and connected "
            "none. The northbound projection still applies.",
            len(bindings),
            resource_iri,
        )

    return WiringPlan(
        northbound_spec=northbound_spec,
        class_scope=class_scope,
        bindings=bindings,
        registrations=registrations,
        southbound_properties=southbound,
        southbound_by_property=southbound_by_property,
    )


def _dedupe_by_address(
    ensure_transport: Optional[Callable[[str, int], None]],
) -> Optional[Callable[[str, int], None]]:
    """Wrap a transport hook so it fires at most once per distinct ``(host, port)``.

    ``None`` in, ``None`` out -- a resource with no hook calls nothing. Otherwise
    a fresh ``seen`` set is closed over per call to :func:`plan_wiring`, which is exactly the
    scope one middleware construction spans. A resource whose parameters ever name two
    addresses gets the hook called twice; four parameters sharing one address get it once,
    before the first connector for that address is built.
    """
    if ensure_transport is None:
        return None

    seen: set = set()

    def wrapped(host: str, port: int) -> None:
        key = (host, port)
        if key in seen:
            return
        seen.add(key)
        ensure_transport(host, port)

    return wrapped


def _recognise(
    *,
    ogm: Any,
    resource_iri: IRI,
    resource_class: IRI,
    spec: Any,
    registry: SemanticConnectorRegistry,
) -> List[ParameterBinding]:
    """Walk a resource's spec and the graph, and pair each parameter with its descriptor.

    Match on the **interface property**, not on ``rdf:type``. A parameter node has no
    named type of its own. It has only anonymous restriction nodes, which exist by inference and so
    never appear in an explicit-graph fetch. The property hierarchy is what survives a
    round trip.

    **The evidence may sit on the parameter or on the resource's Service**. An MQTT connector recognises ``inf:hasMQTTTopic`` on
    the parameter itself. A REST connector has no such marker; its evidence is the resource's
    own ``svc:address``, one hop out through ``svc:isServiceOf``. Look that address up **once**
    per resource here, and fold it into every recognised binding's metadata alongside whatever
    the parameter node itself carries — a binding descriptor that does not care (MQTT) simply
    never reads the key.
    """
    bindings: List[ParameterBinding] = []
    address = _service_address(ogm=ogm, resource_iri=resource_iri)
    # This must equal `type(instance).__name__` for the materialized root -- the {Model}
    # segment `rest_router.py` actually mounts routes under. `kapps_ogm`'s
    # `ClassSpec.to_pydantic_model` names that class `self.iri.lined`, the class IRI
    # mangled whole (`class_spec.py`), not its bare fragment: two classes from different
    # namespaces that happen to share a fragment (e.g. two ontologies each defining their
    # own `TransferUnit`) would otherwise collide on one Python class name. A REST
    # binding derives this route independently, with no served route list to read it
    # from, so it has to reconstruct the same mangling `to_pydantic_model` used, not the
    # shorter fragment a human would write by hand -- using the fragment here 404s every
    # request this binding ever builds.
    root_class_local_name = IRI(str(resource_class)).lined

    for holder_iri, prop_iri, prop_spec, steps in _parameter_properties(
        ogm=ogm, root_iri=resource_iri, spec=spec
    ):
        descriptor = _descriptor_for(ogm=ogm, prop_iri=prop_iri, registry=registry)
        if descriptor is None:
            # Unrecognised complex content is plain data: shown, readable, nothing wired.
            # Not an error. The graph may well hold structure this middleware
            # has no connector for.
            continue

        metadata = _parameter_metadata(
            ogm=ogm, holder_iri=holder_iri, prop_iri=prop_iri, prop_spec=prop_spec
        )
        if metadata is None:
            continue

        metadata = normalize_metadata(metadata)
        if address is not None:
            metadata.setdefault(str(SVC.address), [address])

        bindings.append(
            ParameterBinding(
                resource_iri=holder_iri,
                parameter_property=IRI(str(prop_iri)),
                # ClassSpec keys arrive as rdflib URIRef, not IRI, so the mangling has to go
                # through a converted copy. URIRef has no `lined`.
                field_id=IRI(str(prop_iri)).lined,
                metadata=metadata,
                descriptor=descriptor,
                node_model_type=prop_spec.nested.to_pydantic_model(),
                root_iri=IRI(str(resource_iri)),
                root_class_local_name=root_class_local_name,
                path_steps=steps,
            )
        )

    return bindings


def _parameter_properties(*, ogm: Any, root_iri: IRI, spec: Any, steps: Tuple[Tuple[str, str], ...] = ()):
    """Yield ``(holder_iri, property_iri, property_spec, path_steps)`` for every COMPLEX property.

    A COMPLEX property is the deepest addressable thing. ``ConnectionInfo`` has exactly three
    levels and ``field_id`` resolves by plain ``getattr``, so ``inf:hasValue`` — one level
    further down — is unreachable. This is the same atomic unit the routing side reached.

    Descend one level through object properties to reach a resource's components. A
    TransferUnit's parameters hang off its belts and barriers, not off the unit itself.

    ``path_steps`` accumulates the ``(field_name, child_iri)`` hops taken to reach the
    current ``root_iri``, mirroring ``rest_router.py``'s own tree walk exactly — that is what
    lets ``rest_binding.py`` derive a structural URL with no second walk and no ontology
    term. ``field_name`` is mangled here (``IRI(prop_iri).lined``), the
    same way ``field_id`` is mangled below, because that is the actual pydantic field name
    the served route uses.
    """
    from kapps_ogm.mapping.property_spec import PropertyValueKind

    for prop_iri, prop_spec in getattr(spec, "properties", {}).items():
        if prop_spec.value_kind is PropertyValueKind.COMPLEX:
            yield root_iri, prop_iri, prop_spec, steps
            continue

        nested = getattr(prop_spec, "nested", None)
        if nested is None:
            continue

        for component_iri in _linked_individuals(ogm, root_iri, prop_iri):
            yield from _parameter_properties(
                ogm=ogm,
                root_iri=component_iri,
                spec=nested,
                steps=steps + ((IRI(str(prop_iri)).lined, str(component_iri)),),
            )


def _service_address(*, ogm: Any, resource_iri: IRI) -> Optional[str]:
    """The resource's own ``svc:address``, one hop out through ``svc:isServiceOf``.

    One query per ``_recognise`` call, not per parameter — a resource has one Service, and
    every parameter on it shares the same answer. ``None`` when the resource has no Service,
    or the Service has not (yet) published an address. That is the ordinary state of a
    freshly-constructing instance recognising its *own* resource: ``plan_wiring`` runs from
    ``__init__``, and ``_register_service`` is an ``on_start_up`` callback that has not run
    yet, so there is no address to find and REST recognition finds nothing to bind.
    """
    rows = ogm.db.query(
        f"SELECT ?addr {EXPLICIT_GRAPH} "
        f"WHERE {{ ?svc <{SVC.isServiceOf}> <{resource_iri}> ; <{SVC.address}> ?addr }}",
        convert_bindings=True,
    )["results"]["bindings"]
    return str(rows[0]["addr"]) if rows else None


def _linked_individuals(ogm: Any, subject: IRI, prop_iri: IRI) -> List[IRI]:
    """This function returns the individuals a resource references through one object property."""
    rows = ogm.db.query(
        f"SELECT ?o {EXPLICIT_GRAPH} "
        f"WHERE {{ <{subject}> <{prop_iri}> ?o . FILTER(isIRI(?o)) }}",
        convert_bindings=True,
    )["results"]["bindings"]
    return [IRI(str(row["o"])) for row in rows]


def _descriptor_for(*, ogm: Any, prop_iri: IRI, registry: SemanticConnectorRegistry):
    """This function returns the registered descriptor whose interface property this property specializes.

    Resolution walks ``rdfs:subPropertyOf*``, so ``tu:hasConveyorSpeed`` matches the binding
    registered for ``inf:isInterfaceAccessibleMQTTParameter``. The most specific registered
    match wins. A protocol marker is a subproperty of the generic one, and binding to the
    generic one would leave the middleware without a protocol to speak.
    """
    matches = [
        descriptor
        for descriptor in registry
        if _is_subproperty_of(ogm, prop_iri, descriptor.interface_property)
    ]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    for descriptor in matches:
        specializes_all = all(
            descriptor is other
            or _is_subproperty_of(
                ogm, descriptor.interface_property, other.interface_property
            )
            for other in matches
        )
        if specializes_all:
            return descriptor

    logger.warning(
        "%s matches %d interface properties with no most-specific one (%s); do not bind it.",
        prop_iri,
        len(matches),
        ", ".join(str(d.interface_property) for d in matches),
    )
    return None


def _is_subproperty_of(ogm: Any, prop_iri: IRI, ancestor: IRI) -> bool:
    """This function returns whether ``prop_iri`` is ``ancestor`` or specializes it, transitively."""
    return bool(
        ogm.db.query(
            f"ASK {{ <{prop_iri}> "
            f"<{RDFS.subPropertyOf}>* <{ancestor}> }}"
        ).get("boolean", False)
    )


def _parameter_metadata(
    *, ogm: Any, holder_iri: IRI, prop_iri: IRI, prop_spec: Any
) -> Optional[Dict[str, Any]]:
    """Read one parameter node's properties straight from the ABox.

    Read from the graph rather than from a materialized model because this runs at
    construction, before any datamodel exists. Return only properties the effective shape declares.
    A property the restriction does not declare would never survive a write
    either, so bind to one would produce a connector whose values silently vanish.
    """
    declared = {str(p) for p in getattr(prop_spec.nested, "properties", {})}

    rows = ogm.db.query(
        f"SELECT ?p ?v {EXPLICIT_GRAPH} "
        f"WHERE {{ <{holder_iri}> <{prop_iri}> ?node . ?node ?p ?v }}",
        convert_bindings=True,
    )["results"]["bindings"]

    if not rows:
        logger.warning(
            "%s on %s is interface-accessible but its parameter node is empty; nothing to "
            "bind. Has the resource received provision?",
            prop_iri,
            holder_iri,
        )
        return None

    metadata: Dict[str, Any] = {}
    undeclared = set()
    for row in rows:
        # `row["p"]` is the property IRI -- always stringified for a plain dict key. `row["v"]`
        # is left as `convert_bindings=True` produced it: every metadata property here has been
        # `xsd:string` until now, so this was a no-op, but forcing `str()` on it would turn
        # `inf:hasMQTTBrokerPort`'s `xsd:integer` back into a string (the round trip must hand
        # back 18831, not "18831").
        prop, value = str(row["p"]), row["v"]
        if prop not in declared:
            undeclared.add(prop)
            continue
        metadata.setdefault(prop, []).append(value)

    if undeclared:
        # Naming the property and the restriction that failed to declare it is the whole
        # point: the value would otherwise never flow and the resource would come up
        # silently dead.
        logger.warning(
            "Parameter node of %s on %s carries %s, which the range restriction of %s does "
            "not declare. %s will not be readable or writable through the middleware.",
            prop_iri,
            holder_iri,
            ", ".join(sorted(undeclared)),
            prop_iri,
            "They" if len(undeclared) > 1 else "It",
        )

    return metadata


def class_of(ogm: Any, instance_iri: IRI) -> IRI:
    """Return the individual's asserted domain class, so a caller need not restate it.

    An individual carries several ``rdf:type`` assertions, and their order means nothing.
    So this filters out the structural types rather than trusting them to sort last.
    ``owl:NamedIndividual`` is the trap: it is asserted on everything the OGM writes, and a
    ClassSpec resolved against it cannot resolve at all.

    Public because a consumer that discovers resources in the graph needs exactly this answer,
    and the alternative is the copy that grew in the demo's station board and drifted.
    """
    rows = ogm.db.query(
        f"SELECT ?c {EXPLICIT_GRAPH} "
        f"WHERE {{ <{instance_iri}> a ?c . FILTER(isIRI(?c)) }}",
        convert_bindings=True,
    )["results"]["bindings"]

    candidates = [
        IRI(str(row["c"]))
        for row in rows
        if not str(row["c"]).startswith(META_TYPE_NAMESPACES)
    ]

    if not candidates:
        raise ValueError(
            f"{instance_iri} has no asserted domain rdf:type, so its ClassSpec cannot be "
            f"resolved. Pass resource_class explicitly, or seed the instance first."
        )
    if len(candidates) > 1:
        logger.warning(
            "%s asserts %d domain classes (%s); resolve its ClassSpec against %s. Pass "
            "resource_class explicitly to choose.",
            instance_iri,
            len(candidates),
            ", ".join(str(c) for c in candidates),
            candidates[0],
        )
    return candidates[0]
