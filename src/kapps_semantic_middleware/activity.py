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

**Why a stream ends after two seconds:** uvicorn stops in three steps. It closes its listeners.
It waits for every open connection to finish, with no limit by default. Only then it runs the
lifespan shutdown, where the instance deregisters. A stream that never ends holds that wait open.
The supervisor then kills the instance, and a kill skips the deregistration. So every stream ends
after ``STREAM_LIFETIME_SECONDS``, and the client reconnects. Each event's SSE ``id`` names this
feed and the record. A browser's ``EventSource`` sends it back as ``Last-Event-ID`` and resumes
behind it, so nothing is lost or repeated.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, AsyncIterator, List, Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

STREAM_LIFETIME_SECONDS = 2.0
"""How long one ``/activity/stream`` response lasts before it ends by itself.

Shorter than a supervisor's stop timeout, so that a stop always reaches the deregistration. The
demo launcher allows 5 s.
"""

POLL_SECONDS = 0.25
"""How often a stream looks for new records."""

RECONNECT_MILLISECONDS = int(POLL_SECONDS * 1000)
"""The SSE ``retry:`` value: how soon a browser reconnects after a stream ends.

One poll period. A browser's own default is about 3 s, which would leave a gap in the view at
every end of a stream.
"""


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


def _sse_event(event_id: str, data: dict[str, Any]) -> str:
    """One server-sent event, with the ``id`` a client resumes behind."""
    return f"id: {event_id}\ndata: {json.dumps(data)}\n\n"


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

    # Every event id starts with this token. A new process on the same address numbers its
    # records from 1 again, so the number alone cannot tell a browser's old id from a new one.
    feed_token = uuid.uuid4().hex[:8]

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
        # The JS opens an EventSource to activity/stream and appends lines to the log view.
        # Auto-scrolling is suppressed if the user scrolls up to read history.
        # The stream's path is relative and starts with the page's own name: the page lives
        # at `/activity`, without a trailing slash, so it resolves next to the page, at the
        # process's root and behind a proxy that serves the process under a path prefix.
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
        details.message { display: inline-block; vertical-align: top; }
        details.message summary { cursor: pointer; }
        details.message pre { margin: 0; font: inherit; white-space: pre-wrap; }
        .WARNING .level { color: rgb(153, 102, 0); }
        .ERROR .level { color: rgb(204, 0, 0); }
        .WARNING { background: rgb(255, 248, 225); }
        .ERROR { background: rgb(255, 235, 238); }
    </style>
</head>
<body>
    <header>
        <h1>Middleware Activity</h1>
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
            // The first line is the text before the first line break. A record of several
            // lines shows it in the row, and the rest in an expander that is closed at first.
            const lineBreak = record.message.indexOf('\\n');
            const first = lineBreak < 0 ? record.message : record.message.slice(0, lineBreak);
            const rest = lineBreak < 0 ? '' : record.message.slice(lineBreak + 1);
            div.innerHTML = '<span class="time">' + timeStr + '</span>' +
                            '<span class="level">' + record.level + '</span>' +
                            '<span class="logger">' + loggerShort + '</span>' +
                            '<span class="message">' + escapeHtml(first) + '</span>';
            if (rest !== '') {
                // Built from text nodes, so that the rest is shown as text, with its line
                // breaks and its indentation.
                const span = div.querySelector('.message');
                const details = document.createElement('details');
                details.className = 'message';
                const summary = document.createElement('summary');
                summary.textContent = first;
                const pre = document.createElement('pre');
                pre.textContent = rest;
                details.appendChild(summary);
                details.appendChild(pre);
                span.replaceWith(details);
            }
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

        const source = new EventSource('activity/stream');
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

    # The page is served at one form only, without the trailing slash, because its relative
    # path resolves against its own address. The other form redirects to it by a relative
    # path, which keeps any prefix that a proxy serves the process under, and keeps the
    # query. Starlette's own redirect of a trailing slash names an absolute URL and drops
    # that prefix.
    @router.get("/activity/", include_in_schema=False)
    async def redirect_to_the_activity_page(request: Request) -> RedirectResponse:
        query = request.url.query
        return RedirectResponse(f"../activity?{query}" if query else "../activity")

    @router.get("/activity/stream")
    async def get_activity_stream(
        request: Request,
        last_event_id: Annotated[Optional[str], Header()] = None,
    ) -> StreamingResponse:
        """Server-sent events, one per record. **A stream ends after two seconds.**

        Reconnect when a stream ends, and send the ``id`` of the last event you read as the
        ``Last-Event-ID`` header. The next stream then starts behind that event, so a client
        loses nothing and receives nothing twice. A browser's ``EventSource`` does all of this
        by itself. Without the header, a stream starts with every record still buffered.

        The stream ends so that it never holds a stopping server open. The instance
        deregisters only after its server has closed every open connection.
        """
        # Polls the feed rather than wait on a queue. The logging handler runs in worker
        # threads. No asyncio loop is available there. Push from the handler would require
        # call_soon_threadsafe and a captured loop. This breaks at construction time. Polling
        # is cheap and robust.
        async def generate() -> AsyncIterator[str]:
            ends_at = time.monotonic() + STREAM_LIFETIME_SECONDS
            yield f"retry: {RECONNECT_MILLISECONDS}\n\n"

            # A resumed stream starts behind the last event its client read. A fresh one seeds
            # with everything buffered, so a viewer that arrives mid-run does not see an empty
            # page, and is not told of drops from before it arrived. An id from another feed,
            # such as an earlier process on the same address, starts a fresh stream.
            client_seq = max(feed.oldest_seq - 1, 0)
            token, _, seq = (last_event_id or "").rpartition("-")
            if token == feed_token and seq.isdigit():
                client_seq = int(seq)

            while True:
                # Stop when the viewer goes away. uvicorn does cancel the task on
                # disconnect. Rely on that alone. This leaks a polling loop per disconnect
                # under any server that does not.
                if await request.is_disconnected():
                    return

                # Report anything the ring buffer dropped before this client could be
                # handed it. Unreachable at demo rates. Measured at ~0.4 records/sec
                # against a 200-record window. A burst outruns the window, and so does a
                # client that stays away between two streams. It would otherwise take lines
                # out of the feed with nothing to show for it. A viewer cannot tell "nothing
                # happened" from "I missed it".
                oldest = feed.oldest_seq
                if oldest > client_seq + 1:
                    dropped = oldest - client_seq - 1
                    client_seq = oldest - 1
                    yield _sse_event(
                        f"{feed_token}-{client_seq}",
                        {
                            "seq": client_seq,
                            "timestamp": time.time(),
                            "level": "WARNING",
                            "logger": __name__,
                            "message": (
                                f"{dropped} record(s) dropped -- the feed produced "
                                f"faster than this view could read it"
                            ),
                        },
                    )

                for record in feed.since(client_seq):
                    yield _sse_event(f"{feed_token}-{record.seq}", record.__dict__)
                    client_seq = record.seq

                # No keepalive comment: a stream that ends every two seconds is never idle
                # long enough for a proxy to close it.
                if time.monotonic() >= ends_at:
                    return
                await asyncio.sleep(POLL_SECONDS)

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
