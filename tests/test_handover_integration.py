"""Handover primitive integration tests (#18) against a live GraphDB.

The change-of-possession primitive (ADR 0011): a source resource that currently possesses a
workpiece hands it to a counterpart carrying the complementary handover ability. Possession
is Core's reified `cfc:PossessionState`; the switch is one atomic OGM commit that re-points
the workpiece to a fresh PossessionState possessed by the counterpart. Skipped when GRAPHDB_*
env vars are absent (see conftest).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from kapps_triplestore_interface import IRI
from rdflib.namespace import RDF

from kapps_ogm import OGM
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.credentials import graphdb_env_present, graphdb_for
from kapps_semantic_middleware.registration import (
    HandoverPreconditionError,
    create_possession,
    find_possession_state,
)
from kapps_semantic_middleware.vocabulary import CFC, MES

requires_graphdb = pytest.mark.skipif(
    not graphdb_env_present(),
    reason="GRAPHDB_* environment variables not set; skipping live-GraphDB integration test",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

# The handover middleware is never served, so its service_class is unused by handover();
# it only reads/writes possession through the OGM using resource_iri.
_TRANSFER_SERVICE_CLASS = IRI(f"{seed.DEMO_NS}TransferModuleService")


def _source_middleware(graphdb) -> SemanticMiddleware:
    """The source transfer module (which currently possesses the box), as a middleware."""
    return SemanticMiddleware(
        mode="resource",
        resource_iri=seed.HANDOVER_SOURCE,
        service_class=_TRANSFER_SERVICE_CLASS,
        ogm=OGM(db=graphdb_for(graphdb.repository)),
        host="127.0.0.1",
        port=8999,
    )


@requires_graphdb
def test_handover_switches_possession(graphdb):
    """A clean handover atomically moves possession of the workpiece to the counterpart."""
    db = graphdb
    seed.seed_handover(db, OGM(db=db))
    ogm = OGM(db=db)
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_SOURCE) is not None
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_DEST) is None

    with _source_middleware(graphdb).handover(
        mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST
    ):
        pass  # domain-owned physical transport

    # Possession switched to the counterpart; the source no longer currently possesses it.
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_DEST) is not None
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_SOURCE) is None
    # The workpiece points to exactly ONE current PossessionState (Core cardinality preserved).
    states = list(db.triples_get(sub=seed.HANDOVER_BOX, pred=CFC.hasPossessedWorkpiece))
    assert len(states) == 1


@requires_graphdb
def test_handover_rejects_without_possession(graphdb):
    """A handover of a workpiece the caller does not possess is rejected before the body runs."""
    db = graphdb
    seed.seed_handover(db, OGM(db=db))
    not_possessed = IRI(f"{seed.DEMO_NS}box_999")
    db.triple_add((not_possessed, RDF.type, seed.BOX_CLASS))
    with pytest.raises(HandoverPreconditionError):
        with _source_middleware(graphdb).handover(
            mode=MES.Pass, workpiece=not_possessed, counterpart=seed.HANDOVER_DEST
        ):
            pass


@requires_graphdb
def test_handover_rejects_without_counterpart_ability(graphdb):
    """A handover to a counterpart lacking the complementary ability is rejected before the body."""
    db = graphdb
    seed.seed_handover(db, OGM(db=db))
    no_ability = IRI(f"{seed.DEMO_NS}transfer_module_C")
    db.triple_add((no_ability, RDF.type, seed.TRANSFER_MODULE_CLASS))  # no hasHandoverAbility
    with pytest.raises(HandoverPreconditionError):
        with _source_middleware(graphdb).handover(
            mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=no_ability
        ):
            pass


@requires_graphdb
def test_handover_body_exception_aborts(graphdb):
    """An exception in the domain body aborts with no possession switch."""
    db = graphdb
    seed.seed_handover(db, OGM(db=db))
    ogm = OGM(db=db)
    with pytest.raises(RuntimeError, match="boom"):
        with _source_middleware(graphdb).handover(
            mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST
        ):
            raise RuntimeError("boom")
    # No switch happened: the source still possesses the box, the counterpart does not.
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_SOURCE) is not None
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_DEST) is None


@requires_graphdb
def test_handover_preserves_counterpart_existing_possession(graphdb):
    """The counterpart's new possession is APPENDED — a workpiece it already holds is not lost.

    A resource may possess several workpieces (ADR 0011 — possession is not universally maxCount
    1), so switching a workpiece to a counterpart must not clobber its existing possessions.
    """
    db = graphdb
    seed.seed_handover(db, OGM(db=db))
    ogm = OGM(db=db)
    # The destination already currently possesses a DIFFERENT box.
    other_box = IRI(f"{seed.DEMO_NS}box_002")
    db.triple_add((other_box, RDF.type, seed.BOX_CLASS))
    create_possession(ogm, workpiece_iri=other_box, possessor_iri=seed.HANDOVER_DEST)
    assert find_possession_state(ogm, other_box, seed.HANDOVER_DEST) is not None

    with _source_middleware(graphdb).handover(
        mode=MES.Pass, workpiece=seed.HANDOVER_BOX, counterpart=seed.HANDOVER_DEST
    ):
        pass

    # The destination now possesses BOTH boxes; the pre-existing possession survived the switch.
    assert find_possession_state(ogm, seed.HANDOVER_BOX, seed.HANDOVER_DEST) is not None
    assert find_possession_state(ogm, other_box, seed.HANDOVER_DEST) is not None
