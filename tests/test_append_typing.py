# mypy: warn-unused-ignores
"""The change type of `mw.commit()` accepts kapps_ogm's `Append`, checked by mypy.

The CI's mypy run checks this module (it is named in `[tool.mypy] files`). Each call below
passes an `Append` where `commit(...)` takes a change, and calls the two body verbs, so mypy
reports an error here if the change type narrows again. The negative control passes a value that
is not a change and is marked as an expected `arg-type` error: with unused-ignore warnings on for
this module, the run fails if mypy stops reading kapps_ogm's real types and reads `Any` instead.

pytest runs the module too: the calls run against a stand-in OGM, so they also show that each
form is accepted at run time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from kapps_ogm import Append, Create, Update
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware import Mode, SemanticMiddleware
from kapps_semantic_middleware.commit import Change
from kapps_semantic_middleware.middleware import IssuedOperation

EX = "http://example.org/test#"
RECORD = IRI(f"{EX}record_1")
LOG = IRI(f"{EX}log")
OPERATION_CLASS = IRI(f"{EX}Drilling")
CAPABILITY_CLASS = IRI(f"{EX}DrillingCapability")


class _RecordingOGM:
    def __init__(self) -> None:
        self.calls: list[list[Change]] = []

    def commit(self, *, changes: Sequence[Change], named_graph: object = None) -> list[object]:
        self.calls.append(list(changes))
        return []


def _middleware(ogm: _RecordingOGM) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode=Mode.RESOURCE,
        resource_iri=f"{EX}resource",
        service_class=f"{EX}Service",
        ogm=ogm,
        heartbeat_interval=None,
    )


def _append(value: str) -> Append:
    return Append(instance_iri=RECORD, property_iri=LOG, value=value)


def _exit() -> list[Append]:
    return [_append("exit")]


def _error(exc: BaseException) -> list[Change]:
    return [_append(f"failed: {exc!r}")]


def test_an_append_is_a_change_of_each_transition() -> None:
    ogm = _RecordingOGM()
    mw = _middleware(ogm)
    entry: list[Change] = [_append("entry"), Update(instance_iri=RECORD, data={})]
    with mw.commit(entry=entry, exit=[_append("exit")], error=[_append("error")]):
        pass
    with mw.commit(entry=[_append("entry")], exit=_exit, error=_error) as transition:
        transition.update(str(RECORD), {str(LOG): ["replaced"]})
        transition.append(str(RECORD), str(LOG), "added")
    assert len(ogm.calls) == 4  # each transition's entry and exit


def _a_create_is_a_change() -> Sequence[Change]:
    return [Create(class_iri=IRI(f"{EX}Record"), instance_iri=RECORD, data={})]


def _issue_with_append(mw: SemanticMiddleware) -> None:
    """Typed only: `issue_operation` needs discovery, which this stand-in cannot answer."""
    with mw.issue_operation(
        capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
    ) as issued:
        op: IssuedOperation = issued  # issue_operation() is not annotated: name the type here
        op.update(str(RECORD), {str(LOG): [op.iri]})
        op.append(str(RECORD), str(LOG), op.iri)


if TYPE_CHECKING:

    def _negative_control(mw: SemanticMiddleware) -> None:
        """A value that is no change: mypy must refuse it, or the ignore is unused."""
        not_changes: list[int] = [1]
        mw.commit(entry=not_changes, exit=(), error=())  # type: ignore[arg-type]
