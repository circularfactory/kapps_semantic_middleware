"""The semantic-connector seam: binding descriptors and their registry.

A **semantic connector** is any connector that can register itself from the knowledge graph.
The system realizes it not as a connector subclass, but as a **binding descriptor**. This is an
object that names the connector class it builds, the interface property it binds to, and
the connection metadata its protocol needs. It also states how to turn one parameter's
metadata into one or more ``add_synced_connector`` registrations.

Reference a ``connector_cls`` rather than subclass it. This is the key point. ``transitional_sync_middleware``
ships about ten connectors: MQTT, OPC-UA, HTTP request and polling, websocket and webhook
client and server, AAS client, model. Self-registration must not be added to any
of them in the sibling repo. A descriptor lets a domain expert make a vendor's connector
semantic, without owning or subclassing its source. It also lets two registration strategies
for one protocol coexist without a shared ancestor.

MQTT is the first instance of this seam, not its shape. See ``mqtt_binding.py``.
"""

# ADR: root 0001, 0023

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Type,
)

from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection, SyncRole
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware.vocabulary import INF, AccessMode

logger = logging.getLogger(__name__)


def first(value: Any) -> Any:
    """The first element of an OGM multi-valued property, or the value itself.

    Every property on a materialized node is a list, because RDF properties are set-valued.
    Connection metadata is single-valued in practice, so unwrap here. This keeps every call
    site from restating it. An empty or absent value yields ``None``.
    """
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def normalize_metadata(metadata: Mapping[Any, Any]) -> Dict[str, Any]:
    """Key a parameter node's properties by the string form of their IRI.

    A metadata mapping reaches a binding either from a ``ClassSpec``-derived dict keyed by
    ``IRI`` or from a materialized model keyed by ``str``. Normalize once here. No
    binding has to be indifferent to which it got, and no binding has to import ``IRI``
    just to do a lookup.
    """
    return {str(key): value for key, value in metadata.items()}


@dataclass(frozen=True)
class Registration:
    """Describe one ``add_synced_connector`` call. Do not perform it.

    A binding yields these instead of touching the middleware directly. So a test can
    check the seam with no running middleware. So the caller decides whether to wire
    them at all. This is what an inspecting instance needs.
    """

    # ADR: 0022, 0032

    connector: Any
    """A constructed framework connector instance (e.g. ``MqttClientConnector``)."""

    sync_direction: SyncDirection
    """Which way this particular connector moves data."""

    model_type: Type[Any]
    """The persistence type of the bound field for framework bookkeeping."""

    formatter: Optional[Any] = None
    """Translates between the device payload and the persistence value."""

    sync_role: SyncRole = SyncRole.READ_WRITE
    """Role in the framework's sync bookkeeping. The direction controls the gating."""

    suffix: str = ""
    """Disambiguate the connector id when one binding yields several registrations."""


