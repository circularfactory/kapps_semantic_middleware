"""Shared pytest fixtures.

Integration tests run against a real GraphDB (matching the convention of the
sibling repos, which test against a live triple store rather than a mock). They
are skipped automatically when the ``GRAPHDB_*`` environment variables are not
set, so the pure-logic unit tests still run anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio

from kapps_semantic_middleware.credentials import graphdb_env_present

# The suite wipes the repository it connects to, so it names that repository itself rather
# than inheriting GRAPHDB_REPOSITORY, which may point at anything. Capitalised
# to match the repository on the KIT server; tests do not ship, so this name is dev-only.
TEST_REPOSITORY = "Tests"


requires_graphdb = pytest.mark.skipif(
    not graphdb_env_present(),
    reason="GRAPHDB_* environment variables not set; skipping live-GraphDB integration test",
)


def pytest_collection_modifyitems(items):
    """Mark every test that reaches the triple store as ``live``.

    Derived from the fixture graph rather than written on each test, so the marker cannot
    drift out of step with what a test actually needs. ``-m 'not live'`` then selects the
    tests that need no network at all.
    """
    for item in items:
        if "graphdb" in item.fixturenames:
            item.add_marker("live")


def methods_at(app, path: str) -> set:
    """Every HTTP method served at `path`, unioned across routes.

    A parameter route's GET and PUT are two separate `APIRoute` objects that happen to share a
    path, so picking the first match and reading its `.methods` sees only whichever verb was
    mounted first. That makes a "no PUT here" assertion pass whether or not a PUT exists --
    exactly what the read-only tests are meant to prove. It has already caught one false pass;
    it lives here so the offline and live router tests cannot drift apart on it.
    """
    return {
        method
        for route in app.routes
        if getattr(route, "path", None) == path
        for method in route.methods
    }


def git_in(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a fixed identity and no signing, returning stdout.

    The release tests each build a throwaway repository to plant a violation in, and
    every one of them needs the same three ``-c`` overrides: a bare CI runner has no
    ``user.email``, so ``commit`` fails outright, and a developer with ``commit.gpgsign``
    set globally would have every test commit reach for a signing key.

    It lives here because three test files needed it and two of them had grown their own
    copy, which is one copy away from the two drifting on which of them strips stdout.
    """
    out = subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


@pytest.fixture(autouse=True)
def isolate_connector_sync_manager():
    """Empty transitional_sync_middleware's process-wide connector registry before every test.

    ``ConnectorSyncManager`` is a singleton (`_instance` in
    `transitional_sync_middleware/middleware/sync/connector_sync_manager.py`) whose
    ``persistence_to_synced`` is a plain dict of **strong** references, and it has no
    remove, clear, unregister or deregister method anywhere. Nothing ever takes a
    connector out of it. So every ``SemanticMiddleware`` constructed in a pytest process
    leaves its connectors there for the rest of the run, keyed by
    *(data_model_name, model_id)* -- and any later middleware over the same resource
    inherits them, because ``PersistedConnector._notify_synced_connectors`` fans out over
    whatever that key holds.

    The symptom is not a slow test. It is a *wrong* one: a second file serving
    ``TransferUnit1`` had its writes fanned out to a previous file's dead write legs, which
    published onto the live broker and froze a belt one ramp step short -- the exact
    signature of the bug ``test_southbound_echo_integration.py`` exists to prevent, from a
    cause entirely outside the code under test. Pointing the two files at different brokers
    only converted it into a PUT that hung on an unreachable one.

    Isolating here rather than in one file because the collision is between *any* two tests
    that build a middleware over the same resource, and the next such pair would rediscover
    it. The leak itself is a real defect and is filed separately; a middleware that owns its
    whole process, which is what the factory demo runs, never meets it.
    """
    from transitional_sync_middleware.middleware.sync.connector_sync_manager import (
        connector_sync_manager,
    )

    connector_sync_manager.persistence_to_synced.clear()
    connector_sync_manager.connection_to_persistence.clear()
    connector_sync_manager.synced_connectors.clear()
    yield


@pytest.fixture(scope="session")
def graphdb():
    """A live GraphDB client from the environment, or skip the test.

    Session-scoped: constructing a client costs a login round trip plus a repository
    listing, and the client carries no per-test state — only a growing set of minted
    blank-node ids, which is a collision guard that is happier the longer it lives.
    Tests that need a clean *graph* re-seed it; that is separate from the client.
    """
    if not graphdb_env_present():
        pytest.skip("GRAPHDB_* environment variables not set")
    from kapps_semantic_middleware.credentials import graphdb_for

    db = graphdb_for(TEST_REPOSITORY)
    yield db
    db.close()


@pytest.fixture
def unit_scope():
    """The consumer's view of a scenario-3 TransferUnit, rooted at the unit.

    Two levels, because a TransferUnit's parameters hang off its belts and barriers. A view
    belongs to its consumer and is configured in embedding code rather than in the ontology,
    so it is stated once here rather than in each test that needs a wired unit.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
    import seed
    from kapps_ogm.utils.class_scope import ClassScope

    return ClassScope.from_property_chains(
        [
            [seed.TU_HAS_CONVEYOR_BELT, seed.TU_HAS_CONVEYOR_SPEED],
            [seed.TU_HAS_LIGHT_BARRIER, seed.TU_IS_OCCUPIED],
        ]
    )


MQTT_TEST_PORT = 18831
"""Not 1883: a developer machine may already run a broker there, and a test that silently
joined it would pass against the wrong state and leak its topics into someone's session."""


@pytest_asyncio.fixture
async def mqtt_broker():
    """A real MQTT broker on 127.0.0.1, in-process, for the duration of one test.

    ``amqtt`` is a pure-Python broker, so this needs no ``mosquitto`` and no root — which
    matters because installing a system broker is exactly what a CI runner and a fresh
    checkout cannot assume. The round trip it serves is genuinely live: real sockets, real
    publish and subscribe, the same ``aiomqtt`` client the framework connector uses.
    """
    from amqtt.broker import Broker

    broker = Broker(
        {
            "listeners": {
                "default": {
                    "type": "tcp",
                    "bind": f"127.0.0.1:{MQTT_TEST_PORT}",
                    "max_connections": 100,
                }
            },
            "sys_interval": 0,
            "auth": {"allow-anonymous": True},
            "topic-check": {"enabled": False},
        }
    )
    await broker.start()
    try:
        yield f"127.0.0.1:{MQTT_TEST_PORT}"
    finally:
        try:
            # Bounded, not bare: a client that never cleanly drains (a receive loop
            # still mid-cycle when the test body returns, for instance) can leave
            # amqtt's own shutdown waiting on it indefinitely. A hung fixture
            # teardown blocks the whole run silently; a bounded one fails loudly
            # at a fixed cost instead, which is the only difference an unbounded
            # await here was ever going to buy.
            await asyncio.wait_for(broker.shutdown(), timeout=10.0)
        except asyncio.TimeoutError:
            logging.getLogger(__name__).warning(
                "mqtt_broker fixture: broker.shutdown() did not complete within 10s; "
                "abandoning it rather than hanging the test run."
            )
