"""Behaviour tests for the dashboard's Logs tab.

The tab shows the newest records of the whole process, every logger, from a buffer in memory,
and a form that sets a logger's level at run time through ``POST dashboard/logs/level``. These
tests drive it through a fake middleware and a FastAPI TestClient. No store and no network.

The handler that fills the buffer is attached to the root logger once per process, so records
of other tests reach it too. Each test logs under a logger name and a message of its own and
looks for those.
"""

from __future__ import annotations

import gc
import logging
import re
import weakref
from html.parser import HTMLParser
from typing import Callable, Iterator

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kapps_semantic_middleware import dashboard_logs
from kapps_semantic_middleware.dashboard import enable_dashboard


class FakeMiddleware:
    """The least middleware the dashboard needs: an app to attach routes to."""

    def __init__(self) -> None:
        self.app = FastAPI()


def test_the_logs_tab_shows_a_record_of_any_logger() -> None:
    """A record of a logger outside the library reaches the Logs tab of the page."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw)
    logging.getLogger("tests.dashboard_logs.any").warning("a record of a subproject")

    assert "Logs" in registry.tabs()
    body = TestClient(mw.app).get("/dashboard/box/logs").text
    assert "tests.dashboard_logs.any" in body
    assert "a record of a subproject" in body


def _rows(body: str) -> list[str]:
    """The record rows of the box, top to bottom, as their raw HTML."""
    table = body.split('class="logs-records"', 1)[1]
    return re.findall(r"<tr class=\"lvl-[a-z]+\">.*?</tr>", table)


def test_the_box_keeps_the_newest_records_newest_first() -> None:
    """Past the capacity the oldest record goes, and the newest stands at the top."""
    mw = FakeMiddleware()
    enable_dashboard(mw)
    log = logging.getLogger("tests.dashboard_logs.capacity")
    for n in range(dashboard_logs.CAPACITY + 3):
        log.warning("capacity record %d.", n)

    rows = _rows(TestClient(mw.app).get("/dashboard/box/logs").text)
    assert len(rows) == dashboard_logs.CAPACITY
    assert f"capacity record {dashboard_logs.CAPACITY + 2}." in rows[0]
    assert "capacity record 3." in rows[-1]
    assert "capacity record 2." not in "".join(rows)


def test_the_box_shows_the_records_of_every_logger() -> None:
    """A library's logger, the middleware's own and a subproject's all reach the box."""
    mw = FakeMiddleware()
    enable_dashboard(mw)
    names = ("httpx", "kapps_semantic_middleware.middleware", "jms_kapps.uc2.module")
    for name in names:
        logging.getLogger(name).error("every logger: %s", name)

    body = TestClient(mw.app).get("/dashboard/box/logs").text
    for name in names:
        assert f"every logger: {name}" in body


def test_several_instances_share_one_handler_on_the_root_logger() -> None:
    """Two instances in one process attach one handler, and both show the same record."""
    first, second = FakeMiddleware(), FakeMiddleware()
    enable_dashboard(first)
    enable_dashboard(second)
    logging.getLogger("tests.dashboard_logs.shared").warning("one record, two pages")

    ours = [
        h for h in logging.getLogger().handlers if isinstance(h, dashboard_logs._Handler)
    ]
    assert len(ours) == 1
    for mw in (first, second):
        body = TestClient(mw.app).get("/dashboard/box/logs").text
        assert body.count("one record, two pages") == 1


def test_every_message_and_logger_name_is_escaped() -> None:
    """Markup in a message or a logger's name reaches the page as text, not as markup."""
    mw = FakeMiddleware()
    enable_dashboard(mw)
    logging.getLogger("tests.<b>logger</b>").warning("<script>alert(1)</script> & more")

    body = TestClient(mw.app).get("/dashboard/box/logs").text
    assert "<script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in body
    assert "<b>logger</b>" not in body
    assert "tests.&lt;b&gt;logger&lt;/b&gt;" in body


def test_the_buffer_keeps_the_text_and_not_the_arguments() -> None:
    """A record's arguments are not kept alive by the buffer: it holds the message as text.

    The record goes to a handler of its own rather than through a logger, because pytest's
    capture handler keeps every record of a test alive until the test ends.
    """

    class Big:
        def __str__(self) -> str:
            return "a big object"

    handler = dashboard_logs._Handler()
    argument = Big()
    alive = weakref.ref(argument)
    handler.handle(
        logging.makeLogRecord(
            {
                "name": "tests.dashboard_logs.arguments",
                "levelno": logging.WARNING,
                "levelname": "WARNING",
                "msg": "holds %s",
                "args": (argument,),
            }
        )
    )
    del argument
    gc.collect()

    assert alive() is None
    assert "holds a big object" in dashboard_logs._render(handler)