@dataclass(frozen=True)
class ParameterBinding:
    """One interface-accessible parameter, resolved and ready to wire.

    Everything here comes from the ClassSpec and the graph, never from materialized instance
    data. This is what makes construction-time registration possible.
    """

    # ADR: 0023

    resource_iri: IRI
    """The individual carrying the parameter, for example a belt or barrier."""

    parameter_property: IRI
    """The domain property whose range is the parameter node (the COMPLEX property)."""

    field_id: str
    """The mangled attribute name the property has on the generated pydantic model."""

    metadata: Dict[str, Any]
    """The parameter node's own properties, keyed by IRI string, connection metadata
    included. Build it with :func:`normalize_metadata`."""

    descriptor: "BindingDescriptor"
    """The binding descriptor that recognized this parameter."""

    node_model_type: Type[Any]
    """The generated pydantic model for the parameter node itself.

    A formatter needs it to rebuild the node from an inbound scalar, because the framework
    replaces the whole value on ``setattr`` and hands the formatter nothing but the payload.
    It comes from the **full** spec, not the pruned northbound one. The node the
    binding writes must still be the shape the graph expects."""

    root_iri: Optional[IRI] = None
    """The top-level resource individual recognition started from (e.g. the TransferUnit).

    ``resource_iri`` above is the *holder* of the parameter, a belt or a barrier. REST
    addressing (ADR 0017) is structural and relative to the **root**, not the holder, so a
    binding one level of nesting deep needs both. ``None`` only for a ``ParameterBinding``
    built by hand outside recognition (as the MQTT tests do); nothing that reads
    ``root_iri`` is reachable from one of those."""

    root_class_local_name: Optional[str] = None
    """The root resource's class IRI, mangled (``IRI(...).lined``), the ``{Model}``
    segment of an ADR 0017 route. Not the bare fragment: ``kapps_ogm``'s
    ``ClassSpec.to_pydantic_model`` names the materialized class after the whole mangled
    IRI (``class_spec.py``), so this must match that or a REST connector's own PUT/GET
    404s against the real served route (kapps_semantic_middleware#80). Paired with
    ``root_iri``; see its docstring."""

    path_steps: Tuple[Tuple[str, str], ...] = ()
    """``(field_name, child_iri)`` hops from ``root_iri`` down to ``resource_iri``, mirroring
    ``rest_router.py``'s ``_accumulate_routes``. Empty when ``resource_iri`` already is the
    root. ``field_name`` is the mangled form (``IRI(prop).lined``), matching what the served
    route actually uses; ``child_iri`` is the raw individual IRI, mangled by
    ``rest_binding.build_parameter_path`` itself."""

    def get(self, prop: IRI) -> Any:
        """The single value of one metadata property, or ``None``."""
        return first(self.metadata.get(str(prop)))

    @property
    def access_mode(self) -> str:
        """The parameter's declared access mode, default to read-only.

        An absent or unrecognised value yields ``read``, so a parameter is never writable by
        accident of omission.
        """
        raw = self.get(INF.accessMode)
        return raw if raw in AccessMode.ALL else AccessMode.READ

    @property
    def label(self) -> str:
        """A short human-readable label for a log line, e.g. "Belt1 hasConveyorSpeed".

        Full mangled IRIs are correct in route paths but unreadable in a log line
        a human watches scroll past. Display-only: never parse it, never use it to address
        anything. Shared here rather than restated per binding descriptor — every binding
        builds one of these for its formatter's log lines.
        """
        resource_fragment = (
            self.resource_iri.fragment
            if hasattr(self.resource_iri, "fragment") and self.resource_iri.fragment
            else str(self.resource_iri)
        )
        param_fragment = (
            self.parameter_property.fragment
            if hasattr(self.parameter_property, "fragment") and self.parameter_property.fragment
            else str(self.parameter_property)
        )
        return f"{resource_fragment} {param_fragment}"


class BindingDescriptor(Protocol):
    """What a semantic connector must declare.

    Deliberately a structural protocol over plain class attributes. A descriptor is
    configuration, and requiring inheritance from a base class would defeat the purpose of
    not owning the connector's source.
    """

    connector_cls: ClassVar[Any]
    """The framework connector class this binding constructs. Never subclass it.

    Type loosely and declare ``ClassVar`` because a descriptor is configuration on a class,
    not state on an instance, and because a connector behind an optional extra may legitimately
    resolve to ``None`` until its dependency is installed."""

    interface_property: ClassVar[IRI]
    """The ``inf:`` marker property a domain property must be a subproperty of to match."""

    connection_metadata: ClassVar[Tuple[IRI, ...]]
    """The properties this binding reads in order to construct a connector.

    **Not** the projection's source of truth. It once was, and that was wrong. A set built from
    the registered bindings only knows the protocols this middleware has code for, so a
    parameter reachable over an unregistered protocol had its endpoint served northbound. The
    projection asks the *ontology* instead (ADR 0028). Cross-check this declaration against
    it at construction, and report a disagreement. The two should coincide, and where they
    do not, either the contract grew a term this binding ignores or the binding expects one the
    ontology never declares."""

    @staticmethod
    def build(
        binding: ParameterBinding,
        direction: SyncDirection,
        *,
        ensure_transport: Optional[Callable[[str, int], None]] = None,
    ) -> Iterable[Registration]:
        """Turn one resolved parameter into the registrations that realize it.

        ``ensure_transport``, when given, is the deployment's transport hook,
        already deduped by ``plan_wiring`` to fire once per distinct ``(host, port)`` across
        this resource's whole wiring. A binding with nothing to bring up (REST)
        simply never calls it."""
        ...


