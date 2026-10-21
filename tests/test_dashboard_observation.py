"""The dashboard shows what the instance has seen of its connections and of its graph binding.

HELD DATA and CONNECTORS show each value's connection state and the age of its last sync, and the
header shows the graph binding's last read, last write and last error. The boxes read the public
records, `mw.connections` and `mw.graph_binding`, from memory. `enable_dashboard(mw, dev=False)`
leaves out what is connection metadata: the MQTT topic, the list of pruned properties, and an
error's message.

The instance is a real `SemanticMiddleware` on a fake OGM and store, with its wiring planned by a
stand-in for `plan_wiring`, so the boxes read what a running instance holds.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, List

import pytest
from fastapi.testclient import TestClient
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import TripleStoreUnavailableError
from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection

from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware import middleware as middleware_module
from kapps_semantic_middleware.connectors.semantic import Registration
from kapps_semantic_middleware.dashboard import enable_dashboard
from kapps_semantic_middleware.vocabulary import INF

EX = "https://example.org/test#"
BELT = IRI(f"{EX}belt")
SPEED = f"{EX}hasSpeed"
TOPIC = "unit1/belt/speed"
BROKER_PROPERTY = f"{EX}hasMQTTBrokerIP"
UNREACHABLE = "http://store.example:7200/datasets/Tests"


class _Store:
    def __init__(self) -> None:
        self.calls: List[str] = []
        self.error: Exception | None = None
        self.dataset = "Tests"

    def query(self, query: Any, update: bool = False, convert_bindings: bool = False) -> Any:
        self.calls.append("update" if update else "query")
        if self.error is not None:
            raise self.error
        return {"results": {"bindings": []}}

    def triples_add(self, triples: Any, named_graph: Any = None) -> None:
        self.calls.append("triples_add")


class _Receiving:
    def __init__(self) -> None:
        self.items: List[Any] = []

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def provide(self) -> Any:
        return None

    async def consume(self, body: Any) -> None: ...

    async def receive(self):
        for item in self.items:
            yield item


def _spec(properties: dict) -> Any:
    return SimpleNamespace(properties=properties)


def _plan(connector: Any) -> Any:
    """One parameter on one belt, bound over MQTT, as `plan_wiring` would plan it."""
    binding = SimpleNamespace(
        resource_iri=BELT,
        field_id="hasSpeed_lined",
        parameter_property=SPEED,
        access_mode="READ",
        metadata={str(INF.hasMQTTTopic): [TOPIC]},
        descriptor=SimpleNamespace(interface_property=None),
    )
    registration = Registration(
        connector=connector,
        sync_direction=SyncDirection.TO_PERSISTENCE,
        model_type=float,
    )
    leaf = _spec({str(INF.accessMode): object()})
    return SimpleNamespace(
        class_scope=None,
        northbound_spec=_spec({SPEED: SimpleNamespace(nested=leaf)}),
        bindings=[binding],
        registrations=[(binding, registration)],
        southbound_by_property={SPEED: frozenset({BROKER_PROPERTY})},
    )


@pytest.fixture
def unit(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    store = _Store()
    connector = _Receiving()
    monkeypatch.setattr(middleware_module, "plan_wiring", lambda **_: _plan(connector))
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=f"{EX}unit",
        service_class=f"{EX}UnitService",
        ogm=SimpleNamespace(db=store),
        heartbeat_interval=None,
        class_scope=object(),
    )
    (connector_id,) = mw.connection_registry.connectors
    handed = mw.connection_registry.connectors[connector_id]
    return SimpleNamespace(mw=mw, store=store, connector=connector, handed=handed)


def _box(mw: Any, box: str) -> str:
    response = TestClient(mw.app).get(f"/dashboard/box/{box}")
    assert response.status_code == 200, response.text
    return response.text


def _receive(unit: SimpleNamespace, *items: Any) -> None:
    unit.connector.items = list(items)

    async def drain() -> None:
        async for _ in unit.handed.receive():
            pass

    asyncio.run(drain())


# --- the header --------------------------------------------------------------------------------


def test_the_header_shows_that_the_binding_has_seen_nothing_yet(unit: SimpleNamespace) -> None:
    """Nothing written and nothing failed: the header says so rather than leave a gap."""
    enable_dashboard(unit.mw)

    header = _box(unit.mw, "header")

    assert "Last read" in header and "Last write" in header and "Last error" in header
    assert "none since the instance started" in header


def test_the_header_shows_the_last_read_write_and_error(unit: SimpleNamespace) -> None:
    enable_dashboard(unit.mw)
    unit.mw.ogm.db.query("INSERT DATA {}", update=True)
    unit.store.error = TripleStoreUnavailableError(UNREACHABLE)
    with pytest.raises(TripleStoreUnavailableError):
        unit.mw.ogm.db.query("SELECT * {}")

    header = _box(unit.mw, "header")

    assert "s ago" in header
    assert "TripleStoreUnavailableError" in header
    assert UNREACHABLE in header, "a development page shows the error's message"


def test_without_dev_the_header_shows_the_error_s_type_and_not_its_message(
    unit: SimpleNamespace,
) -> None:
    enable_dashboard(unit.mw, dev=False)
    unit.store.error = TripleStoreUnavailableError(UNREACHABLE)
    with pytest.raises(TripleStoreUnavailableError):
        unit.mw.ogm.db.query("SELECT * {}")

    header = _box(unit.mw, "header")

    assert "TripleStoreUnavailableError" in header
    assert "store.example" not in header


# --- the connectors ----------------------------------------------------------------------------


def test_the_connectors_box_shows_the_state_and_the_last_sync(unit: SimpleNamespace) -> None:
    enable_dashboard(unit.mw)
    before = _box(unit.mw, "connectors")
    assert "never connected" in before
    assert "no sync since the instance started" in before

    asyncio.run(unit.handed.connect())
    _receive(unit, 1.5)
    after = _box(unit.mw, "connectors")

    assert "connected" in after and "never connected" not in after
    assert "s ago" in after


def test_the_connectors_box_shows_the_last_error(unit: SimpleNamespace) -> None:
    enable_dashboard(unit.mw)
    _receive(unit, 1.5)

    async def broken():
        raise ConnectionResetError("broker 10.0.0.5:1883 went away")
        yield  # pragma: no cover - makes this an async generator

    unit.connector.receive = broken
    with pytest.raises(ConnectionResetError):
        _receive_raw(unit)

    box = _box(unit.mw, "connectors")
    assert "ConnectionResetError" in box
    assert "10.0.0.5" in box


def _receive_raw(unit: SimpleNamespace) -> None:
    async def drain() -> None:
        async for _ in unit.handed.receive():
            pass

    asyncio.run(drain())


# --- held data ---------------------------------------------------------------------------------


def test_held_data_shows_each_value_s_state_and_last_sync(unit: SimpleNamespace) -> None:
    enable_dashboard(unit.mw)
    asyncio.run(unit.handed.connect())
    _receive(unit, 1.5)

    held = _box(unit.mw, "held-data")

    assert "<th>State</th>" in held and "<th>Last sync</th>" in held
    assert "connected" in held
    assert "s ago" in held
    assert TOPIC in held
    assert "hasMQTTBrokerIP" in held


def test_without_dev_held_data_shows_no_topic_and_no_pruned_property(
    unit: SimpleNamespace,
) -> None:
    enable_dashboard(unit.mw, dev=False)

    held = _box(unit.mw, "held-data")

    assert "hasSpeed" in held, "the parameter itself is shown"
    assert "1 pruned" in held, "the count of what the projection stripped is shown"
    assert TOPIC not in held
    assert "MQTT topic" not in held
    assert "hasMQTTBrokerIP" not in held


def test_the_registry_says_whether_it_is_a_development_page(unit: SimpleNamespace) -> None:
    assert enable_dashboard(unit.mw).dev is True


def test_rendering_the_boxes_makes_no_store_call(unit: SimpleNamespace) -> None:
    """Nothing in the record path reaches the graph. The page's name is read at enable, once."""
    enable_dashboard(unit.mw)
    unit.store.calls.clear()

    for box in ("header", "held-data", "connectors"):
        _box(unit.mw, box)

    assert unit.store.calls == []
