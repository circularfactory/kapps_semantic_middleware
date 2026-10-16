"""The dashboard names its instance by the resource's label, read from a live store.

The offline tests in ``test_dashboard.py`` run the dashboard's query against an rdflib graph.
These run the same query against the store, whose SPARQL engine is the one that answers in
production: a simple literal and an ``xsd:string`` must both count as plain, a regional English
tag as English, and an IRI or a blank value as no name. Each test writes the labels of a resource
of its own, and removes them again.
"""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kapps_triplestore_interface import IRI
from rdflib import Literal
from rdflib.namespace import RDFS, XSD

from kapps_semantic_middleware.dashboard import enable_dashboard


def _title(store: Any, resource: IRI) -> str:
    """The `<title>` of the dashboard of an instance that represents ``resource``."""
    mw = SimpleNamespace(app=FastAPI(), resource_iri=resource, ogm=SimpleNamespace(db=store))
    enable_dashboard(mw)
    page = TestClient(mw.app).get("/dashboard").text
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    assert title is not None, page
    return title.group(1)


@pytest.mark.parametrize(
    ("labels", "name"),
    [
        (
            [Literal("Modul 1", lang="de"), Literal("Module 1", lang="en-GB"), Literal("  ")],
            "Module 1",
        ),
        (
            [Literal("Module 1", lang="en"), Literal("Module one")],
            "Module one",
        ),
        (
            [Literal("Module 1", lang="en"), Literal("Module one", datatype=XSD.string)],
            "Module one",
        ),
        (
            [IRI("http://example.org/ksm402#NotALiteral"), Literal("Modul 1", lang="de")],
            "Modul 1",
        ),
        ([], None),
    ],
    ids=[
        "a regional English over German, a blank is no name",
        "a simple literal over English",
        "an xsd:string over English",
        "an IRI is no name",
        "no label: the local name",
    ],
)
def test_the_title_reads_the_preferred_label_from_the_store(
    store: Any, labels: list[Any], name: str | None
) -> None:
    """The store chooses the label that the offline tests choose, and the page shows it."""
    resource = IRI(f"http://example.org/ksm402/{uuid.uuid4().hex}#Module1")
    triples = [(resource, IRI(str(RDFS.label)), label) for label in labels]
    store.triples_add(triples)
    try:
        expected = name if name is not None else "Module1"
        assert _title(store, resource) == f"{expected} — dashboard"
    finally:
        store.triples_delete(triples)
