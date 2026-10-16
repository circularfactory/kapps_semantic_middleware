"""What the store's validation report says, read as RDF, and each refused write shown readably.

When the store's shapes refuse a write, the triplestore interface raises `ShaclValidationError`,
and its `report` is the store's validation report, verbatim: N-Quads, as the store sends it. The
refusal is the error's type. This module only reads what the report says, as RDF, never as text:
the store puts the source shape's own description into the report, path included, so every name
can appear in the text while no result names it.

It reads a report three ways:

- `readable_results`, for a person: each result's message, severity, focus node, path, value,
  source shape and source constraint component, each a term, short and full.
- `validation_results`, for a caller that compares: each result's focus node, path and source
  shape as full IRIs, the path in SPARQL property path syntax.
- `rule_names`, for a log line: each source shape at its focus node, by local name.

A text that is no RDF names no result. A caller still carries it verbatim.

A write of a middleware instance that the store's shapes refuse is a `RefusedWrite`: the time,
the verb that wrote (`commit`, `create` or `handover`), the rules that the report names, and the
report. `render_refused_write` shows one as HTML, for a person: its first line visible, and the
results and the whole report behind an expander.

The middleware keeps the newest refused writes of its instance in memory, and logs each one as one
record at INFO on this module's own logger, `kapps_semantic_middleware.validation_report`. The
first line of the record says what was refused and by which rules; the following lines hold the
readable results and the whole report.
"""

from __future__ import annotations

import collections
import functools
import hashlib
import logging
import re
import threading
import typing
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Iterator, NamedTuple, Optional

from kapps_triplestore_interface.exceptions import ShaclValidationError
from rdflib import Dataset, Literal, URIRef
from rdflib.exceptions import ParserError
from rdflib.graph import Graph
from rdflib.namespace import RDF, SH
from rdflib.term import Node

from kapps_semantic_middleware.dashboard import escape

logger = logging.getLogger(__name__)

__all__ = [
    "ReadableResult",
    "RefusedWrite",
    "Term",
    "ValidationResult",
    "readable_results",
    "render_refused_write",
    "rule_names",
    "validation_results",
]


class ValidationResult(NamedTuple):
    """One `sh:result` of a validation report: the focus node, the path, and the source shape.

    `path` is written in SPARQL property path syntax, with every sequence in parentheses, and is
    empty for a result without one, such as that of a shape that is an `sh:or`. A focus node or a
    source shape that the result does not name is empty too. All IRIs are full.
    """

    focus: str
    path: str
    shape: str


def validation_results(report: str) -> set[ValidationResult]:
    """Every validation result of `report`, read as RDF.

    A statement of the report may name a graph; the results are read across all of them. A text
    that is no RDF has no result.
    """
    graph = _parsed(report)
    if graph is None:
        return set()
    return {
        ValidationResult(
            focus=_text(graph.value(result, SH.focusNode)),
            path=_property_path(graph, graph.value(result, SH.resultPath)),
            shape=_text(graph.value(result, SH.sourceShape)),
        )
        for result in graph.objects(None, SH.result)
    }


class Term(NamedTuple):
    """One term of a result for a person to read: `short`, and `full` beside it, as for a tooltip.

    An IRI is short by its local name, the part after its last `#` or `/`, and full as it is. A
    path is in SPARQL property path syntax both ways, each IRI by its local name in one and whole
    in the other. A literal is short by its lexical form, and full in N-Triples syntax, with its
    datatype or its language. A blank node is short as `blank node`. The message of a result that
    gives several holds each of them on a line of its own, in both forms.

    The short form is for display only: it is never compared, and never turned back into an IRI.
    """

    short: str
    full: str


class ReadableResult(NamedTuple):
    """One `sh:result` of a validation report, all that a person reads of it: the message
    (`sh:resultMessage`), the severity, the focus node, the path, the value (`sh:value`), the
    source shape, and the source constraint component.

    A term the result does not give is None, such as the path of a shape that is an `sh:or`, or
    the message of a result without one.
    """

    message: Optional[Term]
    severity: Optional[Term]
    focus: Optional[Term]
    path: Optional[Term]
    value: Optional[Term]
    shape: Optional[Term]
    constraint: Optional[Term]


def readable_results(report: str) -> Optional[list[ReadableResult]]:
    """Every result of `report` as a person reads it, read as RDF, in the order of their source
    shapes, then their focus nodes. None when `report` is no RDF, which a caller then shows
    verbatim."""
    graph = _parsed(report)
    if graph is None:
        return None
    return sorted(
        (_readable_result(graph, result) for result in graph.objects(None, SH.result)),
        key=lambda result: tuple(
            "" if term is None else term.full
            for term in (result.shape, result.focus, result.path, result.value)
        ),
    )


def rule_names(report: str) -> list[str]:
    """The rules `report` names, for a person to read: each source shape at its focus node, by
    local name, such as `PossessionShape at Module2`, in order. The local names are for display
    only: a caller that compares reads `validation_results`."""
    return sorted(
        f"{_local_name(result.shape)} at {_local_name(result.focus)}"
        for result in validation_results(report)
    )


