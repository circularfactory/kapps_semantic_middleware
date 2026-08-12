"""The northbound projection: hiding protocol metadata the ontology declares.

A parameter node bundles two kinds of fact. What the value *means* — its magnitude, its unit,
whether a peer may write it — is northbound content. How the middleware *physically reaches the
device* — a broker address, a topic, an OPC-UA endpoint — is southbound only: a peer holding it
could drive the machine directly and bypass every check the middleware performs.

They are not separable by asking for less. ``PropertySpec._resolve_effective_ranges`` merges the
whole ``rdfs:subPropertyOf*`` chain and there is no merge-depth parameter anywhere in the OGM's
API, so the shape a consumer gets always contains everything every level declares. The middleware
must therefore cut the bundle itself, and it does so on the **shape**, before any data is read.

**What gets cut is decided by the ontology, not by the code.** Walking upward from one parameter
property:

===========================================  =====================  ==========
level                                        contributes            verdict
===========================================  =====================  ==========
the parameter property's own range           value, unit            **keep**
protocol markers between it and the root     broker, topic, …       **delete**
the interface root's own range               ``inf:accessMode``     **keep**
===========================================  =====================  ==========

Deriving the delete set from the **registry** instead — the union of what the registered binding
descriptors declare — was tried and is wrong: it only knows the protocols this middleware happens
to have code for. Measured, on a belt made reachable over both MQTT and OPC-UA with no OPC-UA
binding registered, the registry-derived set removed the MQTT metadata and served
``inf:hasOPCUAEndpoint`` with its address. Asking the ontology finds it, because the ontology is
authoritative about what a protocol parameter *is* whether or not anyone wrote a connector.

Recomputed **at every startup**. Consuming middlewares are decentralized and live in domain
experts' packages; the ontology may have grown a protocol since one of them last looked.

A keep-list — naming what is safe and dropping the rest — was considered and rejected. It reads
as the safer construction, but it is a closed-world assertion in the serving path, and the
architecture has exactly one closed-world moment by design -- SHACL at admission. It
would also hide new legitimate domain content by default, taxing twenty domain engineers to guard
something the OGM write path already governs.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Collection, Dict, Optional, Set

from kapps_triplestore_interface import IRI

from kapps_semantic_middleware.vocabulary import INF

logger = logging.getLogger(__name__)

RDFS_SUBPROPERTY_OF = "http://www.w3.org/2000/01/rdf-schema#subPropertyOf"
RDFS_RANGE = "http://www.w3.org/2000/01/rdf-schema#range"
OWL_INTERSECTION_OF = "http://www.w3.org/2002/07/owl#intersectionOf"
OWL_ON_PROPERTY = "http://www.w3.org/2002/07/owl#onProperty"
RDF_REST = "http://www.w3.org/1999/02/22-rdf-syntax-ns#rest"
RDF_FIRST = "http://www.w3.org/1999/02/22-rdf-syntax-ns#first"


class ProjectionError(RuntimeError):
    """The projection could not prove a payload safe, so nothing is served.

    Raised rather than logged. A projection that cannot determine what to hide has exactly one
    safe behaviour, and continuing is not it: the failure would surface as a broker address on
    a public REST route.
    """


def southbound_properties(
    ogm: Any,
    parameter_property: Any,
    *,
    interface_root: Any = INF.isInterfaceAccessibleParameter,
) -> frozenset:
    """The properties a protocol marker contributes to one parameter's shape.

    Empty for a property that is not interface-accessible at all, which is the ordinary answer
    for a plain object property like ``tu:hasConveyorBelt``.
    """
    markers = _protocol_markers(ogm, parameter_property, interface_root)
    if not markers:
        return frozenset()

    declared = _declared_by(ogm, markers)

    if not declared:
        # The ontology says this parameter is reached over some protocol, and we cannot read
        # what that protocol's contract is. Serving it would mean serving fields we have not
        # classified. Refuse.
        raise ProjectionError(
            f"{parameter_property} is declared interface-accessible through "
            f"{', '.join(sorted(str(m) for m in markers))}, but no connection metadata could be "
            f"read from their rdfs:range. The northbound projection cannot prove what is safe "
            f"to serve, so nothing is served. Check that the interface ontology is loaded and "
            f"that its ranges are owl:Restriction or owl:intersectionOf class expressions."
        )

    return frozenset(declared)


def _protocol_markers(ogm: Any, parameter_property: Any, interface_root: Any) -> Set[str]:
    """The interface properties strictly between a parameter property and the interface root.

    Two exclusions, both load-bearing and neither obvious:

    - **The parameter property itself** matches ``subPropertyOf+ <root>``. Its own range is
      *domain* content — the value and its unit — and deleting that would leave a payload with
      nothing in it.
    - **The interface root** matches too, because GraphDB materialises *reflexive*
      ``rdfs:subPropertyOf`` (RDFS rule rdfs6), so ``+`` behaves like ``*``. Without excluding
      it, the root's own range is treated as protocol metadata and ``inf:accessMode`` — the one
      interface fact a peer legitimately needs — is deleted.

    Both were found by testing, not by reading the query.
    """
    rows = ogm.db.query(
        f"""
        SELECT DISTINCT ?marker WHERE {{
          <{parameter_property}> <{RDFS_SUBPROPERTY_OF}>+ ?marker .
          ?marker <{RDFS_SUBPROPERTY_OF}>+ <{interface_root}> .
          FILTER(?marker != <{parameter_property}> && ?marker != <{interface_root}>)
        }}
        """,
        convert_bindings=True,
    )["results"]["bindings"]
    return {str(row["marker"]) for row in rows}


def _declared_by(ogm: Any, markers: Set[str]) -> Set[str]:
    """Every property named by an ``owl:onProperty`` inside these properties' ranges.

    Handles both range shapes the OGM itself accepts: an ``owl:intersectionOf`` list of
    restrictions, and a single bare ``owl:Restriction``. Reading only the first would silently
    miss a protocol whose contract happens to have one term — and a missed term is a leak.
    """
    values = " ".join(f"<{m}>" for m in markers)
    rows = ogm.db.query(
        f"""
        SELECT DISTINCT ?p WHERE {{
          VALUES ?marker {{ {values} }}
          ?marker <{RDFS_RANGE}> ?range .
          {{ ?range <{OWL_INTERSECTION_OF}>/<{RDF_REST}>*/<{RDF_FIRST}> ?restriction }}
          UNION
          {{ BIND(?range AS ?restriction) }}
          ?restriction <{OWL_ON_PROPERTY}> ?p .
        }}
        """,
        convert_bindings=True,
    )["results"]["bindings"]
    return {str(row["p"]) for row in rows}


def prune_southbound(
    class_spec: Any,
    *,
    ogm: Any,
    interface_root: Any = INF.isInterfaceAccessibleParameter,
    cache: Optional[Dict[str, frozenset]] = None,
) -> Any:
    """Return a copy of ``class_spec`` with each parameter's protocol metadata removed.

    Recurses, because a resource's parameters hang off its components. Every nested spec is
    asked the ontology what its own property contributes southbound, so two parameters reached
    over different protocols are each pruned correctly.

    The input is left untouched: the caller needs both shapes at once — the full one for the
    bindings to read broker addresses out of, the pruned one to serve.

    Copying is deliberately **shallow, per spec node**, never ``copy.deepcopy``. A spec's
    property keys are ``IRI``, which subclasses ``rdflib.URIRef``, which subclasses ``str`` —
    deep-copying one reconstructs it through ``str.__reduce_ex__`` and yields a plain
    ``URIRef``, which still compares equal but has silently lost the ``lined`` accessor
    ``ClassSpec.to_pydantic_model`` needs.

    ``cache`` maps property IRI to its southbound set. Pass one in to have it filled and reused
    — every entry is a live SPARQL round trip, and the caller usually needs the same answers
    again for the binding cross-check.
    """
    cache = {} if cache is None else cache
    removed: Set[str] = set()
    pruned = _pruned_copy(class_spec, ogm, interface_root, cache, removed)
    if removed:
        logger.debug(
            "Northbound projection removed %d protocol propert%s: %s",
            len(removed),
            "y" if len(removed) == 1 else "ies",
            ", ".join(sorted(removed)),
        )
    return pruned


def _pruned_copy(
    class_spec: Any,
    ogm: Any,
    interface_root: Any,
    cache: Dict[str, frozenset],
    removed: Set[str],
) -> Any:
    """One spec node, copied with each nested parameter's protocol metadata gone."""
    properties = getattr(class_spec, "properties", None)
    if not properties:
        return class_spec

    kept = {}
    for prop_iri, prop_spec in properties.items():
        nested = getattr(prop_spec, "nested", None)
        if nested is not None:
            key = str(prop_iri)
            if key not in cache:
                cache[key] = southbound_properties(
                    ogm, prop_iri, interface_root=interface_root
                )
            nested = _pruned_copy(nested, ogm, interface_root, cache, removed)
            nested = _without(nested, cache[key], removed)
            prop_spec = copy.copy(prop_spec)
            prop_spec.nested = nested
        kept[prop_iri] = prop_spec

    pruned = copy.copy(class_spec)
    pruned.properties = kept
    return pruned


