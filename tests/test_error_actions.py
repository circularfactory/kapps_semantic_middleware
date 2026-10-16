"""Each error type that the middleware raises tells one action, offline.

An error asks the consumer for one of five actions: Repair, Retry, Refresh, Refused or Fix the
call. ``OperationResolutionError`` and ``NotificationUndelivered`` asked for two each, so each
gets a subclass per action. The conditions of a broken deployment raise a subtype of
``DeploymentError`` (Repair), in place of a bare ``ValueError`` or ``RuntimeError``. Each new type
also subclasses the type that it replaces, so an ``except`` written against the old type still
catches it, and each message stays as it was. Wrong use of the API keeps the built-ins (Fix the
call), and is not tested here.

The stores are faked. Nothing reaches a triple store.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import pytest
from kapps_ogm import Node
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware import Mode, SemanticMiddleware, credentials, errors, examples_cli
from kapps_semantic_middleware.connectors.wiring import class_of
from kapps_semantic_middleware.credentials import REQUIRED_TRIPLESTORE_ENV, endpoint_for
from kapps_semantic_middleware.errors import (
    CapabilityNotRealizedError,
    DeploymentError,
    DeploymentRuntimeError,
    DeploymentValueError,
    NotificationReceiverAmbiguousError,
    NotificationReceiverUnavailableError,
    NotificationUndelivered,
    OperationReceiverUnavailableError,
    OperationResolutionError,
)
from kapps_semantic_middleware.possession import possession_state
from kapps_semantic_middleware.registration import (
    resolve_dispatch_target,
    resolve_operation_workflow,
)
from kapps_semantic_middleware.vocabulary import CFC, SVC

RESOURCE = "http://example.org/test#Module1"
RECEIVER = "http://example.org/test#Module2"
SERVICE_CLASS = "http://example.org/test#ModuleService"
CAPABILITY_CLASS = IRI("http://example.org/test#BoxTransportCapability")
OPERATION_CLASS = "http://example.org/test#BoxTransport"
OPERATION = IRI("http://example.org/test#Transport1")
CAPABILITY = IRI("http://example.org/test#Module2_capability_box_transport")
MIDDLEWARE_LOGGER = "kapps_semantic_middleware.middleware"

# Each new type, its parents in order, and the action that its docstring names.
NEW_TYPES = [
    pytest.param(DeploymentError, (Exception,), "Repair", id="DeploymentError"),
    pytest.param(
        DeploymentValueError, (DeploymentError, ValueError), "Repair", id="DeploymentValueError"
    ),
    pytest.param(
        DeploymentRuntimeError,
        (DeploymentError, RuntimeError),
        "Repair",
        id="DeploymentRuntimeError",
    ),
    pytest.param(
        OperationReceiverUnavailableError,
        (OperationResolutionError,),
        "Retry",
        id="OperationReceiverUnavailableError",
    ),
    pytest.param(
        CapabilityNotRealizedError,
        (OperationResolutionError, DeploymentError),
        "Repair",
        id="CapabilityNotRealizedError",
    ),
    pytest.param(
        NotificationReceiverUnavailableError,
        (NotificationUndelivered,),
        "Retry",
        id="NotificationReceiverUnavailableError",
    ),
    pytest.param(
        NotificationReceiverAmbiguousError,
        (NotificationUndelivered, DeploymentError),
        "Repair",
        id="NotificationReceiverAmbiguousError",
    ),
]


@pytest.mark.parametrize("new, parents, action", NEW_TYPES)
def test_a_new_type_has_its_parents(new, parents, action):
    assert new.__bases__ == parents


@pytest.mark.parametrize("new, parents, action", NEW_TYPES)
def test_a_new_types_docstring_names_its_action(new, parents, action):
    assert f"Action: {action}." in (new.__doc__ or "")


@pytest.mark.parametrize("new, parents, action", NEW_TYPES)
def test_a_new_type_is_exported_from_the_errors_module(new, parents, action):
    assert new.__name__ in errors.__all__
    assert new.__module__ == "kapps_semantic_middleware.errors"


# --------------------------------------------------------------------------- #
# The resolution of an Operation: Retry or Repair.
# --------------------------------------------------------------------------- #


class _DispatchStore:
    """``ogm.db`` for a dispatch: whether a Workflow realizes a Capability of the class, and
    whether a Service of that Workflow has an address. It records each query."""

    def __init__(self, *, realized: bool, live: bool) -> None:
        self.realized = realized
        self.live = live
        self.queries: list[str] = []

    def query(self, sparql, convert_bindings=False, update=False):
        assert not update, f"the dispatch wrote to the store: {sparql}"
        self.queries.append(str(sparql))
        assert f"<{CAPABILITY_CLASS}>" in str(sparql)
        if f"<{SVC.address}>" in str(sparql):  # the read of a live receiver
            rows = [{"cap": CAPABILITY, "svc": RECEIVER, "addr": "http://127.0.0.1:9"}]
            return {"results": {"bindings": rows if self.realized and self.live else []}}
        assert str(sparql).lstrip().startswith("ASK"), sparql
        return {"head": {}, "boolean": self.realized}


class _DispatchOGM:
    def __init__(self, *, realized: bool, live: bool) -> None:
        self.db = _DispatchStore(realized=realized, live=live)

    def __getattr__(self, name):
        raise AssertionError(f"the dispatch used the OGM's {name}")


def _issuer(ogm: Any) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=RESOURCE,
        service_class=SERVICE_CLASS,
        ogm=ogm,
        heartbeat_interval=None,
    )


def test_a_capability_that_a_workflow_realizes_but_no_live_service_takes_raises_retry():
    ogm = _DispatchOGM(realized=True, live=False)
    ran = []

    with pytest.raises(OperationResolutionError) as raised:
        with _issuer(ogm).issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ):
            ran.append(True)

    assert type(raised.value) is OperationReceiverUnavailableError
    assert "cannot be resolved to any reachable Service" in str(raised.value)
    assert ran == []


def test_a_capability_that_no_workflow_realizes_raises_repair():
    ogm = _DispatchOGM(realized=False, live=False)
    ran = []

    with pytest.raises(OperationResolutionError) as raised:
        with _issuer(ogm).issue_operation(
            capability_class=CAPABILITY_CLASS, operation_class=OPERATION_CLASS
        ):
            ran.append(True)

    assert type(raised.value) is CapabilityNotRealizedError
    assert isinstance(raised.value, DeploymentError)
    assert "cannot be resolved to any reachable Service" in str(raised.value)
    assert ran == []


def test_the_read_that_tells_retry_from_repair_keeps_the_target_resource():
    ogm = _DispatchOGM(realized=True, live=False)

    with pytest.raises(OperationReceiverUnavailableError):
        resolve_dispatch_target(ogm, CAPABILITY_CLASS, target_resource=IRI(RECEIVER))

    assert len(ogm.db.queries) == 2
    assert all(f"<{SVC.isServiceOf}> <{RECEIVER}>" in query for query in ogm.db.queries)


def test_a_live_receiver_is_found_in_one_read():
    """The read that tells Retry from Repair runs only when no live receiver is found."""
    ogm = _DispatchOGM(realized=True, live=True)

    assert resolve_dispatch_target(ogm, CAPABILITY_CLASS) == (
        CAPABILITY,
        IRI(RECEIVER),
        "http://127.0.0.1:9",
    )
    assert len(ogm.db.queries) == 1


class _ClaimOGM:
    """An OGM whose store finds no Workflow for the Operation's Capability."""

    class _Store:
        def query(self, sparql, convert_bindings=False, update=False):
            assert not update, f"the claim wrote to the store before its read: {sparql}"
            assert f"<{CFC.implementsCapability}>" in sparql
            return {"results": {"bindings": []}}

    def __init__(self) -> None:
        self.db = self._Store()

    def __getattr__(self, name):
        raise AssertionError(f"the claim used the OGM's {name}")


