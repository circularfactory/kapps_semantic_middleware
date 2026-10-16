"""The dashboard's Logs tab: the process's newest log records, and a logger's level.

`/activity` streams the library's own logger. A reviewer of a participant also needs the
records of the subproject's loggers and of the libraries it calls, and needs to make a logger
more or less verbose while the process runs. This tab gives both.

A handler on the root logger keeps the newest records of every logger in a bounded buffer.
The handler is attached once per process, by the first `enable_dashboard` that registers the
default boxes, and every instance in the process reads the same buffer: the records belong to
the process, not to an instance. A record reaches the root logger only when its logger emits
it, at or above the level in force, and propagates it, which every logger does unless it is
told otherwise.

The box renders from that buffer and from the levels the loggers hold, which are both in
memory: the page causes no traffic. The one route here, `POST dashboard/logs/level`, changes
one logger's level in this process and nothing else. It writes nothing to the graph and
nothing to a device.

Nothing here is a public name of its own. A subproject reaches the tab through
`enable_dashboard`, and hides it, like the other default boxes, by its id `logs`.
"""

from __future__ import annotations

import collections
import itertools
import logging
import threading
import time
from dataclasses import dataclass
from typing import Annotated, Any, ContextManager, Deque, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from kapps_semantic_middleware.dashboard import DashboardRegistry, escape, hint

#: How many records the buffer keeps. The oldest goes when a new one arrives.
CAPACITY = 500

#: How many characters of a message the buffer keeps. With the capacity it bounds the buffer's
#: memory, and the size of the box, whatever a logger writes.
MESSAGE_LIMIT = 4000

#: How many logger names the box lists. The name seen longest ago goes when a new one arrives,
#: so a process that makes a logger per request cannot grow the list without bound.
LOGGER_LIMIT = 200

#: The box's id, which `hide` takes, and its tab.
BOX_ID = "logs"
TAB = "Logs"

#: The levels of the view filter, lowest first.
_FILTER_LEVELS = (
    logging.DEBUG,
    logging.INFO,
    logging.WARNING,
    logging.ERROR,
    logging.CRITICAL,
)

