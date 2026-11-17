"""One Capability has at most one live realizer: a second live instance that would realize it is
refused at startup.

A resource-mode instance reads the holder of every Capability it will register before its first
write. The holder is the Service of the Workflow or StateProperty that the Capability leads to
now. By the holder, registration creates the Capability (none), takes it back (this instance's
own Service, at the same address), takes it over (a Service that is not live: deregistered,
swept, or with a heartbeat older than this instance's staleness threshold), or refuses the start
(another live Service) with `CapabilityAlreadyRealizedError`. A refusal after the first write
deregisters the instance's own Service. Registration reads the holder once more after any
`Update` of a Capability, and after a write that the store refused with `PreconditionFailedError`.

The unit tests run the real middleware and a real OGM over an in-memory stand-in of the store
that applies the writes, with the time and the interleavings injected. The integration tests
need a live store.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pytest
from _memory_store import MemoryStore, Refused
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import PreconditionFailedError
from rdflib import OWL, RDF, RDFS, URIRef

from kapps_ogm import OGM
from kapps_semantic_middleware import SemanticMiddleware, middleware, registration
from kapps_semantic_middleware.credentials import triplestore_for
from kapps_semantic_middleware.errors import (
    CapabilityAlreadyRealizedError,
    DeploymentError,
)
from kapps_semantic_middleware.registration import (
    check_capabilities,
    deregister_service,
    mint_capability_iri,
    reachable_service_addresses,
    register_exposures,
    register_service,
    sweep_stale_services,
    update_heartbeat,
)
from kapps_semantic_middleware.vocabulary import CFC, SVC

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import seed  # noqa: E402

NS = "http://example.org/one-realizer#"
RESOURCE = IRI(f"{NS}Station")
SERVICE_CLASS = IRI(f"{NS}StationService")
WORKFLOW_CLASS = IRI(f"{NS}RunWorkflow")
STATE_CLASS = IRI(f"{NS}StatusState")
CAPABILITY_CLASS = IRI(f"{NS}RunCapability")

PORT_A, PORT_B = 8981, 8982
THRESHOLD = 90.0


# --------------------------------------------------------------------------- #
# The stand-in and the instances.
# --------------------------------------------------------------------------- #


@pytest.fixture
def memory() -> MemoryStore:
    """The shipped ontologies, the test's classes and its Resource."""
    store = MemoryStore()
    for cls, base in (
        (SERVICE_CLASS, SVC.Service),
        (WORKFLOW_CLASS, SVC.Workflow),
        (STATE_CLASS, SVC.StateProperty),
        (CAPABILITY_CLASS, CFC.Capability),
    ):
        store.graph.add((URIRef(cls), RDFS.subClassOf, URIRef(str(base))))
        store.graph.add((URIRef(cls), RDF.type, OWL.Class))
    store.graph.add((URIRef(RESOURCE), RDF.type, URIRef(str(CFC.Resource))))
    return store


@pytest.fixture
def clock(monkeypatch) -> list[datetime]:
    """The time by which a starting instance judges liveness: ``clock[0]``. A test moves it."""
    now = [datetime.now(timezone.utc)]
    monkeypatch.setattr(middleware, "_registration_now", lambda: now[0])
    return now


def _instance(
    ogm: Any,
    port: int,
    *,
    workflows: tuple[str, ...] = (),
    states: tuple[str, ...] = (),
    resource: IRI = RESOURCE,
    service_class: IRI = SERVICE_CLASS,
    workflow_class: IRI = WORKFLOW_CLASS,
    state_class: IRI = STATE_CLASS,
    capability_class: IRI = CAPABILITY_CLASS,
) -> SemanticMiddleware:
    """A resource-mode instance at ``port`` that exposes ``workflows`` and ``states``. No server
    is started: the test runs its registration steps."""
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=resource,
        service_class=service_class,
        ogm=ogm,
        host="127.0.0.1",
        port=port,
        staleness_threshold=THRESHOLD,
    )
    for name in workflows:

        def run() -> None:
            """Run."""

        run.__name__ = name
        mw.workflow(capability_class=capability_class, workflow_class=workflow_class)(run)
    for name in states:
        mw.state(capability_class=capability_class, state_property_class=state_class, name=name)(
            lambda: "ok"
        )
    return mw


