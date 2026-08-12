"""Unit tests for the failed-Operation resource-datamodel dump (#14 / svc:failureState).

These verify the load-bearing guarantee that ``_dump_resource_datamodel`` is BEST-EFFORT and
never raises into ``claim_next``'s failure-recording path, across all of its branches. Pure
unit tests — the OGM is faked, so no live GraphDB is needed (unlike the happy-path assertion in
``test_event_trigger_integration``).
"""

from __future__ import annotations

import json
import types

from pydantic import BaseModel

from kapps_semantic_middleware.middleware import SemanticMiddleware


class _Node:
    def __init__(self, instance):
        self.instance = instance


class _FakeOGM:
    def __init__(self, *, node=None, error=None):
        self._node = node
        self._error = error

    def fetch(self, **kwargs):
        if self._error is not None:
            raise self._error
        return self._node


def _dump(ogm):
    """Invoke the helper with a minimal stand-in for ``self`` (it only reads ogm/resource_iri)."""
    stub = types.SimpleNamespace(ogm=ogm, resource_iri="urn:resource")
    return SemanticMiddleware._dump_resource_datamodel(stub)


class _DemoModel(BaseModel):
    id: str


def test_dump_serializes_basemodel_instance():
    result = _dump(_FakeOGM(node=_Node(_DemoModel(id="urn:resource"))))
    assert json.loads(result) == {"id": "urn:resource"}


def test_dump_serializes_non_basemodel_instance():
    result = _dump(_FakeOGM(node=_Node({"a": 1})))
    assert json.loads(result) == {"a": 1}


def test_dump_returns_none_when_instance_missing():
    assert _dump(_FakeOGM(node=_Node(None))) is None


def test_dump_returns_none_on_fetch_error():
    # A fetch/serialization failure must NEVER propagate — failure recording must still complete.
    assert _dump(_FakeOGM(error=RuntimeError("graphdb down"))) is None