#: The names the route sets a level by: the standard levels, spelled as `logging` spells them.
#: NOTSET clears a logger's own level, so that it takes its parent's again.
_LEVEL_NAMES = ("NOTSET", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class _Record:
    """One log record, reduced to the text the box shows.

    The record keeps strings and numbers only, never the `LogRecord` itself: a record's
    arguments may be large objects, and the buffer must not keep them alive.
    """

    created: float
    levelno: int
    levelname: str
    logger: str
    message: str
    #: A private sequence number, the same for the record in every render of the box. It gives
    #: a record with several lines an expander id that holds still while the box refreshes.
    seq: int


class _Handler(logging.Handler):
    """Keeps the newest records of every logger that reaches it, and the loggers' names.

    `emit` does no I/O and calls no formatter: it reduces the record to text and appends it to
    a bounded deque. `logging` calls `emit` under the handler's own lock, and a reader takes
    the same lock to copy the deque, so a render never sees it change in the middle of a copy.
    """

    def __init__(self, capacity: int = CAPACITY) -> None:
        super().__init__(logging.NOTSET)
        self._records: Deque[_Record] = collections.deque(maxlen=capacity)
        # A dict as an ordered set: the names in the order they were first seen.
        self._loggers: Dict[str, None] = {}
        self._sequence = itertools.count(1)

    def _locked(self) -> ContextManager[Any]:
        # `Handler.__init__` always creates the lock; the type says it may be None.
        return self.lock if self.lock is not None else threading.RLock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._records.append(
                _Record(
                    created=record.created,
                    levelno=record.levelno,
                    levelname=record.levelname,
                    logger=record.name,
                    message=_message(record),
                    seq=next(self._sequence),
                )
            )
            self._see(record.name)
        except Exception:  # noqa: BLE001 - a handler must never raise into the caller
            self.handleError(record)

    def _see(self, name: str) -> None:
        """Add a logger's name to the list, dropping the oldest past `LOGGER_LIMIT`."""
        if name in self._loggers:
            return
        if len(self._loggers) >= LOGGER_LIMIT:
            del self._loggers[next(iter(self._loggers))]
        self._loggers[name] = None

    def see(self, name: str) -> None:
        """Add a logger's name to the list from outside `emit`, under the handler's lock."""
        with self._locked():
            self._see(name)

    def records(self) -> List[_Record]:
        """The buffered records, oldest first."""
        with self._locked():
            return list(self._records)

    def loggers(self) -> List[str]:
        """The names of the loggers seen so far."""
        with self._locked():
            return list(self._loggers)


def _message(record: logging.LogRecord) -> str:
    """The record's message, with its exception's type and text, cut at `MESSAGE_LIMIT`.

    The exception is named, not traced: formatting a traceback reads the source files of its
    frames, and `emit` does no I/O.
    """
    message = record.getMessage()
    if record.exc_info and record.exc_info[1] is not None:
        error = record.exc_info[1]
        message += f"\n{type(error).__name__}: {error}"
    if len(message) > MESSAGE_LIMIT:
        cut = len(message) - MESSAGE_LIMIT
        message = f"{message[:MESSAGE_LIMIT]} … {cut} more characters"
    return message


_ATTACH_LOCK = threading.Lock()
_handler: Optional[_Handler] = None


def _process_handler() -> _Handler:
    """The process's one handler, attached to the root logger if it is not there.

    One handler for the process, however many instances enable the dashboard: a second
    handler would store every record twice. It changes no logger's level, so the host's
    other handlers see what they saw before.
    """
    global _handler
    with _ATTACH_LOCK:
        if _handler is None:
            _handler = _Handler()
        root = logging.getLogger()
        if _handler not in root.handlers:
            root.addHandler(_handler)
        return _handler


def _set_level(handler: _Handler, name: str, level: str) -> logging.Logger:
    """Set the named logger's level, and add the logger to the box's list.

    Raises:
        ValueError: If `level` is not one of the standard level names, or `name` is empty.
            `logging` itself would take a number or a level name of its own, and an empty
            name would set the root logger's level; the route takes neither.
    """
    if level not in _LEVEL_NAMES:
        raise ValueError(f"{level!r} is not a level. Use one of {', '.join(_LEVEL_NAMES)}.")
    name = name.strip()
    if not name:
        raise ValueError("Name a logger. The root logger is named root.")
    # `getLogger("root")` is the root logger, as `logging` names it.
    target = logging.getLogger(name)
    target.setLevel(level)
    handler.see(target.name)
    return target


def _logger_named(name: str) -> Optional[logging.Logger]:
    """The logger of that name if the process has one. Unlike `getLogger`, it makes none."""
    if name == logging.getLogger().name:
        return logging.getLogger()
    found = logging.Logger.manager.loggerDict.get(name)
    return found if isinstance(found, logging.Logger) else None


def _time(created: float) -> str:
    """`HH:MM:SS.mmm` in the process's local time."""
    milliseconds = int(created % 1 * 1000)
    return time.strftime("%H:%M:%S", time.localtime(created)) + f".{milliseconds:03d}"


def _date_time(created: float) -> str:
    """`YYYY-MM-DD HH:MM:SS` in the process's local time, for the time's tooltip."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created))


def _bucket(levelno: int) -> str:
    """The filter level a record is shown at: the highest one at or below its own level.

    A custom level, such as 25 or 5, falls into the standard level below it, and a level
    below DEBUG into DEBUG, so every record is shown by the filter that shows its neighbours.
    """
    return logging.getLevelName(
        max((level for level in _FILTER_LEVELS if level <= levelno), default=logging.DEBUG)
    ).lower()


def _filter_css() -> str:
    """The rules that hide the records below the level the filter selects.

    The filter is CSS, not a request: the box shows every buffered record, and a rule per
    level hides the lower ones while that level's radio is checked. The radios keep their
    state across the box's refresh because htmx keeps an element marked `hx-preserve`, by its
    id, when it swaps the box in again.
    """
    rules = []
    for index, level in enumerate(_FILTER_LEVELS[1:], start=1):
        shown = logging.getLevelName(level).lower()
        for lower in _FILTER_LEVELS[:index]:
            rules.append(
                f".logs-view:has(#logs-show-{shown}:checked) "
                f".logs-records tr.lvl-{logging.getLevelName(lower).lower()}"
            )
    return ",\n".join(rules) + " { display: none; }"


_CSS = (
    """
.logs-view form, .logs-view fieldset { margin: 0 0 8px; font-family: var(--sans); }
.logs-view fieldset { border: 1px solid var(--panel-border); border-radius: 4px; }
.logs-view fieldset label { margin-right: 12px; }
.logs-view output { margin-left: 8px; }
.logs-view output .error-box { margin-top: 8px; }
.logs-loggers { max-height: 14rem; overflow-y: auto; margin: 0 0 12px; }
.logs-records td:nth-child(-n+2) { white-space: nowrap; }
.logs-records td:nth-child(4) { white-space: pre-wrap; }
.logs-records details pre { margin: 0; font: inherit; white-space: pre-wrap; }
.logs-records summary { cursor: pointer; }
.logs-records tr.lvl-warning td:nth-child(2) { font-weight: bold; }
.logs-records tr.lvl-error td, .logs-records tr.lvl-critical td { background: var(--error-bg); }
"""
    + _filter_css()
)


def _form_html(loggers: List[str]) -> str:
    """The form that sets a level, and the list of names its field offers.

    The form is marked `hx-preserve`, so a refresh of the box keeps what the operator typed
    and the answer to the last post. The list of names is outside it, so it does refresh.
    """
    options = "".join(
        f'<option value="{level}"{" selected" if level == "DEBUG" else ""}>{level}</option>'
        for level in _LEVEL_NAMES
    )
    names = "".join(f'<option value="{escape(name)}"></option>' for name in loggers)
    return (
        # Relative, without a leading slash: the box lives in the page at `/dashboard`, and a
        # proxy may serve the process under a path prefix.
        '<form id="logs-level-form" hx-preserve="true" hx-post="dashboard/logs/level" '
        'hx-ext="json-enc" hx-target="#logs-level-answer">'
        '<label>Logger <input name="logger" list="logs-logger-names" required '
        'placeholder="root"></label> '
        f'<label>Level <select name="level">{options}</select></label> '
        '<button type="submit">Set level</button>'
        '<output id="logs-level-answer"></output>'
        "</form>"
        f'<datalist id="logs-logger-names">{names}</datalist>'
    )


def _loggers_html(loggers: List[str]) -> str:
    """Each logger seen so far, with its own level and the level in force."""
    rows = []
    for name in loggers:
        found = _logger_named(name)
        own = logging.getLevelName(found.level) if found is not None else None
        in_force = logging.getLevelName(found.getEffectiveLevel()) if found else None
        rows.append(
            f"<tr><td>{escape(name)}</td><td>{escape(own)}</td><td>{escape(in_force)}</td></tr>"
        )
    return (
        '<div class="logs-loggers"><table>'
        "<tr><th>Logger</th><th>Its own level</th><th>Level in force</th></tr>"
        f"{''.join(rows)}"
        "</table></div>"
    )


def _filter_html() -> str:
    """The radios of the view filter: show the records at or above one level."""
    radios = "".join(
        f'<input type="radio" name="logs-show" id="logs-show-{name.lower()}" '
        f'value="{name}"{" checked" if level == logging.DEBUG else ""}>'
        f'<label for="logs-show-{name.lower()}">{name}</label>'
        for level in _FILTER_LEVELS
        for name in (logging.getLevelName(level),)
    )
    return (
        '<fieldset id="logs-filter" hx-preserve="true">'
        f"<legend>Show the records at or above</legend>{radios}"
        "</fieldset>"
    )


def _message_html(record: _Record) -> str:
    """The text of a record's message cell.

    A message of one line is shown as it is. A message of several lines shows its first line,
    the text before the first line break, in the row, and the rest, every later line unchanged,
    in an expander that is closed at first. Opened, the rest keeps its line breaks and its
    indentation, as text. The expander has an id of its own, from the record's sequence number,
    and asks htmx to preserve it: the box polls and is swapped in again, and an expander that the
    reader opened stays open, because a record never changes once it is buffered. A message that
    only ends in a line break has nothing to expand.
    """
    first, _, rest = record.message.partition("\n")
    if not rest:
        return escape(record.message)
    return (
        f'<details id="logs-record-{record.seq}" hx-preserve="true">'
        f"<summary>{escape(first)}</summary><pre>{escape(rest)}</pre></details>"
    )


def _records_html(records: List[_Record]) -> str:
    """The records, newest first, each row classed by the filter level it is shown at."""
    rows = "".join(
        f'<tr class="lvl-{_bucket(r.levelno)}">'
        f'<td title="{escape(_date_time(r.created))}">{escape(_time(r.created))}</td>'
        f"<td>{escape(r.levelname)}</td>"
        f"<td>{escape(r.logger)}</td>"
        f"<td>{_message_html(r)}</td>"
        "</tr>"
        for r in reversed(records)
    )
    return (
        '<table class="logs-records">'
        "<tr><th>Time</th><th>Level</th><th>Logger</th><th>Message</th></tr>"
        f"{rows}"
        "</table>"
    )


def _render(handler: _Handler) -> str:
    """The box, from the handler's memory and the loggers' levels. It causes no traffic."""
    records = handler.records()
    root = logging.getLogger().name
    loggers = [root] + sorted(name for name in handler.loggers() if name != root)
    detached = (
        hint(
            "The process's logging was configured again after the dashboard was enabled, "
            "and that removed the handler of this box. No new record reaches it."
        )
        if handler not in logging.getLogger().handlers
        else ""
    )
    return (
        f"<style>{_CSS}</style>"
        '<div class="logs-view">'
        + hint(
            "A logger sends a record only at or above the level in force for it. "
            "Set a lower level to see more. The level holds until the process ends or "
            "the level is set again."
        )
        + _form_html(loggers)
        + _loggers_html(loggers)
        + hint(
            f"The newest {len(records)} records of every logger in this process, newest "
            f"first. The box keeps up to {CAPACITY}."
        )
        + detached
        + _filter_html()
        + _records_html(records)
        + "</div>"
    )


def _answer_html(target: logging.Logger) -> str:
    """The form's answer: the level now in force for the logger."""
    level = logging.getLevelName(target.getEffectiveLevel())
    return f"Level in force for {escape(target.name)}: {escape(level)}"


def _refusal_html(reason: str) -> str:
    """The form's answer when the route refuses: the reason, in the page's error style."""
    return f'<div class="error-box"><p>Not set.</p><pre>{escape(reason)}</pre></div>'


def register(registry: DashboardRegistry, router: APIRouter) -> None:
    """Attach the process's handler, register the Logs box, and mount the level route."""
    handler = _process_handler()

    @registry.box(id=BOX_ID, title=TAB, tab=TAB, refresh_seconds=5)
    def logs_box(middleware: Any) -> str:
        return _render(handler)

    @router.post("/dashboard/logs/level")
    async def set_logger_level(
        request: Request,
        logger: Annotated[str, Body()],
        level: Annotated[str, Body()],
    ) -> Response:
        """Set one logger's level in this process, and answer the level now in force.

        The body is `{"logger": "<name>", "level": "<LEVEL>"}`. The level is one of NOTSET,
        DEBUG, INFO, WARNING, ERROR and CRITICAL; NOTSET hands the logger its parent's level
        again. The root logger is named `root`. A name the process has no logger for yet
        gets one, so a level can be set before the logger first writes.

        The answer is `{"logger": "<name>", "level": "<LEVEL in force>"}`, and 422 with the
        reason for a level that is not one of those names or an empty logger name. The
        dashboard's own form posts with htmx, which shows only a 2xx answer, so it gets its
        answer, and a refusal, as 200 with an HTML fragment.
        """
        if registry.get(BOX_ID) is None:
            raise HTTPException(status_code=404, detail=f"Unknown box: {BOX_ID}")
        from_the_form = request.headers.get("HX-Request") == "true"
        try:
            target = _set_level(handler, logger, level)
        except ValueError as refusal:
            if from_the_form:
                return HTMLResponse(_refusal_html(str(refusal)))
            raise HTTPException(status_code=422, detail=str(refusal)) from None
        if from_the_form:
            return HTMLResponse(_answer_html(target))
        return JSONResponse(
            {
                "logger": target.name,
                "level": logging.getLevelName(target.getEffectiveLevel()),
            }
        )
