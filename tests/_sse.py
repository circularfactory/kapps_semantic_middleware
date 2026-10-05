"""Follow an activity stream the way a browser's ``EventSource`` does.

A stream of ``/activity/stream`` ends after a short lifetime, so that it never holds a stopping
server open. A client reconnects at once and sends the id of the last event it read as
``Last-Event-ID``.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Callable, Iterator, Optional

import httpx


def follow(
    url: str,
    *,
    until: Callable[[], bool],
    opened: Optional[threading.Event] = None,
    read_timeout: float = 10.0,
) -> Iterator[str]:
    """Yield every line of the stream at ``url``, across its ends, until ``until()`` is true.

    ``until`` is checked after every line and before every reconnect. A stream ends within its
    lifetime, so the check comes round within it too. ``opened`` is set once a response is in
    flight. A refused or broken connection is retried, as a browser retries it; an HTTP error
    status is raised, because a browser gives up on one as well.
    """
    last_event_id = ""
    with httpx.Client(timeout=httpx.Timeout(read_timeout, connect=10.0)) as client:
        while not until():
            headers = {"Last-Event-ID": last_event_id} if last_event_id else {}
            try:
                with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    if opened is not None:
                        opened.set()
                    for line in response.iter_lines():
                        if line.startswith("id: "):
                            last_event_id = line[len("id: ") :]
                        yield line
                        if until():
                            return
            except httpx.TransportError:
                time.sleep(0.05)


def hold(url: str, *, until: Callable[[], bool], opened: threading.Event) -> threading.Thread:
    """Keep the stream at ``url`` open from a daemon thread, as an open browser page does.

    The thread follows the stream and discards what it reads, until ``until()`` is true.
    ``opened`` is set once a response is in flight.
    """
    thread = threading.Thread(
        target=lambda: collections.deque(follow(url, until=until, opened=opened), maxlen=0),
        daemon=True,
    )
    thread.start()
    return thread