def _start(mw: SemanticMiddleware) -> None:
    """The instance's registration: the Service with the check of the holders, and then what it
    exposes, the two startup steps in their order."""

    async def steps() -> None:
        await mw._register_service()
        await mw._register_exposures()

    asyncio.run(steps())


def _capability(name: str) -> IRI:
    return mint_capability_iri(RESOURCE, name)


def _links(memory: MemoryStore, capability: IRI) -> list[str]:
    """What the Capability leads to: its Workflows and StateProperties."""
    return memory.objects(capability, SVC.realizedByWorkflow) + memory.objects(
        capability, SVC.providedByStateProperty
    )


def _reachability(memory: MemoryStore, mw: SemanticMiddleware) -> dict:
    """The instance's address, heartbeat, and the endpoint of each of its Workflows and
    StateProperties."""
    items = [e.iri for e in mw._exposures]
    return {
        "address": memory.objects(mw.service_iri, SVC.hasAddress),
        "heartbeat": memory.objects(mw.service_iri, SVC.hasLastHeartbeat),
        "endpoints": {str(i): memory.objects(i, SVC.hasEndpoint) for i in items},
    }


def _refused_leaves_nothing(memory: MemoryStore, mw: SemanticMiddleware) -> None:
    """A refused instance has no address and no endpoint in the graph."""
    reach = _reachability(memory, mw)
    assert reach["address"] == []
    assert all(endpoints == [] for endpoints in reach["endpoints"].values())


# --------------------------------------------------------------------------- #
# The startup order.
# --------------------------------------------------------------------------- #


def test_the_registration_is_one_step_directly_after_the_service_before_the_queue_and_heartbeat():
    mw = _instance(object(), PORT_A, workflows=("run",), states=("status",))

    names = [callback.func.__name__ for callback in mw.on_start_up_callbacks]

    assert names[:5] == [
        "_register_service",
        "_register_exposures",
        "_load_resource_datamodel",
        "_reconstruct_queue",
        "_start_heartbeat",
    ]
    # No step of its own per workflow or state any more.
    assert len(names) == 5


def test_a_refused_start_ends_the_lifespan_before_the_queue_and_the_heartbeat(memory, clock):
    ogm = OGM(db=memory.store)
    _start(_instance(ogm, PORT_A, workflows=("run",)))
    second = _instance(ogm, PORT_B, workflows=("run",))
    reached: list[str] = []

    async def load() -> None:
        reached.append("datamodel")

    async def reconstruct() -> None:
        reached.append("queue")

    second._load_resource_datamodel = load  # type: ignore[method-assign]
    second._reconstruct_queue = reconstruct  # type: ignore[method-assign]
    second.on_start_up_callbacks[2:4] = [load, reconstruct]

    async def lifespan() -> None:
        async with second.lifespan(second.app):
            pass

    with pytest.raises(CapabilityAlreadyRealizedError):
        asyncio.run(lifespan())

    assert reached == []
    assert second._heartbeat_task is None
    assert second._operation_queue == []
    _refused_leaves_nothing(memory, second)


# --------------------------------------------------------------------------- #
# Two live instances.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exposes",
    [pytest.param({"workflows": ("run",)}, id="workflow"), pytest.param({"states": ("run",)}, id="state")],
)
def test_a_second_live_instance_that_exposes_the_same_name_is_refused(memory, clock, exposes):
    ogm = OGM(db=memory.store)
    first = _instance(ogm, PORT_A, **exposes)
    _start(first)
    links = _links(memory, _capability("run"))
    first_reach = _reachability(memory, first)
    statements = len(memory.graph)

    second = _instance(ogm, PORT_B, **exposes)
    with pytest.raises(CapabilityAlreadyRealizedError) as raised:
        _start(second)

    assert raised.value.holder == str(first.service_iri)
    assert _links(memory, _capability("run")) == links == [str(first._exposures[0].iri)]
    assert _reachability(memory, first) == first_reach
    # Refused before the first write: nothing of the second instance is in the graph.
    assert len(memory.graph) == statements
    _refused_leaves_nothing(memory, second)
    assert reachable_service_addresses(ogm, RESOURCE) == [(first.service_iri, first.address)]


