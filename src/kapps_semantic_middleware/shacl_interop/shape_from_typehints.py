"""Generate SHACL NodeShapes, from the type hints of a Python function.

This is temporary scaffolding for workflow signature representation. This logic
properly belongs in kapps_ogm. Delete this logic from here once that support
lands.

The shape targets the Workflow *class* (sh:targetClass). It does not target instances. Mint argument
property IRIs as ``{workflow_class_iri}#param_{name}``. Mint the return property IRI as
``{workflow_class_iri}#return``. A zero-argument function has no return annotation or ``None`` return
annotation. It produces a minimal shape. The shape is the NodeShape and its sh:targetClass only.
"""

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Callable
from typing import Any, Union, get_args, get_origin

from rdflib import BNode, Graph, Literal, Namespace, URIRef, XSD
from rdflib.namespace import RDF

from kapps_semantic_middleware.vocabulary import SVC, SVC_NS

# SHACL namespace
SH = Namespace("http://www.w3.org/ns/shacl#")

# Python-to-XSD datatype mapping
PYTHON_TO_XSD: dict[type, URIRef] = {
    str: XSD.string,
    bool: XSD.boolean,
    int: XSD.integer,
    float: XSD.double,
    bytes: XSD.base64Binary,
}

# Union origins cover both typing.Optional[T]/typing.Union[...] and PEP 604 (T | None).
_UNION_ORIGINS: tuple[Any, ...] = (Union, getattr(types, "UnionType", Union))


def _unwrap_optional(type_hint: Any) -> tuple[Any, bool]:
    """Unwrap ``Optional[T]`` or ``T | None`` into ``(inner_type, is_optional)``.

    For a two-member union with ``NoneType``, return the non-None member and
    ``True``. For any other type hint, return it unchanged with ``False``.
    """
    origin = get_origin(type_hint)
    if origin in _UNION_ORIGINS:
        args = get_args(type_hint)
        non_none = [a for a in args if a is not type(None)]
        if len(args) == 2 and len(non_none) == 1:
            return non_none[0], True
        # Wider unions cannot map to a single datatype. Report optionality only.
        return (non_none[0] if non_none else Any), (type(None) in args)
    return type_hint, False


def build_workflow_shape(
    func: Callable,
    workflow_class_iri: str | URIRef,
    *,
    shape_iri: str | URIRef | None = None,
) -> Graph:
    """Build a SHACL NodeShape. Describe a Workflow precondition and outcome.

    Args:
        func: The Python function. Its type hints define the shape.
        workflow_class_iri: The IRI of the Workflow class. This shape targets it.
        shape_iri: Optional explicit IRI for the shape node. Omit it. Derive it
            by append ``"Shape"`` to ``workflow_class_iri``.

    Returns:
        An rdflib Graph. It contains the NodeShape with bound prefixes.
    """
    g = Graph()

    # Normalize IRIs to URIRef
    if isinstance(workflow_class_iri, str):
        workflow_class_iri = URIRef(workflow_class_iri)
    if shape_iri is None:
        shape_iri = URIRef(f"{workflow_class_iri}Shape")
    elif isinstance(shape_iri, str):
        shape_iri = URIRef(shape_iri)

    # Convert vocabulary IRIs (kapps_triplestore_interface.IRI) to rdflib.URIRef
    precondition_iri = URIRef(str(SVC.precondition))
    outcome_iri = URIRef(str(SVC.outcome))

    # Create the NodeShape with targetClass
    g.add((shape_iri, RDF.type, SH.NodeShape))
    g.add((shape_iri, SH.targetClass, workflow_class_iri))

    # Introspect function signature and type hints
    sig = inspect.signature(func)
    try:
        type_hints = typing.get_type_hints(func)
    except Exception:
        # If get_type_hints fails, degrade gracefully. Example: unresolved forward references.
        type_hints = {}

    # Filter out self/cls parameters (for methods)
    params = [p for p in sig.parameters.values() if p.name not in ("self", "cls")]

    # Build PRECONDITION shape if there are parameters
    if params:
        precondition_node = BNode()
        g.add((shape_iri, precondition_iri, precondition_node))
        g.add((precondition_node, RDF.type, SH.NodeShape))

        for param in params:
            prop_node = BNode()
            g.add((precondition_node, SH.property, prop_node))

            # Mint deterministic property IRI: {workflow_class_iri}#param_{name}
            param_iri = URIRef(f"{workflow_class_iri}#param_{param.name}")
            g.add((prop_node, SH.path, param_iri))

            type_hint = type_hints.get(param.name, Any)
            unwrapped_type, is_optional = _unwrap_optional(type_hint)

            # Map to XSD datatype if known. Skip if unknown.
            if unwrapped_type in PYTHON_TO_XSD:
                g.add((prop_node, SH.datatype, PYTHON_TO_XSD[unwrapped_type]))

            # Cardinality: minCount=1 iff required (no default AND not Optional)
            has_default = param.default is not inspect.Parameter.empty
            if not has_default and not is_optional:
                g.add((prop_node, SH.minCount, Literal(1, datatype=XSD.integer)))
            g.add((prop_node, SH.maxCount, Literal(1, datatype=XSD.integer)))

    # Build OUTCOME shape if there is a meaningful (non-None) return annotation.
    # Note: typing.get_type_hints() normalizes a ``-> None`` annotation to
    # ``type(None)`` (NoneType). Both the bare ``None`` and ``NoneType`` must
    # count as "no outcome". An absent annotation also counts.
    return_annotation = type_hints.get("return", inspect.Signature.empty)
    if return_annotation not in (None, type(None), inspect.Signature.empty):
        outcome_node = BNode()
        g.add((shape_iri, outcome_iri, outcome_node))
        g.add((outcome_node, RDF.type, SH.NodeShape))

        return_prop = BNode()
        g.add((outcome_node, SH.property, return_prop))

        return_iri = URIRef(f"{workflow_class_iri}#return")
        g.add((return_prop, SH.path, return_iri))

        unwrapped_return, _ = _unwrap_optional(return_annotation)
        if unwrapped_return in PYTHON_TO_XSD:
            g.add((return_prop, SH.datatype, PYTHON_TO_XSD[unwrapped_return]))

        g.add((return_prop, SH.maxCount, Literal(1, datatype=XSD.integer)))

    # Bind prefixes for serialization
    g.bind("sh", SH)
    g.bind("svc", Namespace(SVC_NS))
    g.bind("xsd", XSD)

    return g
