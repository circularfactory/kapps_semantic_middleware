"""Every write that the store's shapes refuse is recorded, in memory, and shown readably.

The OGM is faked at its public `commit(changes=...)` call, which is the seam that `mw.commit()`
and `mw.create()` write through: it raises the interface's `ShaclValidationError` with a report
that the store sent, as the store does. The middleware keeps the refusal, logs it once, and the
dashboard's box "Refused writes" shows it. The live counterpart, with a refused handover and both
pages read from a running instance, is `test_refused_writes_integration.py`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from _refusal_view import BrowserView
from fastapi.testclient import TestClient
from kapps_ogm import Update
from kapps_ogm.utils.class_scope import ClassScope
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import (
    TripleStoreResponseError,
    PreconditionFailedError,
    ShaclValidationError,
)

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.dashboard import enable_dashboard
from kapps_semantic_middleware.validation_report import (
    RefusedWrite,
    render_refused_write,
)
from kapps_semantic_middleware.validation_report import _first_line as _first_line_of

EX = "https://example.org/test#"
OP = IRI(f"{EX}op_1")
STATUS = IRI(f"{EX}status")
SNAPSHOT = IRI(f"{EX}DecisionSnapshot")
DECISION = IRI(f"{EX}decision")

#: A report as the store sent it: a handover of Box2 onto Module3, refused while Module3 holds
#: another box.
REPORT = (Path(__file__).parent / "reports" / "skip-reserve.nq").read_text()
RULE = "FlexConveyorModulePossessionShape at Module3"


class _RefusingOGM:
    """Admits every commit until `refusal` is set, and then raises it for every commit."""

    def __init__(self) -> None:
        self.refusal: Exception | None = None
        self.written: list[list] = []

    def commit(self, *, changes, named_graph=None):
        if self.refusal is not None:
            raise self.refusal
        self.written.append(list(changes))
        return []

    def fetch(self, *, instance_iri, **kwargs):
        return instance_iri


def _middleware(ogm, **options) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=f"{EX}module",
        service_class=f"{EX}ModuleService",
        ogm=ogm,
        heartbeat_interval=None,
        **options,
    )


def _status(value: str) -> list:
    return [Update(instance_iri=OP, data={STATUS: [value]})]


def _refusal(report: str = REPORT) -> ShaclValidationError:
    return ShaclValidationError(f"Error while querying the store (400) - {report}", report=report)


def _commit(mw: SemanticMiddleware) -> None:
    with mw.commit(OP, entry=_status("running")):
        pass


def _create(mw: SemanticMiddleware) -> None:
    with mw.create(SNAPSHOT, ClassScope()) as snapshot:
        snapshot.update({DECISION: ["unscrew"]})


# -- The record --------------------------------------------------------------------------------- #


@pytest.mark.parametrize(("write", "verb"), [(_commit, "commit"), (_create, "create")])
def test_a_refused_write_is_recorded_and_its_error_reaches_the_caller_unchanged(write, verb):
    ogm = _RefusingOGM()
    mw = _middleware(ogm)
    ogm.refusal = refusal = _refusal()
    before = datetime.now(timezone.utc)

    with pytest.raises(ShaclValidationError) as raised:
        write(mw)

    assert raised.value is refusal
    assert raised.value.report == REPORT
    assert getattr(raised.value, "__notes__", []) == []
    (recorded,) = mw.refused_writes
    assert recorded == RefusedWrite(recorded.time, verb, (RULE,), REPORT)
    assert before <= recorded.time <= datetime.now(timezone.utc)


def test_a_commit_that_names_the_handover_records_its_refusal_as_a_handover():
    """The possession extension's handover commits with ``verb="handover"``."""
    ogm = _RefusingOGM()
    mw = _middleware(ogm)
    ogm.refusal = _refusal()

    with pytest.raises(ShaclValidationError):
        with mw.commit(OP, entry=_status("running"), verb="handover"):
            pytest.fail("the body of a refused commit ran")

    assert [refused.verb for refused in mw.refused_writes] == ["handover"]


@pytest.mark.parametrize("verb", ["create", "hand_over", "Handover", "", None])
def test_a_commit_refuses_any_other_verb_before_anything_is_written(verb):
    """``create`` is the verb of ``mw.create()``, not one that a commit may claim."""
    ogm = _RefusingOGM()
    mw = _middleware(ogm)

    with pytest.raises(ValueError, match="verb 'commit' or 'handover'"):
        mw.commit(OP, entry=_status("running"), verb=verb)

    assert ogm.written == []
    assert mw.refused_writes == []


def test_a_creation_that_joins_a_refused_commit_is_recorded_once_as_that_commit():
    """The creation's changes join the commit's exit, so the store refuses one write: the commit."""
    ogm = _RefusingOGM()
    mw = _middleware(ogm)

    with pytest.raises(ShaclValidationError):
        with mw.commit(OP, entry=_status("running")):
            ogm.refusal = _refusal()
            _create(mw)

    assert [refused.verb for refused in mw.refused_writes] == ["commit"]


