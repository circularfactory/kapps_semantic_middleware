"""The heartbeat loop's order, offline.

The loop writes once at startup, draws one random phase between zero and the interval, and then
repeats a write and the interval. The store is a stand-in that records each commit. The wait and the draw are replaced by
recorders, so the test checks order and bounds, not a duration.
"""

from __future__ import annotations

import asyncio

import pytest

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware import middleware as middleware_module

RESOURCE = "http://example.org/test#Module1"
SERVICE_CLASS = "http://example.org/test#ModuleService"
INTERVAL = 30.0


class _Stop(Exception):
    pass


def test_heartbeat_loop_writes_then_draws_phase_then_beats_each_interval(monkeypatch):
    class _RecordingOGM:
        """A stand-in store: it records each commit that the heartbeat makes."""

        def commit(self, **kwargs):
            record.append("write")

    record: list = []
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=RESOURCE,
        service_class=SERVICE_CLASS,
        ogm=_RecordingOGM(),
        heartbeat_interval=INTERVAL,
    )
    def draw(low, high):
        record.append(("draw", low, high))
        return 7.5

    async def wait(seconds):
        record.append(("wait", seconds))
        if len([r for r in record if r == "write"]) == 3:
            raise _Stop

    monkeypatch.setattr(middleware_module, "_heartbeat_phase", draw)
    monkeypatch.setattr(middleware_module, "_heartbeat_wait", wait)

    with pytest.raises(_Stop):
        asyncio.run(mw._heartbeat_loop())

    assert record == [
        "write",
        ("draw", 0.0, INTERVAL),
        ("wait", 7.5),
        "write",
        ("wait", INTERVAL),
        "write",
        ("wait", INTERVAL),
    ]