def test_a_record_with_an_exception_shows_the_exception() -> None:
    """The exception's type and message follow the record's message."""
    mw = FakeMiddleware()
    enable_dashboard(mw)
    try:
        raise ValueError("the reason it failed")
    except ValueError:
        logging.getLogger("tests.dashboard_logs.exc").exception("a step failed")

    body = TestClient(mw.app).get("/dashboard/box/logs").text
    assert "a step failed" in body
    assert "ValueError: the reason it failed" in body


def test_a_long_message_is_cut_and_says_so() -> None:
    """A message past the limit is kept up to the limit, with the count of what was cut."""
    mw = FakeMiddleware()
    enable_dashboard(mw)
    limit = dashboard_logs.MESSAGE_LIMIT
    logging.getLogger("tests.dashboard_logs.long").warning("x" * (limit + 7))

    body = TestClient(mw.app).get("/dashboard/box/logs").text
    assert "x" * limit in body
    assert "x" * (limit + 1) not in body
    assert "7 more characters" in body


@pytest.fixture
def remember() -> Iterator[Callable[[str], str]]:
    """Remember a logger's level before a test sets it, and give it back afterwards."""
    saved: dict[str, int] = {}

    def remember(name: str) -> str:
        saved.setdefault(name, logging.getLogger(name).level)
        return name

    yield remember
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def _client() -> TestClient:
    mw = FakeMiddleware()
    enable_dashboard(mw)
    return TestClient(mw.app)


def test_the_level_route_sets_a_loggers_level_and_answers_it(
    remember: Callable[[str], str],
) -> None:
    """A standard level name sets the logger's level, and the answer is the level in force."""
    name = remember("tests.dashboard_logs.route")

    response = _client().post("/dashboard/logs/level", json={"logger": name, "level": "DEBUG"})

    assert response.status_code == 200
    assert response.json() == {"logger": name, "level": "DEBUG"}
    assert logging.getLogger(name).level == logging.DEBUG


def test_a_record_below_the_old_level_reaches_the_box_once_the_level_is_set(
    remember: Callable[[str], str],
) -> None:
    """The level is in force at once: an INFO record that WARNING held back now shows."""
    name = remember("tests.dashboard_logs.verbose")
    client = _client()
    log = logging.getLogger(name)
    log.setLevel(logging.WARNING)
    log.info("held back")

    client.post("/dashboard/logs/level", json={"logger": name, "level": "INFO"})
    log.info("let through")

    body = client.get("/dashboard/box/logs").text
    assert "let through" in body
    assert "held back" not in body


@pytest.mark.parametrize("level", ["VERBOSE", "debug", "Warning", "WARN", "FATAL", "10", ""])
def test_the_level_route_refuses_a_name_that_is_not_a_standard_level(
    level: str, remember: Callable[[str], str]
) -> None:
    """Only the standard names, spelled as `logging` spells them, set a level."""
    name = remember("tests.dashboard_logs.refused")
    logging.getLogger(name).setLevel(logging.ERROR)

    response = _client().post("/dashboard/logs/level", json={"logger": name, "level": level})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "DEBUG, INFO, WARNING, ERROR, CRITICAL" in detail
    assert logging.getLogger(name).level == logging.ERROR


def test_the_level_route_refuses_a_level_that_is_not_a_name(
    remember: Callable[[str], str],
) -> None:
    """A number is not a level name, although `logging` would take one."""
    name = remember("tests.dashboard_logs.number")

    response = _client().post("/dashboard/logs/level", json={"logger": name, "level": 10})

    assert response.status_code == 422
    assert logging.getLogger(name).level == logging.NOTSET


def test_the_level_route_refuses_an_empty_logger_name() -> None:
    """The root logger is named `root`; an empty name is refused rather than taken for it."""
    root_level = logging.getLogger().level

    response = _client().post("/dashboard/logs/level", json={"logger": " ", "level": "DEBUG"})

    assert response.status_code == 422
    assert "root" in response.json()["detail"]
    assert logging.getLogger().level == root_level


