"""The start checks the dataset's ruleset against the one the deployment requires (offline).

A store stub answers `ruleset_get`; nothing reaches a network.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Optional

import pytest

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.errors import DeploymentRuntimeError

_VARIABLE = "TRIPLESTORE_REQUIRED_RULESET"
_LOGGER = "kapps_semantic_middleware"


class _Store:
    dataset = "the-dataset"

    def __init__(
        self, answer: Optional[str] = None, error: Optional[Exception] = None
    ) -> None:
        self._answer = answer
        self._error = error
        self.asked = 0

    def ruleset_get(self) -> Optional[str]:
        self.asked += 1
        if self._error is not None:
            raise self._error
        return self._answer


def _start(store: _Store) -> SemanticMiddleware:
    return SemanticMiddleware(
        resource_iri="http://example.org/r",
        service_class="http://example.org/Service",
        ogm=SimpleNamespace(db=store),
    )


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    monkeypatch.delenv(_VARIABLE, raising=False)


def test_another_ruleset_refuses_the_start_with_one_readable_line(monkeypatch, caplog):
    monkeypatch.setenv(_VARIABLE, "rdfsplus-optimized")
    caplog.set_level(logging.INFO, logger=_LOGGER)

    with pytest.raises(DeploymentRuntimeError) as refusal:
        _start(_Store("owl2-rl"))

    assert str(refusal.value) == (
        "dataset `the-dataset` runs ruleset `owl2-rl`; "
        "this deployment requires `rdfsplus-optimized`"
    )
    assert [r.getMessage() for r in caplog.records if "owl2-rl" in r.getMessage()] == [
        str(refusal.value)
    ]


def test_a_matching_ruleset_starts(monkeypatch):
    monkeypatch.setenv(_VARIABLE, "rdfsplus-optimized")
    store = _Store("rdfsplus-optimized")
    assert _start(store).mode == "resource"
    assert store.asked == 1


def test_an_unknown_ruleset_starts_and_logs_one_line(monkeypatch, caplog):
    monkeypatch.setenv(_VARIABLE, "rdfsplus-optimized")
    caplog.set_level(logging.INFO, logger=_LOGGER)

    _start(_Store(None))

    lines = [r.getMessage() for r in caplog.records if "ruleset" in r.getMessage()]
    assert len(lines) == 1 and "unknown" in lines[0]


def test_an_unset_variable_starts_without_asking_the_store(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    store = _Store("owl2-rl")

    _start(store)

    assert store.asked == 0
    lines = [r.getMessage() for r in caplog.records if "ruleset" in r.getMessage()]
    assert len(lines) == 1 and "not checked" in lines[0]


def test_an_error_answer_propagates_unchanged(monkeypatch):
    monkeypatch.setenv(_VARIABLE, "rdfsplus-optimized")
    failure = ConnectionError("no answer")
    with pytest.raises(ConnectionError) as raised:
        _start(_Store(error=failure))
    assert raised.value is failure


def test_the_check_runs_before_the_first_ontology_read(monkeypatch):
    """With a class scope, the wiring reads the ontology in the constructor: the refusal is first."""
    monkeypatch.setenv(_VARIABLE, "rdfsplus-optimized")
    with pytest.raises(DeploymentRuntimeError):
        SemanticMiddleware(
            resource_iri="http://example.org/r",
            service_class="http://example.org/Service",
            ogm=SimpleNamespace(db=_Store("owl-max")),
            class_scope=object(),  # would fail in the wiring, if the check were not first
        )