def _without(class_spec: Any, southbound: Collection[str], removed: Set[str]) -> Any:
    """A copy of one spec node with the named properties gone."""
    properties = getattr(class_spec, "properties", None)
    if not properties or not southbound:
        return class_spec

    kept = {}
    for prop_iri, prop_spec in properties.items():
        if str(prop_iri) in southbound:
            removed.add(str(prop_iri))
            continue
        kept[prop_iri] = prop_spec

    stripped = copy.copy(class_spec)
    stripped.properties = kept
    return stripped


def carries_southbound(value: Any, southbound: Collection[str]) -> Set[str]:
    """Which of the given protocol properties appear anywhere in a materialized payload.

    The projection's assertion, usable as a guard or in a test: a northbound payload must
    contain none of them. A generated model's field names are IRI-mangled, so this matches the
    mangled form as well as the raw IRI — the former is what a served JSON body carries, the
    latter what a spec or a graph dump does.

    Mangling goes through ``IRI.lined``, the OGM's own field-name derivation, rather than a
    local copy of the rule: a detector that mirrored it could drift and start reporting "clean"
    for a payload that leaks.
    """
    haystack = str(value)
    return {
        prop
        for prop in southbound
        if prop in haystack or IRI(prop).lined in haystack
    }


def cross_check(
    parameter_property: Any,
    ontology_declared: Collection[str],
    descriptor_declared: Optional[Collection[Any]],
) -> None:
    """Warn when a binding's declared metadata and the ontology's disagree.

    The ontology decides what is hidden; a descriptor's ``connection_metadata`` says what that
    binding *reads*. They should coincide, and where they do not, one of the two has drifted:

    - **In the ontology only** — the protocol contract grew a term this binding ignores. Safe
      northbound (it is hidden either way), but the connector may be missing configuration.
    - **In the descriptor only** — the code expects a term the ontology does not declare. That
      term will not survive a write and will not reach the connector, so the parameter may come
      up silently dead. This is the direction that costs debugging time.

    Warned, never raised: a drift in either direction is a real deployment state, and the
    projection is already safe because it follows the ontology.
    """
    if descriptor_declared is None:
        return

    ontology = {str(p) for p in ontology_declared}
    descriptor = {str(p) for p in descriptor_declared}
    if ontology == descriptor:
        return

    only_ontology = sorted(ontology - descriptor)
    only_descriptor = sorted(descriptor - ontology)
    logger.warning(
        "Connection metadata for %s disagrees between the ontology and its binding. "
        "Declared only in the ontology: %s. Declared only by the binding: %s. The projection "
        "follows the ontology, so nothing leaks; but a term the ontology does not declare will "
        "not survive a write, and the parameter may come up with no value flowing.",
        parameter_property,
        only_ontology or "none",
        only_descriptor or "none",
    )


# `load_northbound` stood here: a second entry point that built a spec, pruned it and fetched
# with it, for the consumer that loads a peer's datamodel. Deleted on #105. Nothing in the
# product ever called it -- a consumer reaches the same pruned fetch through
# `WiringPlan.northbound_fetch_kwargs()` (`connectors/wiring.py`), which `plan_wiring` has
# already built by the time anything has a datamodel to load, and which carries the class scope
# a bare prune-and-fetch would have had to be told about separately. Two ways to obtain the one
# shape ADR 0028 governs is one more than the invariant can afford, and the tests it had were
# the only thing keeping the unused one honest.
#
# Its INFO line per pruned parameter went with it. The same breakdown survives as
# `WiringPlan.southbound_by_property`, which #82's station board renders per parameter -- a
# surface a viewer can actually read, rather than a log line in a library with no UI.
