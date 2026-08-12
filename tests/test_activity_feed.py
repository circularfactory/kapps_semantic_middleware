"""Activity feed tests for the middleware machinery.

This module guards the live activity feed implementation against regressions.
Regressions would break the browser-readable log stream. The feed is the only
window into the middleware internal wiring decisions, message traffic,
registrations, and heartbeats. Resource state belongs to the monitor. Keep
these apart. This boundary is decided. Mix them and you conflate the middleware
health with the device status. Debugging becomes ambiguous.

**Why these tests exist:** The feed thread-safety depends on numbering and
insertion under one lock. A refactor split them. It took a sequence number in
one critical section. It inserted in another. Two threads interleave. The deque
orders ``[.., 6, 5]``. The stream re-sends record 6 on every poll forever. The
concurrency test pins this race.

**Critical hazard:** The SSE endpoint is an endless generator. TestClient runs
it on a blocking portal. The portal waits for the generator to finish. Iterate
through it and the test run hangs forever. Break and exit the with block. The
hang persists. Tests drive the generator directly instead. The HTML page at
/activity is safe to fetch with TestClient.

No GraphDB, no broker, no network. These tests run entirely offline under
``pytest -m 'not live'``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest import mock
import threading
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kapps_semantic_middleware.activity import (
    ActivityFeed,
    _ActivityHandler,
    enable_activity_feed,
)


@pytest.fixture(autouse=True)
def _snapshot_logger_state():
    """Snapshot and restore the package logger handlers and level around each test.

    These tests attach handlers to the real ``kapps_semantic_middleware`` logger.
    Cleanup prevents leaks. A handler leaked from one test persists into others.
    Leaks corrupt assertions about what reaches the feed. This fixture captures
    the logger state before each test. It restores the state after each test.
    Cross-test contamination stops.
    """
    logger = logging.getLogger("kapps_semantic_middleware")
    original_level = logger.level
    original_handlers = list(logger.handlers)
    
    try:
        yield
    finally:
        current_handlers = list(logger.handlers)
        for handler in current_handlers:
            if handler not in original_handlers:
                logger.removeHandler(handler)
        logger.setLevel(original_level)


class _FakeMiddleware:
    """The whole surface `enable_activity_feed` touches: an app and a feed location.

    Module scope, not nested in the fixture. A test enables the feed twice. It
    needs two independent instances. Construct a real `SemanticMiddleware`. A
    graph connection loads for no gain here.
    """

    def __init__(self) -> None:
        self.app = FastAPI()
        self.activity_feed = None


@pytest.fixture
def fake_middleware():
    """One fake middleware for tests that need a single instance."""
    return _FakeMiddleware()


async def _drive_stream(mw: Any, n: int, timeout: float = 3.0) -> list[str]:
    """Drive the SSE generator directly and return n chunks.

    **Critical:** Do NOT use TestClient for /activity/stream. The endpoint
    returns an endless StreamingResponse generator. TestClient runs it on a
    blocking portal. The portal waits for the generator to finish. Iterate
    through it. The test run hangs forever. Break and exit the with block. The
    hang persists.

    This helper bypasses TestClient. It finds the route. It calls the endpoint
    directly with a fake request. The request never disconnects. It pulls n
    chunks from the async iterator with a timeout.
    """
    route = next(r for r in mw.app.routes if getattr(r, "path", "") == "/activity/stream")

    class _NeverDisconnected:
        async def is_disconnected(self) -> bool:
            return False

    response = await route.endpoint(_NeverDisconnected())
    iterator = response.body_iterator.__aiter__()
    chunks = [await asyncio.wait_for(iterator.__anext__(), timeout) for _ in range(n)]
    await response.body_iterator.aclose()
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Buffer behavior tests (ActivityFeed directly, no HTTP)
# ─────────────────────────────────────────────────────────────────────────────


def test_the_buffer_is_bounded():
    """The rolling window drops oldest records when capacity exceeds.

    Capacity is 3. Append 5 records. Only the last 3 remain in the buffer.
    Memory growth stops during long-running middleware sessions.
    """
    feed = ActivityFeed(capacity=3)
    
    for i in range(5):
        feed.append(timestamp=float(i), level="INFO", logger="test", message=f"msg{i}")
    
    records = feed.snapshot()
    assert len(records) == 3
    assert [r.message for r in records] == ["msg2", "msg3", "msg4"]


def test_sequence_numbers_survive_concurrent_appends():
    """Numbering and insertion happen under one lock. Interleaving races stop.

    Numbering occurs in one critical section. Insertion occurs in another. Two
    threads interleave. The deque orders ``[.., 6, 5]``. The stream reports
    last_seq as 5. Record 6 re-sends on every poll indefinitely. This test
    spawns multiple threads. They append simultaneously. Sequence numbers are
    strictly increasing in deque order. No duplicates exist.
    """
    feed = ActivityFeed(capacity=500)
    threads = []
    
    def append_many(start: int, count: int) -> None:
        for i in range(count):
            feed.append(timestamp=float(start + i), level="INFO", logger="test", message=f"msg{start + i}")
    
    for t in range(8):
        thread = threading.Thread(target=append_many, args=(t * 50, 50))
        threads.append(thread)
        thread.start()
    
    for thread in threads:
        thread.join()
    
    records = feed.snapshot()
    seqs = [r.seq for r in records]
    
    assert len(seqs) == len(set(seqs))
    assert seqs == sorted(seqs)


def test_since_returns_only_what_is_new():
    """since(last_seq) filters to records newer than the given sequence.

    Capture the current state. Append one more record. Query since the previous
    last sequence number. Exactly that new record returns.
    """
    feed = ActivityFeed(capacity=10)
    
    feed.append(timestamp=1.0, level="INFO", logger="test", message="first")
    feed.append(timestamp=2.0, level="INFO", logger="test", message="second")
    
    last_seq = feed.last_seq
    assert feed.since(last_seq) == []
    
    feed.append(timestamp=3.0, level="INFO", logger="test", message="third")
    
    new_records = feed.since(last_seq)
    assert len(new_records) == 1
    assert new_records[0].message == "third"


# ─────────────────────────────────────────────────────────────────────────────
# Wiring tests (enable_activity_feed on fake middleware)
# ─────────────────────────────────────────────────────────────────────────────


def test_a_log_record_reaches_the_feed(fake_middleware):
    """Attach at the package root. Catch child loggers with a single handler.

    Log through kapps_semantic_middleware.connectors.mqtt_binding. This is a
    child logger. The feed receives the message. The one-attach-at-package-root
    design works.
    """
    enable_activity_feed(fake_middleware)
    
    child_logger = logging.getLogger("kapps_semantic_middleware.connectors.mqtt_binding")
    child_logger.info("test message from child")
    
    records = fake_middleware.activity_feed.snapshot()
    assert len(records) == 1
    assert records[0].message == "test message from child"
    assert records[0].logger == "kapps_semantic_middleware.connectors.mqtt_binding"


def test_no_mqtt_topic_reaches_the_feed_even_at_debug(fake_middleware):
    """#76. The feed serves this package's records over HTTP. Not one may carry a topic.

    ADR 0028 deletes a parameter's broker address and topics from the served
    datamodel, so that a peer learns values from the middleware rather than the
    route around it. The feed is the same web server, one URL along, and it used
    to print the topic on every value that moved.

    Driven through the real ``MQTTParameterFormatter`` rather than a hand-written
    log line, because the claim is about what this package emits and not about
    what a test can phrase. The package logger is forced to DEBUG first: the
    protection has to be the handler's own level, which the feed sets, and not
    the logger level, which the host application owns and may widen at will.
    """
    from pydantic import create_model

    from kapps_semantic_middleware.connectors.mqtt_binding import MQTTParameterFormatter
    from kapps_semantic_middleware.vocabulary import INF

    enable_activity_feed(fake_middleware)
    logging.getLogger("kapps_semantic_middleware").setLevel(logging.DEBUG)

    fields: dict[str, Any] = {INF.hasValue.lined: (list, [])}
    model = create_model("AnonymousClass", **fields)
    formatter = MQTTParameterFormatter(
        model_type=model,
        northbound_facets={},
        value_field=INF.hasValue.lined,
        parameter_label="Belt1 hasConveyorSpeed",
        topic="unit1/ConveyorBelt/1/speed",
        set_topic="unit1/ConveyorBelt/1/speed_set",
    )

    [node] = formatter.deserialize(1.5)
    formatter.deserialize(2.5)
    formatter.deserialize(2.5)
    formatter.serialize([node])

    served = [record.message for record in fake_middleware.activity_feed.snapshot()]
    assert served, "the test proves nothing if the feed stayed empty"
    assert not any("unit1/" in message for message in served)
    # The parameter and its value still reach the feed -- this is a relevel, not a silencing.
    assert any("Belt1 hasConveyorSpeed" in message for message in served)


def test_enabling_lowers_the_package_logger_to_the_handler_level():
    """Enable the feed. Lower `kapps_semantic_middleware` level so records exist.

    The package logger is NOTSET by default. Its effective level is root level.
    Root defaults to WARNING unless the host application configured logging. A handler
    cannot rescue a record that was never emitted. A version touched only the
    handler level. The feed stayed permanently and silently empty in the default
    case. Page loads. Stream connects. Nothing appears.

    The widening is the documented cost of opt-in. It widens only. An
    application asked for something more verbose. It keeps that level.
    """
    package_logger = logging.getLogger("kapps_semantic_middleware")
    package_logger.setLevel(logging.WARNING)

    middleware = _FakeMiddleware()
    enable_activity_feed(middleware)
    assert package_logger.level == logging.INFO

    # Widening only: a more verbose level already chosen by the host survives.
    other = _FakeMiddleware()
    package_logger.setLevel(logging.DEBUG)
    enable_activity_feed(other)
    assert package_logger.level == logging.DEBUG

def test_enabling_twice_does_not_attach_a_second_handler(fake_middleware):
    """Call enable_activity_feed twice. Return the existing feed. Add no handlers.

    Multiple calls are idempotent. Same feed object returns. Exactly one handler
    attaches to the package logger.
    """
    logger = logging.getLogger("kapps_semantic_middleware")
    original_handler_count = len(logger.handlers)
    
    feed1 = enable_activity_feed(fake_middleware)
    feed2 = enable_activity_feed(fake_middleware)
    
    assert feed1 is feed2
    assert len(logger.handlers) == original_handler_count + 1


def test_the_handler_never_raises_into_logging():
    """A handler throws. It takes down whatever logged. This one swallows and reports.

    Drive against `_ActivityHandler.emit` directly. Do not go through
    `logger.info(...)`. Do this deliberately. Pytest installs its own
    `LogCaptureHandler`. Its `handleError` re-raises. Bad log calls surface
    during tests. Go through the logging chain. Measure pytest handler, not this
    one.
    """
    feed = ActivityFeed(capacity=4)
    handler = _ActivityHandler(feed)

    # "%d" against a string: the failure happens inside `record.getMessage()`.
    bad = logging.LogRecord(
        name="kapps_semantic_middleware.x",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%d",
        args=("not-a-number",),
        exc_info=None,
    )

    with mock.patch.object(handler, "handleError") as handle_error:
        handler.emit(bad)  # must not raise

    assert handle_error.called, "a swallowed failure must still be reported to handleError"
    assert feed.snapshot() == [], "a record that could not be formatted must not be buffered"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP surface tests
# ─────────────────────────────────────────────────────────────────────────────


def test_the_page_is_self_contained(fake_middleware):
    """The HTML page contains no external references. Isolated networks work.

    GET /activity returns 200. Body contains no http:// URLs. Body contains no
    https:// URLs. No <script src= tags exist. No <link rel="stylesheet"> tags
    exist. Everything is inline. The page renders on a factory network. CDN
    access is not required.
    """
    enable_activity_feed(fake_middleware)
    
    client = TestClient(fake_middleware.app)
    response = client.get("/activity")
    
    assert response.status_code == 200
    
    body = response.text
    assert "http://" not in body
    assert "https://" not in body
    assert '<script src=' not in body
    assert '<link rel="stylesheet"' not in body


@pytest.mark.asyncio
async def test_the_stream_replays_the_backlog(fake_middleware):
    """A viewer connects mid-run. Existing records appear. No empty page.

    Log two records. Open the stream. Drive the generator. Verify both appear
    as data: lines. Messages are correct. Sequence numbers ascend.
    """
    feed = enable_activity_feed(fake_middleware)
    
    feed.append(timestamp=1.0, level="INFO", logger="test", message="first")
    feed.append(timestamp=2.0, level="INFO", logger="test", message="second")
    
    chunks = await _drive_stream(fake_middleware, 2)
    
    assert len(chunks) == 2
    assert chunks[0].startswith("data: ")
    assert chunks[1].startswith("data: ")
    
    first = json.loads(chunks[0][6:])
    second = json.loads(chunks[1][6:])
    
    assert first["message"] == "first"
    assert second["message"] == "second"
    assert first["seq"] < second["seq"]


@pytest.mark.asyncio
async def test_the_stream_is_server_sent_events(fake_middleware):
    """The response uses SSE media type. Caching disables.

    Verify media_type is text/event-stream. Cache-Control header is no-cache.
    """
    enable_activity_feed(fake_middleware)
    
    route = next(r for r in fake_middleware.app.routes if getattr(r, "path", "") == "/activity/stream")

    class _NeverDisconnected:
        async def is_disconnected(self) -> bool:
            return False

    response = await route.endpoint(_NeverDisconnected())
    
    assert response.media_type == "text/event-stream"
    assert response.headers.get("Cache-Control") == "no-cache"
    
    await response.body_iterator.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# Opt-in and mode-independence tests (real SemanticMiddleware)
# ─────────────────────────────────────────────────────────────────────────────


def test_disabled_allocates_nothing():
    """Build a middleware without activity_feed=True. No activity routes exist.

    This is the acceptance criterion for the off state. activity_feed is None.
    No route path contains 'activity'.
    """
    from kapps_semantic_middleware import SemanticMiddleware, Mode
    
    mw = SemanticMiddleware(mode=Mode.WATCHDOG, ogm=object(), activity_feed=False)
    
    assert mw.activity_feed is None
    
    route_paths = [getattr(r, "path", "") for r in mw.app.routes]
    assert not any("activity" in p for p in route_paths)


def test_the_feed_is_mounted_the_same_way_regardless_of_mode():
    """Controller and monitor inherit the feed from the same code path. No flavour-specific branching exists.

    ADR 0022 establishes that controller and monitor are the same library.
    Configure them differently. Build a middleware in Mode.WATCHDOG with
    activity_feed=True. Verify the activity routes exist. The mode-independence
    is structural. enable_activity_feed takes no mode argument.
    """
    from kapps_semantic_middleware import SemanticMiddleware, Mode
    
    mw = SemanticMiddleware(mode=Mode.WATCHDOG, ogm=object(), activity_feed=True)
    
    assert mw.activity_feed is not None
    
    route_paths = [getattr(r, "path", "") for r in mw.app.routes]
    assert any("/activity" in p for p in route_paths)


# ─────────────────────────────────────────────────────────────────────────────
# Drop detection and disconnect handling tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_client_that_falls_behind_is_told_records_were_dropped(fake_middleware):
    """A feed loses lines quietly. This is worse than admission. The viewer
    cannot distinguish 'nothing happened' from 'I missed it'. The stream
    detects when the ring buffer evicted records. The client has not seen them
    yet. Emit a synthetic WARNING. Continue with normal records.
    """
    feed = enable_activity_feed(fake_middleware, capacity=5)
    
    # Seed with some records through the package logger.
    logger = logging.getLogger("kapps_semantic_middleware")
    for i in range(20):
        logger.info(f"pre_msg{i}")
    
    # Open the stream.
    route = next(r for r in fake_middleware.app.routes if getattr(r, "path", "") == "/activity/stream")
    
    class _StayConnected:
        async def is_disconnected(self) -> bool:
            return False
    
    response = await route.endpoint(_StayConnected())
    iterator = response.body_iterator.__aiter__()
    
    # Drain the initial seeding chunks.
    seed_chunks = []
    for _ in range(5):
        chunk = await asyncio.wait_for(iterator.__anext__(), timeout=2.0)
        seed_chunks.append(chunk)
    
    # Now append enough to force eviction of records the client has not consumed.
    for i in range(200):
        logger.info(f"post_msg{i}")
    
    # Poll for the drop warning and subsequent records.
    chunks = []
    found_warning = False
    warning_data = None
    for _ in range(20):
        chunk = await asyncio.wait_for(iterator.__anext__(), timeout=2.0)
        chunks.append(chunk)
        if chunk.startswith("data: "):
            data = json.loads(chunk[6:])
            if data.get("level") == "WARNING" and "dropped" in data.get("message", "").lower():
                found_warning = True
                warning_data = data
                break
    
    assert found_warning, "Expected a WARNING about dropped records"

    # The count is derived from where the buffer now starts, not hardcoded. The
    # client consumed through seq 20 when it fell behind.
    expected_dropped = feed.oldest_seq - 20 - 1
    assert warning_data["message"].startswith(f"{expected_dropped} record(s) dropped")

    # The stream carries on afterwards rather than stalling on the gap. Pulled
    # after the warning was found. The search loop above breaks on it. `chunks`
    # ends there. Looking inside it for what comes next can only fail.
    following = []
    for _ in range(3):
        chunk = await asyncio.wait_for(iterator.__anext__(), timeout=2.0)
        if chunk.startswith("data: "):
            following.append(json.loads(chunk[6:]))

    await response.body_iterator.aclose()

    assert following, "the stream stopped after reporting the gap instead of resuming"
    assert all(
        "dropped" not in record["message"].lower() for record in following
    ), "the gap was reported more than once; the client position did not advance past it"


@pytest.mark.asyncio
async def test_the_stream_stops_when_the_client_disconnects(fake_middleware):
    """The generator checks await request.is_disconnected() each poll. Return
    when true. Without that check, a disconnected viewer leaves a polling loop
    running forever. The server does not cancel the task. The endpoint cannot
    be tested. An endless generator hangs any client that stops reading.
    """
    feed = enable_activity_feed(fake_middleware)
    
    # Seed with some records.
    feed.append(timestamp=1.0, level="INFO", logger="test", message="first")
    feed.append(timestamp=2.0, level="INFO", logger="test", message="second")
    
    # Fake request that disconnects after a couple polls.
    call_count = [0]
    
    class _DisconnectsSoon:
        async def is_disconnected(self) -> bool:
            call_count[0] += 1
            return call_count[0] >= 3
    
    route = next(r for r in fake_middleware.app.routes if getattr(r, "path", "") == "/activity/stream")
    response = await route.endpoint(_DisconnectsSoon())
    
    # Drive with timeout -- should terminate on its own, not hang.
    chunks = []
    completed = False
    try:
        async with asyncio.timeout(5.0):
            async for chunk in response.body_iterator:
                chunks.append(chunk)
        completed = True
    except asyncio.TimeoutError:
        pass
    
    assert completed, "Stream did not terminate after client disconnected"
    
    # Backlog was delivered before stopping.
    data_chunks = [c for c in chunks if c.startswith("data: ") and not c.startswith("data: :")]
    messages = [json.loads(c[6:])["message"] for c in data_chunks if not json.loads(c[6:]).get("message", "").startswith(":")]
    assert "first" in messages
    assert "second" in messages