#: The calls whose refused writes an instance records.
_Verb = typing.Literal["commit", "create", "handover"]


class RefusedWrite(NamedTuple):
    """A write of a middleware instance that the store's shapes refused.

    `time` is when the refusal reached the instance, with its time zone. `verb` is the call that
    wrote: `commit`, `create` or `handover`. `rules` are the rules that `report` names, as
    `rule_names` gives them, and `report` is the store's validation report, verbatim.
    """

    time: datetime
    verb: typing.Literal["commit", "create", "handover"]
    rules: tuple[str, ...]
    report: str


class _RefusedErrorTransition(RefusedWrite):
    """A `RefusedWrite` of the error transition of a `commit`.

    It adds no field and keeps the text form of `RefusedWrite`; it equals a plain `RefusedWrite`
    with the same fields, as tuples do. The marker is for the first line only: see `_first_line`.
    A subclass, and no field or verb value, because the public record type must keep its fields
    and their values (KSM 463).
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return (
            f"RefusedWrite(time={self.time!r}, verb={self.verb!r}, "
            f"rules={self.rules!r}, report={self.report!r})"
        )


#: The fields of a result, in order: the name a person reads, and the attribute that holds it.
_FIELDS = (
    ("Message", "message"),
    ("Severity", "severity"),
    ("Focus node", "focus"),
    ("Path", "path"),
    ("Value", "value"),
    ("Source shape", "shape"),
    ("Constraint", "constraint"),
)


def _shown_fields(result: ReadableResult) -> Iterator[tuple[str, list[str], Optional[str]]]:
    """Each field of `result` as a person reads it: its name, the lines of its short form, and
    its full form for a tooltip. A field that the result does not give has no line and no full
    form."""
    for name, attribute in _FIELDS:
        term = getattr(result, attribute)
        if term is None:
            yield name, [], None
        else:
            yield name, term.short.split("\n"), term.full


def render_refused_write(refused: RefusedWrite) -> str:
    """One refused write as HTML, for a person to read.

    Its first line is always visible: the time, the verb, and the rules, each source shape at its
    focus node, by local name. An expander holds a table of the results, one block of rows for
    each, and the whole report, verbatim, with its line breaks. Each term of the table shows its
    local name, with the full term as its tooltip. Every value is escaped, so a value that holds
    markup is shown as text.

    The expander has an id of its own, the same for the same refused write, and asks htmx to
    preserve it: a box that polls swaps its fragment in again, and an expander that the reader
    opened stays open, because a refused write never changes once it is recorded.
    """
    shown_at = refused.time.astimezone()
    summary = (
        f'<summary><time datetime="{escape(refused.time.isoformat())}" '
        f'title="{escape(shown_at.isoformat(sep=" ", timespec="seconds"))}">'
        f"{escape(shown_at.strftime('%H:%M:%S'))}</time> "
        f"{escape(_first_line(refused))}</summary>"
    )
    results = _readable_once(refused.report)
    if results is None:
        table = "<p>The report is no RDF, so it names no result. It is shown verbatim below.</p>"
    elif not results:
        table = "<p>The report names no result.</p>"
    else:
        table = (
            '<div class="table-scroll"><table><tbody>'
            + "".join(_result_rows(n, len(results), r) for n, r in enumerate(results, 1))
            + "</tbody></table></div>"
        )
    report = (
        "<p>The store's validation report, verbatim:</p>"
        '<pre style="white-space: pre-wrap; overflow-wrap: anywhere; margin: 0;">'
        f"{escape(refused.report)}</pre>"
    )
    return (
        f'<details class="refused-write" id="{_element_id(refused)}" hx-preserve="true">'
        f"{summary}{table}{report}</details>"
    )


class _RefusedWrites:
    """The newest refused writes of one middleware instance, in memory, and their log records.

    The middleware tells it of each refusal from the thread that wrote, which may be a worker
    thread, and the dashboard reads it from another, so both take one lock.
    """

    #: How many refused writes an instance keeps. An older one drops.
    CAPACITY = 50

    def __init__(self) -> None:
        self._kept: collections.deque[RefusedWrite] = collections.deque(maxlen=self.CAPACITY)
        self._lock = threading.Lock()

    def record(
        self, verb: _Verb, refusal: ShaclValidationError, *, error_transition: bool = False
    ) -> None:
        """Keep `refusal` of a write by `verb`, and log it as one record at INFO. The write of
        an error transition is kept as a `_RefusedErrorTransition`."""
        report = refusal.report or ""
        kind = _RefusedErrorTransition if error_transition else RefusedWrite
        refused = kind(
            time=datetime.now(timezone.utc),
            verb=verb,
            rules=tuple(rule_names(report)),
            report=report,
        )
        with self._lock:
            self._kept.append(refused)
        logger.info("%s", _log_text(refused))

    def newest_first(self) -> list[RefusedWrite]:
        with self._lock:
            return list(reversed(self._kept))


def _log_text(refused: RefusedWrite) -> str:
    """The log record of `refused`: its first line, then its readable results, then the whole
    report, verbatim."""
    lines = [_first_line(refused)]
    results = _readable_once(refused.report)
    if results is None:
        lines.append("The report is no RDF, so it names no result.")
    else:
        for number, result in enumerate(results, 1):
            lines.append(f"Result {number} of {len(results)}")
            for name, shown, _ in _shown_fields(result):
                text = "\n    ".join(shown) if shown else "—"
                lines.append(f"  {name}: {text}")
    lines.append("The store's validation report, verbatim:")
    lines.append(refused.report.rstrip("\n"))
    return "\n".join(lines)


def _first_line(refused: RefusedWrite) -> str:
    """What was refused, and by which rules: the first line of the page and of the log record."""
    what: str = refused.verb
    if isinstance(refused, _RefusedErrorTransition):
        what += " (error transition)"
    if refused.rules:
        return f"{what} refused by {', '.join(refused.rules)}"
    return f"{what} refused by the store's shapes; the report names no rule"


@functools.lru_cache(maxsize=128)
def _readable_once(report: str) -> Optional[tuple[ReadableResult, ...]]:
    """`readable_results`, read once for each report: a box that polls renders it again and
    again, and a refused write never changes."""
    results = readable_results(report)
    return None if results is None else tuple(results)


def _result_rows(number: int, count: int, result: ReadableResult) -> str:
    """The rows of one result in the table: a heading row, then one row for each field."""
    rows = [f'<tr><th colspan="2">Result {number} of {count}</th></tr>']
    for name, lines, full in _shown_fields(result):
        cell = "<br>".join(escape(line) for line in lines) if lines else escape(None)
        if full is not None:
            cell = f'<span title="{escape(full)}">{cell}</span>'
        rows.append(f"<tr><th>{escape(name)}</th><td>{cell}</td></tr>")
    return "".join(rows)


def _element_id(refused: RefusedWrite) -> str:
    """An HTML id of `refused`, the same for the same refused write and unique among others."""
    key = "\n".join((refused.time.isoformat(), refused.verb, refused.report))
    return "refused-write-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _parsed(report: str) -> Optional[Dataset]:
    """`report` as RDF, every graph of it read as one; None when it is no RDF."""
    graph = Dataset(default_union=True)
    try:
        graph.parse(data=report, format="nquads")
    except ParserError:
        return None
    return graph


def _readable_result(graph: Graph, result: Node) -> ReadableResult:
    path = graph.value(result, SH.resultPath)
    return ReadableResult(
        message=_messages(graph.objects(result, SH.resultMessage)),
        severity=_term(graph.value(result, SH.resultSeverity)),
        focus=_term(graph.value(result, SH.focusNode)),
        path=None
        if path is None
        else Term(
            short=_property_path(graph, path, _local_name),
            full=_property_path(graph, path),
        ),
        value=_term(graph.value(result, SH.value)),
        shape=_term(graph.value(result, SH.sourceShape)),
        constraint=_term(graph.value(result, SH.sourceConstraintComponent)),
    )


def _messages(nodes: typing.Iterable[Node]) -> Optional[Term]:
    """The messages of a result as one term, each on a line of its own, in the order of their
    text; None when it gives none."""
    terms = sorted({term for node in nodes if (term := _term(node)) is not None})
    if not terms:
        return None
    return Term(
        short="\n".join(term.short for term in terms),
        full="\n".join(term.full for term in terms),
    )


def _term(node: Optional[Node]) -> Optional[Term]:
    if node is None:
        return None
    if isinstance(node, URIRef):
        return Term(short=_local_name(node), full=str(node))
    if isinstance(node, Literal):
        return Term(short=str(node), full=node.n3())
    return Term(short="blank node", full=node.n3())


def _local_name(iri: str) -> str:
    """The part of `iri` after its last `#` or `/`, or `iri` itself if that is empty.

    For a person to read only: a local name is never compared, and never turned back into an IRI.
    """
    return re.split(r"[#/]", str(iri))[-1] or str(iri)


def _text(node: Optional[Node]) -> str:
    return "" if node is None else str(node)


def _whole(iri: str) -> str:
    return f"<{iri}>"


def _property_path(
    graph: Graph, node: Optional[Node], name: Callable[[str], str] = _whole
) -> str:
    """The SHACL path at `node`, in SPARQL property path syntax, each IRI written by `name`:
    whole, by default. An IRI, a sequence or an inverse.

    Any other kind of path comes back as the node itself, so that it matches no path a caller
    expects.
    """
    if node is None:
        return ""
    if isinstance(node, URIRef):
        return name(node)
    inverse = graph.value(node, SH.inversePath)
    if inverse is not None:
        return f"^{_property_path(graph, inverse, name)}"
    if graph.value(node, RDF.first) is not None:
        steps = (_property_path(graph, step, name) for step in graph.items(node))
        return f"({'/'.join(steps)})"
    return node.n3()
