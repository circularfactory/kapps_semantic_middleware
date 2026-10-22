"""The instance observes its connections and its graph binding, and nothing in that writes.

A connector is observed by the wrapper that the instance hands to the framework in its place,
and the graph binding by the observed store on its OGM. Both are faked here at the protocol they
serve: a connector with `connect`, `disconnect`, `provide`, `consume` and maybe `receive`, and a
store with the methods of the triple store interface. The records are read through the public
accessors, `mw.connections` and `mw.graph_binding`, as a dashboard box or an exporter reads them.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import patch

import pytest
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import (
    PreconditionFailedError,
    ShaclValidationError,
    TripleStoreResponseError,
    TripleStoreUnavailableError,
)
from transitional_sync_middleware.connect.connectors.async_connector import Receiver
from transitional_sync_middleware.connect.connectors.connector import Consumer, Provider
from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware import middleware as middleware_module
from kapps_semantic_middleware.connectors.semantic import Registration
from kapps_semantic_middleware.observation import (
    CallError,
    ConnectionRecord,
    GraphBindingRecord,
    _connection_record,
    _ConnectorObservation,
    _observed_connector,
    is_connection_metadata,
)

EX = "http://example.org/test#"
RESOURCE = f"{EX}module"
SERVICE_CLASS = f"{EX}ModuleService"


class _Store:
    """A store that logs each call, and raises `error` when it is set."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.error: Exception | None = None
        self.dataset = "Tests"

    def _call(self, name: str) -> Any:
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        return name

    def query(self, query: Any, update: bool = False, convert_bindings: bool = False) -> Any:
        return self._call("query")

    def triples_get(self, **kwargs: Any) -> Any:
        return self._call("triples_get")

    def triples_add(self, triples: Any, named_graph: Any = None) -> Any:
        return self._call("triples_add")

    def triple_delete(self, triple: Any, named_graph: Any = None) -> Any:
        return self._call("triple_delete")

    def close(self) -> None:
        self.calls.append("close")


class _OGM:
    """An OGM whose own methods reach the store, as the real one's do."""

    def __init__(self, store: Any) -> None:
        self.db = store

    def fetch(self, **kwargs: Any) -> Any:
        return self.db.triples_get(**kwargs)

    def commit(self, **kwargs: Any) -> Any:
        return self.db.query("INSERT DATA {}", update=True)


def _middleware(ogm: Any, resource: str = RESOURCE, **options: Any) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=resource,
        service_class=SERVICE_CLASS,
        ogm=ogm,
        heartbeat_interval=None,
        **options,
    )


# --- the graph binding --------------------------------------------------------------------------


def test_a_read_stamps_the_last_read_and_returns_what_the_store_returned() -> None:
    store = _Store()
    mw = _middleware(_OGM(store))

    assert mw.ogm.db.query("SELECT * {}") == "query"

    binding = mw.graph_binding
    assert binding.last_read is not None
    assert binding.last_write is None
    assert binding.last_error is None


@pytest.mark.parametrize(
    "write",
    [
        lambda db: db.query("INSERT DATA {}", update=True),
        lambda db: db.query("INSERT DATA {}", True),
        lambda db: db.triples_add([]),
        lambda db: db.triple_delete(("s", "p", "o")),
    ],
    ids=["update keyword", "update positional", "triples_add", "triple_delete"],
)
def test_a_write_stamps_the_last_write(write: Any) -> None:
    mw = _middleware(_OGM(_Store()))

    write(mw.ogm.db)

    assert mw.graph_binding.last_write is not None
    assert mw.graph_binding.last_read is None


def test_the_ogm_s_own_calls_are_observed() -> None:
    """The OGM writes and reads through its `db`, so its calls reach the observed store."""
    mw = _middleware(_OGM(_Store()))

    mw.ogm.fetch(instance_iri="x")
    mw.ogm.commit(changes=[])

    assert mw.graph_binding.last_read is not None
    assert mw.graph_binding.last_write is not None


