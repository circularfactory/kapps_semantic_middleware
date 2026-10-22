"""The IRIs a middleware takes in, and the paths that need its own Resource, offline.

The named graph enters as a string or an `IRI` and is an `IRI` from the constructor on, so a
named graph that is no IRI is refused there. The Resource, the Service class and the Service
exist in resource mode only: a resource-only path that a watchdog reaches raises a
`RuntimeError` that names the mode. Nothing here reaches a triple store; the OGM is faked.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from kapps_ogm.utils.class_scope import ClassScope
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import InvalidIRIError

from kapps_semantic_middleware import Mode, SemanticMiddleware

EX = "http://example.org/test#"
GRAPH = f"{EX}graph"
BOX = IRI(f"{EX}Box")


class _RecordingOGM:
    """Records each commit and the named graph it names, and refuses every other use."""

    def __init__(self) -> None:
        self.graphs: list = []

    def commit(self, *, changes=None, instance_iri=None, data=None, named_graph=None):
        self.graphs.append(named_graph)
        return [SimpleNamespace(id=change.instance_iri) for change in changes or ()]

    def __getattr__(self, name):
        raise AssertionError(f"the middleware used the OGM's {name}")


def _resource(ogm, **kwargs) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode=Mode.RESOURCE,
        resource_iri=f"{EX}detector",
        service_class=f"{EX}DetectorService",
        ogm=ogm,
        heartbeat_interval=None,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# The named graph: converted to an IRI once, where it enters.
# --------------------------------------------------------------------------- #


def test_a_named_graph_given_as_a_string_is_an_iri_from_the_constructor_on():
    mw = _resource(_RecordingOGM(), named_graph=GRAPH)
    assert isinstance(mw.named_graph, IRI)
    assert mw.named_graph == IRI(GRAPH)


def test_a_named_graph_given_as_a_string_reaches_the_commit_as_an_iri():
    ogm = _RecordingOGM()
    mw = _resource(ogm, named_graph=GRAPH)
    with mw.create(BOX, ClassScope()):
        pass
    assert ogm.graphs == [IRI(GRAPH)]
    assert isinstance(ogm.graphs[0], IRI)


def test_no_named_graph_stays_the_default_graph():
    ogm = _RecordingOGM()
    mw = _resource(ogm)
    with mw.create(BOX, ClassScope()):
        pass
    assert mw.named_graph is None
    assert ogm.graphs == [None]


@pytest.mark.parametrize("mode", [Mode.RESOURCE, Mode.WATCHDOG])
def test_a_named_graph_that_is_no_iri_is_refused_at_construction(mode):
    """The store's interface refused it before too, but only at the first write that named
    it, and a watchdog with no stale Service never wrote at all."""
    with pytest.raises(InvalidIRIError):
        SemanticMiddleware(
            mode=mode,
            resource_iri=f"{EX}detector",
            service_class=f"{EX}DetectorService",
            ogm=_RecordingOGM(),
            named_graph="not an iri",
        )


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        (Mode.RESOURCE, "resource mode requires: resource_iri, service_class, ogm"),
        (Mode.WATCHDOG, "watchdog mode requires: ogm"),
    ],
)
def test_a_missing_argument_is_still_the_first_error_beside_a_named_graph_that_is_no_iri(
    mode, message
):
    with pytest.raises(ValueError, match=f"^{message}$"):
        SemanticMiddleware(mode=mode, named_graph="not an iri")


# --------------------------------------------------------------------------- #
# The Resource, the Service class and the Service: resource mode only.
# --------------------------------------------------------------------------- #


def test_resource_mode_still_names_each_missing_argument_in_order():
    with pytest.raises(ValueError, match=r"^resource mode requires: resource_iri, ogm$"):
        SemanticMiddleware(mode=Mode.RESOURCE, service_class=f"{EX}DetectorService")


def test_a_watchdog_has_no_resource_no_service_class_and_no_service():
    watchdog = SemanticMiddleware(mode=Mode.WATCHDOG, ogm=_RecordingOGM())
    assert watchdog.resource is None
    assert (watchdog.resource_iri, watchdog.service_class, watchdog.service_iri) == (
        None,
        None,
        None,
    )


def test_emit_heartbeat_is_resource_mode_only():
    """A watchdog has no Service whose heartbeat it could refresh. Before, the call reached the
    OGM's commit without an instance and failed there, with an error that named no mode."""
    ogm = _RecordingOGM()
    watchdog = SemanticMiddleware(mode=Mode.WATCHDOG, ogm=ogm)
    with pytest.raises(
        RuntimeError,
        match=r"^emit_heartbeat\(\) is only valid in resource mode; current mode is .*watchdog",
    ):
        asyncio.run(watchdog.emit_heartbeat())
    assert ogm.graphs == []
