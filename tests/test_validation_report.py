"""The reader of a validation report, and the page and the log line of a refused write.

When the store's shapes refuse a write, the triplestore interface raises `ShaclValidationError`,
and its `report` is the store's validation report, verbatim: N-Quads, as the store sends it. These
tests read reports as RDF, render one refusal as HTML, and check the record that the middleware
keeps of each refusal, without a triple store. The live counterpart, which makes a real store
refuse a write, is `test_refused_writes_integration.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from _refusal_view import BrowserView, Shown
from rdflib.namespace import RDF, SH, XSD

from kapps_semantic_middleware.validation_report import (
    ReadableResult,
    RefusedWrite,
    Term,
    ValidationResult,
    readable_results,
    render_refused_write,
    rule_names,
    validation_results,
)

FOCUS = "http://example.org/Module2"
SHAPE = "http://example.org/PossessionShape"
OTHER_SHAPE = "http://example.org/OtherShape"
HAS_POSSESSOR = "http://example.org/hasPossessor"
POSSESSES = "http://example.org/possesses"

HEAD = (
    f"_:report <{RDF.type}> <{SH.ValidationReport}> .\n"
    f'_:report <{SH.conforms}> "false"^^<{XSD.boolean}> .\n'
)


def one_result(path_triples: str = "", path: str | None = None, graph: str = "") -> str:
    """A report with one result at `FOCUS` from `SHAPE`, whose path is `path` if given."""
    in_graph = f" <{graph}>" if graph else ""
    report = HEAD + (
        f"_:report <{SH.result}> _:result .\n"
        f"_:result <{SH.focusNode}> <{FOCUS}> .\n"
        f"_:result <{SH.sourceShape}> <{SHAPE}>{in_graph} .\n"
    )
    if path is not None:
        report += f"_:result <{SH.resultPath}> {path} .\n"
    return report + path_triples


# -- What a caller compares --------------------------------------------------------------------- #


def test_a_result_names_its_focus_node_its_path_and_its_source_shape():
    report = one_result(path=f"<{HAS_POSSESSOR}>")

    assert validation_results(report) == {
        ValidationResult(focus=FOCUS, path=f"<{HAS_POSSESSOR}>", shape=SHAPE)
    }


def test_a_sequence_with_an_inverse_step_reads_as_a_sparql_property_path():
    """A possession shape's path: from the module to its state, then back to each box."""
    sequence = (
        f"_:path <{RDF.first}> <{HAS_POSSESSOR}> .\n"
        f"_:path <{RDF.rest}> _:rest .\n"
        f"_:rest <{RDF.first}> _:inverse .\n"
        f"_:rest <{RDF.rest}> <{RDF.nil}> .\n"
        f"_:inverse <{SH.inversePath}> <{POSSESSES}> .\n"
    )

    (result,) = validation_results(one_result(sequence, path="_:path"))

    assert result.path == f"(<{HAS_POSSESSOR}>/^<{POSSESSES}>)"


def test_a_result_without_a_path_has_an_empty_path():
    """A shape that is an `sh:or` reports no path."""
    (result,) = validation_results(one_result())

    assert result.path == ""


def test_a_result_that_names_no_focus_node_and_no_shape_has_them_empty():
    report = HEAD + f"_:report <{SH.result}> _:result .\n"

    assert validation_results(report) == {ValidationResult(focus="", path="", shape="")}


def test_every_result_of_a_report_is_read():
    report = one_result() + (
        f"_:report <{SH.result}> _:second .\n"
        f"_:second <{SH.focusNode}> <{FOCUS}> .\n"
        f"_:second <{SH.sourceShape}> <{OTHER_SHAPE}> .\n"
    )

    assert {result.shape for result in validation_results(report)} == {SHAPE, OTHER_SHAPE}


def test_a_report_with_statements_in_a_named_graph_is_read_whole():
    """The store sends N-Quads, so a statement may name a graph. Its blank nodes are the
    report's."""
    report = one_result(graph="http://example.org/shapes")

    assert validation_results(report) == {ValidationResult(focus=FOCUS, path="", shape=SHAPE)}


def test_a_text_that_is_no_rdf_has_no_result():
    """The refusal is the error's type, not what its report says: an unreadable report names no
    rule, and still travels verbatim."""
    assert validation_results("Unknown dataset: Tests") == set()


# -- What a log line names ---------------------------------------------------------------------- #


