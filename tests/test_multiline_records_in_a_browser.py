"""A record of several lines on the activity page and in the dashboard's Logs tab, in a browser.

Both pages draw a record's message: the activity page in its own script, the Logs tab on the
server, with htmx swapping the box in again every five seconds. A test that read the markup could
not tell what a reader sees, so these tests serve an instance in this process on a real port and
open its pages in a headless browser, which skips where none is installed (see `_browser.py`).
The first line is the text before the first line break; the rest is behind an expander that is
closed at first.
"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect

from kapps_semantic_middleware.activity import _ActivityHandler, enable_activity_feed
from kapps_semantic_middleware.dashboard import enable_dashboard

pytestmark = pytest.mark.browser

# A logger inside the package, so that the activity feed captures it.
ACTIVITY_LOGGER = "kapps_semantic_middleware.tests.multiline"
# Any logger does for the Logs tab.
LOGS_LOGGER = "tests.multiline.browser"


class _Instance:
    """The least middleware that both pages need: an app to attach routes to."""

    def __init__(self) -> None:
        self.app = FastAPI()


@contextmanager
def _serving(app: FastAPI) -> Iterator[str]:
    """Serve `app` on a free port of 127.0.0.1 until the block ends, and give its address."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the server did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def served() -> Iterator[str]:
    """An instance with both pages, served until the test ends."""
    instance = _Instance()
    feed_logger = logging.getLogger("kapps_semantic_middleware")
    enable_activity_feed(instance)
    enable_dashboard(instance)
    try:
        with _serving(instance.app) as address:
            yield address
    finally:
        # The feed's handler belongs to this instance; leave none behind for other tests.
        feed_logger.handlers = [
            h for h in feed_logger.handlers if not isinstance(h, _ActivityHandler)
        ]


def _entry(page: Page, marker: str):
    """The row of the activity page whose text holds `marker`."""
    return page.locator("#log .entry").filter(has_text=marker)


def test_the_activity_page_folds_a_record_of_three_lines_behind_an_expander(
    served: str, page: Page
) -> None:
    log = logging.getLogger(ACTIVITY_LOGGER)
    log.info("head of three A\n  two, indented\n    three, more")
    log.info("a single line B")
    log.info("markup record C\n<b>x</b> & <i>y</i>")
    page.goto(f"{served}/activity")

    folded = _entry(page, "head of three A")
    expect(folded).to_have_count(1)
    expect(folded.locator("summary")).to_have_text("head of three A")
    # Closed at first: the other two lines are not shown.
    expect(folded.locator("details")).not_to_have_attribute("open", "")
    expect(folded.locator("details > pre")).not_to_be_visible()
    # Opened, they show on two lines, with their leading spaces kept.
    folded.locator("summary").click()
    rest = folded.locator("details > pre")
    expect(rest).to_be_visible()
    assert rest.text_content() == "  two, indented\n    three, more"
    lines = rest.evaluate(
        "el => { const r = document.createRange(); r.selectNodeContents(el);"
        " const rects = Array.from(r.getClientRects()); return new Set(rects.map(x => Math.round(x.top))).size; }"
    )
    assert lines == 2
    assert rest.evaluate("el => getComputedStyle(el).whiteSpace").startswith("pre")

    # A one-line record has no expander.
    single = _entry(page, "a single line B")
    expect(single).to_have_count(1)
    expect(single.locator("details")).to_have_count(0)
    expect(single.locator(".message")).to_have_text("a single line B")

    # Markup is text, in the expander.
    marked = _entry(page, "markup record C")
    marked.locator("summary").click()
    expect(marked.locator("details > pre")).to_have_text("<b>x</b> & <i>y</i>")
    expect(marked.locator("b, i")).to_have_count(0)


def test_the_activity_page_shows_markup_in_a_first_line_as_text(
    served: str, page: Page
) -> None:
    logging.getLogger(ACTIVITY_LOGGER).info("<u>first</u> line D\nsecond")
    page.goto(f"{served}/activity")

    row = _entry(page, "line D")
    expect(row.locator("summary")).to_have_text("<u>first</u> line D")
    expect(row.locator("u")).to_have_count(0)


def test_the_activity_page_folds_a_record_that_arrives_after_it_opened(
    served: str, page: Page
) -> None:
    page.goto(f"{served}/activity")
    logging.getLogger(ACTIVITY_LOGGER).info("late head E\nlate rest")

    row = _entry(page, "late head E")
    expect(row.locator("summary")).to_have_text("late head E")
    expect(row.locator("details > pre")).not_to_be_visible()