def test_an_operation_whose_capability_no_workflow_realizes_cannot_be_claimed_repair():
    mw = _issuer(_ClaimOGM())
    mw._operation_queue.append(OPERATION)
    ran = []

    with pytest.raises(OperationResolutionError) as raised:
        with mw.claim_next():
            ran.append(True)

    assert type(raised.value) is CapabilityNotRealizedError
    assert "cannot be resolved to any Workflow" in str(raised.value)
    assert ran == []
    assert mw._operation_queue == [OPERATION]  # it stays queued for a later try


def test_the_provenance_resolver_raises_the_same_repair_type():
    with pytest.raises(OperationResolutionError) as raised:
        resolve_operation_workflow(_ClaimOGM(), OPERATION)

    assert type(raised.value) is CapabilityNotRealizedError


# --------------------------------------------------------------------------- #
# A notification: Retry or Repair.
# --------------------------------------------------------------------------- #


class _SenderOGM:
    """The sender's view of the graph: the live Services of the receiver and their addresses."""

    class _Store:
        def __init__(self, addresses: dict) -> None:
            self.addresses = addresses

        def query(self, sparql, convert_bindings=False, update=False):
            assert not update, f"notify() wrote to the store: {sparql}"
            rows = [{"svc": svc, "addr": addr} for svc, addr in self.addresses.items()]
            return {"results": {"bindings": rows}}

    def __init__(self, addresses: dict) -> None:
        self.db = self._Store(addresses)

    def __getattr__(self, name):
        raise AssertionError(f"notify() used the OGM's {name}")