def test_a_workflow_and_a_state_of_one_name_in_two_instances_contest_one_capability(
    memory, clock
):
    ogm = OGM(db=memory.store)
    _start(_instance(ogm, PORT_A, states=("run",)))

    with pytest.raises(CapabilityAlreadyRealizedError):
        _start(_instance(ogm, PORT_B, workflows=("run",)))


def test_a_workflow_and_a_state_of_one_name_in_one_instance_share_their_capability(
    memory, clock
):
    ogm = OGM(db=memory.store)
    mw = _instance(ogm, PORT_A, workflows=("run",), states=("run",))

    _start(mw)
    _start(mw)  # and a restart takes both back

    assert sorted(_links(memory, _capability("run"))) == sorted(str(e.iri) for e in mw._exposures)


def test_the_refusal_reads_on_its_own(memory, clock):
    ogm = OGM(db=memory.store)
    first = _instance(ogm, PORT_A, workflows=("run",))
    _start(first)
    beat = datetime(2026, 11, 5, 9, 0, 0, tzinfo=timezone.utc)
    update_heartbeat(ogm, first.service_iri, timestamp=beat)
    clock[0] = beat + timedelta(seconds=10)

    with pytest.raises(CapabilityAlreadyRealizedError) as raised:
        _start(_instance(ogm, PORT_B, workflows=("run",)))

    error = raised.value
    message = str(error)
    stale_from = beat + timedelta(seconds=THRESHOLD)
    assert isinstance(error, DeploymentError)
    for part in (
        str(RESOURCE),
        str(_capability("run")),
        first.address,
        beat.isoformat(),
        stale_from.isoformat(),
        "stop one of the two instances",
        "start this one again after",
    ):
        assert part in message
    assert "edit" not in message.lower()
    assert error.resource == str(RESOURCE)
    assert error.capability == str(_capability("run"))
    assert error.holder_address == first.address
    assert error.holder_heartbeat == beat
    assert error.stale_from == stale_from


# --------------------------------------------------------------------------- #
# Restarts.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("old_heartbeat", ["fresh", "stale"])
def test_a_restart_at_the_same_address_takes_back_its_registration_and_writes_nothing_else(
    memory, clock, old_heartbeat
):
    ogm = OGM(db=memory.store)
    mw = _instance(ogm, PORT_A, workflows=("run",), states=("status",))
    _start(mw)
    if old_heartbeat == "stale":
        clock[0] += timedelta(seconds=10 * THRESHOLD)

    def without_heartbeat() -> set:
        return {t for t in memory.graph if t[1] != URIRef(str(SVC.hasLastHeartbeat))}

    before = without_heartbeat()
    restarted = _instance(ogm, PORT_A, workflows=("run",), states=("status",))
    _start(restarted)

    assert without_heartbeat() == before


def test_a_restart_at_a_new_address_is_refused_while_the_old_service_is_live_and_takes_over_after(
    memory, clock, caplog
):
    ogm = OGM(db=memory.store)
    old = _instance(ogm, PORT_A, workflows=("run",))
    _start(old)

    with pytest.raises(CapabilityAlreadyRealizedError):
        _start(_instance(ogm, PORT_B, workflows=("run",)))

    # The old heartbeat is older than the threshold now: the same start succeeds.
    clock[0] += timedelta(seconds=THRESHOLD + 1)
    new = _instance(ogm, PORT_B, workflows=("run",))
    with caplog.at_level(logging.WARNING, logger=registration.__name__):
        _start(new)

    assert _links(memory, _capability("run")) == [str(new._exposures[0].iri)]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(old.service_iri) in warnings[0].getMessage()


def test_a_take_back_beside_a_stale_link_of_the_other_kind_logs_no_take_over(
    memory, clock, caplog
):
    """A stale instance's state property of one name leads to the Capability too; a workflow of
    that name then takes it over, and the state's stale link stays in place. A restart of the
    workflow's instance at its own address takes it back: that is no take-over."""
    ogm = OGM(db=memory.store)
    _start(_instance(ogm, PORT_A, states=("run",)))
    clock[0] += timedelta(seconds=THRESHOLD + 1)
    workflow = _instance(ogm, PORT_B, workflows=("run",))
    _start(workflow)
    assert len(_links(memory, _capability("run"))) == 2

    with caplog.at_level(logging.WARNING, logger=registration.__name__):
        _start(_instance(ogm, PORT_B, workflows=("run",)))

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


