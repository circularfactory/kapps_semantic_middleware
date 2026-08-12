"""Phase 4 liveness integration tests against a live GraphDB (ADR 0007).

Deterministic (no waiting on real intervals): a heartbeat is written and refreshed
idempotently, and a watchdog sweep deregisters a stale service while leaving a
fresh one reachable. Skipped when GRAPHDB_* env vars are absent.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kapps_ogm import OGM
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import graphdb_env_present, graphdb_for
from kapps_semantic_middleware.registration import (
    mint_service_iri,
    register_service,
    update_heartbeat,
)
from kapps_semantic_middleware.vocabulary import SVC

requires_graphdb = pytest.mark.skipif(
    not graphdb_env_present(),
    reason="GRAPHDB_* environment variables not set; skipping live-GraphDB integration test",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402


@requires_graphdb
def test_heartbeat_write_and_refresh(graphdb):
    db = graphdb
    seed.seed_scenario1(db)

    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HELLO_RESOURCE,
        service_class=seed.HELLO_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=8987,
    )
    # Per-instance since ADR 0022, so it is read off the instance rather than reconstructed.
    service_iri = mw.service_iri
    asyncio.run(mw._register_service())

    asyncio.run(mw.emit_heartbeat())
    hb1 = db.triples_get(sub=service_iri, pred=SVC.lastHeartbeat)
    assert len(hb1) == 1, "exactly one heartbeat after first emit"

    asyncio.run(mw.emit_heartbeat())
    hb2 = db.triples_get(sub=service_iri, pred=SVC.lastHeartbeat)
    assert len(hb2) == 1, "heartbeat is replaced, not accumulated"


@requires_graphdb
def test_watchdog_sweeps_only_stale_service(graphdb):
    db = graphdb
    seed.seed_scenario1(db)

    ogm = OGM(db=db)
    fresh_service = mint_service_iri(seed.HELLO_RESOURCE, "http://127.0.0.1:8001")
    stale_service = mint_service_iri(seed.PLANNER_RESOURCE, "http://127.0.0.1:8002")

    # A fresh, reachable service (heartbeat now).
    register_service(
        ogm,
        resource_iri=seed.HELLO_RESOURCE,
        service_iri=fresh_service,
        service_class=seed.HELLO_SERVICE_CLASS,
        address="http://127.0.0.1:8001",
    )
    update_heartbeat(ogm, fresh_service)

    # A reachable but silent service (heartbeat far in the past → stale).
    register_service(
        ogm,
        resource_iri=seed.PLANNER_RESOURCE,
        service_iri=stale_service,
        service_class=seed.PLANNER_SERVICE_CLASS,
        address="http://127.0.0.1:8002",
    )
    update_heartbeat(ogm, stale_service, timestamp=datetime.now(timezone.utc) - timedelta(hours=1))

    watchdog = SemanticMiddleware(
        mode="watchdog",
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        staleness_threshold=60.0,
    )
    swept = asyncio.run(watchdog.sweep())

    assert str(stale_service) in swept
    assert str(fresh_service) not in swept
    # The stale service lost its reachability; the fresh one kept it.
    assert not db.triples_get(sub=stale_service, pred=SVC.address)
    assert db.triples_get(sub=fresh_service, pred=SVC.address)
    # The stale service's individual is preserved (only reachability removed).
    from rdflib.namespace import RDF

    assert db.triple_exists((stale_service, RDF.type, seed.PLANNER_SERVICE_CLASS))
