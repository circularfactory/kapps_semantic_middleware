"""Live activity feed for the middleware machinery.

This module turns the middleware internal logging stream into a live feed for browsers.
It is library-level and mode-agnostic. A controller and a monitor are the same library with
different configuration. They inherit this feed from the same code with no
role-specific branching. That is the architectural claim the demo exists to make.

**What it shows:** wiring decisions, message traffic, registrations, heartbeats.

**What it does not show:** resource state. That is the monitor job. Keeping the two apart is
a decided boundary. Mixing them would conflate the middleware health with the device status. Debugging
would become ambiguous.

**Why the stream is polled rather than pushed:** Log records arrive from two places. The event
loop sends records. Worker threads send records (this codebase calls ``anyio.to_thread.run_sync``
for every graph write). Pushing from the logging handler into an ``asyncio.Queue`` is not
thread-safe. It would require capture of the running loop. This breaks when no loop runs
(construction time, tests). Instead, the handler appends to a thread-safe ``collections.deque``.
The SSE endpoint polls that deque on a short sleep. This decouples the logging path
(synchronous, multi-threaded) from the serving path (asynchronous, single-threaded). No loop
introspection or ``call_soon_threadsafe`` is required.
"""

# ADR: 0022

from __future__ import annotations

import asyncio
import collections
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse


@dataclass(frozen=True)
class ActivityRecord:
    """One entry in the activity feed.

    Immutable so that records handed to the SSE generator cannot receive mutation by later
    logging activity. The ``seq`` field allows clients to request only what is new.
    """

    seq: int
    timestamp: float  # unix epoch seconds
    level: str  # "INFO", "WARNING", ...
    logger: str  # the record logger name
    message: str  # already formatted


class ActivityFeed:
    """Thread-safe buffer for activity records.

    Holds a rolling window of records. Appends receive protection by a lock because the logging
    handler runs in arbitrary threads. Reads receive protection for the same reason. The SSE
    generator holds the lock only long enough to copy data out.
    """

    def __init__(self, capacity: int = 200) -> None:
        self._capacity = capacity
        self._deque: collections.deque[ActivityRecord] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def append(
        self, *, timestamp: float, level: str, logger: str, message: str
    ) -> ActivityRecord:
        """Buffer one record. Assign its sequence number. Drop the oldest when full.

        Numbering and insertion happen under **one** lock, together. That is the whole point of
        this method that takes fields rather than a finished record. Split them. Take a number
        in one critical section. Insert in another. Two threads interleave. The deque becomes
        ordered ``[.., 6, 5]``. ``last_seq`` then reports 5. The stream re-sends record 6 on
        every poll, forever. The race is reachable here rather than theoretical. This codebase
        logs from worker threads (``anyio.to_thread.run_sync`` wraps every graph write) and from
        the event loop.
        """
        with self._lock:
            self._seq += 1
            record = ActivityRecord(
                seq=self._seq,
                timestamp=timestamp,
                level=level,
                logger=logger,
                message=message,
            )
            self._deque.append(record)
            return record

    def since(self, seq: int) -> List[ActivityRecord]:
        """Return records with ``.seq > seq``, oldest first.

        Called by the SSE generator. The lock is held only for the duration of the slice
        operation, not during the yield. The event loop is not blocked.
        """
        with self._lock:
            return [r for r in self._deque if r.seq > seq]

    def snapshot(self) -> List[ActivityRecord]:
        """Return everything currently buffered.

        Used to seed a client that connects mid-run. Same locking discipline as ``since``.
        """
        with self._lock:
            return list(self._deque)

    @property
    def last_seq(self) -> int:
        """The highest sequence number **currently buffered**. Zero if empty.

        Not a resume token. Eviction begins. This is still the newest record. The oldest
        moves forward under it. A consumer stores this value. The consumer goes away. The
        consumer comes back. It asks for everything after that value. It has no way to learn
        what fell out of the window meanwhile. Pair it with :attr:`oldest_seq` to detect that.
        The stream does.
        """
        with self._lock:
            if not self._deque:
                return 0
            return self._deque[-1].seq

    @property
    def oldest_seq(self) -> int:
        """The lowest sequence number still buffered. Zero if empty.

        A consumer expected ``n``. It finds this value greater than ``n``. The feed outran it.
        The records between are gone. Exposed so that loss is reportable rather than invisible.
        A feed that quietly drops lines is worse than one that says it dropped them. The viewer
        cannot tell "nothing happened" from "I missed it".
        """
        with self._lock:
            if not self._deque:
                return 0
            return self._deque[0].seq