def test_the_rules_name_each_shape_at_its_focus_node_by_local_name_in_order():
    report = one_result() + (
        f"_:report <{SH.result}> _:second .\n"
        f"_:second <{SH.focusNode}> <http://example.org/Box1> .\n"
        f"_:second <{SH.sourceShape}> <http://example.org/shapes#BoxShape> .\n"
    )

    assert rule_names(report) == ["BoxShape at Box1", "PossessionShape at Module2"]


def test_a_text_that_is_no_rdf_names_no_rule():
    assert rule_names("Unknown dataset: Tests") == []


# -- What a person reads of each result --------------------------------------------------------- #

# Reports as the store sent them, recorded in a live run of two faults of a conveyor use case: the
# store refusing a module's handover of Box2 onto Module3 while Module3 holds another box, and a
# module's completing Box1's transport without releasing the box. The first result has a path
# that is a sequence with an inverse step; the second report has two results of `sh:or` shapes,
# which name no path.
RECORDED = Path(__file__).parent / "reports"
CORE = "https://w3id.org/circularfactory/Core#"
FC = "http://w3id.org/circularfactory/FlexConveyor#"
FCI = "http://w3id.org/circularfactory/FlexConveyorInstances#"


def recorded(fault: str) -> str:
    return (RECORDED / f"{fault}.nq").read_text()


def message(text: str) -> Term:
    """One message without a language, as a term: its text, and in full in N-Triples syntax."""
    return Term(text, f'"{text}"')


def test_a_refused_handover_reads_as_its_possession_rule_with_its_message():
    assert readable_results(recorded("skip-reserve")) == [
        ReadableResult(
            message=message("A FlexConveyorModule may possess at most one Box at a time."),
            severity=Term("Violation", str(SH.Violation)),
            focus=Term("Module3", f"{FCI}Module3"),
            path=Term(
                "(hasPossessor/^hasPossessedWorkpiece)",
                f"(<{CORE}hasPossessor>/^<{CORE}hasPossessedWorkpiece>)",
            ),
            value=None,
            shape=Term(
                "FlexConveyorModulePossessionShape", f"{FC}FlexConveyorModulePossessionShape"
            ),
            constraint=Term("MaxCountConstraintComponent", str(SH.MaxCountConstraintComponent)),
        )
    ]


def test_a_report_of_two_or_shapes_reads_as_both_results_without_a_path():
    """Each rule is an `sh:or`, so it names no path, and the box is the value."""
    box = Term("Box1", f"{FCI}Box1")
    violation = Term("Violation", str(SH.Violation))
    either = Term("OrConstraintComponent", str(SH.OrConstraintComponent))

    assert readable_results(recorded("complete-without-releasing")) == [
        ReadableResult(
            message=message(
                "A Box possessed by a FlexConveyorModule must have a "
                "BoxTransportOperation that is queued, running or failed."
            ),
            severity=violation,
            focus=box,
            path=None,
            value=box,
            shape=Term("BoxOnModuleHasTransportShape", f"{FC}BoxOnModuleHasTransportShape"),
            constraint=either,
        ),
        ReadableResult(
            message=message(
                "The Box of a done BoxTransportOperation must not be possessed by a "
                "FlexConveyorModule."
            ),
            severity=violation,
            focus=box,
            path=None,
            value=box,
            shape=Term("DoneTransportBoxReleasedShape", f"{FC}DoneTransportBoxReleasedShape"),
            constraint=either,
        ),
    ]


def test_a_literal_value_reads_as_its_lexical_form_and_in_full_with_its_datatype():
    report = one_result() + (
        f'_:result <{SH.value}> "7"^^<{XSD.integer}> .\n'
        f'_:result <{SH.resultMessage}> "Too many." .\n'
        f'_:result <{SH.resultMessage}> "Zu viele."@de .\n'
    )

    (result,) = readable_results(report)

    assert result.value == Term("7", f'"7"^^<{XSD.integer}>')
    assert result.message == Term("Too many.\nZu viele.", '"Too many."\n"Zu viele."@de')


def test_a_blank_node_value_reads_as_a_blank_node():
    report = one_result() + f"_:result <{SH.value}> _:thing .\n"

    (result,) = readable_results(report)

    assert result.value is not None
    assert result.value.short == "blank node"
    assert result.value.full.startswith("_:")


def test_a_result_that_names_nothing_reads_with_every_term_empty():
    """A message too is a term, so a result without one has None for it."""
    report = HEAD + f"_:report <{SH.result}> _:result .\n"

    assert readable_results(report) == [ReadableResult(None, None, None, None, None, None, None)]


def test_a_text_that_is_no_rdf_has_no_readable_result_and_an_empty_report_names_none():
    """None tells the caller that the text is no RDF, to show it verbatim, and why."""
    assert readable_results("Unknown dataset: Tests") is None
    assert readable_results("") == []


