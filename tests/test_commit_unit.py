"""`mw.commit()` and `mw.fetch()` without a triple store.

The OGM is faked at its public `commit(changes=...)` and `fetch(...)` calls, which is the seam
`mw.commit()` writes through. The fake keeps kapps_ogm's commit rules that `mw.commit()` must
respect: a call is admitted as a whole or refused as a whole, and two changes of one call must
not write one instance. The live counterpart is `test_commit_integration.py`.
"""

from __future__ import annotations

import asyncio
import threading
import time

import anyio
import pytest
from kapps_ogm import Create, Update
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import TripleStoreResponseError

from kapps_semantic_middleware import SemanticMiddleware

OP = IRI("https://example.org/test#op_1")
STATUS = IRI("https://example.org/test#status")
RESULT = IRI("https://example.org/test#result")


class _FakeOGM:
    """Records the calls that the store admitted, and refuses the ones `refuse` names."""

    def __init__(self, refuse=None):
        self.refuse = refuse or (lambda changes: False)
        self.written: list[list] = []

    def commit(self, *, changes, named_graph=None):
        changes = list(changes)
        named = [IRI(c.instance_iri) for c in changes if c.instance_iri is not None]
        if len(named) != len(set(named)):
            raise ValueError("Two changes name one instance.")
        if self.refuse(changes):
            raise TripleStoreResponseError("SHACL validation failed")
        self.written.append(changes)
        return []

    def fetch(self, *, instance_iri, **kwargs):
        return _Node(instance_iri, kwargs)


class _Node:
    def __init__(self, iri, kwargs):
        self.id = iri
        self.fetched_with = kwargs


def _middleware(ogm) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri="https://example.org/test#resource",
        service_class="https://example.org/test#Service",
        ogm=ogm,
        heartbeat_interval=None,
    )


def _status(value: str) -> list:
    return [Update(instance_iri=OP, data={STATUS: [value]})]


def test_a_refused_entry_runs_no_body_and_writes_nothing():
    ogm = _FakeOGM(refuse=lambda changes: True)
    ran = []
    with pytest.raises(TripleStoreResponseError):
        with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ):
            ran.append(True)
    assert ran == []
    assert ogm.written == []


def _written_data(ogm) -> list[list[tuple]]:
    """Each admitted call, as (change type, instance, data) per change."""
    return [
        [(type(c).__name__, str(c.instance_iri), {str(k): v for k, v in c.data.items()}) for c in call]
        for call in ogm.written
    ]


def test_an_error_in_the_body_drops_its_changes_and_commits_only_the_error_transition():
    ogm = _FakeOGM()
    with pytest.raises(RuntimeError, match="gripper jammed"):
        with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ) as transition:
            transition.update(OP, {RESULT: ["half a screw"]})
            raise RuntimeError("gripper jammed")
    assert _written_data(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [("Update", OP, {STATUS: ["failed"]})],
    ]


OTHER = IRI("https://example.org/test#box_1")
LOCATION = IRI("https://example.org/test#location")


def test_a_clean_exit_commits_the_body_changes_and_the_exit_transition_in_one_commit():
    """The body's change to the Operation and the exit transition merge into one `Update`."""
    ogm = _FakeOGM()
    with _middleware(ogm).commit(
        OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
    ) as transition:
        transition.update(OP, {RESULT: ["screwed"]})
        transition.update(OTHER, {LOCATION: ["station 2"]})
    assert _written_data(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [
            ("Update", OP, {STATUS: ["done"], RESULT: ["screwed"]}),
            ("Update", OTHER, {LOCATION: ["station 2"]}),
        ],
    ]


def test_two_body_changes_to_one_instance_merge_and_the_later_value_wins():
    ogm = _FakeOGM()
    with _middleware(ogm).commit(OP) as transition:
        transition.update(OP, {RESULT: ["first"], LOCATION: ["station 1"]})
        transition.update(OP, {RESULT: ["second"]})
    assert _written_data(ogm) == [
        [("Update", OP, {RESULT: ["second"], LOCATION: ["station 1"]})],
    ]


