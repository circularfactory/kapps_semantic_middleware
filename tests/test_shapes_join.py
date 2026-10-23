"""The join of the shipped shapes with a test's own, read offline.

Each live test that needs a shape of its own sends it through ``_shapes.with_shipped_shapes``.
These tests parse every such shape through the join, so a shape that the RDF merge cannot read
fails here rather than at the first live run.
"""

from __future__ import annotations

import pytest
import test_capability_link
import test_commit_integration
import test_create_integration
import test_handover_integration
import test_refused_writes_integration
import test_request_claim_integration
from _shapes import with_shipped_shapes
from rdflib import Graph

from kapps_semantic_middleware.seeding import read_service_shapes

_OWN = {
    "commit, all but running": test_commit_integration._status_shape("queued", "done", "failed"),
    "commit, all but done": test_commit_integration._status_shape("queued", "running", "failed"),
    "create, all but done": test_create_integration._status_shape("queued", "running", "failed"),
    "claim, all but running": test_request_claim_integration._status_shape(
        "queued", "done", "failed"
    ),
    "claim, all but done": test_request_claim_integration._status_shape(
        "queued", "running", "failed"
    ),
    "handover, one workpiece": test_handover_integration._ONE_WORKPIECE_PER_MODULE,
    "capability, one resource": test_capability_link.ONE_RESOURCE_SHAPE,
    "refused writes, snapshot": test_refused_writes_integration._SNAPSHOT_SHAPES,
    "refused writes, never running": test_refused_writes_integration._NEVER_RUNNING,
    "refused writes, never failed": test_refused_writes_integration._NEVER_FAILED,
    "refused writes, one workpiece": test_refused_writes_integration._ONE_WORKPIECE_PER_MODULE,
}


def _parsed(turtle: str) -> Graph:
    return Graph().parse(data=turtle, format="turtle")


@pytest.mark.parametrize("name", sorted(_OWN))
def test_the_join_holds_the_shipped_shapes_and_the_tests_own(name):
    joined = _parsed(with_shipped_shapes(_OWN[name]))
    shipped = _parsed(read_service_shapes())
    own = _parsed(_OWN[name])
    assert len(joined) == len(shipped) + len(own)
