"""An in-memory stand-in of the store contract that applies the writes it takes.

It holds the shipped ontologies in an rdflib graph, answers the reads that a real OGM and the
registration make, and applies each `triples_update` as the store does: the precondition first
(each old triple there, each absent subject without a statement), then the removal and the
insertion, or `PreconditionFailedError` and nothing written. It holds explicit statements only,
and no named graph, so it suits the offline tests of a registration, not of inference.

A test injects another writer between two steps with `before_update`: a callable that the
stand-in calls with each update's arguments before it checks the precondition.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import Mock

from kapps_triplestore_interface import IRI, SPARQLQuery, TripleStore, XSDToPythonTypes
from kapps_triplestore_interface.exceptions import PreconditionFailedError
from kapps_triplestore_interface.utils.utils import convert_multi_bindings_to_python_type
from rdflib import RDF, RDFS, Graph, Literal, URIRef
from rdflib.term import Node

SHARED_ONTOLOGIES = sorted(
    (Path(__file__).resolve().parents[1] / "src" / "kapps_semantic_middleware" / "ontology").glob(
        "*.ttl"
    )
)

# The form in which the triple store interface asks for explicit statements only.
DATASET_CLAUSE = re.compile(r"^FROM <[^>]*>$", re.MULTILINE)

PREFIXES = {
    "rdf:": str(RDF),
    "rdfs:": str(RDFS),
    "xsd:": "http://www.w3.org/2001/XMLSchema#",
    "owl:": "http://www.w3.org/2002/07/owl#",
}


class Refused(PreconditionFailedError):
    """The stand-in's refusal, built without the interface's own arguments."""

    def __init__(self, message: str = "the precondition of the write was false") -> None:
        Exception.__init__(self, message)


def _python_value(term: Node) -> Any:
    if isinstance(term, URIRef):
        iri = IRI(str(term))
        return XSDToPythonTypes.get(iri, iri)
    return term.toPython() if isinstance(term, Literal) else term


def _node(term: Any, position: int = 2) -> Node:
    """A term of a write as an rdflib node: an `IRI` or a prefixed name of a known prefix as a
    URIRef, a plain IRI string in the subject or predicate as a URIRef, any other object as a
    literal."""
    if isinstance(term, (URIRef, Literal)):
        return term
    if isinstance(term, IRI):
        return URIRef(str(term))
    if isinstance(term, str):
        for prefix, namespace in PREFIXES.items():
            if term.startswith(prefix):
                return URIRef(namespace + term[len(prefix) :])
        if position < 2:
            return URIRef(term)
    return Literal(term)


class MemoryStore:
    """The graph and the store contract over it. `store` is the object the OGM takes."""

    def __init__(self) -> None:
        self.graph = Graph()
        for path in SHARED_ONTOLOGIES:
            self.graph.parse(path, format="turtle")
        self.before_update: Optional[Callable[[dict], None]] = None
        self.updates: list[dict] = []
        store = Mock(spec=TripleStore)
        store.dataset = "memory"
        store.triples_get.side_effect = self.triples_get
        store.owl_get_classes_of_individual.side_effect = self.classes_of
        store.is_subclass.side_effect = self.is_subclass
        store.query.side_effect = self.query
        store.triples_update.side_effect = self.triples_update
        store.triples_add.side_effect = self.triples_add
        self.store = store

    # ---- reads ----

    def triples_get(self, sub=None, pred=None, obj=None, **_: Any) -> list:
        pattern = tuple(None if term is None else _node(term, i) for i, term in enumerate((sub, pred, obj)))
        return [
            (IRI(str(s)), IRI(str(p)), _python_value(o)) for s, p, o in self.graph.triples(pattern)
        ]

    def classes_of(self, instance: Any, **_: Any) -> list:
        """The asserted classes of ``instance`` outside the OWL, RDF and RDFS namespaces, as
        the store gives them by default."""
        ignored = (str(RDF), str(RDFS), PREFIXES["owl:"])
        return [
            IRI(str(o))
            for o in self.graph.objects(URIRef(str(instance)), RDF.type)
            if not str(o).startswith(ignored)
        ]

    def is_subclass(self, subclass_iri: Any, class_iri: Any, **_: Any) -> bool:
        return bool(
            self.graph.query(
                f"ASK {{ <{subclass_iri}> <{RDFS.subClassOf}>+ <{class_iri}> }}"
            ).askAnswer
        )

    def query(self, query: Any, update: bool = False, convert_bindings: bool = False, **_: Any):
        assert not update, "the stand-in takes no SPARQL update"
        text = query.to_string() if isinstance(query, SPARQLQuery) else query
        answer = json.loads(self.graph.query(DATASET_CLAUSE.sub("", text)).serialize(format="json"))
        if convert_bindings and "results" in answer:
            answer["results"]["bindings"] = convert_multi_bindings_to_python_type(
                answer["results"]["bindings"]
            )
        return answer

    # ---- the write ----

    def triples_update(
        self,
        old_triples: Any,
        new_triples: Any,
        check_exist: Optional[bool] = True,
        named_graph: Any = None,
        absent_subjects: Optional[list] = None,
    ) -> bool:
        arguments = {
            "old_triples": list(old_triples or []),
            "new_triples": list(new_triples or []),
            "absent_subjects": list(absent_subjects or []),
            "named_graph": named_graph,
        }
        if self.before_update is not None:
            self.before_update(arguments)
        self.updates.append(arguments)
        old = [self._triple(t) for t in arguments["old_triples"]]
        new = [self._triple(t) for t in arguments["new_triples"]]
        for subject in arguments["absent_subjects"]:
            if (URIRef(str(subject)), None, None) in self.graph:
                raise Refused(f"the store holds {subject} already")
        if check_exist:
            for triple in old:
                if triple not in self.graph:
                    raise Refused(f"the store does not hold {triple}")
        for triple in old:
            self.graph.remove(triple)
        for triple in new:
            self.graph.add(triple)
        return True

    def triples_add(
        self, triples: Any, named_graph: Any = None, absent_subjects: Optional[list] = None, **_: Any
    ) -> bool:
        """An insertion, with the precondition of its absent subjects: `triples_update` with no
        old triple."""
        return self.triples_update(
            [], triples, check_exist=False, named_graph=named_graph, absent_subjects=absent_subjects
        )

    @staticmethod
    def _triple(triple: Any) -> tuple:
        s, p, o = triple
        return (_node(s, 0), _node(p, 1), _node(o, 2))

    # ---- helpers for the tests ----

    def add(self, s: Any, p: Any, o: Any) -> None:
        """Add one statement, as another writer would: an `IRI` object as a node."""
        self.graph.add((_node(s, 0), _node(p, 1), _node(o)))

    def objects(self, s: Any, p: Any) -> list[str]:
        return sorted(str(o) for o in self.graph.objects(URIRef(str(s)), URIRef(str(p))))