def test_a_body_that_sets_the_status_commits_its_own_value_and_the_exit_transition_is_skipped():
    ogm = _FakeOGM()
    exit_transition = [
        Update(instance_iri=OP, data={STATUS: ["done"]}),
        Update(instance_iri=OTHER, data={LOCATION: ["outbound buffer"]}),
    ]
    with _middleware(ogm).commit(
        OP, entry=_status("running"), exit=exit_transition, error=_status("failed")
    ) as transition:
        transition.update(OP, {STATUS: ["waiting for parts"], RESULT: ["3 of 4 screws"]})
    assert _written_data(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [("Update", OP, {STATUS: ["waiting for parts"], RESULT: ["3 of 4 screws"]})],
    ]


def test_a_body_that_sets_the_status_still_commits_the_error_transition_when_it_raises():
    ogm = _FakeOGM()
    with pytest.raises(RuntimeError):
        with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ) as transition:
            transition.update(OP, {STATUS: ["waiting for parts"]})
            raise RuntimeError("no parts arrived")
    assert _written_data(ogm)[-1] == [("Update", OP, {STATUS: ["failed"]})]


def _sets(status: str):
    """A refusal rule: the store refuses every call that sets the status to ``status``."""
    return lambda changes: any(c.data.get(STATUS) == [status] for c in changes)


def test_a_refused_exit_commits_the_error_transition_and_raises_the_stores_error():
    ogm = _FakeOGM(refuse=_sets("done"))
    seen = []

    def failed(exc: BaseException) -> list:
        seen.append(exc)
        return [Update(instance_iri=OP, data={STATUS: ["failed"], RESULT: [str(exc)]})]

    with pytest.raises(TripleStoreResponseError) as raised:
        with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=failed
        ) as transition:
            transition.update(OP, {RESULT: ["screwed"]})
    assert seen == [raised.value]
    assert _written_data(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [("Update", OP, {STATUS: ["failed"], RESULT: ["SHACL validation failed"]})],
    ]


def test_the_transitions_may_be_computed_when_they_are_committed():
    """The exit transition can depend on what the body did, the error one on the exception."""
    ogm = _FakeOGM()
    outcome = {}
    with _middleware(ogm).commit(
        OP,
        entry=_status("running"),
        exit=lambda: [Update(instance_iri=OP, data={STATUS: ["done"], RESULT: [outcome["torque"]]})],
    ):
        outcome["torque"] = "4.2 Nm"
    assert _written_data(ogm)[-1] == [("Update", OP, {STATUS: ["done"], RESULT: ["4.2 Nm"]})]


def test_a_refused_error_transition_leaves_the_bodys_error_to_propagate_with_a_note(caplog):
    ogm = _FakeOGM(refuse=_sets("failed"))
    with pytest.raises(RuntimeError, match="gripper jammed") as raised:
        with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ):
            raise RuntimeError("gripper jammed")
    assert _written_data(ogm) == [[("Update", OP, {STATUS: ["running"]})]]
    assert any("error transition" in note for note in raised.value.__notes__)
    assert "error transition" in caplog.text


@pytest.mark.parametrize("stop", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_an_interrupt_or_an_exit_in_the_body_writes_no_transition(stop):
    """Not an error of the work: the entry transition stays, as after a crash."""
    ogm = _FakeOGM()
    with pytest.raises(stop):
        with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ):
            raise stop()
    assert _written_data(ogm) == [[("Update", OP, {STATUS: ["running"]})]]


class _RecordingOGM(_FakeOGM):
    """Records the order of commits and fetches, to see what the body is given and when."""

    def __init__(self, refuse=None, fail_fetch=False):
        super().__init__(refuse)
        self.log: list[str] = []
        self.fail_fetch = fail_fetch

    def commit(self, *, changes, named_graph=None):
        result = super().commit(changes=changes, named_graph=named_graph)
        self.log.append("commit")
        return result

    def fetch(self, *, instance_iri, **kwargs):
        self.log.append(f"fetch {instance_iri}")
        if self.fail_fetch:
            raise ValueError(f"Could not determine class IRI for instance {instance_iri}")
        return super().fetch(instance_iri=instance_iri, **kwargs)


