"""Each exposed Workflow or StateProperty arrives with its Capability in one commit.

One `OGM.commit` carries the Workflow (or StateProperty) with its link to the Service and its
endpoint, the Capability with its link to it, and the resource's `cfc:hasCapability` link to the
Capability. So no reader sees a Workflow with an endpoint whose Capability does not resolve yet.
A restart, and a start that finds only part of an exposure from an earlier run, write each
exposure in one commit as well. A commit that the store refuses leaves none of its parts.

The tests run the real middleware and a real OGM over the in-memory stand-in of the store that
applies the writes and their preconditions, with the reads injected between the commits.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from _memory_store import MemoryStore, Refused
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import PreconditionFailedError
from rdflib import OWL, RDF, RDFS, XSD, Literal, URIRef

from kapps_ogm import OGM
from kapps_semantic_middleware import SemanticMiddleware, middleware
from kapps_semantic_middleware.errors import (
    CapabilityNotRealizedError,
    OperationReceiverUnavailableError,
)
from kapps_semantic_middleware.registration import (
    STATE_PROPERTY,
    WORKFLOW,
    deregister_service,
    mint_capability_iri,
    mint_service_iri,
    mint_state_property_iri,
    mint_workflow_iri,
    register_state_property,
    register_workflow,
    resolve_dispatch_target,
)
from kapps_semantic_middleware.vocabulary import CFC, SVC

NS = "http://example.org/one-commit#"
RESOURCE = IRI(f"{NS}Station")
SERVICE_CLASS = IRI(f"{NS}StationService")
WORKFLOW_CLASS = IRI(f"{NS}RunWorkflow")
STATE_CLASS = IRI(f"{NS}StatusState")
CAPABILITY_CLASS = IRI(f"{NS}RunCapability")
PORT = 8991
THRESHOLD = 90.0

_OWNER = {WORKFLOW: SVC.isWorkflowOf, STATE_PROPERTY: SVC.isStatePropertyOf}
_LINK = {WORKFLOW: SVC.realizedByWorkflow, STATE_PROPERTY: SVC.providedByStateProperty}


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


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(middleware, "_registration_now", lambda: now)


def _instance(
    ogm: Any, *, workflows: tuple[str, ...] = (), states: tuple[str, ...] = ()
) -> SemanticMiddleware:
    """A resource-mode instance that exposes ``workflows`` and ``states``. No server starts."""
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=RESOURCE,
        service_class=SERVICE_CLASS,
        ogm=ogm,
        host="127.0.0.1",
        port=PORT,
        staleness_threshold=THRESHOLD,
    )
    for name in workflows:

        def run() -> None:
            """Run."""

        run.__name__ = name
        mw.workflow(capability_class=CAPABILITY_CLASS, workflow_class=WORKFLOW_CLASS)(run)
    for name in states:
        mw.state(capability_class=CAPABILITY_CLASS, state_property_class=STATE_CLASS, name=name)(
            lambda: "ok"
        )
    return mw


def _start(mw: SemanticMiddleware) -> None:
    async def steps() -> None:
        await mw._register_service()
        await mw._register_exposures()

    asyncio.run(steps())


def _uri(value: str) -> Literal:
    """An endpoint as the OGM writes it: a string literal."""
    return Literal(value, datatype=XSD.string)


def _parts(mw: SemanticMiddleware) -> dict[IRI, set[tuple]]:
    """The four parts of each exposure, by the IRI of its Workflow or StateProperty: its link
    to the Service, its endpoint, the Capability's link to it, the resource's link to the
    Capability."""
    parts: dict[IRI, set[tuple]] = {}
    for e in mw._exposures:
        parts[e.iri] = {
            MemoryStore._triple((e.iri, _OWNER[e.kind], IRI(str(mw.service_iri)))),
            MemoryStore._triple((e.iri, SVC.hasEndpoint, _uri(e.endpoint))),
            MemoryStore._triple((e.capability, _LINK[e.kind], e.iri)),
            MemoryStore._triple((RESOURCE, CFC.hasCapability, e.capability)),
        }
    return parts


def _written(update: dict) -> set[tuple]:
    return {MemoryStore._triple(t) for t in update["new_triples"]}


def _assert_each_exposure_in_one_commit(memory: MemoryStore, mw: SemanticMiddleware) -> None:
    """Every part of an exposure is in the graph, and the commits that wrote any of them wrote
    all of them: exactly one commit per exposure."""
    for iri, parts in _parts(mw).items():
        for part in parts:
            assert part in memory.graph, (iri, part)
        carrying = [u for u in memory.updates if _written(u) & parts]
        assert len(carrying) == 1, (iri, len(carrying))
        assert parts <= _written(carrying[0]), iri


def _graph(memory: MemoryStore) -> set:
    return {t for t in memory.graph if t[1] != URIRef(str(SVC.hasLastHeartbeat))}


# --------------------------------------------------------------------------- #
# A first start.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exposes",
    [
        pytest.param({"workflows": ("run",)}, id="workflow"),
        pytest.param({"states": ("status",)}, id="state"),
        pytest.param({"workflows": ("run", "stop"), "states": ("status",)}, id="several"),
    ],
)
def test_a_first_start_writes_each_exposure_and_its_capability_in_one_commit(memory, exposes):
    mw = _instance(OGM(db=memory.store), **exposes)

    _start(mw)

    _assert_each_exposure_in_one_commit(memory, mw)


def test_a_workflow_and_a_state_that_share_a_capability_arrive_in_one_commit(memory):
    mw = _instance(OGM(db=memory.store), workflows=("run",), states=("run",))

    _start(mw)

    parts = set().union(*_parts(mw).values())
    carrying = [u for u in memory.updates if _written(u) & parts]
    assert len(carrying) == 1
    assert parts <= _written(carrying[0])


@pytest.mark.parametrize("kind", [WORKFLOW, STATE_PROPERTY])
def test_the_public_writers_write_one_exposure_in_one_commit(memory, kind):
    ogm = OGM(db=memory.store)
    address = f"http://127.0.0.1:{PORT}"
    service = mint_service_iri(RESOURCE, address)
    memory.add(service, RDF.type, URIRef(SERVICE_CLASS))
    memory.add(service, SVC.isServiceOf, URIRef(RESOURCE))
    capability = mint_capability_iri(RESOURCE, "run")
    if kind == WORKFLOW:
        item = mint_workflow_iri(service, "run")
        endpoint = f"{address}/workflows/run/execute"
        register_workflow(
            ogm,
            resource_iri=RESOURCE,
            service_iri=service,
            workflow_iri=item,
            workflow_class=WORKFLOW_CLASS,
            capability_iri=capability,
            capability_class=CAPABILITY_CLASS,
            endpoint=endpoint,
        )
    else:
        item = mint_state_property_iri(service, "run")
        endpoint = f"{address}/state/run"
        register_state_property(
            ogm,
            resource_iri=RESOURCE,
            service_iri=service,
            state_property_iri=item,
            state_property_class=STATE_CLASS,
            capability_iri=capability,
            capability_class=CAPABILITY_CLASS,
            endpoint=endpoint,
        )

    (update,) = memory.updates
    assert {
        MemoryStore._triple((item, _OWNER[kind], service)),
        MemoryStore._triple((item, SVC.hasEndpoint, _uri(endpoint))),
        MemoryStore._triple((capability, _LINK[kind], item)),
        MemoryStore._triple((RESOURCE, CFC.hasCapability, capability)),
    } <= _written(update)


# --------------------------------------------------------------------------- #
# Restarts and an earlier run's parts.
# --------------------------------------------------------------------------- #


def test_a_restart_writes_each_exposure_in_one_commit_and_leaves_the_graph_as_it_was(memory):
    ogm = OGM(db=memory.store)
    _start(_instance(ogm, workflows=("run",), states=("status",)))
    before = _graph(memory)
    memory.updates.clear()

    restarted = _instance(ogm, workflows=("run",), states=("status",))
    _start(restarted)

    assert _graph(memory) == before
    for parts in _parts(restarted).values():
        # Each exposure's parts are in the graph already, so its one commit changes none of
        # them, and carries the resource's link to the Capability again. No other commit
        # touches a part.
        touching = [
            u
            for u in memory.updates
            if (_written(u) | {MemoryStore._triple(t) for t in u["old_triples"]}) & parts
        ]
        assert len(touching) == 1
        assert _written(touching[0]) & parts == {
            p for p in parts if p[1] == URIRef(str(CFC.hasCapability))
        }


def test_a_restart_after_a_clean_stop_writes_each_endpoint_back_with_its_capability(memory):
    ogm = OGM(db=memory.store)
    first = _instance(ogm, workflows=("run",), states=("status",))
    _start(first)
    before = _graph(memory)
    deregister_service(ogm, first.service_iri)
    memory.updates.clear()

    restarted = _instance(ogm, workflows=("run",), states=("status",))
    _start(restarted)

    assert _graph(memory) == before
    for e in restarted._exposures:
        endpoint = MemoryStore._triple((e.iri, SVC.hasEndpoint, _uri(e.endpoint)))
        (carrying,) = [u for u in memory.updates if endpoint in _written(u)]
        assert MemoryStore._triple((RESOURCE, CFC.hasCapability, e.capability)) in _written(
            carrying
        )


def _remove_about(memory: MemoryStore, subject: IRI) -> None:
    memory.graph.remove((URIRef(str(subject)), None, None))


@pytest.mark.parametrize(
    "left", ["the workflow only", "the capability only", "the workflow without its endpoint"]
)
def test_a_start_that_finds_part_of_an_exposure_completes_it_in_one_commit(memory, left):
    ogm = OGM(db=memory.store)
    first = _instance(ogm, workflows=("run",))
    _start(first)
    complete = _graph(memory)
    (exposure,) = first._exposures
    if left == "the workflow only":
        _remove_about(memory, exposure.capability)
        memory.graph.remove(
            (URIRef(RESOURCE), URIRef(str(CFC.hasCapability)), URIRef(exposure.capability))
        )
    elif left == "the capability only":
        _remove_about(memory, exposure.iri)
    else:
        memory.graph.remove((URIRef(exposure.iri), URIRef(str(SVC.hasEndpoint)), None))
    memory.updates.clear()

    restarted = _instance(ogm, workflows=("run",))
    _start(restarted)

    assert _graph(memory) == complete
    (parts,) = _parts(restarted).values()
    carrying = [u for u in memory.updates if _written(u) & parts]
    assert len(carrying) == 1


# --------------------------------------------------------------------------- #
# A refused commit.
# --------------------------------------------------------------------------- #


def test_a_commit_the_store_refuses_leaves_none_of_the_parts(memory):
    ogm = OGM(db=memory.store)
    mw = _instance(ogm, workflows=("run",))

    def refuse(arguments: dict) -> None:
        if any(str(p) == str(SVC.realizedByWorkflow) for _, p, _ in arguments["new_triples"]):
            raise Refused("refused")

    memory.before_update = refuse
    with pytest.raises(PreconditionFailedError):
        _start(mw)

    for parts in _parts(mw).values():
        for part in parts:
            assert part not in memory.graph
    (exposure,) = mw._exposures
    assert (URIRef(exposure.iri), None, None) not in memory.graph
    assert (URIRef(exposure.capability), None, None) not in memory.graph


# --------------------------------------------------------------------------- #
# A reader between every two commits.
# --------------------------------------------------------------------------- #


def _read(memory: MemoryStore, ogm: OGM, mw: SemanticMiddleware, seen: list[str]) -> None:
    """What a reader sees now: every Workflow of ``mw`` with an endpoint has its Capability and
    the resource's link, and the resolution of the Capability class finds this instance's
    Service or no Workflow at all."""
    for e in mw._exposures:
        has_endpoint = (URIRef(e.iri), URIRef(str(SVC.hasEndpoint)), None) in memory.graph
        if has_endpoint:
            assert (URIRef(e.capability), URIRef(str(_LINK[e.kind])), URIRef(e.iri)) in memory.graph
            assert (
                URIRef(RESOURCE),
                URIRef(str(CFC.hasCapability)),
                URIRef(e.capability),
            ) in memory.graph
    try:
        _, service, _ = resolve_dispatch_target(ogm, CAPABILITY_CLASS)
    except (CapabilityNotRealizedError, OperationReceiverUnavailableError) as error:
        # No Workflow of this instance is reachable yet: none of them has an endpoint either.
        assert not any(
            (URIRef(e.iri), URIRef(str(SVC.hasEndpoint)), None) in memory.graph
            for e in mw._exposures
            if e.kind == WORKFLOW
        ), error
        seen.append("none")
        return
    assert service == mw.service_iri
    seen.append("found")


@pytest.mark.parametrize("restart", [False, True], ids=["first start", "restart"])
def test_a_reader_between_any_two_commits_never_finds_a_workflow_without_its_capability(
    memory, restart
):
    ogm = OGM(db=memory.store)
    if restart:
        earlier = _instance(ogm, workflows=("run", "stop"), states=("status",))
        _start(earlier)
        deregister_service(ogm, earlier.service_iri)
    mw = _instance(ogm, workflows=("run", "stop"), states=("status",))
    reader = OGM(db=memory.store)
    seen: list[str] = []
    memory.before_update = lambda _: _read(memory, reader, mw, seen)

    _start(mw)
    memory.before_update = None
    _read(memory, reader, mw, seen)

    # A read before each commit of the start, and one after the last.
    assert len(seen) >= 3
    assert seen[-1] == "found"