def test_a_resource_with_no_live_service_raises_retry():
    with pytest.raises(NotificationUndelivered, match="no live Service") as raised:
        _issuer(_SenderOGM({})).notify(operation=OPERATION, resource=RECEIVER)

    assert type(raised.value) is NotificationReceiverUnavailableError


def test_a_post_that_the_resource_does_not_take_raises_retry():
    sender = _issuer(_SenderOGM({f"{RECEIVER}_service": "http://127.0.0.1:9"}))

    with pytest.raises(NotificationUndelivered, match="did not take") as raised:
        sender.notify(operation=OPERATION, resource=RECEIVER, timeout=2.0)

    assert type(raised.value) is NotificationReceiverUnavailableError
    assert raised.value.__cause__ is not None


def test_a_resource_with_two_live_services_raises_repair():
    addresses = {f"{RECEIVER}_controller": "http://127.0.0.1:9", f"{RECEIVER}_monitor": "x"}

    with pytest.raises(NotificationUndelivered, match="more than one live Service") as raised:
        _issuer(_SenderOGM(addresses)).notify(operation=OPERATION, resource=RECEIVER)

    assert type(raised.value) is NotificationReceiverAmbiguousError
    assert isinstance(raised.value, DeploymentError)


# --------------------------------------------------------------------------- #
# The conditions of a broken deployment: Repair, never a bare built-in.
# --------------------------------------------------------------------------- #


class _RowsOGM:
    """An OGM whose store answers every SELECT with ``rows``."""

    class _Store:
        def __init__(self, rows: list) -> None:
            self.rows = rows

        def query(self, sparql, convert_bindings=False, update=False):
            assert not update, f"a read wrote to the store: {sparql}"
            return {"results": {"bindings": self.rows}}

    def __init__(self, rows: list) -> None:
        self.db = self._Store(rows)


def _missing_store_variable(monkeypatch) -> None:
    # Each variable, and the deprecated fallback that would stand in for it.
    for name in (*REQUIRED_TRIPLESTORE_ENV, *credentials._FALLBACKS.values()):
        monkeypatch.delenv(name, raising=False)
    endpoint_for("Tests")


def _unknown_mode(monkeypatch) -> None:
    SemanticMiddleware(mode="resourse")


def _incomplete_resource_mode(monkeypatch) -> None:
    SemanticMiddleware(mode=Mode.RESOURCE)


def _incomplete_watchdog_mode(monkeypatch) -> None:
    SemanticMiddleware(mode=Mode.WATCHDOG)


def _several_possession_states(monkeypatch) -> None:
    states = [{"state": f"{RESOURCE}_possession"}, {"state": f"{RESOURCE}_possession_2"}]
    possession_state(_RowsOGM(states), IRI(RESOURCE))


def _no_asserted_type(monkeypatch) -> None:
    only_meta_types = [{"c": "http://www.w3.org/2002/07/owl#NamedIndividual"}]
    class_of(_RowsOGM(only_meta_types), IRI(RESOURCE))