def _logs_row(page: Page, marker: str):
    return page.locator("#box-logs .logs-records tr").filter(has_text=marker)


def test_the_logs_tab_folds_a_record_and_keeps_an_open_expander_open_when_the_box_refreshes(
    served: str, page: Page
) -> None:
    log = logging.getLogger(LOGS_LOGGER)
    log.warning("logs head of three F\n  two, indented\n    three")
    log.warning("logs one line G")
    log.warning("logs markup H\n<b>x</b>")
    page.goto(f"{served}/dashboard?tab=Logs")

    folded = _logs_row(page, "logs head of three F")
    expect(folded.locator("summary")).to_have_text("logs head of three F")
    expect(folded.locator("details > pre")).not_to_be_visible()
    expect(_logs_row(page, "logs one line G").locator("details")).to_have_count(0)
    marked = _logs_row(page, "logs markup H")
    marked.locator("summary").click()
    expect(marked.locator("details > pre")).to_have_text("<b>x</b>")
    expect(marked.locator("b")).to_have_count(0)

    # Open one, then let the box refresh: a new record arrives, and the box is swapped in again.
    folded.locator("summary").click()
    expect(folded.locator("details > pre")).to_be_visible()
    log.warning("logs newcomer I\nnew rest")
    with page.expect_response(
        lambda r: r.url.endswith("dashboard/box/logs"), timeout=15000
    ):
        pass
    newcomer = _logs_row(page, "logs newcomer I")
    expect(newcomer).to_have_count(1, timeout=15000)

    # The reader's expanders stay open; the new record's is closed.
    expect(
        _logs_row(page, "logs head of three F").locator("details > pre")
    ).to_be_visible()
    expect(_logs_row(page, "logs markup H").locator("details > pre")).to_be_visible()
    expect(newcomer.locator("details > pre")).not_to_be_visible()
    # And one that was closed stays closed.
    newcomer.locator("summary").click()
    expect(newcomer.locator("details > pre")).to_be_visible()


# --- Live: the store refuses a write, and both pages show the record folded. ----------------


def test_a_refused_write_shows_as_its_first_line_on_both_pages(
    store, page: Page
) -> None:
    """The INFO record of a refusal: on `/activity` the first line names the verb and the rules
    and the whole record is behind the expander; in the Logs tab the rest is, up to the tab's cut."""
    from test_refused_writes_integration import (
        _SNAPSHOT_SHAPES,
        _TBOX,
        DECISION,
        SNAPSHOT,
        ClassScope,
        _instance,
        _refused,
        seed,
    )

    seed.seed_scenario1(store)
    store.graph_add(_TBOX, content_type="application/x-turtle")
    mw = _instance(store, seed.HELLO_RESOURCE, seed.HELLO_SERVICE_CLASS)
    created = []

    def create() -> None:
        with mw.create(SNAPSHOT, ClassScope()) as snapshot:
            created.append(snapshot.iri)
            snapshot.update({DECISION: ["drill"]})

    refusal = _refused(store, _SNAPSHOT_SHAPES, create)
    local = str(created[0]).rsplit("#", 1)[-1]
    first_line = (
        f"create refused by DecisionValueShape at {local}, KeepOrAboutShape at {local}"
    )

    with _serving(mw.app) as address:
        page.goto(f"{address}/activity")
        row = _entry(page, first_line)
        expect(row.locator("summary")).to_have_text(first_line)
        expect(row.locator("details > pre")).not_to_be_visible()
        row.locator("summary").click()
        shown = row.locator("details > pre").text_content() or ""
        assert "Result 1 of 2" in shown and shown.endswith(refusal.report.rstrip("\n"))

        page.goto(f"{address}/dashboard?tab=Logs")
        logs_row = _logs_row(page, first_line)
        expect(logs_row.locator("summary")).to_have_text(first_line)
        expect(logs_row.locator("details > pre")).not_to_be_visible()
        logs_row.locator("summary").click()
        shown = logs_row.locator("details > pre").text_content() or ""
        assert "Result 1 of 2" in shown
        # Either the whole record, or the tab's cut of it with its note of what was cut.
        assert shown.endswith(refusal.report.rstrip("\n")) or re.search(
            r" … \d+ more characters$", shown
        )