class SemanticConnectorRegistry:
    """Maps an interface property to the binding descriptor that serves it.

    Resolve by the **interface property**, not by ``rdf:type``. A parameter node has no
    named type of its own. It has only anonymous restriction nodes, which are inferred and so
    absent from an explicit-graph fetch. The property hierarchy is what survives.

    Build and consult the registry for **every** connector wiring, including one that
    wires nothing. Do not implement "no connectors" as "no registry". With no registry,
    no property is recognized as a parameter. The parameter node then becomes ordinary
    data, and is served northbound. The least-privileged instance would leak the most.
    """

    # ADR: 0020, 0028

    def __init__(self, descriptors: Optional[Sequence[Type[BindingDescriptor]]] = None) -> None:
        self._by_property: Dict[str, Type[BindingDescriptor]] = {}
        for descriptor in descriptors or ():
            self.register(descriptor)

    def register(self, descriptor: Type[BindingDescriptor]) -> Type[BindingDescriptor]:
        """Add a descriptor, replace any earlier one for the same interface property.

        We deliberately replace rather than refuse. A domain expert may override the
        built-in MQTT binding with a site-specific one. This is a supported use of the
        seam. The expert should not have to unregister first.

        Takes the descriptor **class**, not an instance of it. Every member of
        ``BindingDescriptor`` is a ``ClassVar``, so the class itself is the configuration.
        """
        key = str(descriptor.interface_property)
        existing = self._by_property.get(key)
        if existing is not None and existing is not descriptor:
            logger.info(
                "Replace the binding registered for %s (%s -> %s)",
                key,
                existing.__name__,
                descriptor.__name__,
            )
        self._by_property[key] = descriptor
        return descriptor

    def for_interface_property(self, iri: IRI) -> Optional[Type[BindingDescriptor]]:
        """The descriptor that registers for exactly this interface property, if any."""
        return self._by_property.get(str(iri))

    def declared_connection_metadata(self) -> frozenset:
        """Every property any registered binding reads, keyed by IRI string.

        **Not the projection's prune set**. See ``BindingDescriptor.connection_metadata``.
        This is one side of the construction-time cross-check against what the ontology
        declares. The projection itself follows the ontology
        (:func:`kapps_semantic_middleware.projection.southbound_properties`).
        """
        return frozenset(
            str(prop)
            for descriptor in self._by_property.values()
            for prop in descriptor.connection_metadata
        )

    def __len__(self) -> int:
        return len(self._by_property)

    def __iter__(self) -> Iterator[Type[BindingDescriptor]]:
        return iter(self._by_property.values())


# The registry a SemanticMiddleware gets when the system does not hand one. Populate when the
# binding modules are imported, so the MQTT binding is available out of the box while
# remaining an ordinary registration rather than a special case.
default_registry = SemanticConnectorRegistry()


def semantic_connector(cls):
    """Register a binding descriptor class on the default registry.

    Use as a plain decorator on the descriptor class::

        @semantic_connector
        class MQTTBinding:
            connector_cls = MqttClientConnector
            interface_property = INF.isInterfaceAccessibleMQTTParameter
            ...
    """
    default_registry.register(cls)
    return cls


def resolve_direction(access_mode: str, flavour: SyncDirection) -> SyncDirection:
    """The most restrictive of the parameter access mode and the instance flavor.

    Neither may widen the other. A monitor can therefore never drive a writable
    belt, and a controller can never write a read-only sensor. This is structural, not by
    convention.

    The read leg is always available. The question is only whether the write leg is. So this
    returns ``BIDIRECTIONAL`` when both sides permit writing, and ``TO_PERSISTENCE``
    (device to middleware, read-only) otherwise.
    """
    parameter_permits_write = access_mode == AccessMode.READWRITE
    flavour_permits_write = flavour in (
        SyncDirection.BIDIRECTIONAL,
        SyncDirection.FROM_PERSISTENCE,
    )
    if parameter_permits_write and flavour_permits_write:
        return SyncDirection.BIDIRECTIONAL
    return SyncDirection.TO_PERSISTENCE
