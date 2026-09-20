"""The repository a client connects to is named by the caller, never by the environment.

Tests and demos wipe the repository they connect to, so inheriting a repository name from
the ambient environment is how a stray ``GRAPHDB_REPOSITORY`` gets a real repository
cleared. These tests are the non-live half of that guarantee: they prove the name the
caller passes wins, without needing a triple store to prove it.

The live half cannot be tested here — a silent repository switch only shows up against a
real server — which is why this file asserts on the credentials rather than on a client.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kapps_semantic_middleware.credentials import (
    DEMO_REPOSITORY,
    REQUIRED_GRAPHDB_ENV,
    credentials_for,
)


@pytest.fixture
def graphdb_env(monkeypatch):
    """The three variables a client genuinely needs, and nothing else."""
    monkeypatch.setenv("GRAPHDB_URL", "http://localhost:7200")
    monkeypatch.setenv("GRAPHDB_USERNAME", "user")
    monkeypatch.setenv("GRAPHDB_PASSWORD", "secret")
    monkeypatch.delenv("GRAPHDB_REPOSITORY", raising=False)


def test_a_stray_repository_variable_is_ignored(graphdb_env, monkeypatch):
    """The acceptance criterion: a repository named in the environment does not win.

    This is the failure the repository pin exists to prevent, and the one form of it the non-live
    tier can catch.
    """
    monkeypatch.setenv("GRAPHDB_REPOSITORY", "Production")

    credentials = credentials_for("Tests")

    assert credentials.repository == "Tests"


def test_the_repository_variable_is_not_required(graphdb_env):
    """An unset GRAPHDB_REPOSITORY is not an error, because nothing reads it.

    ``GraphDBCredentials.from_env`` raises when it is missing, so this is the reason
    the helper builds credentials directly rather than delegating to it.
    """
    credentials = credentials_for(DEMO_REPOSITORY)

    assert credentials.repository == DEMO_REPOSITORY


def test_the_other_three_variables_are_read_from_the_environment(graphdb_env):
    """Only the repository is overridden; the connection details still come from the env."""
    credentials = credentials_for("Tests")

    assert credentials.base_url == "http://localhost:7200"
    assert credentials.username == "user"
    assert credentials.password == "secret"


@pytest.mark.parametrize("missing", REQUIRED_GRAPHDB_ENV)
def test_a_missing_connection_variable_is_reported_by_name(
    graphdb_env, monkeypatch, missing
):
    """Fail loudly and name the variable, rather than connecting somewhere unintended."""
    monkeypatch.delenv(missing)

    with pytest.raises(ValueError, match=missing):
        credentials_for("Tests")


def test_the_demo_repository_is_the_one_the_compose_file_creates():
    """The pinned demo repository must be the one the shipped Docker setup creates.

    Read out of the config rather than restated, so that renaming the repository in
    ``docker/`` without repinning here fails this test instead of failing a reader on a
    fresh install — where GraphDB rejects an absent repository at construction.
    """
    config = (
        Path(__file__).resolve().parents[1] / "examples" / "docker" / "graphdb-repo-config.ttl"
    ).read_text(encoding="utf-8")

    declared = re.findall(r'rep:repositoryID\s+"([^"]+)"', config)

    assert declared == [DEMO_REPOSITORY]
