"""Offline tests for `seeding`: a seed stops where the store refused the clear.

A fake store stands in for the store of `connect()`. It answers `graph_clear()` the way the
store's contract does, raising the store's error, or the way a store outside the contract might,
returning `False`. The shared ontologies load with the Graph Store verbs.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest
from kapps_triplestore_interface import TripleStoreResponseError
from requests.exceptions import RequestException

from kapps_semantic_middleware.errors import DeploymentValueError
from kapps_semantic_middleware.seeding import (
    _ALLOW_DESTRUCTIVE_ENV,
    CORE_GRAPH,
    MES_GRAPH,
    SERVICE_GRAPH,
    clear_dataset,
    is_disposable_target,
    load_shared_ontologies,
)


class _FakeStore:
    """A store with one dataset whose `graph_clear()` answers what it is told."""

    def __init__(
        self,
        dataset: str,
        answer=True,
        populated: bool = False,
        url: str | None = "http://localhost:7200",
    ):
        self.dataset = dataset
        self._endpoint = SimpleNamespace(url=url)
        self._answer = answer
        self._populated = populated
        self.cleared: list[tuple] = []
        self.added: list[tuple] = []

    def graph_clear(self, *args):
        self.cleared.append(args)
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer

    def graph_add(self, content, **kwargs):
        self.added.append((kwargs.get("graph_iri"), kwargs["content_type"]))
        return True

    def query(self, query, **kwargs):
        return {"boolean": self._populated}


@pytest.fixture(autouse=True)
def _no_opt_out(monkeypatch):
    monkeypatch.delenv(_ALLOW_DESTRUCTIVE_ENV, raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:7200",
        "http://127.0.0.1:7200/",
        "http://[::1]:7200",
        "http://LOCALHOST",
        "http://store:7200",
    ],
)
def test_a_loopback_host_and_a_bare_alias_are_disposable(url):
    assert is_disposable_target(url)


@pytest.mark.parametrize(
    "url", ["https://store.example.org", "http://10.0.0.5:7200", "", None, "localhost:7200x/"]
)
def test_a_dotted_host_and_an_unreadable_url_are_not_disposable(url):
    assert not is_disposable_target(url)


def test_a_clear_on_a_dotted_host_is_refused_before_anything_is_cleared():
    store = _FakeStore("Tests", url="https://store.example.org")

    with pytest.raises(DeploymentValueError, match="TRIPLESTORE_ALLOW_DESTRUCTIVE") as raised:
        clear_dataset(store)

    assert "'Tests'" in str(raised.value)
    assert "https://store.example.org" in str(raised.value)
    assert store.cleared == []


def test_a_reload_on_a_dotted_host_is_refused_before_anything_is_cleared():
    store = _FakeStore("Tests", url="https://store.example.org", populated=True)

    with pytest.raises(DeploymentValueError):
        load_shared_ontologies(store, reload=True)

    assert store.cleared == [] and store.added == []


def test_a_load_without_reload_wipes_nothing_so_a_dotted_host_is_fine():
    store = _FakeStore("Tests", url="https://store.example.org")

    load_shared_ontologies(store)

    assert len(store.added) == 3


def test_a_store_that_names_no_url_is_refused():
    store = _FakeStore("Tests", url=None)

    with pytest.raises(DeploymentValueError):
        clear_dataset(store)


def test_the_opt_in_lets_a_dotted_host_be_cleared(monkeypatch):
    monkeypatch.setenv(_ALLOW_DESTRUCTIVE_ENV, "1")
    store = _FakeStore("Tests", url="https://store.example.org")

    clear_dataset(store)

    assert store.cleared == [()]


def test_an_opt_in_other_than_one_does_not_count(monkeypatch):
    monkeypatch.setenv(_ALLOW_DESTRUCTIVE_ENV, "true")

    with pytest.raises(DeploymentValueError):
        clear_dataset(_FakeStore("Tests", url="https://store.example.org"))


def test_a_clear_that_returns_false_raises_naming_the_dataset():
    store = _FakeStore("scenario-seeds", answer=False)

    with pytest.raises(TripleStoreResponseError, match="'scenario-seeds'"):
        clear_dataset(store)


# Any error passes on, whatever its type: the store's error answer, and a store out of reach.
@pytest.mark.parametrize(
    "error",
    [
        TripleStoreResponseError("HTTP 500: Internal error while trying to validate SHACL Shape"),
        RequestException("the store cannot be reached"),
    ],
    ids=["error-answer", "unreachable"],
)
def test_a_clear_that_raises_passes_the_error_on_unchanged(error):
    store = _FakeStore("scenario-seeds", answer=error)

    with pytest.raises(type(error)) as raised:
        clear_dataset(store)

    assert raised.value is error


def test_a_clear_that_succeeds_clears_the_default_graph_once_and_returns():
    store = _FakeStore("scenario-seeds", answer=True)

    clear_dataset(store)

    assert store.cleared == [()]


def test_the_shared_ontologies_load_each_into_its_graph_with_graph_add():
    store = _FakeStore("scenario-seeds")

    load_shared_ontologies(store)

    assert store.cleared == []
    assert store.added == [
        (graph, "application/x-turtle") for graph in (CORE_GRAPH, SERVICE_GRAPH, MES_GRAPH)
    ]


def test_a_reload_clears_each_graph_with_graph_clear_first():
    store = _FakeStore("scenario-seeds", populated=True)

    load_shared_ontologies(store, reload=True)

    assert store.cleared == [(CORE_GRAPH,), (SERVICE_GRAPH,), (MES_GRAPH,)]
    assert [graph for graph, _ in store.added] == [CORE_GRAPH, SERVICE_GRAPH, MES_GRAPH]


def test_a_graph_that_holds_statements_is_not_loaded_again():
    store = _FakeStore("scenario-seeds", populated=True)

    load_shared_ontologies(store)

    assert store.added == []


# vendor-check: off -- the deprecated name, until it goes.


def test_the_old_clear_warns_once_and_runs_its_successor():
    from kapps_semantic_middleware.seeding import clear_repository

    store = _FakeStore("scenario-seeds", answer=False)

    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        with pytest.raises(TripleStoreResponseError, match="'scenario-seeds'"):
            clear_repository(store)

    assert [str(w.message) for w in record] == [
        "clear_repository is deprecated; use clear_dataset, which replaces it."
    ]
    assert store.cleared == [()]


# vendor-check: on


def test_a_scenario_seed_replaces_the_shared_ontology_graphs_before_it_adds_them():
    """A dataset seeded by an earlier version keeps its old service graph unless the seed reloads."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
    import seed

    store = _FakeStore("Tests", populated=True)

    seed.load_scenario1_ontologies(store)

    shared = {SERVICE_GRAPH, CORE_GRAPH, MES_GRAPH}
    assert shared <= {args[0] for args in store.cleared}
    assert shared <= {graph for graph, _ in store.added}