class _ActivityHandler(logging.Handler):
    """Logging handler that pushes records into an ``ActivityFeed``.

    Attached to the package-root logger (``kapps_semantic_middleware``) to catch every child
    logger with one attach. The level is set on the handler, not the logger. This feature does
    not change what every *other* handler in the host application sees.

    ``emit`` is wrapped in a try/except. On failure it calls ``self.handleError`` as the stdlib
    expects. It never raises into the logging system. This would risk deadlock of the caller.
    """

    def __init__(self, feed: ActivityFeed, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._feed = feed

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Numbering belongs to the feed, under the same lock as the insertion. See
            # `ActivityFeed.append`. A handler that took a number and then handed over a
            # finished record would reintroduce exactly the ordering race that method exists
            # to close.
            #
            # `getMessage` applies %-style formatting. It happens here rather than at the
            # call site. The cost is paid only when this handler is attached.
            self._feed.append(
                timestamp=record.created,
                level=record.levelname,
                logger=record.name,
                message=record.getMessage(),
            )
        except Exception:
            self.handleError(record)


def enable_activity_feed(
    middleware: Any,
    *,
    capacity: int = 200,
    level: int = logging.INFO,
    logger_name: str = "kapps_semantic_middleware",
) -> ActivityFeed:
    """Enable the activity feed on a middleware instance.

    Builds an ``ActivityFeed``. Attaches a logging handler to capture records. Mounts the HTTP
    routes on ``middleware.app``. A second call on the same middleware returns the existing feed
    without attach of a second handler.

    The feed is stored on the middleware as ``middleware.activity_feed``. Other components
    (tests, diagnostics) can inspect it directly.
    """
    # `getattr(..., None) is not None` rather than `hasattr`: a middleware that declares
    # `self.activity_feed = None` when the feature is off would otherwise satisfy `hasattr`
    # and be handed its own `None` back as a feed.
    existing = getattr(middleware, "activity_feed", None)
    if existing is not None:
        return existing

    feed = ActivityFeed(capacity=capacity)
    handler = _ActivityHandler(feed, level=level)

    # Attach to the package-root logger. This catches every child logger with one attach.
    target_logger = logging.getLogger(logger_name)
    target_logger.addHandler(handler)

    # The package logger is NOTSET by default. Its effective level comes from root. Root is
    # WARNING unless the host application configured logging. A handler cannot rescue a record
    # that was never emitted. Leave the level alone. This whole feature becomes a no-op in
    # exactly the default case. It fails *silently*. The page loads. The stream connects.
    # Nothing ever appears.
    #
    # Opt into the feed. This lowers this package level far enough to produce the records it
    # was asked to show. That is a real, documented side effect rather than a free one. Records
    # this package emits at INFO now also reach whatever handlers the host has on the root
    # logger. Widening only. An application that has deliberately set a *more* verbose level
    # keeps it.
    if target_logger.level == logging.NOTSET or target_logger.level > level:
        target_logger.setLevel(level)

    router = APIRouter()

    @router.get("/activity")
    async def get_activity_page() -> HTMLResponse:
        # Self-contained HTML. No external fetches, no external CSS/JS.
        # The JS opens an EventSource to /activity/stream and appends lines to the log view.
        # Auto-scrolling is suppressed if the user scrolls up to read history.
        html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Middleware Activity</title>
    <style>
        body { font-family: monospace; margin: 0; padding: 0; background: rgb(245, 245, 245); }
        header { background: rgb(51, 51, 51); color: white; padding: 10px 20px; }
        header h1 { margin: 0; font-size: 1.2rem; }
        header p { margin: 5px 0 0; font-size: 0.9rem; opacity: 0.8; }
        .log { height: calc(100vh - 80px); overflow-y: auto; padding: 10px 20px; }
        .entry { padding: 4px 0; border-bottom: 1px solid rgb(221, 221, 221); }
        .entry:last-child { border-bottom: none; }
        .time { color: rgb(102, 102, 102); width: 90px; display: inline-block; }
        .level { font-weight: bold; width: 80px; display: inline-block; }
        .logger { color: rgb(0, 102, 204); width: 200px; display: inline-block; }
        .message { color: rgb(51, 51, 51); }
        .WARNING .level { color: rgb(153, 102, 0); }
        .ERROR .level { color: rgb(204, 0, 0); }
        .WARNING { background: rgb(255, 248, 225); }
        .ERROR { background: rgb(255, 235, 238); }
    </style>
</head>
<body>
    <header>
        <h1>Middleware Activity</h1>
        <p>Shows machinery: wiring, traffic, registrations. Not resource state.</p>
    </header>
    <div id="log" class="log"></div>
    <script>
        const log = document.getElementById('log');
        let lastSeq = 0;
        let isAtBottom = true;

        function appendRecord(record) {
            const div = document.createElement('div');
            div.className = 'entry ' + record.level;
            const date = new Date(record.timestamp * 1000);
            const timeStr = date.toLocaleTimeString();
            const loggerShort = record.logger.split('.').pop();
            div.innerHTML = '<span class="time">' + timeStr + '</span>' +
                            '<span class="level">' + record.level + '</span>' +
                            '<span class="logger">' + loggerShort + '</span>' +
                            '<span class="message">' + escapeHtml(record.message) + '</span>';
            log.appendChild(div);
            if (isAtBottom) {
                log.scrollTop = log.scrollHeight;
            }
        }

        function escapeHtml(text) {
            const map = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;'};
            return text.replace(/[&<>"']/g, m => map[m]);
        }

        log.addEventListener('scroll', () => {
            isAtBottom = (log.scrollTop + log.clientHeight >= log.scrollHeight - 10);
        });

        const source = new EventSource('/activity/stream');
        source.onmessage = (event) => {
            const record = JSON.parse(event.data);
            appendRecord(record);
            lastSeq = record.seq;
        };
        source.onerror = () => {
            // Silent fail; browser will reconnect.
        };
    </script>
</body>
</html>
        """
        return HTMLResponse(html)

    @router.get("/activity/stream")
    async def get_activity_stream(request: Request) -> StreamingResponse:
        # SSE endpoint. Polls the feed rather than wait on a queue. The logging handler runs in
        # worker threads. No asyncio loop is available there. Push from the handler would
        # require call_soon_threadsafe and a captured loop. This breaks at construction time.
        # Polling is cheap and robust.
        async def generate() -> AsyncIterator[str]:
            last_yield = time.time()
            client_seq = 0

            # Seed with existing records. A viewer that arrives mid-run does not see an empty
            # page.
            for record in feed.snapshot():
                yield f"data: {json.dumps(record.__dict__)}\n\n"
                client_seq = record.seq
                last_yield = time.time()

            try:
                while True:
                    # Stop when the viewer goes away. uvicorn does cancel the task on
                    # disconnect. Rely on that alone. This leaks a polling loop per disconnect
                    # under any server that does not. This makes the endpoint untestable. A
                    # client that stops reading otherwise hangs forever. It waits for a
                    # generator that never ends.
                    if await request.is_disconnected():
                        return

                    # Report anything the ring buffer dropped before this client could be
                    # handed it. Unreachable at demo rates. Measured at ~0.4 records/sec
                    # against a 200-record window. A burst outruns the window. It would
                    # otherwise take lines out of the feed with nothing to show for it. A
                    # viewer cannot tell "nothing happened" from "I missed it".
                    oldest = feed.oldest_seq
                    if oldest > client_seq + 1:
                        dropped = oldest - client_seq - 1
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "seq": oldest - 1,
                                    "timestamp": time.time(),
                                    "level": "WARNING",
                                    "logger": __name__,
                                    "message": (
                                        f"{dropped} record(s) dropped -- the feed produced "
                                        f"faster than this view could read it"
                                    ),
                                }
                            )
                            + "\n\n"
                        )
                        client_seq = oldest - 1
                        last_yield = time.time()

                    # Poll for new records.
                    new_records = feed.since(client_seq)
                    for record in new_records:
                        yield f"data: {json.dumps(record.__dict__)}\n\n"
                        client_seq = record.seq
                        last_yield = time.time()

                    # Keepalive every 15s of silence. Proxies do not close the connection.
                    if time.time() - last_yield > 15:
                        yield ": keepalive\n\n"
                        last_yield = time.time()

                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                # Clean termination when the client disconnects or the server shuts down.
                raise

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    middleware.app.include_router(router)
    middleware.activity_feed = feed
    return feed