def _bundled_examples_missing(monkeypatch) -> None:
    def no_package(package: str):
        raise ModuleNotFoundError(package)

    monkeypatch.setattr(examples_cli.ir, "files", no_package)
    examples_cli._locate_examples_root()


def _no_live_receiver_realizes_it(monkeypatch) -> None:
    resolve_dispatch_target(_DispatchOGM(realized=False, live=False), CAPABILITY_CLASS)


def _two_live_services(monkeypatch) -> None:
    addresses = {f"{RECEIVER}_controller": "http://127.0.0.1:9", f"{RECEIVER}_monitor": "x"}
    _issuer(_SenderOGM(addresses)).notify(operation=OPERATION, resource=RECEIVER)


# The conditions that raised a bare built-in: each site, the built-in it raised, the type it
# raises now, and a part of its message, which stays as it was.
DEPLOYMENT_SITES = [
    pytest.param(
        _missing_store_variable,
        ValueError,
        DeploymentValueError,
        "Missing triple store environment variable",
        id="missing-store-variable",
    ),
    pytest.param(
        _unknown_mode, ValueError, DeploymentValueError, "mode must be one of", id="unknown-mode"
    ),
    pytest.param(
        _incomplete_resource_mode,
        ValueError,
        DeploymentValueError,
        "resource mode requires: resource_iri, service_class, ogm",
        id="incomplete-resource-mode",
    ),
    pytest.param(
        _incomplete_watchdog_mode,
        ValueError,
        DeploymentValueError,
        "watchdog mode requires: ogm",
        id="incomplete-watchdog-mode",
    ),
    pytest.param(
        _several_possession_states,
        ValueError,
        DeploymentValueError,
        "has 2 possession states",
        id="several-possession-states",
    ),
    pytest.param(
        _no_asserted_type,
        ValueError,
        DeploymentValueError,
        "has no asserted domain rdf:type",
        id="no-asserted-type",
    ),
    pytest.param(
        _bundled_examples_missing,
        RuntimeError,
        DeploymentRuntimeError,
        "could not locate the bundled examples",
        id="bundled-examples-missing",
    ),
]


@pytest.mark.parametrize("site, replaced, new, message", DEPLOYMENT_SITES)
def test_a_deployment_condition_raises_its_repair_type_and_the_old_except_still_catches_it(
    site: Callable[..., None], replaced, new, message, monkeypatch
):
    with pytest.raises(replaced) as raised:
        site(monkeypatch)

    assert type(raised.value) is new
    assert message in str(raised.value)


# Every Repair condition of the middleware's own that a caller can catch.
REPAIR_SITES = [
    *(pytest.param(param.values[0], id=param.id) for param in DEPLOYMENT_SITES),
    pytest.param(_no_live_receiver_realizes_it, id="no-workflow-realizes-the-capability"),
    pytest.param(_two_live_services, id="two-live-services"),
]


@pytest.mark.parametrize("site", REPAIR_SITES)
def test_no_repair_condition_raises_a_bare_value_error_or_runtime_error(site, monkeypatch):
    with pytest.raises(Exception) as raised:
        site(monkeypatch)

    assert type(raised.value) not in (ValueError, RuntimeError)
    assert isinstance(raised.value, DeploymentError)


class _ForeignOperationOGM:
    """An OGM that reads the Operation as implementing a Capability of no workflow here."""

    def fetch(self, **kwargs):
        foreign = IRI("http://example.org/test#WMS_capability_store")
        return Node(id=OPERATION, data={IRI(CFC.implementsCapability): [Node(id=foreign)]})


def test_an_operation_in_the_intake_without_a_handler_logs_a_repair_type(caplog):
    """The intake runs only on an instance with a handler. The error is logged, not raised:
    no caller waits for the intake."""
    mw = _issuer(_ForeignOperationOGM())

    async def deliver():
        mw._take_in(OPERATION)
        while mw._background_tasks:
            await asyncio.wait(set(mw._background_tasks))

    with caplog.at_level(logging.ERROR, logger=MIDDLEWARE_LOGGER):
        asyncio.run(deliver())

    [record] = [r for r in caplog.records if r.exc_info is not None]
    error = record.exc_info[1]
    assert type(error) is DeploymentRuntimeError
    assert isinstance(error, RuntimeError)
    assert "without a notification handler" in str(error)