def test_root_names_the_root_logger(remember: Callable[[str], str]) -> None:
    """`root` sets the root logger's level, as `logging.getLogger("root")` names it."""
    remember("root")

    response = _client().post("/dashboard/logs/level", json={"logger": "root", "level": "ERROR"})

    assert response.json() == {"logger": "root", "level": "ERROR"}
    assert logging.getLogger().level == logging.ERROR


def test_notset_hands_the_level_back_to_the_parent(remember: Callable[[str], str]) -> None:
    """NOTSET clears the logger's own level, and the answer is the level it inherits."""
    parent = remember("tests.dashboard_logs.parent")
    child = remember("tests.dashboard_logs.parent.child")
    logging.getLogger(parent).setLevel(logging.ERROR)
    logging.getLogger(child).setLevel(logging.DEBUG)

    response = _client().post("/dashboard/logs/level", json={"logger": child, "level": "NOTSET"})

    assert response.json() == {"logger": child, "level": "ERROR"}
    assert logging.getLogger(child).level == logging.NOTSET


def test_the_form_gets_the_answer_as_html(remember: Callable[[str], str]) -> None:
    """The page's form posts through htmx, and the answer it shows is HTML, escaped."""
    name = remember("tests.<i>form</i>")

    response = _client().post(
        "/dashboard/logs/level",
        json={"logger": name, "level": "WARNING"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "tests.&lt;i&gt;form&lt;/i&gt;" in response.text
    assert "WARNING" in response.text


def test_the_form_shows_a_refusal_in_the_page(remember: Callable[[str], str]) -> None:
    """htmx swaps only a 2xx answer, so a refusal to the form comes as 200 with an error box."""
    name = remember("tests.dashboard_logs.form_refused")

    response = _client().post(
        "/dashboard/logs/level",
        json={"logger": name, "level": "LOUD"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'class="error-box"' in response.text
    assert "LOUD" in response.text
    assert logging.getLogger(name).level == logging.NOTSET


def test_the_level_route_answers_404_when_the_box_is_hidden(
    remember: Callable[[str], str],
) -> None:
    """A hidden Logs box takes its route with it, as a hidden box's fragment answers 404."""
    name = remember("tests.dashboard_logs.hidden")
    mw = FakeMiddleware()
    enable_dashboard(mw).hide("logs")

    response = TestClient(mw.app).post(
        "/dashboard/logs/level", json={"logger": name, "level": "DEBUG"}
    )

    assert response.status_code == 404
    assert logging.getLogger(name).level == logging.NOTSET


def test_without_the_default_boxes_there_is_no_logs_tab_and_no_route() -> None:
    """`register_default_boxes=False` still starts an empty page: the Logs box is a default."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    assert registry.tabs() == []
    paths = {getattr(route, "path", "") for route in mw.app.routes}
    assert "/dashboard/logs/level" not in paths


def _box() -> str:
    return _client().get("/dashboard/box/logs").text


def _hidden_by_filter(body: str) -> dict[str, set[str]]:
    """For each filter radio, the row classes the box's style hides while it is checked."""
    style = body.split("<style>", 1)[1].split("</style>", 1)[0]
    hidden: dict[str, set[str]] = {}
    for shown, row in re.findall(
        r"\.logs-view:has\(#logs-show-([a-z]+):checked\)\s+\.logs-records\s+tr\.(lvl-[a-z]+)",
        style,
    ):
        hidden.setdefault(shown, set()).add(row)
    return hidden


def test_the_filter_hides_the_records_below_the_chosen_level() -> None:
    """Each level of the filter hides exactly the records below it; DEBUG hides none."""
    body = _box()

    radios = re.findall(r'<input type="radio" name="logs-show" id="logs-show-([a-z]+)"', body)
    assert radios == ["debug", "info", "warning", "error", "critical"]
    assert re.search(r'id="logs-show-debug"[^>]*checked', body)
    assert _hidden_by_filter(body) == {
        "info": {"lvl-debug"},
        "warning": {"lvl-debug", "lvl-info"},
        "error": {"lvl-debug", "lvl-info", "lvl-warning"},
        "critical": {"lvl-debug", "lvl-info", "lvl-warning", "lvl-error"},
    }


def test_a_record_of_a_custom_level_is_filtered_with_the_standard_level_below_it(
    remember: Callable[[str], str],
) -> None:
    """Levels 5, 25 and 45 are shown with DEBUG, INFO and ERROR records."""
    name = remember("tests.dashboard_logs.custom")
    log = logging.getLogger(name)
    log.setLevel(1)
    for level in (5, 25, 45):
        log.log(level, "custom level %d.", level)

    rows = _rows(_box())
    for level, row_class in ((5, "lvl-debug"), (25, "lvl-info"), (45, "lvl-error")):
        [row] = [row for row in rows if f"custom level {level}." in row]
        assert row.startswith(f'<tr class="{row_class}">')


def test_the_filter_and_the_form_survive_a_refresh_of_the_box() -> None:
    """htmx keeps an element marked hx-preserve, by its id, when the box is swapped in again."""
    body = _box()

    assert re.search(r'<fieldset id="logs-filter" hx-preserve="true"', body)
    assert re.search(r'<form id="logs-level-form" hx-preserve="true"', body)


def test_the_form_posts_json_to_the_level_route_by_a_relative_path() -> None:
    """The form names the route without a leading slash, and shows the answer in itself."""
    body = _box()
    form = re.search(r"<form .*?</form>", body)
    assert form is not None

    assert 'hx-post="dashboard/logs/level"' in form.group(0)
    assert 'hx-ext="json-enc"' in form.group(0)
    assert 'name="logger"' in form.group(0)
    levels = re.findall(r'<option value="([A-Z]+)"', form.group(0))
    assert levels == ["NOTSET", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    target = re.search(r'hx-target="#([a-z-]+)"', form.group(0))
    assert target is not None
    assert f'id="{target.group(1)}"' in form.group(0)


def test_the_box_lists_the_loggers_with_their_levels(remember: Callable[[str], str]) -> None:
    """Root first, then every logger seen, with its own level and the level in force."""
    parent = remember("tests.dashboard_logs.listed")
    child = "tests.dashboard_logs.listed.child"
    logging.getLogger(parent).setLevel(logging.ERROR)
    logging.getLogger(child).error("a record of the child")

    body = _box()
    table = body.split("<th>Logger</th>", 1)[1].split("</table>", 1)[0]
    rows = re.findall(r"<tr><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td></tr>", table)
    assert rows[0][0] == "root"
    assert (child, "NOTSET", "ERROR") in rows
    assert f'<option value="{child}">' in body


def test_a_logger_whose_level_was_set_is_listed_before_it_writes(
    remember: Callable[[str], str],
) -> None:
    """A level set through the route puts the logger in the list at once."""
    name = remember("tests.dashboard_logs.silent")
    client = _client()

    client.post("/dashboard/logs/level", json={"logger": name, "level": "INFO"})

    assert f"<tr><td>{name}</td><td>INFO</td><td>INFO</td></tr>" in (
        client.get("/dashboard/box/logs").text
    )


def test_listing_a_name_with_no_logger_makes_no_logger() -> None:
    """A record can name a logger the process does not have; the box makes none for it."""
    name = "tests.dashboard_logs.from_elsewhere"
    _client()
    logging.getLogger().handle(
        logging.makeLogRecord(
            {"name": name, "levelno": logging.ERROR, "levelname": "ERROR", "msg": "forwarded"}
        )
    )

    body = _box()

    assert f"<tr><td>{name}</td><td>&mdash;</td><td>&mdash;</td></tr>" in body
    assert name not in logging.Logger.manager.loggerDict


def test_the_box_says_so_when_its_handler_was_removed() -> None:
    """Logging configured again after the dashboard drops the handler; the box tells."""
    client = _client()
    handler = dashboard_logs._process_handler()
    logging.getLogger().removeHandler(handler)
    try:
        body = client.get("/dashboard/box/logs").text
    finally:
        logging.getLogger().addHandler(handler)

    assert "No new record reaches it" in body
    assert "No new record reaches it" not in client.get("/dashboard/box/logs").text


# --- A record of several lines: its first line in the row, the rest behind an expander. -------


class _Cell:
    """The message cell of one row, as a browser shows it."""

    def __init__(self) -> None:
        self.plain = ""  # text outside any expander: what the row shows
        self.summary = ""  # the expander's first line, which is always visible
        self.rest = ""  # what the expander holds, as the browser's text
        self.expander: dict[str, str | None] | None = None  # its attributes, if it has one
        self.tags: list[str] = []


class _Rows(HTMLParser):
    """Reads the box as a browser does: character references become text, and each row's message
    cell keeps its text, its expander and every tag it holds."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[_Cell] = []
        self._in_records = False
        self._td = 0
        self._open: list[str] = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "table" and "logs-records" in (a.get("class") or ""):
            self._in_records = True
        if not self._in_records:
            return
        if tag == "tr":
            self._td = 0
        elif tag == "td":
            self._td += 1
            if self._td == 4:
                self.cells.append(_Cell())
        elif self._td == 4:
            self.cells[-1].tags.append(tag)
            if tag == "details":
                self.cells[-1].expander = a
        if tag not in ("br", "input"):
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._in_records = False
        if tag in self._open:
            while self._open.pop() != tag:
                pass

    def handle_data(self, data: str) -> None:
        if not self._in_records or self._td != 4:
            return
        cell = self.cells[-1]
        if "summary" in self._open:
            cell.summary += data
        elif "details" in self._open:
            cell.rest += data
        else:
            cell.plain += data


def _cell_of(marker: str, body: str | None = None) -> _Cell:
    """The message cell of the one row whose text holds `marker`."""
    cells = _Rows(body if body is not None else _box()).cells
    (found,) = [c for c in cells if marker in c.plain + c.summary + c.rest]
    return found


def test_a_record_of_three_lines_shows_its_first_line_and_the_rest_behind_an_expander() -> None:
    mw = FakeMiddleware()
    enable_dashboard(mw)
    logging.getLogger("tests.dashboard_logs.lines").warning("first line A\n  second, indented\n    third")

    cell = _cell_of("first line A", TestClient(mw.app).get("/dashboard/box/logs").text)
    assert cell.summary == "first line A"
    assert cell.rest == "  second, indented\n    third"
    assert cell.plain == ""
    assert cell.expander is not None and "open" not in cell.expander


def test_a_record_of_one_line_has_no_expander() -> None:
    logging.getLogger("tests.dashboard_logs.oneline").warning("one line only B")

    cell = _cell_of("one line only B")
    assert cell.plain == "one line only B"
    assert cell.expander is None


def test_a_record_with_a_trailing_line_break_only_has_no_expander() -> None:
    logging.getLogger("tests.dashboard_logs.trailing").warning("ends with a break C\n")

    assert _cell_of("ends with a break C").expander is None


def test_markup_in_a_later_line_is_shown_as_text_in_the_expander() -> None:
    logging.getLogger("tests.dashboard_logs.markup").warning("markup record D\n<b>x</b> & <i>y</i>")

    cell = _cell_of("markup record D")
    assert cell.rest == "<b>x</b> & <i>y</i>"
    assert "b" not in cell.tags and "i" not in cell.tags


def test_markup_in_the_first_line_is_shown_as_text() -> None:
    logging.getLogger("tests.dashboard_logs.markup1").warning("<b>head</b> record E\nrest")

    cell = _cell_of("record E")
    assert cell.summary == "<b>head</b> record E"
    assert "b" not in cell.tags


def test_an_expander_keeps_its_line_breaks_and_indentation_in_the_page() -> None:
    """The rest is preformatted text, so a browser shows each line on its own line."""
    logging.getLogger("tests.dashboard_logs.pre").warning("pre record F\n  a\n    b")

    assert "<pre" in _box().split("pre record F", 1)[1]


def test_each_expander_has_a_stable_id_that_htmx_preserves_across_a_refresh() -> None:
    """The id comes from a private sequence number, so it is the same in every render of the
    box, and a new record changes no other record's id."""
    log = logging.getLogger("tests.dashboard_logs.ids")
    log.warning("ids first G\nrest 1")
    before = _cell_of("ids first G").expander
    log.warning("ids second H\nrest 2")

    after = _cell_of("ids first G").expander
    newer = _cell_of("ids second H").expander
    assert before is not None and after is not None and newer is not None
    assert before["id"] == after["id"] and before["id"]
    assert newer["id"] != after["id"]
    assert after.get("hx-preserve") is not None
    ids = [c.expander["id"] for c in _Rows(_box()).cells if c.expander]
    assert len(ids) == len(set(ids))


def test_the_cut_of_a_long_message_is_inside_the_expander() -> None:
    """Past the limit the record is cut as before, and the rest ends with the note of what was cut."""
    limit = dashboard_logs.MESSAGE_LIMIT
    logging.getLogger("tests.dashboard_logs.longlines").warning("long record I\n" + "y" * (limit + 5))

    cell = _cell_of("long record I")
    assert cell.summary == "long record I"
    assert cell.rest.endswith(" … 19 more characters")
    assert len(cell.rest) < limit + 40