def test_the_body_is_given_its_instances_fetched_after_the_entry():
    ogm = _RecordingOGM()
    scope = object()
    with _middleware(ogm).commit(OP, OTHER, class_scope=scope, entry=_status("running")) as transition:
        assert [node.id for node in transition.nodes.values()] == [OP, OTHER]
        assert transition.nodes[OP].fetched_with == {"class_scope": scope, "materialize": True}
    assert ogm.log == ["commit", f"fetch {OP}", f"fetch {OTHER}"]


def test_without_a_class_scope_the_body_is_given_a_reference_to_its_one_instance():
    ogm = _RecordingOGM()
    with _middleware(ogm).commit(OP) as transition:
        assert transition.node.id == OP
        assert transition.node.fetched_with == {"as_reference": True}


def test_a_failed_fetch_after_the_entry_runs_no_body_and_commits_the_error_transition():
    ogm = _RecordingOGM(fail_fetch=True)
    ran = []
    with pytest.raises(ValueError, match="Could not determine class"):
        with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ):
            ran.append(True)
    assert ran == []
    assert _written_data(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [("Update", OP, {STATUS: ["failed"]})],
    ]


class _OneConnectionOGM(_FakeOGM):
    """A store reached through one connection, which every call holds while it runs.

    A heartbeat is a write too, so it waits for the connection. If the commit held the
    connection, or a transaction on it, across its body, no heartbeat could land while the body
    runs. Each call also takes a little time and records the thread it ran on, so a commit that
    did its graph I/O on the event loop's thread is seen as well.
    """

    def __init__(self):
        super().__init__()
        self.connection = threading.Lock()
        self.heartbeats: list[float] = []
        self.commit_threads: list[int] = []

    def commit(self, *, changes=None, instance_iri=None, data=None, named_graph=None):
        with self.connection:
            time.sleep(0.01)
            if changes is None:
                self.heartbeats.append(time.monotonic())
                return None
            self.commit_threads.append(threading.get_ident())
            return super().commit(changes=changes, named_graph=named_graph)

    def fetch(self, *, instance_iri, **kwargs):
        with self.connection:
            return super().fetch(instance_iri=instance_iri, **kwargs)


HEARTBEAT_INTERVAL = 0.05
BODY_SECONDS = 0.6


def _heartbeating_middleware(ogm) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri="https://example.org/test#resource",
        service_class="https://example.org/test#Service",
        ogm=ogm,
        heartbeat_interval=HEARTBEAT_INTERVAL,
    )


def _assert_heartbeats_kept_landing(ogm, started: float, ended: float) -> None:
    during = [t for t in ogm.heartbeats if started <= t <= ended]
    assert len(during) >= BODY_SECONDS / HEARTBEAT_INTERVAL / 3, during
    beats = [started, *during, ended]
    assert max(b - a for a, b in zip(beats, beats[1:])) < 5 * HEARTBEAT_INTERVAL


@pytest.mark.asyncio
async def test_an_awaiting_body_delays_no_heartbeat_and_its_graph_io_runs_off_the_event_loop():
    ogm = _OneConnectionOGM()
    mw = _heartbeating_middleware(ogm)
    # The loop that resource mode starts at startup, started alone: the rest of startup
    # would register the service in a real graph.
    await mw._start_heartbeat()
    try:
        async with mw.commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ) as transition:
            started = time.monotonic()
            await asyncio.sleep(BODY_SECONDS)  # a robot's move, awaited
            transition.update(OP, {RESULT: ["screwed"]})
            ended = time.monotonic()
    finally:
        await mw._stop_heartbeat()

    _assert_heartbeats_kept_landing(ogm, started, ended)
    assert threading.get_ident() not in ogm.commit_threads
    assert _written_data(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [("Update", OP, {STATUS: ["done"], RESULT: ["screwed"]})],
    ]


