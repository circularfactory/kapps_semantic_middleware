"""The dataset a store connects to is named by the caller, never by the environment.

Tests and demos wipe the dataset they connect to, so inheriting a dataset name from the ambient
environment is how a stray ``TRIPLESTORE_DATASET`` gets a real dataset cleared. These tests are
the non-live half of that guarantee: they prove the name the caller passes wins, without needing
a triple store to prove it.

The store comes from the interface's factory, ``connect()``. Here a stand-in takes its place and
records what it was given, so no test reaches a store. The deprecated names and the deprecated
variables each still work, and each use of one warns.
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

# vendor-check: off -- the deprecated variables.
# The deprecated variable that the interface reads for each variable that is not set.
OLD = {
    "TRIPLESTORE_URL": "GRAPHDB_URL",
    "TRIPLESTORE_USERNAME": "GRAPHDB_USERNAME",
    "TRIPLESTORE_PASSWORD": "GRAPHDB_PASSWORD",
}
OLD_DATASET = "GRAPHDB_REPOSITORY"
# vendor-check: on


@pytest.fixture
def no_store_variables(monkeypatch):
    """An environment with no store variable, new or deprecated."""
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
    """The deprecated variables alone."""
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
    """The acceptance criterion: a dataset named in the environment does not win.

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


# vendor-check: off -- the deprecated variables and names, until they go.

# -- The deprecated variables -------------------------------------------------------------- #


def test_with_only_the_deprecated_variables_set_it_connects_and_warns(old_env, connect):
    """The fallback: the deprecated variables still reach the store, and each warns."""
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        present = triplestore_env_present()
        store = triplestore_for("Tests")

    assert present
    assert store is connect.store
    [(endpoint, auth)] = connect.calls
    assert endpoint == TripleStoreEndpoint(url=URL, dataset="Tests")
    assert repr(auth) == "GDBTokenAuth(username='user')"
    assert sorted(_deprecations(record)) == sorted(
        f"{old} is deprecated; set {new}, which replaces it." for new, old in OLD.items()
    )


def test_a_new_variable_wins_over_its_deprecated_one(old_env, connect, monkeypatch):
    monkeypatch.setenv("TRIPLESTORE_URL", "http://new:7200")

    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        triplestore_for("Tests")

    [(endpoint, _)] = connect.calls
    assert endpoint.url == "http://new:7200"
    assert not any("GRAPHDB_URL" in message for message in _deprecations(record))


# -- The deprecated names ------------------------------------------------------------------ #


def _one_use(call):
    """Run ``call`` and return its result and the deprecation warnings of that one use."""
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        result = call()
    return result, _deprecations(record)


def test_the_old_store_helper_warns_once_and_runs_its_successor(store_env, connect):
    from kapps_semantic_middleware.credentials import graphdb_for

    store, messages = _one_use(lambda: graphdb_for("Tests"))

    assert store is connect.store
    assert connect.calls[0][0] == TripleStoreEndpoint(url=URL, dataset="Tests")
    assert messages == ["graphdb_for is deprecated; use triplestore_for, which replaces it."]


def test_the_old_presence_check_warns_once_and_runs_its_successor(store_env, monkeypatch):
    from kapps_semantic_middleware.credentials import graphdb_env_present

    present, messages = _one_use(graphdb_env_present)
    assert present
    assert messages == [
        "graphdb_env_present is deprecated; use triplestore_env_present, which replaces it."
    ]

    monkeypatch.delenv("TRIPLESTORE_URL")
    absent, _ = _one_use(graphdb_env_present)
    assert not absent


def test_the_old_credentials_helper_warns_once_and_returns_the_old_credentials(store_env):
    from kapps_semantic_middleware.credentials import credentials_for

    old, messages = _one_use(lambda: credentials_for("Tests"))

    assert messages == ["credentials_for is deprecated; use endpoint_for, which replaces it."]
    assert (old.base_url, old.username, old.password, old.repository) == (
        URL,
        "user",
        "secret",
        "Tests",
    )


def test_the_old_credentials_helper_reads_the_deprecated_variables(old_env):
    from kapps_semantic_middleware.credentials import credentials_for

    old, messages = _one_use(lambda: credentials_for("Tests"))

    assert (old.base_url, old.username, old.password) == (URL, "user", "secret")
    assert messages[0] == "credentials_for is deprecated; use endpoint_for, which replaces it."


def test_the_old_credentials_helper_reports_a_missing_variable(store_env, monkeypatch):
    from kapps_semantic_middleware.credentials import credentials_for

    monkeypatch.delenv("TRIPLESTORE_USERNAME")

    with pytest.warns(DeprecationWarning), pytest.raises(DeploymentValueError, match="USERNAME"):
        credentials_for("Tests")


@pytest.mark.parametrize(
    ("old", "new"),
    [("REQUIRED_GRAPHDB_ENV", "REQUIRED_TRIPLESTORE_ENV"), ("DEMO_REPOSITORY", "DEMO_DATASET")],
)
def test_an_old_constant_warns_once_and_is_its_successor(old, new):
    message = f"{old} is deprecated; use {new}, of which it is an alias."

    namespace: dict = {}
    _, on_import = _one_use(
        lambda: exec(f"from kapps_semantic_middleware.credentials import {old}", namespace)
    )
    value, on_read = _one_use(lambda: getattr(credentials, old))

    assert on_import == [message]
    assert on_read == [message]
    assert namespace[old] == value == getattr(credentials, new)


def test_a_name_the_module_does_not_have_is_an_attribute_error():
    with pytest.raises(AttributeError, match="no attribute 'NOT_A_NAME'"):
        credentials.NOT_A_NAME  # noqa: B018


# vendor-check: on
