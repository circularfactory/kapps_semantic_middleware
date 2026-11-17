"""The dataset a store connects to is named by the caller, never by the environment.

Tests and demos wipe the dataset they connect to, so inheriting a dataset name from the ambient
environment is how a stray ``TRIPLESTORE_DATASET`` gets a real dataset cleared. These tests are
the non-live half of that guarantee: they prove the name the caller passes wins, without needing
a triple store to prove it.

The store comes from the interface's factory, ``connect()``. Here a stand-in takes its place and
records what it was given, so no test reaches a store. The old names and the old variables are
removed: KSM reads only the ``TRIPLESTORE_*`` variables.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest
from kapps_triplestore_interface import GDBTokenAuth, TripleStoreEndpoint

from kapps_semantic_middleware import credentials
from kapps_semantic_middleware.credentials import (
    DEMO_DATASET,
    REQUIRED_TRIPLESTORE_ENV,
    endpoint_for,
    triplestore_env_present,
    triplestore_for,
)
from kapps_semantic_middleware.errors import DeploymentValueError

URL = "http://localhost:7200"

# vendor-check: off -- the old variables, which prove that KSM ignores them.
# The old variable of each variable, which KSM no longer reads.
OLD = {
    "TRIPLESTORE_URL": "GRAPHDB_URL",
    "TRIPLESTORE_USERNAME": "GRAPHDB_USERNAME",
    "TRIPLESTORE_PASSWORD": "GRAPHDB_PASSWORD",
}
OLD_DATASET = "GRAPHDB_REPOSITORY"
# vendor-check: on


@pytest.fixture
def no_store_variables(monkeypatch):
    """An environment with no store variable, new or old."""
    for name in (*OLD, *OLD.values(), "TRIPLESTORE_DATASET", OLD_DATASET):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def store_env(no_store_variables, monkeypatch):
    """The three variables a store genuinely needs, and nothing else."""
    monkeypatch.setenv("TRIPLESTORE_URL", URL)
    monkeypatch.setenv("TRIPLESTORE_USERNAME", "user")
    monkeypatch.setenv("TRIPLESTORE_PASSWORD", "secret")


@pytest.fixture
def old_env(no_store_variables, monkeypatch):
    """The old variables alone."""
    monkeypatch.setenv(OLD["TRIPLESTORE_URL"], URL)
    monkeypatch.setenv(OLD["TRIPLESTORE_USERNAME"], "user")
    monkeypatch.setenv(OLD["TRIPLESTORE_PASSWORD"], "secret")


class _Connect:
    """Stands in for the interface's ``connect()``: records its arguments, returns a store."""

    def __init__(self) -> None:
        self.calls: list[tuple[TripleStoreEndpoint, object]] = []
        self.store = object()

    def __call__(self, endpoint, auth):
        self.calls.append((endpoint, auth))
        return self.store


@pytest.fixture
def connect(monkeypatch) -> _Connect:
    stand_in = _Connect()
    monkeypatch.setattr(credentials, "connect", stand_in)
    return stand_in


def _deprecations(record: list[warnings.WarningMessage]) -> list[str]:
    return [str(w.message) for w in record if issubclass(w.category, DeprecationWarning)]


# -- The dataset the caller names ----------------------------------------------------------- #


def test_a_stray_dataset_variable_is_ignored(store_env, monkeypatch):
    """A dataset named in the environment does not win.

    This is the failure the dataset pin exists to prevent, and the one form of it the non-live
    tier can catch.
    """
    monkeypatch.setenv("TRIPLESTORE_DATASET", "Production")

    assert endpoint_for("Tests").dataset == "Tests"


def test_the_dataset_variable_is_not_required(store_env):
    """An unset TRIPLESTORE_DATASET is not an error, because nothing reads it."""
    assert endpoint_for(DEMO_DATASET) == TripleStoreEndpoint(url=URL, dataset=DEMO_DATASET)


def test_the_store_comes_from_connect_with_the_endpoint_and_a_token(store_env, connect):
    """``triplestore_for`` builds the store with the interface's factory, ``connect()``."""
    store = triplestore_for("Tests")

    assert store is connect.store
    [(endpoint, auth)] = connect.calls
    assert endpoint == TripleStoreEndpoint(url=URL, dataset="Tests")
    assert isinstance(auth, GDBTokenAuth)
    assert repr(auth) == "GDBTokenAuth(username='user')"


def test_the_new_variables_raise_no_warning(store_env, connect):
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        triplestore_for("Tests")

    assert _deprecations(record) == []


@pytest.mark.parametrize("missing", REQUIRED_TRIPLESTORE_ENV)
def test_a_missing_connection_variable_is_reported_by_name(
    store_env, connect, monkeypatch, missing
):
    """Fail loudly and name the variable, rather than connecting somewhere unintended."""
    monkeypatch.delenv(missing)

    with pytest.raises(DeploymentValueError, match=missing):
        triplestore_for("Tests")
    assert connect.calls == []


def test_the_endpoint_needs_only_the_url(no_store_variables, monkeypatch):
    with pytest.raises(DeploymentValueError, match="TRIPLESTORE_URL"):
        endpoint_for("Tests")

    monkeypatch.setenv("TRIPLESTORE_URL", URL)

    assert endpoint_for("Tests").url == URL


def test_the_variables_are_present_only_when_each_is_set(store_env, monkeypatch):
    assert triplestore_env_present()

    monkeypatch.delenv("TRIPLESTORE_PASSWORD")

    assert not triplestore_env_present()


def test_the_demo_dataset_is_the_one_the_compose_file_creates():
    """The pinned demo dataset must be the one the shipped Docker setup creates.

    Read out of the config rather than restated, so that renaming it in ``docker/`` without
    repinning here fails this test instead of failing a reader on a fresh install -- where
    ``connect()`` rejects an absent dataset.
    """
    # vendor-check: off -- the configuration of the local Docker store.
    config = (
        Path(__file__).resolve().parents[1] / "examples" / "docker" / "graphdb-repo-config.ttl"
    ).read_text(encoding="utf-8")

    declared = re.findall(r'rep:repositoryID\s+"([^"]+)"', config)
    # vendor-check: on

    assert declared == [DEMO_DATASET]


# -- The old variables and names are gone -------------------------------------------------- #


def test_with_only_the_old_variables_set_no_store_is_present(old_env, connect):
    """KSM reads no old variable: with only those set, no store is reachable, and nothing warns."""
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        present = triplestore_env_present()
        with pytest.raises(DeploymentValueError, match="TRIPLESTORE_URL"):
            triplestore_for("Tests")
        with pytest.raises(DeploymentValueError, match="TRIPLESTORE_URL"):
            endpoint_for("Tests")

    assert not present
    assert connect.calls == []
    assert _deprecations(record) == []


def _own_public_names(module) -> set[str]:
    """The public names that ``module`` defines itself, its imports left out."""
    return {
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and (name.isupper() or getattr(value, "__module__", None) == module.__name__)
    }


def test_the_module_has_only_its_current_names():
    """The old helpers and constants are removed, not kept as aliases."""
    assert _own_public_names(credentials) == {
        "REQUIRED_TRIPLESTORE_ENV",
        "DEMO_DATASET",
        "triplestore_env_present",
        "endpoint_for",
        "triplestore_for",
    }


def test_a_name_the_module_does_not_have_is_an_attribute_error():
    with pytest.raises(AttributeError, match="no attribute 'NOT_A_NAME'"):
        credentials.NOT_A_NAME  # noqa: B018