def test_a_refused_exit_and_its_refused_error_transition_are_both_recorded_in_that_order():
    """The caller gets the refusal of the exit, and the refusal of the error transition is noted
    on it. Both are refused writes of the instance, and both are recorded."""
    ogm = _RefusingOGM()
    mw = _middleware(ogm)

    with pytest.raises(ShaclValidationError) as raised:
        with mw.commit(OP, entry=_status("running"), exit=_status("done"), error=_status("failed")):
            ogm.refusal = _refusal()

    assert len(raised.value.__notes__) == 1
    assert [_first_line_of(each) for each in reversed(mw.refused_writes)] == [
        f"commit refused by {RULE}",
        f"commit (error transition) refused by {RULE}",
    ]
    assert [each.verb for each in mw.refused_writes] == ["commit", "commit"]


def _fail_the_body(mw: SemanticMiddleware, ogm: _RefusingOGM, refusal) -> RuntimeError:
    """A commit whose body fails once the store refuses every further write."""
    failure = RuntimeError("the body failed")
    with pytest.raises(RuntimeError) as raised:
        with mw.commit(OP, entry=_status("running"), error=_status("failed")):
            ogm.refusal = refusal
            raise failure
    assert raised.value is failure
    return failure


def test_a_refused_error_transition_is_recorded_as_a_plain_refused_write():
    ogm = _RefusingOGM()
    mw = _middleware(ogm)

    failure = _fail_the_body(mw, ogm, _refusal())

    (recorded,) = mw.refused_writes
    assert type(recorded) is not RefusedWrite and isinstance(recorded, RefusedWrite)
    assert recorded.verb == "commit"
    assert recorded.rules == (RULE,)
    assert recorded.report == REPORT
    assert repr(recorded).startswith("RefusedWrite(")
    assert recorded == RefusedWrite(recorded.time, "commit", (RULE,), REPORT)
    assert not hasattr(recorded, "__dict__")
    assert _first_line_of(recorded) == f"commit (error transition) refused by {RULE}"
    assert failure.__notes__ == [f"The error transition was not committed: {ogm.refusal!r}"]


def test_a_refused_error_transition_whose_report_names_no_rule_says_so():
    ogm = _RefusingOGM()
    mw = _middleware(ogm)

    _fail_the_body(mw, ogm, _refusal("not a report"))

    (recorded,) = mw.refused_writes
    assert _first_line_of(recorded) == (
        "commit (error transition) refused by the store's shapes; the report names no rule"
    )