def test_a_store_that_cannot_be_reached_is_the_binding_s_last_error() -> None:
    store = _Store()
    mw = _middleware(_OGM(store))
    error = TripleStoreUnavailableError("http://store.example:7200/datasets/Tests")
    store.error = error

    with pytest.raises(TripleStoreUnavailableError) as raised:
        mw.ogm.db.query("SELECT * {}")

    assert raised.value is error, "the caller gets the store's error itself"
    last = mw.graph_binding.last_error
    assert last is not None
    assert last.type == "TripleStoreUnavailableError"
    assert "store.example:7200" in last.message
    assert mw.graph_binding.last_read is None


@pytest.mark.parametrize(
    "answer",
    [
        ShaclValidationError("refused", report="", status=500),
        PreconditionFailedError("changed"),
    ],
    ids=["refused by a shape", "false precondition"],
)
def test_a_refusal_is_an_answer_and_no_error_of_the_binding(answer: Exception) -> None:
    """The store answered: the binding works. The refusal shows in Refused writes."""
    store = _Store()
    mw = _middleware(_OGM(store))
    store.error = answer

    with pytest.raises(type(answer)):
        mw.ogm.db.query("INSERT DATA {}", update=True)

    assert mw.graph_binding.last_error is None


def test_a_server_error_of_the_store_is_an_error_of_the_binding() -> None:
    store = _Store()
    mw = _middleware(_OGM(store))
    store.error = TripleStoreResponseError("internal", status=503)

    with pytest.raises(TripleStoreResponseError):
        mw.ogm.db.query("SELECT * {}")

    assert mw.graph_binding.last_error is not None


def test_attributes_pass_through_the_observed_store() -> None:
    store = _Store()
    mw = _middleware(_OGM(store))

    assert mw.ogm.db.dataset == "Tests"
    mw.ogm.db.dataset = "Other"
    assert store.dataset == "Other", "an attribute set on the observed store lands on the store"


def test_a_patch_on_the_observed_store_is_undone_on_the_store() -> None:
    store = _Store()
    mw = _middleware(_OGM(store))

    with patch.object(mw.ogm.db, "dataset", "Patched"), patch.object(mw.ogm.db, "query") as query:
        assert store.dataset == "Patched"
        assert store.query is query

    assert store.dataset == "Tests"
    assert "query" not in vars(store), "the patched method is gone, and the store's own is back"
    assert mw.ogm.db.dataset == "Tests"


def test_two_instances_on_one_ogm_share_the_binding_and_each_names_itself() -> None:
    """The binding is the store's, so both see its traffic; the IRIs are each instance's own."""
    ogm = _OGM(_Store())
    first = _middleware(ogm, resource=f"{EX}module1")
    observed = ogm.db
    second = _middleware(ogm, resource=f"{EX}module2")

    assert ogm.db is observed, "the store is observed once, not once per instance"
    second.ogm.db.query("SELECT * {}")

    assert first.graph_binding.last_read == second.graph_binding.last_read
    assert first.graph_binding.resource == IRI(f"{EX}module1")
    assert second.graph_binding.resource == IRI(f"{EX}module2")
    assert second.graph_binding.service == second.service_iri


def test_the_record_names_the_dataset_and_the_named_graph() -> None:
    mw = _middleware(_OGM(_Store()), named_graph=f"{EX}graph")

    binding = mw.graph_binding
    assert isinstance(binding, GraphBindingRecord)
    assert binding.dataset == "Tests"
    assert binding.named_graph == IRI(f"{EX}graph")


def test_reading_the_records_causes_no_store_call() -> None:
    """Nothing in the record path reaches the graph: no write, and no read either."""
    store = _Store()
    mw = _middleware(_OGM(store))
    store.calls.clear()

    mw.graph_binding
    mw.connections

    assert store.calls == []


def test_an_ogm_without_a_store_has_an_unobserved_binding() -> None:
    """A fake OGM with no `db` is left alone, and the record says nothing was seen."""
    ogm = SimpleNamespace(fetch=lambda **_: None, commit=lambda **_: [])
    mw = _middleware(ogm)

    binding = mw.graph_binding
    assert binding.last_read is None and binding.last_write is None
    assert binding.dataset is None


