"""Provisioning: write each declared parameter's connection metadata onto the instance.

At first setup a parameter node carries no connection metadata, and recognition finds nothing
to bind. The embedding code's declaration, a :class:`Provision`, is then the only source of the
parameter's interface. This module writes the declared metadata onto the instance through the
OGM, never as raw triples, so the metadata is in the graph for this instance's recognition and
for every peer that discovers what it wired. From then on the middleware works from the graph
alone.

The crux is idempotency. A declaration is compared with what the stored node carries:

- the node carries none of a declared property: the commit writes it;
- the node carries the declared values: nothing is written, so a second start changes nothing;
- the node carries other values of a declared property: that is a contradiction between
  instance and embedding code, and construction fails loudly, unless the declaration is
  **exclusive**. An exclusive declaration replaces the stored values, and the OGM
  removes the metadata of every interface the declaration does not select, in the same commit.

The metadata of another interface on the node is no contradiction. Every interface that the
node gives evidence of merges with the declared one, and a plain declaration leaves that
metadata as it is: a node may be reached over two protocols. Only an exclusive declaration
makes it stale. A property that no interface restricts, such as an ``rdfs:comment``, is never
stale.

The write updates the stored parameter node, so the OGM keeps its address: a re-provision
never makes a second parameter node. All writes of one start are one ``OGM.commit``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from kapps_ogm.utils.class_scope import ClassScope
from kapps_ogm.utils.skolem import is_skolem_iri
from kapps_triplestore_interface import IRI, SPARQLQuery
from rdflib import BNode

from kapps_semantic_middleware.connectors.wiring import class_of, linked_individuals
from kapps_semantic_middleware.errors import DeploymentValueError

if TYPE_CHECKING:
    from kapps_ogm import Interface

logger = logging.getLogger(__name__)

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


@dataclass(frozen=True)
class Provision:
    """The embedding code's declaration of one parameter: its interface and its metadata.

    Args:
        holder_iri: The individual that holds the parameter, for example a conveyor belt.
        property_iri: The parameter property, for example ``tu:hasConveyorSpeed``.
        interface_iri: The interface property that reaches the parameter, for example
            ``inf:isInterfaceAccessibleMQTTParameter``. The parameter property resolves as if it
            were ``rdfs:subPropertyOf`` it, also without that assertion in the ontology.
        metadata: The content to write on the parameter node, by property IRI, each with its
            list of values: the connection metadata, and any domain content such as a unit.
            A value is always a list, also a single one: ``{INF_ACCESS_MODE: ["readwrite"]}``.
        exclusive: Whether the declaration overrides the stored metadata. Without it, a
            stored value that differs from a declared one fails construction.

    Raises:
        DeploymentValueError: If the values of a property are a string, bytes, not iterable,
            or empty. A string would split into its characters, and an empty list declares
            nothing. It is a ``ValueError``.
    """

    holder_iri: IRI
    property_iri: IRI
    interface_iri: IRI
    metadata: Mapping[Any, Sequence[Any]] = field(default_factory=dict)
    exclusive: bool = False

    def __post_init__(self) -> None:
        # A caller may pass strings or rdflib terms; every use below compares IRIs.
        for name in ("holder_iri", "property_iri", "interface_iri"):
            object.__setattr__(self, name, IRI(str(getattr(self, name))))
        metadata: Dict[IRI, list] = {}
        for p, values in self.metadata.items():
            if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
                raise DeploymentValueError(
                    f"The declaration of {self.property_iri} on {self.holder_iri} gives {p} the "
                    f"value {values!r}, not a list of values. Write [{values!r}]."
                )
            metadata[IRI(str(p))] = list(values)
            if not metadata[IRI(str(p))]:
                raise DeploymentValueError(
                    f"The declaration of {self.property_iri} on {self.holder_iri} gives {p} no "
                    "value. Declare at least one value, or leave the property out."
                )
        object.__setattr__(self, "metadata", metadata)

    def interface(self) -> Interface:
        """The declaration as the OGM takes it, for a write."""
        return _ogm_interface()(
            self.property_iri, self.interface_iri, exclusive=self.exclusive
        )


def _ogm_interface() -> Any:
    """The OGM's ``Interface``, imported here and not at module level: a kapps-ogm before
    0.4.0 has none, and the package must import on it. Only a middleware with provisions reaches this.

    Raises:
        DeploymentValueError: If the installed OGM has no ``Interface``.
    """
    try:
        from kapps_ogm import Interface
    except ImportError as error:
        try:
            installed = version("kapps-ogm")
        except PackageNotFoundError:
            installed = "unknown"
        raise DeploymentValueError(
            "Provisions need a kapps-ogm with per-instance interfaces (kapps_ogm.Interface); "
            f"the installed kapps-ogm {installed} has none. Install kapps-ogm 0.4.0 or later, "
            "or start without provisions."
        ) from error
    return Interface


def check_provisions(
    ogm: Any, provisions: Sequence[Provision], *, resource_iri: IRI, class_scope: Any
) -> None:
    """Refuse ``provisions`` that the middleware cannot honour, before anything is written.

    Raises:
        DeploymentValueError: If the installed OGM has no ``Interface``, or two declarations
            name the same parameter of one holder, or a declared parameter is not reached
            under ``class_scope`` from ``resource_iri``: it would be written but never wired.
            It is a ``ValueError``.
    """
    if not provisions:
        return
    # Only for its refusal: every later step of a start with provisions needs the Interface.
    _ogm_interface()

    declared: Set[Tuple[IRI, IRI]] = set()
    for declaration in provisions:
        pair = (declaration.holder_iri, declaration.property_iri)
        if pair in declared:
            raise DeploymentValueError(
                f"{declaration.property_iri} on {declaration.holder_iri} is declared more than "
                "once. Declare each parameter of a holder once."
            )
        declared.add(pair)

    unreached = sorted(declared - _scoped_pairs(ogm, resource_iri, class_scope))
    if unreached:
        raise DeploymentValueError(
            f"The class_scope does not reach these declared parameters from {resource_iri}, "
            "so they would be written but never wired: "
            f"{', '.join(f'{prop} on {holder}' for holder, prop in unreached)}. Add their "
            "property chain to the class_scope, or correct the declaration."
        )


def _scoped_pairs(ogm: Any, resource_iri: IRI, class_scope: Any) -> Set[Tuple[IRI, IRI]]:
    """Each ``(holder, property)`` that ``class_scope`` reaches from ``resource_iri``: every
    individual at a position of a property chain, over the asserted links, with the chain's
    next property."""
    pairs: Set[Tuple[IRI, IRI]] = set()
    for chain in class_scope.to_property_chains():
        holders = [IRI(str(resource_iri))]
        for step in chain:
            prop = IRI(str(step))
            pairs.update((holder, prop) for holder in holders)
            holders = [
                linked for holder in holders for linked in linked_individuals(ogm, holder, prop)
            ]
    return pairs


def declared_interfaces(provisions: Sequence[Provision]) -> Tuple[Interface, ...]:
    """The interfaces that ``provisions`` declare, for recognition and the projection.

    Not exclusive, and each ``(property, interface)`` pair once: two holders share one
    parameter property, and the OGM refuses declarations of one property that disagree on
    ``exclusive``. The flag selects what a write keeps; a read needs only the shape.
    """
    if not provisions:
        return ()
    Interface = _ogm_interface()

    pairs = {(p.property_iri, p.interface_iri) for p in provisions}
    return tuple(Interface(prop, iface) for prop, iface in sorted(pairs))


def provision(
    ogm: Any,
    provisions: Sequence[Provision],
    *,
    named_graph: Optional[IRI] = None,
) -> List[IRI]:
    """Write what ``provisions`` declare and the stored nodes lack, as one ``OGM.commit``.

    Returns:
        The holders that the commit wrote, in declaration order. Empty when every stored node
        carries its declared metadata already; then nothing is sent.

    Raises:
        DeploymentValueError: If a stored node contradicts a declaration that is not
            exclusive, or a holder carries more than one node of a declared property
            (Repair): correct the declaration or the data. Nothing is written. It is a
            ``ValueError``.
    """
    from kapps_ogm import Update

    by_holder: Dict[IRI, List[Provision]] = {}
    for declaration in provisions:
        by_holder.setdefault(declaration.holder_iri, []).append(declaration)

    changes = []
    for holder, declarations in by_holder.items():
        data = _holder_data(ogm, holder, declarations)
        if data:
            changes.append(
                Update(
                    instance_iri=holder,
                    data=data,
                    interfaces=[d.interface() for d in declarations],
                )
            )

    if changes:
        ogm.commit(changes=changes, named_graph=named_graph)
        logger.info(
            "Provisioned the connection metadata of %d holder(s): %s",
            len(changes),
            ", ".join(str(change.instance_iri) for change in changes),
        )
    return [IRI(str(change.instance_iri)) for change in changes]


def _holder_data(ogm: Any, holder: IRI, declarations: List[Provision]) -> Dict[IRI, list]:
    """The data of one holder's ``Update``: each declared property whose node needs a write."""
    shapes = _declared_shapes(ogm, holder, declarations)
    data: Dict[IRI, list] = {}
    for declaration in declarations:
        prop = declaration.property_iri
        node, stored = _stored_node(ogm, holder, prop)
        if not _needs_write(ogm, declaration, stored, shapes[prop]):
            continue
        # The node keeps what it carries inside the declared shape: an Update replaces the
        # property's values, so content left out of the data would be removed. Content outside
        # the shape is left out on purpose: the OGM keeps it under a plain declaration, and
        # under an exclusive one removes what an interface it does not select restricts.
        content: Dict[Any, Any] = {
            p: values for p, values in (stored or {}).items() if str(p) in shapes[prop]
        }
        content.update(declaration.metadata)
        # A named node is written onto by its IRI: the OGM gives a value without an IRI a new
        # node, and aligns only anonymous nodes with the stored ones by position.
        if node is not None and not isinstance(node, BNode) and not is_skolem_iri(IRI(str(node))):
            content["id"] = IRI(str(node))
        data[prop] = [content]
    return data