def test_a_refused_error_transition_keeps_the_error_log_line_and_adds_one_info_record(
    caplog: pytest.LogCaptureFixture,
):
    ogm = _RefusingOGM()
    mw = _middleware(ogm, activity_feed=True)
    assert mw.activity_feed is not None

    with caplog.at_level(logging.INFO, logger="kapps_semantic_middleware"):
        _fail_the_body(mw, ogm, _refusal())

    (errored,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errored.getMessage().startswith("The error transition was not committed")
    (info,) = [r for r in caplog.records if "refused" in r.getMessage()]
    assert info.levelno == logging.INFO
    assert info.getMessage().split("\n")[0] == f"commit (error transition) refused by {RULE}"
    (streamed,) = [r for r in mw.activity_feed.snapshot() if "refused" in r.message]
    assert streamed.message.split("\n")[0] == f"commit (error transition) refused by {RULE}"


def test_a_refused_error_transition_counts_toward_the_newest_50():
    ogm = _RefusingOGM()
    mw = _middleware(ogm)
    for n in range(51):
        ogm.refusal = None
        _fail_the_body(mw, ogm, _refusal(f"report {n}"))

    assert [refused.report for refused in mw.refused_writes] == [
        f"report {n}" for n in range(50, 0, -1)
    ]


def test_the_box_shows_a_refused_error_transition_and_a_refused_commit_each_as_such():
    ogm = _RefusingOGM()
    mw = _middleware(ogm)
    enable_dashboard(mw)
    _fail_the_body(mw, ogm, _refusal())
    ogm.refusal = _refusal()
    with pytest.raises(ShaclValidationError):
        _commit(mw)

    box = _box(mw)

    assert [shown.summary.split(" ", 1)[1] for shown in box.refused] == [
        f"commit refused by {RULE}",
        f"commit (error transition) refused by {RULE}",
    ]
    assert render_refused_write(mw.refused_writes[1]).count("commit (error transition) refused by") == 1


@pytest.mark.parametrize(
    "error",
    [
        PreconditionFailedError("the store holds the instance already"),
        TripleStoreResponseError("Error while querying the store (500) - out of memory"),
    ],
    ids=["a failed precondition", "another error of the store"],
)
def test_an_error_that_is_no_refusal_by_the_shapes_is_not_recorded(error):
    ogm = _RefusingOGM()
    mw = _middleware(ogm)
    ogm.refusal = error

    with pytest.raises(type(error)):
        _commit(mw)

    assert mw.refused_writes == []


def test_the_memory_keeps_the_newest_50_refusals_newest_first():
    ogm = _RefusingOGM()
    mw = _middleware(ogm)
    for n in range(51):
        ogm.refusal = _refusal(f"report {n}")
        with pytest.raises(ShaclValidationError):
            _commit(mw)

    assert [refused.report for refused in mw.refused_writes] == [
        f"report {n}" for n in range(50, 0, -1)
    ]


def test_a_refusal_is_one_info_record_on_its_own_logger(caplog: pytest.LogCaptureFixture):
    ogm = _RefusingOGM()
    mw = _middleware(ogm)
    ogm.refusal = _refusal()

    with caplog.at_level(logging.INFO, logger="kapps_semantic_middleware"):
        with pytest.raises(ShaclValidationError):
            _commit(mw)

    (record,) = [r for r in caplog.records if "refused" in r.getMessage()]
    assert record.levelno == logging.INFO
    assert record.name == "kapps_semantic_middleware.validation_report"
    first, *rest = record.getMessage().split("\n")
    assert first == f"commit refused by {RULE}"
    following = "\n".join(rest)
    for readable in (
        "Result 1 of 1",
        "A FlexConveyorModule may possess at most one Box at a time.",
        "Severity: Violation",
        "Focus node: Module3",
        "Path: (hasPossessor/^hasPossessedWorkpiece)",
        "Source shape: FlexConveyorModulePossessionShape",
        "Constraint: MaxCountConstraintComponent",
    ):
        assert readable in following, following
    assert following.endswith(REPORT.rstrip("\n")), following


def test_the_activity_feed_holds_the_refusal_as_one_info_record():
    ogm = _RefusingOGM()
    mw = _middleware(ogm, activity_feed=True)
    assert mw.activity_feed is not None
    ogm.refusal = _refusal()

    with pytest.raises(ShaclValidationError):
        _commit(mw)

    (record,) = [r for r in mw.activity_feed.snapshot() if "refused" in r.message]
    assert record.level == "INFO"
    assert record.message.split("\n")[0] == f"commit refused by {RULE}"
    assert REPORT.rstrip("\n") in record.message


# -- The box "Refused writes" ------------------------------------------------------------------- #


def _box(mw: SemanticMiddleware) -> BrowserView:
    """The box as a browser loads it: the page names its fragment, and the browser fetches that."""
    client = TestClient(mw.app)
    page = client.get("/dashboard").text
    slot = re.search(r'<div id="box-refused-writes" hx-get="([^"]+)" hx-trigger="([^"]+)"', page)
    assert slot is not None, "the page has no box refused-writes"
    assert slot.group(2) == "load, every 5s"
    tab = re.search(r'id="panel-([^"]+)"[^>]*>(?:(?!</section>).)*box-refused-writes', page, re.S)
    assert tab is not None and tab.group(1) == "system"
    fragment = client.get(f"/{slot.group(1)}")
    assert fragment.status_code == 200
    return BrowserView(fragment.text)


def test_the_box_shows_each_refused_write_newest_first():
    ogm = _RefusingOGM()
    mw = _middleware(ogm)
    enable_dashboard(mw)
    ogm.refusal = _refusal()
    with pytest.raises(ShaclValidationError):
        _commit(mw)
    with pytest.raises(ShaclValidationError):
        _create(mw)

    box = _box(mw)

    assert "Refused writes" in box.text
    assert [shown.summary.split(" ", 1)[1] for shown in box.refused] == [
        f"create refused by {RULE}",
        f"commit refused by {RULE}",
    ]
    for shown in box.refused:
        assert shown.results[0]["Focus node"].text == "Module3"
        assert shown.report == REPORT


def test_the_box_says_so_when_no_write_was_refused():
    mw = _middleware(_RefusingOGM())
    enable_dashboard(mw)

    box = _box(mw)

    assert box.refused == []
    assert "No write of this instance was refused" in box.text


def test_the_box_renders_from_memory_alone():
    """Rendering the box reads no store: the OGM is not called, whatever the box shows."""
    ogm = _RefusingOGM()
    mw = _middleware(ogm)
    enable_dashboard(mw)
    ogm.refusal = _refusal()
    with pytest.raises(ShaclValidationError):
        _commit(mw)
    ogm.refusal = AssertionError("the box called the store")
    ogm.fetch = None  # type: ignore[assignment,method-assign]

    assert len(_box(mw).refused) == 1