@pytest.mark.parametrize("gone", ["clean stop", "watchdog sweep"])
def test_a_restart_at_a_new_address_takes_over_once_the_old_address_is_gone(
    memory, clock, gone
):
    ogm = OGM(db=memory.store)
    old = _instance(ogm, PORT_A, workflows=("run",))
    _start(old)
    if gone == "clean stop":
        deregister_service(ogm, old.service_iri)
    else:
        update_heartbeat(
            ogm, old.service_iri, timestamp=clock[0] - timedelta(seconds=THRESHOLD + 1)
        )
        assert sweep_stale_services(ogm, THRESHOLD, now=clock[0]) == [old.service_iri]

    new = _instance(ogm, PORT_B, workflows=("run",))
    _start(new)

    assert _links(memory, _capability("run")) == [str(new._exposures[0].iri)]


def test_a_controller_and_a_monitor_and_two_instances_with_different_names_all_start(
    memory, clock
):
    ogm = OGM(db=memory.store)
    controller = _instance(ogm, PORT_A, workflows=("run",))
    monitor = _instance(ogm, PORT_B)
    other = _instance(ogm, 8983, workflows=("stop",), states=("status",))

    for mw in (controller, monitor, other):
        _start(mw)

    assert sorted(svc for svc, _ in reachable_service_addresses(ogm, RESOURCE)) == sorted(
        mw.service_iri for mw in (controller, monitor, other)
    )


# --------------------------------------------------------------------------- #
# Races, with the interleaving injected at the registration's seam.
# --------------------------------------------------------------------------- #


def _steps(ogm: Any, mw: SemanticMiddleware, now: datetime):
    """The two registration steps of ``mw`` as separate calls, so a test interleaves them."""
    own = mw._own_iris("test")
    exposures = list(mw._exposures)

    def check() -> dict:
        return check_capabilities(
            ogm,
            resource_iri=own.resource,
            service_iri=own.service,
            exposures=exposures,
            staleness_threshold=THRESHOLD,
            now=now,
        )

    def service() -> None:
        register_service(
            ogm,
            resource_iri=own.resource,
            service_iri=own.service,
            service_class=own.service_class,
            address=mw.address,
        )

    def expose(holders: Optional[dict]) -> None:
        register_exposures(
            ogm,
            resource_iri=own.resource,
            service_iri=own.service,
            exposures=exposures,
            staleness_threshold=THRESHOLD,
            holders=holders,
            now=lambda: now,
        )

    return check, service, expose


def _capability_writes(memory: MemoryStore, hook: Callable[[dict], None]) -> None:
    """Call ``hook`` before each write that creates or updates a Capability of the Resource."""

    def before(arguments: dict) -> None:
        if any(
            str(p) == str(SVC.realizedByWorkflow) or str(p) == str(SVC.providedByStateProperty)
            for _, p, _ in arguments["new_triples"]
        ):
            hook(arguments)

    memory.before_update = before


@pytest.mark.parametrize(
    "names_b", [("run", "stop"), ("stop", "run")], ids=["same order", "declared reversed"]
)
def test_two_instances_that_start_together_one_realizes_every_shared_capability(
    memory, names_b
):
    """Both read no holder and both register their Service; then they write. The one whose
    first write lands first realizes both shared Capabilities; the other is refused."""
    ogm = OGM(db=memory.store)
    now = datetime.now(timezone.utc)
    a = _instance(ogm, PORT_A, workflows=("run", "stop"))
    b = _instance(ogm, PORT_B, workflows=names_b)
    check_a, service_a, expose_a = _steps(ogm, a, now)
    check_b, service_b, expose_b = _steps(ogm, b, now)
    held_a, held_b = check_a(), check_b()
    service_a()
    service_b()

    # A's whole registration lands right before B's first Capability write.
    def a_first(_: dict) -> None:
        memory.before_update = None
        expose_a(held_a)
        _capability_writes(memory, lambda _: None)

    _capability_writes(memory, a_first)
    with pytest.raises(CapabilityAlreadyRealizedError):
        expose_b(held_b)

    a_items = {e.capability: str(e.iri) for e in a._exposures}
    for name in ("run", "stop"):
        assert _links(memory, _capability(name)) == [a_items[_capability(name)]]
    _refused_leaves_nothing(memory, b)
    assert memory.objects(a.service_iri, SVC.hasAddress) == [a.address]