@pytest.mark.asyncio
async def test_a_blocking_body_in_a_worker_thread_delays_no_heartbeat():
    """The synchronous form, as a callback runs it: in a worker thread, off the event loop."""
    ogm = _OneConnectionOGM()
    mw = _heartbeating_middleware(ogm)
    window = {}

    def run_the_work() -> None:
        with mw.commit(OP, entry=_status("running"), exit=_status("done")):
            window["started"] = time.monotonic()
            time.sleep(BODY_SECONDS)  # a robot's move, blocking
            window["ended"] = time.monotonic()

    await mw._start_heartbeat()
    try:
        await anyio.to_thread.run_sync(run_the_work)
    finally:
        await mw._stop_heartbeat()

    _assert_heartbeats_kept_landing(ogm, window["started"], window["ended"])


def _another_write_completes_at_once(ogm: _OneConnectionOGM) -> bool:
    """Whether a write from another thread, through the store's one connection, completes
    within a short bound rather than waiting for the connection."""
    other = threading.Thread(
        target=ogm.commit,
        kwargs={"instance_iri": OTHER, "data": {LOCATION: ["station 3"]}},
        daemon=True,
    )
    other.start()
    other.join(timeout=10 * HEARTBEAT_INTERVAL)
    return not other.is_alive()


@pytest.mark.asyncio
async def test_an_awaiting_body_holds_no_connection_so_another_write_completes_meanwhile():
    """Were the connection, or a transaction on it, held across the body, the other write would
    wait for the body to end."""
    ogm = _OneConnectionOGM()
    async with _middleware(ogm).commit(OP, entry=_status("running"), exit=_status("done")):
        assert await anyio.to_thread.run_sync(_another_write_completes_at_once, ogm)


@pytest.mark.asyncio
async def test_a_blocking_body_holds_no_connection_so_another_write_completes_meanwhile():
    """The synchronous form, run entirely in a worker thread as a callback runs it."""
    ogm = _OneConnectionOGM()

    def run_the_work() -> bool:
        with _middleware(ogm).commit(OP, entry=_status("running"), exit=_status("done")):
            return _another_write_completes_at_once(ogm)

    assert await anyio.to_thread.run_sync(run_the_work)


@pytest.mark.asyncio
async def test_the_async_form_commits_the_error_transition_and_reraises():
    ogm = _FakeOGM()
    with pytest.raises(RuntimeError, match="gripper jammed"):
        async with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ) as transition:
            transition.update(OP, {RESULT: ["half a screw"]})
            raise RuntimeError("gripper jammed")
    assert _written_data(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [("Update", OP, {STATUS: ["failed"]})],
    ]


@pytest.mark.asyncio
async def test_the_async_form_runs_no_body_after_a_refused_entry():
    ogm = _FakeOGM(refuse=lambda changes: True)
    ran = []
    with pytest.raises(TripleStoreResponseError):
        async with _middleware(ogm).commit(OP, entry=_status("running")):
            ran.append(True)
    assert ran == []
    assert ogm.written == []


def test_fetch_reads_an_instance_through_the_ogm_as_a_plain_call():
    ogm = _RecordingOGM()
    scope = object()
    node = _middleware(ogm).fetch(str(OP), class_scope=scope, materialize=True)
    assert node.id == OP and isinstance(node.id, IRI)
    assert node.fetched_with == {
        "class_scope": scope,
        "as_reference": False,
        "materialize": True,
    }
    assert ogm.written == []


def test_an_assignment_to_the_transition_is_refused_and_names_update():
    """An attribute set on the transition would never reach the graph, so it raises at once."""
    ogm = _FakeOGM()
    with pytest.raises(AttributeError, match=r"transition\.update\("):
        with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ) as transition:
            transition.hasTimeSeriesReference = "https://example.org/test#series_1"
    assert _written_data(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [("Update", OP, {STATUS: ["failed"]})],
    ]


SNAPSHOT_CLASS = IRI("https://example.org/test#DecisionSnapshot")
SNAPSHOT = IRI("https://example.org/test#snapshot_1")


def _snapshot(class_scope=None) -> Create:
    """A `Create` that carries data, with or without the class scope that data needs."""
    return Create(
        class_iri=SNAPSHOT_CLASS,
        data={RESULT: ["reject"]},
        instance_iri=SNAPSHOT,
        class_scope=class_scope,
    )


