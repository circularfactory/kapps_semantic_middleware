"""Every class a shipped Turtle file names as a range or a domain is declared as a class.

The example Docker store's dataset runs the `rdfsplus-optimized` ruleset. Under it, the store
does not report a class that is named only as the `rdfs:range` or `rdfs:domain` of a property as an
`owl:Class` or an `rdfs:Class`, and the object-graph mapper refuses an IRI as a class without
that report. So the files must declare what they name, and need no entailment from the store.

Each file is checked together with the three shared ontologies, which a seed loads beside it.
Offline: the files are read with rdflib, and no store is involved. The shared ontologies are
shipped verbatim, so a class they leave undeclared is reported, not repaired here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import OWL, RDF, RDFS, XSD, Graph, URIRef

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_FILES = sorted(
    (REPO_ROOT / "src" / "kapps_semantic_middleware" / "ontology").glob("*.ttl")
)
OTHER_FILES = sorted(
    [
        *(REPO_ROOT / "examples").glob("*.ttl"),
        *(REPO_ROOT / "demo").rglob("*.ttl"),
    ]
)

# Ranges that name literals or everything, not a class the mapper looks up.
NOT_CLASSES = {RDFS.Literal, RDFS.Resource, RDF.langString, OWL.Thing}


def _declared(graph: Graph) -> set[object]:
    return set(graph.subjects(RDF.type, OWL.Class)) | set(
        graph.subjects(RDF.type, RDFS.Class)
    )


def _undeclared(graph: Graph) -> list[str]:
    declared = _declared(graph)
    found: set[str] = set()
    for predicate, label in ((RDFS.range, "rdfs:range"), (RDFS.domain, "rdfs:domain")):
        for prop, named in graph.subject_objects(predicate):
            if (
                isinstance(named, URIRef)
                and not str(named).startswith(str(XSD))
                and named not in NOT_CLASSES
                and named not in declared
            ):
                found.add(f"{prop} {label} {named}")
    return sorted(found)


def _load(*files: Path) -> Graph:
    graph = Graph()
    for file in files:
        graph.parse(file, format="turtle")
    return graph


def test_the_shipped_files_are_found() -> None:
    assert SHARED_FILES, "no shared ontology found"
    assert OTHER_FILES, "no example or demo Turtle file found"


def test_shared_ontologies_declare_the_classes_they_name() -> None:
    undeclared = _undeclared(_load(*SHARED_FILES))
    assert not undeclared, "\n".join(undeclared)


@pytest.mark.parametrize(
    "file", OTHER_FILES, ids=lambda f: str(f.relative_to(REPO_ROOT))
)
def test_example_and_demo_file_declares_the_classes_it_names(file: Path) -> None:
    undeclared = _undeclared(_load(*SHARED_FILES, file))
    assert not undeclared, (
        f"{file.relative_to(REPO_ROOT)} names a class that no loaded file declares. Declare "
        "each one, or the dataset needs a ruleset that infers it:\n"
        + "\n".join(undeclared)
    )