def test_two_instances_that_alternate_their_writes_never_win_one_each(memory):
    """Before each Capability write of B, A writes its next Capability, as two processes that
    alternate would. B declares its workflows in the other order, but both take their
    Capabilities in one fixed order, so B loses the first one and never reaches the second."""
    ogm = OGM(db=memory.store)
    now = datetime.now(timezone.utc)
    a = _instance(ogm, PORT_A, workflows=("stop", "run"))
    b = _instance(ogm, PORT_B, workflows=("run", "stop"))
    check_a, service_a, expose_a = _steps(ogm, a, now)
    check_b, service_b, expose_b = _steps(ogm, b, now)
    held_a, held_b = check_a(), check_b()
    service_a()
    service_b()
    pending = sorted(e.capability for e in a._exposures)

    def one_of_a(_: dict) -> None:
        if not pending:
            return
        capability = pending.pop(0)
        memory.before_update = None
        register_exposures(
            ogm,
            resource_iri=RESOURCE,
            service_iri=a.service_iri,
            exposures=[e for e in a._exposures if e.capability == capability],
            staleness_threshold=THRESHOLD,
            holders={capability: held_a[capability]},
            now=lambda: now,
        )
        _capability_writes(memory, one_of_a)

    _capability_writes(memory, one_of_a)
    with pytest.raises(CapabilityAlreadyRealizedError):
        expose_b(held_b)
    memory.before_update = None
    # A goes on with the Capabilities it has not written yet: a restart's take-back.
    expose_a(held_a)

    for e in a._exposures:
        assert _links(memory, e.capability) == [str(e.iri)]
    _refused_leaves_nothing(memory, b)


def test_a_take_over_that_finds_another_live_instance_after_its_commit_refuses_itself(
    memory, monkeypatch
):
    ogm = OGM(db=memory.store)
    now = datetime.now(timezone.utc)
    stale = _instance(ogm, PORT_A, workflows=("run",))
    check_s, service_s, expose_s = _steps(ogm, stale, now - timedelta(seconds=10 * THRESHOLD))
    service_s()
    expose_s(check_s())
    update_heartbeat(ogm, stale.service_iri, timestamp=now - timedelta(seconds=10 * THRESHOLD))
    a = _instance(ogm, PORT_B, workflows=("run",))
    b = _instance(ogm, 8983, workflows=("run",))
    check_a, service_a, expose_a = _steps(ogm, a, now)
    check_b, service_b, expose_b = _steps(ogm, b, now)
    held_a, held_b = check_a(), check_b()
    service_a()
    service_b()
    capability = _capability("run")

    # B's take-over lands between A's commit and A's read after it.
    real_holders = registration._holders
    reads: list[int] = []

    def holders(ogm_: Any, capability_iri: IRI, threshold: float, at: datetime):
        reads.append(1)
        if len(reads) == 1:
            monkeypatch.setattr(registration, "_holders", real_holders)
            expose_b(held_b)
        return real_holders(ogm_, capability_iri, threshold, at)

    monkeypatch.setattr(registration, "_holders", holders)
    with pytest.raises(CapabilityAlreadyRealizedError) as raised:
        expose_a(held_a)

    assert raised.value.holder == str(b.service_iri)
    assert _links(memory, capability) == [str(b._exposures[0].iri)]
    _refused_leaves_nothing(memory, a)
    assert memory.objects(b.service_iri, SVC.hasAddress) == [b.address]


