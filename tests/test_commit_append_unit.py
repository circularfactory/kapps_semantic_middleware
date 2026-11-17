"""An `Append` in `mw.commit()` and `mw.create()`, without a triple store.

kapps_ogm's `OGM.commit` takes three kinds of change: `Create`, `Update` and `Append`. An
`Append` adds one value to a property of a stored instance and replaces none. These tests look
at the calls that reach `OGM.commit`: which changes, in which order, in how many calls. The OGM
is a stand-in that records each call and keeps none of kapps_ogm's rules, so a test sees what
the middleware hands on, also a call that kapps_ogm would refuse.
"""

from __future__ import annotations

import pytest
from kapps_ogm import Append, ClassScope, Create, Update
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware import SemanticMiddleware

EX = "http://example.org/test#"
OP = IRI(f"{EX}op_1")
RECORD = IRI(f"{EX}record_1")
STATUS = IRI(f"{EX}status")
LOG = IRI(f"{EX}log")
LINK = IRI(f"{EX}link")
PART = IRI(f"{EX}part_1")
PART_CLASS = IRI(f"{EX}Part")


class _RecordingOGM:
    """Records each call of `commit`. It reads no `data`, so every change kind passes."""

    def __init__(self) -> None:
        self.calls: list[list] = []

    def commit(self, *, changes, named_graph=None):
        self.calls.append(list(changes))
        return []

    def fetch(self, *, instance_iri, **kwargs):
        raise AssertionError(f"a read of {instance_iri}: an append reads nothing")


def _middleware(ogm) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=f"{EX}resource",
        service_class=f"{EX}Service",
        ogm=ogm,
        heartbeat_interval=None,
    )


def _status(value: str) -> list:
    return [Update(instance_iri=OP, data={STATUS: [value]})]


def _log(value: str, instance: IRI = RECORD) -> Append:
    return Append(instance_iri=instance, property_iri=LOG, value=value)


def test_an_append_in_the_entry_reaches_the_ogm_unchanged_in_the_entrys_one_call():
    ogm = _RecordingOGM()
    with _middleware(ogm).commit(entry=[*_status("running"), _log("started")], exit=(), error=()):
        pass
    assert ogm.calls == [[*_status("running"), _log("started")]]


@pytest.mark.parametrize("as_function", [False, True])
def test_an_append_in_the_exit_transition_is_committed_with_its_other_changes(as_function):
    ogm = _RecordingOGM()
    exit_changes = [*_status("done"), _log("finished")]
    exit = (lambda: exit_changes) if as_function else exit_changes
    with _middleware(ogm).commit(entry=(), exit=exit, error=()):
        pass
    assert ogm.calls == [exit_changes]


@pytest.mark.parametrize("as_function", [False, True])
def test_an_append_in_the_error_transition_is_committed_with_its_other_changes(as_function):
    ogm = _RecordingOGM()
    error_changes = [*_status("failed"), _log("failed")]
    error = (lambda exc: error_changes) if as_function else error_changes
    with pytest.raises(KeyError):
        with _middleware(ogm).commit(entry=(), exit=_status("done"), error=error):
            raise KeyError("the work failed")
    assert ogm.calls == [error_changes]


def test_a_staged_append_is_committed_unchanged_at_the_clean_exit():
    ogm = _RecordingOGM()
    staged = _log("from the body")
    with _middleware(ogm).commit(entry=(), exit=_status("done"), error=()) as transition:
        transition._stage(staged)
    assert ogm.calls == [[*_status("done"), staged]]


def test_transition_append_stages_one_append_and_reads_nothing():
    ogm = _RecordingOGM()
    with _middleware(ogm).commit(entry=(), exit=_status("done"), error=()) as transition:
        transition.append(str(PART), str(LINK), OP)
        transition.append(PART, LINK, {"id": str(RECORD)})
        transition.append(RECORD, LOG, "a literal")
    (call,) = ogm.calls
    assert call == [
        *_status("done"),
        Append(instance_iri=PART, property_iri=LINK, value={"id": str(OP)}),  # a reference
        Append(instance_iri=PART, property_iri=LINK, value={"id": str(RECORD)}),
        _log("a literal"),
    ]


def test_an_error_in_the_body_drops_a_staged_append():
    ogm = _RecordingOGM()
    with pytest.raises(KeyError):
        with _middleware(ogm).commit(entry=(), exit=(), error=_status("failed")) as transition:
            transition.append(RECORD, LOG, "dropped")
            raise KeyError("the work failed")
    assert ogm.calls == [_status("failed")]