# --- the connectors -----------------------------------------------------------------------------


class _Connector:
    """A connector that provides and consumes, as TSM's `Connector` protocol asks."""

    def __init__(self) -> None:
        self.value = 1
        self.consumed: List[Any] = []
        self.error: Exception | None = None

    async def connect(self) -> None:
        if self.error is not None:
            raise self.error

    async def disconnect(self) -> None:
        return None

    async def provide(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.value

    async def consume(self, body: Any) -> None:
        if self.error is not None:
            raise self.error
        self.consumed.append(body)


class _Receiving(_Connector):
    """A connector that also receives, as TSM's `AsyncConnector` does."""

    def __init__(self, items: List[Any]) -> None:
        super().__init__()
        self.items = items
        self.closed = False

    async def receive(self):
        try:
            for item in self.items:
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            self.closed = True


class _ConsumeOnly:
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def consume(self, body: Any) -> None: ...


@pytest.mark.parametrize(
    "inner",
    [
        _Connector(),
        _Receiving([]),
        _ConsumeOnly(),
    ],
    ids=["connector", "receiver", "consumer only"],
)
def test_the_wrapper_has_the_protocols_of_the_connector_it_wraps(inner: Any) -> None:
    """TSM decides what to run by `isinstance` against its protocols, so the wrapper must
    answer as the connector does: a receive loop on a connector without `receive` dies."""
    wrapper = _observed_connector(inner, _ConnectorObservation())

    for protocol in (Provider, Consumer, Receiver):
        assert isinstance(wrapper, protocol) == isinstance(inner, protocol), protocol.__name__


def test_the_wrapper_passes_other_attributes_through() -> None:
    inner = _Connector()
    wrapper = _observed_connector(inner, _ConnectorObservation())

    assert wrapper.value == 1


def test_provide_and_consume_are_stamped_and_pass_values_through() -> None:
    inner = _Connector()
    observation = _ConnectorObservation()
    wrapper = _observed_connector(inner, observation)

    assert asyncio.run(wrapper.provide()) == 1
    asyncio.run(wrapper.consume({"v": 2}))

    seen = observation.seen()
    assert inner.consumed == [{"v": 2}]
    assert seen.last_provide is not None
    assert seen.last_consume is not None
    assert seen.last_receive is None


def test_connect_and_disconnect_set_the_connection_state() -> None:
    inner = _Connector()
    observation = _ConnectorObservation()
    wrapper = _observed_connector(inner, observation)
    assert observation.seen().connected is None, "never asked to connect"

    asyncio.run(wrapper.connect())
    assert observation.seen().connected is True

    asyncio.run(wrapper.disconnect())
    assert observation.seen().connected is False


def test_a_failed_connect_is_recorded_and_raised_unchanged() -> None:
    inner = _Connector()
    inner.error = ConnectionRefusedError("broker at 10.0.0.5:1883")
    observation = _ConnectorObservation()
    wrapper = _observed_connector(inner, observation)

    with pytest.raises(ConnectionRefusedError) as raised:
        asyncio.run(wrapper.connect())

    assert raised.value is inner.error
    seen = observation.seen()
    assert seen.connected is False
    assert seen.last_error is not None and seen.last_error.type == "ConnectionRefusedError"


def test_each_received_item_is_stamped() -> None:
    inner = _Receiving(["a", "b"])
    observation = _ConnectorObservation()
    wrapper = _observed_connector(inner, observation)

    async def drain() -> List[Any]:
        return [item async for item in wrapper.receive()]

    assert asyncio.run(drain()) == ["a", "b"]
    assert observation.seen().last_receive is not None
    assert inner.closed


def test_an_error_in_receive_is_recorded_and_raised_unchanged() -> None:
    error = RuntimeError("lost the broker")
    inner = _Receiving(["a", error])
    observation = _ConnectorObservation()
    wrapper = _observed_connector(inner, observation)

    async def drain() -> None:
        async for _ in wrapper.receive():
            pass

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(drain())

    assert raised.value is error
    assert observation.seen().last_error is not None


def test_a_cancelled_receive_is_no_error() -> None:
    inner = _Receiving(["a", asyncio.CancelledError()])
    observation = _ConnectorObservation()
    wrapper = _observed_connector(inner, observation)

    async def drain() -> None:
        async for _ in wrapper.receive():
            pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(drain())

    assert observation.seen().last_error is None


def test_the_last_sync_is_the_newer_of_the_last_receive_and_the_last_consume() -> None:
    """`provide` reads no device for a pushing connector, so it is no sync."""
    inner = _Receiving(["a"])
    observation = _ConnectorObservation()
    wrapper = _observed_connector(inner, observation)
    record = _record_of(inner, observation)
    asyncio.run(wrapper.provide())
    assert record().last_sync is None

    async def drain() -> None:
        async for _ in wrapper.receive():
            pass

    asyncio.run(drain())
    assert record().last_sync == record().last_receive


def _record_of(connector: Any, observation: _ConnectorObservation) -> Any:
    """A reader of the record of ``connector``, as the instance makes it."""
    binding = SimpleNamespace(resource_iri=IRI(f"{EX}belt"), field_id="speed")
    registration = SimpleNamespace(connector=connector, sync_direction=SyncDirection.TO_PERSISTENCE)
    return lambda: _connection_record(
        resource=None,
        service=None,
        connector_id="belt#speed#",
        binding=binding,
        registration=registration,
        observation=observation,
    )


def test_anything_that_is_no_record_has_no_connection_metadata() -> None:
    assert is_connection_metadata(object(), "message") is False


# --- the instance ------------------------------------------------------------------------------


def _binding(field: str) -> Any:
    return SimpleNamespace(resource_iri=IRI(f"{EX}belt"), field_id=field)


def _plan(registrations: List[Any]) -> Any:
    return SimpleNamespace(registrations=registrations, bindings=[b for b, _ in registrations])


def test_the_instance_hands_the_framework_the_wrapper_and_records_each_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connectors = [_Connector(), _Receiving([])]
    registrations = [
        (
            _binding(f"field{i}"),
            Registration(
                connector=connector,
                sync_direction=SyncDirection.TO_PERSISTENCE,
                model_type=int,
                suffix=str(i),
            ),
        )
        for i, connector in enumerate(connectors)
    ]
    monkeypatch.setattr(middleware_module, "plan_wiring", lambda **_: _plan(registrations))

    mw = _middleware(_OGM(_Store()), class_scope=object())

    handed = [
        mw.connection_registry.connectors[connector_id]
        for connector_id in mw.connection_registry.connectors
    ]
    assert [h is c for h, c in zip(handed, connectors)] == [False, False]
    assert [h.value for h in handed] == [1, 1]

    records = mw.connections
    assert [type(r) for r in records] == [ConnectionRecord, ConnectionRecord]
    assert [r.field for r in records] == ["field0", "field1"]
    assert [r.connector for r in records] == ["_Connector", "_Receiving"]
    assert {r.resource for r in records} == {IRI(RESOURCE)}
    assert {r.service for r in records} == {mw.service_iri}
    assert records[0].holder == IRI(f"{EX}belt")
    assert records[0].connected is None

    asyncio.run(handed[0].provide())
    assert mw.connections[0].last_provide is not None
    assert mw.connections[1].last_provide is None


def test_an_instance_without_wiring_has_no_connections() -> None:
    mw = _middleware(_OGM(_Store()))

    assert mw.connections == []


# --- the records -------------------------------------------------------------------------------


def test_the_error_message_is_connection_metadata_and_nothing_else_of_a_record_is() -> None:
    """An error message may name a host, a port, a topic or a URL. The rest names no address."""
    marked = {
        (record.__name__, f.name)
        for record in (ConnectionRecord, GraphBindingRecord, CallError)
        for f in dataclasses.fields(record)
        if is_connection_metadata(record, f.name)
    }

    assert marked == {("CallError", "message")}


def test_the_records_are_frozen() -> None:
    mw = _middleware(_OGM(_Store()))

    with pytest.raises(dataclasses.FrozenInstanceError):
        mw.graph_binding.last_read = None  # type: ignore[misc]