def test_a_refused_write_is_read_again_and_decided_once_more(memory):
    """Another writer creates the Capability, with no holder, right before this instance's
    create, so the store refuses the create; the read again finds no live holder, and the
    update that follows is admitted."""
    ogm = OGM(db=memory.store)
    now = datetime.now(timezone.utc)
    mw = _instance(ogm, PORT_A, workflows=("run",))
    check, service, expose = _steps(ogm, mw, now)
    held = check()
    service()
    capability = _capability("run")
    writes: list[dict] = []

    def other_writer_first(arguments: dict) -> None:
        writes.append(arguments)
        if len(writes) == 1:
            memory.graph.add((URIRef(capability), RDF.type, URIRef(CAPABILITY_CLASS)))

    _capability_writes(memory, other_writer_first)
    expose(held)

    # The commit creates the Workflow too, so only the Capability's place in it tells the
    # create from the update.
    assert [capability in w["absent_subjects"] for w in writes] == [True, False]
    assert _links(memory, capability) == [str(mw._exposures[0].iri)]
    assert memory.objects(mw.service_iri, SVC.hasAddress) == [mw.address]


def test_a_second_refusal_of_one_write_deregisters_and_propagates_unchanged(memory):
    """A take-over of a stale holder is refused twice: that is no race, so the instance
    deregisters and the store's error propagates as it is."""
    ogm = OGM(db=memory.store)
    now = datetime.now(timezone.utc)
    stale = _instance(ogm, PORT_B, workflows=("run",))
    check_s, service_s, expose_s = _steps(ogm, stale, now)
    service_s()
    expose_s(check_s())
    update_heartbeat(ogm, stale.service_iri, timestamp=now - timedelta(seconds=10 * THRESHOLD))
    mw = _instance(ogm, PORT_A, workflows=("run",))
    check, service, expose = _steps(ogm, mw, now)
    held = check()
    service()
    refusals = [Refused("second"), Refused("first")]
    raised_ones: list[PreconditionFailedError] = []

    def refuse(_: dict) -> None:
        refusal = refusals.pop()
        raised_ones.append(refusal)
        raise refusal

    _capability_writes(memory, refuse)
    with pytest.raises(PreconditionFailedError) as raised:
        expose(held)

    assert raised.value is raised_ones[-1]
    assert len(raised_ones) == 2
    _refused_leaves_nothing(memory, mw)


# --------------------------------------------------------------------------- #
# Against a live store.
# --------------------------------------------------------------------------- #


def _hello(store: Any, port: int) -> SemanticMiddleware:
    return _instance(
        OGM(db=triplestore_for(store.dataset)),
        port,
        workflows=("hello_world",),
        resource=IRI(seed.HELLO_RESOURCE),
        service_class=IRI(seed.HELLO_SERVICE_CLASS),
        workflow_class=IRI(seed.HELLO_WORKFLOW_CLASS),
        capability_class=IRI(seed.HELLO_CAPABILITY_CLASS),
    )


class TestOneLiveRealizerIntegration:
    """Two instances of the hello resource against the store."""

    def test_a_second_live_instance_is_refused_and_the_first_keeps_the_capability(self, store):
        seed.seed_scenario1(store)
        first = _hello(store, PORT_A)
        _start(first)
        capability = mint_capability_iri(IRI(seed.HELLO_RESOURCE), "hello_world")

        second = _hello(store, PORT_B)
        with pytest.raises(CapabilityAlreadyRealizedError):
            _start(second)

        realized = store.triples_get(
            sub=capability, pred=SVC.realizedByWorkflow, include_implicit=False
        )
        assert [str(t[2]) for t in realized] == [str(first._exposures[0].iri)]
        assert not store.triples_get(sub=second.service_iri, pred=SVC.hasAddress)
        assert reachable_service_addresses(OGM(db=store), IRI(seed.HELLO_RESOURCE)) == [
            (first.service_iri, first.address)
        ]

    def test_a_restart_at_a_new_address_takes_over_once_the_old_heartbeat_is_stale(
        self, store, monkeypatch
    ):
        seed.seed_scenario1(store)
        old = _hello(store, PORT_A)
        _start(old)
        later = datetime.now(timezone.utc) + timedelta(seconds=THRESHOLD + 5)
        monkeypatch.setattr(middleware, "_registration_now", lambda: later)

        new = _hello(store, PORT_B)
        _start(new)

        capability = mint_capability_iri(IRI(seed.HELLO_RESOURCE), "hello_world")
        realized = store.triples_get(
            sub=capability, pred=SVC.realizedByWorkflow, include_implicit=False
        )
        assert [str(t[2]) for t in realized] == [str(new._exposures[0].iri)]