def test_two_appends_to_one_property_do_not_overlap_and_the_exit_transition_is_written():
    """The example of the overlap rule: the exit transition sets the Operation's status and
    appends to a record's log; the body appends to the same log. All three are written."""
    ogm = _RecordingOGM()
    exit_changes = [*_status("done"), _log("from the exit")]
    with _middleware(ogm).commit(entry=(), exit=exit_changes, error=()) as transition:
        transition.append(RECORD, LOG, "from the body")
    assert ogm.calls == [[*exit_changes, _log("from the body")]]


def test_an_append_staged_to_a_property_that_the_exit_transition_sets_skips_the_exit():
    ogm = _RecordingOGM()
    exit_changes = [*_status("done"), Update(instance_iri=RECORD, data={LOG: ["replaced"]})]
    with _middleware(ogm).commit(entry=(), exit=exit_changes, error=()) as transition:
        transition.append(RECORD, LOG, "from the body")
    assert ogm.calls == [[_log("from the body")]]


def test_a_staged_update_of_a_property_that_the_exit_transition_appends_to_skips_the_exit():
    ogm = _RecordingOGM()
    exit_changes = [*_status("done"), _log("from the exit")]
    with _middleware(ogm).commit(entry=(), exit=exit_changes, error=()) as transition:
        transition.update(RECORD, {LOG: ["replaced"]})
    assert ogm.calls == [[Update(instance_iri=RECORD, data={LOG: ["replaced"]})]]


def test_a_staged_create_that_sets_a_property_the_exit_transition_appends_to_skips_the_exit():
    ogm = _RecordingOGM()
    staged = Create(
        class_iri=PART_CLASS,
        instance_iri=RECORD,
        data={str(LOG): ["new"]},
        class_scope=ClassScope.from_property_chains([[LOG]]),
    )
    with _middleware(ogm).commit(
        entry=(), exit=[*_status("done"), _log("from the exit")], error=()
    ) as transition:
        transition._stage(staged)
    assert ogm.calls == [[staged]]


def test_an_append_to_another_property_or_instance_does_not_overlap():
    ogm = _RecordingOGM()
    exit_changes = [*_status("done"), Update(instance_iri=RECORD, data={LOG: ["kept"]})]
    with _middleware(ogm).commit(entry=(), exit=exit_changes, error=()) as transition:
        transition.append(RECORD, LINK, OP)
        transition.append(PART, LOG, "elsewhere")
    (call,) = ogm.calls
    assert call[:2] == exit_changes
    assert [type(change) for change in call[2:]] == [Append, Append]


def test_a_prefixed_and_a_full_property_iri_of_one_property_overlap():
    ogm = _RecordingOGM()
    exit_changes = [Update(instance_iri=OP, data={"rdfs:label": ["exit"]})]
    with _middleware(ogm).commit(entry=(), exit=exit_changes, error=()) as transition:
        transition.append(OP, "http://www.w3.org/2000/01/rdf-schema#label", "body")
    (call,) = ogm.calls
    assert [type(change) for change in call] == [Append]


def test_an_append_beside_an_update_of_its_instance_is_passed_on_as_it_is():
    """The merge never merges an Append: kapps_ogm refuses such a call against the store."""
    ogm = _RecordingOGM()
    with _middleware(ogm).commit(entry=(), exit=_status("done"), error=()) as transition:
        transition.append(OP, LOG, "first")
        transition.update(OP, {LINK: [{"id": str(PART)}]})
        transition.append(OP, LOG, "second")
    (call,) = ogm.calls
    assert call == [
        Update(instance_iri=OP, data={STATUS: ["done"], LINK: [{"id": str(PART)}]}),
        Append(instance_iri=OP, property_iri=LOG, value="first"),
        Append(instance_iri=OP, property_iri=LOG, value="second"),
    ]


def test_an_append_beside_the_create_of_its_instance_in_a_creation_is_passed_on_as_it_is():
    ogm = _RecordingOGM()
    middleware = _middleware(ogm)
    with middleware.commit(entry=(), exit=(), error=()) as transition:
        with middleware.create(PART_CLASS, {}, instance_iri=str(PART)) as part:
            part.update({str(LOG): ["created"]})
        transition.append(PART, LOG, "appended")
    (call,) = ogm.calls
    assert [type(change) for change in call] == [Create, Append]
    assert call[1] == Append(instance_iri=PART, property_iri=LOG, value="appended")


def test_a_change_of_a_creation_may_be_an_append():
    ogm = _RecordingOGM()
    middleware = _middleware(ogm)
    with middleware.create(PART_CLASS, {}, instance_iri=str(PART)) as part:
        part._stage(_log("staged in the creation"))
    (call,) = ogm.calls
    assert [type(change) for change in call] == [Create, Append]
    assert call[1] == _log("staged in the creation")
    assert part.changes == tuple(call)