# -- One refused write, as a page shows it ------------------------------------------------------ #

WHEN = datetime(2026, 10, 6, 12, 3, 4, tzinfo=timezone.utc)


def shown(refused: RefusedWrite) -> Shown:
    (one,) = BrowserView(render_refused_write(refused)).refused
    return one


def test_a_refused_write_shows_its_verb_and_its_rules_on_its_first_line():
    report = one_result(path=f"<{HAS_POSSESSOR}>")
    refused = RefusedWrite(WHEN, "handover", tuple(rule_names(report)), report)

    summary = shown(refused).summary

    assert summary.endswith("handover refused by PossessionShape at Module2"), summary
    assert WHEN.astimezone().strftime("%H:%M:%S") in summary


def test_its_expander_holds_a_table_of_the_results_with_each_term_in_full_as_its_tooltip():
    refused = RefusedWrite(WHEN, "handover", (), recorded("skip-reserve"))

    (result,) = shown(refused).results

    assert {name: (cell.text, cell.title) for name, cell in result.items()} == {
        "Message": (
            "A FlexConveyorModule may possess at most one Box at a time.",
            '"A FlexConveyorModule may possess at most one Box at a time."',
        ),
        "Severity": ("Violation", str(SH.Violation)),
        "Focus node": ("Module3", f"{FCI}Module3"),
        "Path": (
            "(hasPossessor/^hasPossessedWorkpiece)",
            f"(<{CORE}hasPossessor>/^<{CORE}hasPossessedWorkpiece>)",
        ),
        "Value": ("—", None),
        "Source shape": (
            "FlexConveyorModulePossessionShape",
            f"{FC}FlexConveyorModulePossessionShape",
        ),
        "Constraint": ("MaxCountConstraintComponent", str(SH.MaxCountConstraintComponent)),
    }


def test_every_result_has_its_rows_and_several_messages_keep_a_line_each():
    report = recorded("complete-without-releasing")
    several_messages = one_result() + (
        f'_:result <{SH.resultMessage}> "Too many." .\n'
        f'_:result <{SH.resultMessage}> "Zu viele."@de .\n'
    )

    two = shown(RefusedWrite(WHEN, "commit", (), report)).results
    (several,) = shown(RefusedWrite(WHEN, "commit", (), several_messages)).results

    assert [r["Source shape"].text for r in two] == [
        "BoxOnModuleHasTransportShape",
        "DoneTransportBoxReleasedShape",
    ]
    assert [r["Path"].text for r in two] == ["—", "—"]
    assert several["Message"].text == "Too many.\nZu viele."


def test_its_expander_holds_the_whole_report_verbatim_with_its_line_breaks():
    report = recorded("skip-reserve")

    one = shown(RefusedWrite(WHEN, "handover", (), report))

    assert one.report == report


def test_values_that_hold_html_are_shown_as_text():
    hostile = "<script>alert(1)</script> & <b>bold</b>"
    report = one_result() + f'_:result <{SH.resultMessage}> "{hostile}" .\n'
    refused = RefusedWrite(WHEN, "create<i>", (f"{hostile} at Module2",), report)

    view = BrowserView(render_refused_write(refused))

    assert set(view.tags) <= {"details", "summary", "time", "div", "table", "tbody", "tr",
                              "th", "td", "span", "br", "p", "pre"}, view.tags
    (one,) = view.refused
    assert one.summary.endswith(f"create<i> refused by {hostile} at Module2")
    assert one.results[0]["Message"].text == hostile
    assert one.report == report


def test_a_report_that_is_no_rdf_has_no_table_and_is_kept_verbatim():
    one = shown(RefusedWrite(WHEN, "commit", (), "Unknown dataset: <Tests>"))

    assert one.summary.endswith("commit refused by the store's shapes; the report names no rule")
    assert one.results == []
    assert any("no RDF" in note for note in one.notes), one.notes
    assert one.report == "Unknown dataset: <Tests>"


def test_each_refused_write_keeps_its_own_id_so_an_open_expander_stays_open_on_refresh():
    """The box polls, and the page swaps its fragment in. An expander that htmx preserves by its
    id stays as the reader left it: a refused write never changes once it is recorded."""
    first = shown(RefusedWrite(WHEN, "commit", (), "a"))
    again = shown(RefusedWrite(WHEN, "commit", (), "a"))
    other = shown(RefusedWrite(WHEN, "commit", (), "b"))

    assert first.preserved
    assert first.id and first.id == again.id != other.id