def _needs_write(
    ogm: Any, declaration: Provision, stored: Optional[Dict[IRI, list]], shape: Set[str]
) -> bool:
    """Whether the stored node lacks, or contradicts, what ``declaration`` declares, or, for an
    exclusive declaration, carries metadata that the declaration makes stale.

    Raises:
        DeploymentValueError: If the node carries a value of a declared property that differs
            from the declaration, and the declaration is not exclusive.
    """
    if stored is None:
        return True

    contradictions = []
    missing = False
    for p, values in declaration.metadata.items():
        have = sorted({_comparable(v) for v in stored.get(p, [])})
        want = sorted({_comparable(v) for v in values})
        if not have:
            missing = True
        elif have != want:
            contradictions.append(f"{p}: stored {have}, declared {want}")

    if not declaration.exclusive:
        if contradictions:
            raise DeploymentValueError(
                f"The parameter node of {declaration.property_iri} on {declaration.holder_iri} "
                f"contradicts its declaration: {'; '.join(contradictions)}. Correct the "
                "declaration or the data, or mark the declaration exclusive to replace the "
                "stored metadata."
            )
        return missing
    return missing or bool(contradictions) or _carries_stale(ogm, declaration, stored, shape)


def _carries_stale(
    ogm: Any, declaration: Provision, stored: Dict[IRI, list], shape: Set[str]
) -> bool:
    """Whether the node carries a property outside ``shape`` that the range of another
    interface restricts: metadata that an exclusive declaration makes stale.

    The OGM's own test of evidence decides it, so this writes exactly when the OGM removes
    something. The OGM subtracts every interface that the holder's ``Update`` declares for the
    property; that is ``declaration.interface_iri`` alone, since :func:`check_provisions`
    refuses a second declaration of one parameter. A property that no interface restricts,
    such as an ``rdfs:comment``, stays.
    """
    # Imported here for the same reason as ``Interface``: a kapps-ogm before 0.4.0 has no
    # such module.
    from kapps_ogm.mapping.interfaces import evidenced_interfaces

    return any(
        evidenced_interfaces(ogm, declaration.property_iri, [p]) - {declaration.interface_iri}
        for p in stored
        if str(p) not in shape
    )