def test_an_entry_create_with_data_but_no_class_scope_raises_before_any_write():
    ogm = _FakeOGM()
    ran = []
    with pytest.raises(ValueError, match="class_scope"):
        with _middleware(ogm).commit(entry=[_snapshot()], error=_status("failed")):
            ran.append(True)
    assert ran == []
    assert ogm.written == []


def test_a_staged_create_with_data_but_no_class_scope_raises_at_once_in_the_body():
    """The seam that a creation nested in the body uses: it fails where it is called."""
    ogm = _FakeOGM()
    reached = []
    with pytest.raises(ValueError, match="class_scope"):
        with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ) as transition:
            transition._stage(_snapshot())
            reached.append(True)
    assert reached == []
    assert _written_data(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [("Update", OP, {STATUS: ["failed"]})],
    ]


def test_an_exit_create_with_data_but_no_class_scope_commits_the_error_transition_and_raises():
    ogm = _FakeOGM()
    with pytest.raises(ValueError, match="class_scope"):
        with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=[_snapshot()], error=_status("failed")
        ):
            pass
    assert _written_data(ogm) == [
        [("Update", OP, {STATUS: ["running"]})],
        [("Update", OP, {STATUS: ["failed"]})],
    ]


def test_a_create_with_a_class_scope_or_without_data_is_written():
    ogm = _FakeOGM()
    scope = object()
    bare = Create(class_iri=SNAPSHOT_CLASS, data={}, instance_iri=OTHER)
    with _middleware(ogm).commit(entry=[bare], exit=[_snapshot(class_scope=scope)]):
        pass
    assert _written_data(ogm) == [
        [("Create", OTHER, {})],
        [("Create", SNAPSHOT, {RESULT: ["reject"]})],
    ]


_FAILED_AFTER_START = [
    [("Update", OP, {STATUS: ["running"]})],
    [("Update", OP, {STATUS: ["failed"]})],
]


@pytest.mark.asyncio
async def test_a_deadline_that_cancels_the_body_commits_the_error_transition():
    """The cancellation is re-raised, and the deadline's scope catches it as usual."""
    ogm = _FakeOGM()
    seen = []

    def failed(exc: BaseException) -> list:
        seen.append(type(exc))
        return _status("failed")

    with anyio.move_on_after(0.05) as deadline:
        async with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=failed
        ):
            await anyio.sleep(5)  # a move that takes too long
    assert deadline.cancelled_caught
    assert seen == [anyio.get_cancelled_exc_class()]
    assert _written_data(ogm) == _FAILED_AFTER_START


@pytest.mark.asyncio
async def test_a_failing_deadline_around_the_body_commits_the_error_transition():
    ogm = _FakeOGM()
    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.05):
            async with _middleware(ogm).commit(
                OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
            ):
                await anyio.sleep(5)
    assert _written_data(ogm) == _FAILED_AFTER_START


@pytest.mark.asyncio
async def test_a_failing_deadline_inside_the_body_commits_the_error_transition():
    ogm = _FakeOGM()
    with pytest.raises(TimeoutError):
        async with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ):
            with anyio.fail_after(0.05):
                await anyio.sleep(5)
    assert _written_data(ogm) == _FAILED_AFTER_START


@pytest.mark.asyncio
async def test_a_cancelled_task_commits_the_error_transition_and_stays_cancelled():
    ogm = _FakeOGM()
    started = asyncio.Event()

    async def run_the_work() -> None:
        async with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ):
            started.set()
            await asyncio.sleep(5)

    task = asyncio.create_task(run_the_work())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _written_data(ogm) == _FAILED_AFTER_START


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [KeyboardInterrupt, SystemExit, GeneratorExit])
async def test_an_interrupt_or_an_exit_in_the_async_body_writes_no_transition(stop):
    ogm = _FakeOGM()
    with pytest.raises(stop):
        async with _middleware(ogm).commit(
            OP, entry=_status("running"), exit=_status("done"), error=_status("failed")
        ):
            raise stop()
    assert _written_data(ogm) == [[("Update", OP, {STATUS: ["running"]})]]