def _comparable(value: Any) -> str:
    """A value in the form a stored literal and a declared python value share: ``18831`` and
    the stored ``"18831"^^xsd:integer`` compare equal, and so do ``True`` and ``"true"``."""
    return str(value).lower() if isinstance(value, bool) else str(value)


def _declared_shapes(
    ogm: Any, holder: IRI, declarations: List[Provision]
) -> Dict[IRI, Set[str]]:
    """The properties that each declared parameter node may hold, with the declared interfaces.

    The class's ``rdfs:subPropertyOf`` markers merge in as always; the evidence of the stored
    nodes does not, so a property outside this shape is one only the evidence admits.
    """
    props = sorted({d.property_iri for d in declarations})
    spec = ogm.get_class_spec(
        class_iri=class_of(ogm, holder),
        class_scope=ClassScope.from_property_chains([[prop] for prop in props]),
        interfaces=[d.interface() for d in declarations],
    )
    shapes: Dict[IRI, Set[str]] = {}
    for prop in props:
        prop_spec = spec.properties.get(prop)
        nested = getattr(prop_spec, "nested", None)
        if nested is None:
            raise DeploymentValueError(
                f"{prop} is declared for provisioning on {holder}, but the class of {holder} "
                "has no such parameter property. Correct the declaration."
            )
        shapes[prop] = {str(p) for p in nested.properties}
    return shapes


def _stored_node(
    ogm: Any, holder: IRI, prop: IRI
) -> Tuple[Optional[Any], Optional[Dict[IRI, list]]]:
    """The address and the asserted content of ``holder``'s one node of ``prop``; two ``None``
    without a node.

    Raises:
        DeploymentValueError: If ``holder`` carries more than one node of ``prop``.
    """
    rows = ogm.db.query(
        SPARQLQuery.select(
            variables=["?node", "?p", "?v"],
            where_clauses=[f"<{holder}> <{prop}> ?node . OPTIONAL {{ ?node ?p ?v }}"],
            include_implicit=False,
        ),
        convert_bindings=True,
    )["results"]["bindings"]
    nodes = {str(row["node"]) for row in rows}
    if not nodes:
        return None, None
    if len(nodes) > 1:
        raise DeploymentValueError(
            f"{holder} carries {len(nodes)} nodes of {prop} ({', '.join(sorted(nodes))}); "
            "provisioning writes onto one. Remove the extra nodes."
        )
    content: Dict[IRI, list] = {}
    for row in rows:
        if "p" in row and str(row["p"]) != RDF_TYPE:
            content.setdefault(IRI(str(row["p"])), []).append(row["v"])
    return rows[0]["node"], content
