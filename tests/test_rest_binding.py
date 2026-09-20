"""Unit tests for the REST semantic connector, including the Service-join amendment.

Pure logic plus a mocked HTTP layer. No GraphDB, no network. The seam exists so a binding can
be described and reasoned about without a running middleware or a running peer -- this is the
first consumer of that property for REST, mirroring ``test_semantic_connectors.py``'s split for
MQTT. Live recognition (the Service join itself, against a seeded TransferUnit) is covered in
``test_scenario3_wiring_integration.py``, the same way MQTT recognition is.
"""

from __future__ import annotations

import pytest
from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection
from kapps_triplestore_interface import IRI
from pydantic import create_model

from kapps_semantic_middleware.connectors.rest_binding import (
    RESTBinding,
    RESTParameterConnector,
    RESTParameterFormatter,
    build_parameter_path,
)
from kapps_semantic_middleware.connectors.semantic import ParameterBinding, normalize_metadata
from kapps_semantic_middleware.vocabulary import INF, SVC, AccessMode

UNIT_IRI = "https://example.org/tui#TransferUnit1"
LEFT_BELT_IRI = "https://example.org/tui#ConveyorBelt1_left"
RIGHT_BELT_IRI = "https://example.org/tui#ConveyorBelt1_right"
SPEED = IRI("https://example.org/tu#hasConveyorSpeed")
HAS_BELT = "tu:hasConveyorBelt"
ADDRESS = "http://10.0.0.5:8010"


def _node_model():
    """A node model with field names shaped like what the OGM mangles from the inf: IRIs."""
    fields = {
        INF.hasValue.lined: (list, []),
        INF.accessMode.lined: (list, []),
        IRI("https://example.org/tu#hasUnit").lined: (list, []),
    }
    return create_model("AnonymousClass", **{k: (v[0], v[1]) for k, v in fields.items()})


def _binding(
    access_mode=AccessMode.READWRITE,
    address=ADDRESS,
    root_iri=UNIT_IRI,
    root_class_local_name="TransferUnit",
    path_steps=((HAS_BELT, LEFT_BELT_IRI),),
    resource_iri=LEFT_BELT_IRI,
):
    metadata = {str(INF.accessMode): [access_mode]}
    if address is not None:
        metadata[str(SVC.address)] = [address]
    return ParameterBinding(
        resource_iri=IRI(resource_iri),
        parameter_property=SPEED,
        field_id=SPEED.lined,
        metadata=normalize_metadata(metadata),
        descriptor=RESTBinding,
        node_model_type=_node_model(),
        root_iri=IRI(root_iri) if root_iri is not None else None,
        root_class_local_name=root_class_local_name,
        path_steps=path_steps,
    )


class TestBuildParameterPath:
    """Mirrors rest_router.py's _accumulate_routes shape exactly."""

    def test_matches_the_recursive_router_shape(self):
        path = build_parameter_path(
            "TransferUnit", IRI(UNIT_IRI), [(HAS_BELT, LEFT_BELT_IRI)], SPEED.lined
        )

        expected = f"/TransferUnit/{IRI(UNIT_IRI).lined}/{HAS_BELT}/{IRI(LEFT_BELT_IRI).lined}/{SPEED.lined}"
        assert path == expected

    def test_no_steps_addresses_a_root_level_parameter(self):
        """A parameter hung directly off the root, not a nested component."""
        path = build_parameter_path("TransferUnit", IRI(UNIT_IRI), [], SPEED.lined)

        assert path == f"/TransferUnit/{IRI(UNIT_IRI).lined}/{SPEED.lined}"

    def test_sibling_components_produce_different_paths(self):
        left = build_parameter_path(
            "TransferUnit", IRI(UNIT_IRI), [(HAS_BELT, LEFT_BELT_IRI)], SPEED.lined
        )
        right = build_parameter_path(
            "TransferUnit", IRI(UNIT_IRI), [(HAS_BELT, RIGHT_BELT_IRI)], SPEED.lined
        )

        assert left != right


class TestRESTBindingBuild:
    """A live resource, generically interface-accessible, binds a REST connector."""

    def test_settable_parameter_yields_read_and_write_registrations(self):
        registrations = list(RESTBinding.build(_binding(), SyncDirection.BIDIRECTIONAL))

        assert [r.sync_direction for r in registrations] == [
            SyncDirection.TO_PERSISTENCE,
            SyncDirection.FROM_PERSISTENCE,
        ]
        expected_path = f"/TransferUnit/{IRI(UNIT_IRI).lined}/{HAS_BELT}/{IRI(LEFT_BELT_IRI).lined}/{SPEED.lined}"
        assert all(r.connector.path == expected_path for r in registrations)
        assert all(r.connector.base_url == ADDRESS for r in registrations)

    def test_the_write_leg_does_not_poll(self):
        """The write leg's sync_direction is FROM_PERSISTENCE. A poll loop there would never
        act on anything (SyncedConnector.receive gates on TO_PERSISTENCE/BIDIRECTIONAL) --
        just double the request rate."""
        read, write = RESTBinding.build(_binding(), SyncDirection.BIDIRECTIONAL)

        assert read.connector.poll is True
        assert write.connector.poll is False

    def test_read_only_direction_yields_only_the_read_leg(self):
        registrations = list(RESTBinding.build(_binding(), SyncDirection.TO_PERSISTENCE))

        assert len(registrations) == 1
        assert registrations[0].sync_direction is SyncDirection.TO_PERSISTENCE

    def test_a_resource_with_no_live_service_binds_nothing_and_says_so(self, caplog):
        """A generically interface-accessible parameter whose resource never registered --
        the acceptance criterion this ticket names explicitly."""
        binding = _binding(address=None)

        assert list(RESTBinding.build(binding, SyncDirection.BIDIRECTIONAL)) == []
        assert "no live" in caplog.text
        assert str(SPEED) in caplog.text

    def test_no_root_context_binds_nothing_and_says_so(self, caplog):
        """A ParameterBinding built by hand, outside recognition, carries no root to derive
        a route from. Defensive: wiring.py's _recognise always fills this in."""
        binding = _binding(root_iri=None, root_class_local_name=None)

        assert list(RESTBinding.build(binding, SyncDirection.BIDIRECTIONAL)) == []
        assert "no root" in caplog.text.lower() or "root resource" in caplog.text

    def test_a_root_level_parameter_needs_no_steps(self):
        """resource_iri already is the root -- path_steps is empty."""
        binding = _binding(path_steps=(), resource_iri=UNIT_IRI)

        [registration] = RESTBinding.build(binding, SyncDirection.TO_PERSISTENCE)

        assert registration.connector.path == f"/TransferUnit/{IRI(UNIT_IRI).lined}/{SPEED.lined}"


class TestRESTBindingRegistration:
    """RESTBinding registers at the interface root, the fallback for any protocol."""

    def test_registers_at_the_generic_interface_root(self):
        assert RESTBinding.interface_property == INF.isInterfaceAccessibleParameter

    def test_declares_no_parameter_local_connection_metadata(self):
        """Its evidence is the Service, not the parameter -- so there
        is nothing here for the projection's cross-check to disagree about."""
        assert RESTBinding.connection_metadata == ()

    def test_is_reachable_through_the_default_registry(self):
        from kapps_semantic_middleware.connectors.semantic import default_registry

        assert (
            default_registry.for_interface_property(INF.isInterfaceAccessibleParameter)
            is RESTBinding
        )


class TestRESTParameterFormatter:
    """The wire body already carries the whole node -- no facet reassembly, unlike MQTT."""

    def _formatter(self):
        return RESTParameterFormatter(model_type=_node_model(), url="http://peer/x")

    def test_deserialize_builds_model_instances_from_the_wire_list(self):
        wire = [{INF.hasValue.lined: [12.1], INF.accessMode.lined: ["readwrite"]}]

        [node] = self._formatter().deserialize(wire)

        assert getattr(node, INF.hasValue.lined) == [12.1]
        assert getattr(node, INF.accessMode.lined) == ["readwrite"]

    def test_serialize_dumps_model_instances_back_to_the_wire_list(self):
        formatter = self._formatter()
        wire = [{INF.hasValue.lined: [12.1], INF.accessMode.lined: ["readwrite"]}]
        nodes = formatter.deserialize(wire)

        assert formatter.serialize(nodes) == [
            {
                INF.hasValue.lined: [12.1],
                INF.accessMode.lined: ["readwrite"],
                IRI("https://example.org/tu#hasUnit").lined: [],
            }
        ]

    def test_round_trips_a_put_echo(self):
        """PUT sends exactly what GET returned. deserialize then serialize must
        be the identity on the wire shape."""
        formatter = self._formatter()
        wire = [{INF.hasValue.lined: [3.5], INF.accessMode.lined: ["readwrite"]}]

        assert formatter.serialize(formatter.deserialize(wire)) == [
            {**wire[0], IRI("https://example.org/tu#hasUnit").lined: []}
        ]

    def test_an_absent_body_deserializes_as_empty(self):
        """The locator pattern: no value until the peer has one either."""
        assert self._formatter().deserialize(None) == []

    def test_passing_model_instances_through_is_a_no_op(self):
        """deserialize may see its own model instances again (e.g. via receive()'s reuse of
        provide()'s raw data) -- tolerate that rather than double-wrap."""
        formatter = self._formatter()
        [node] = formatter.deserialize([{INF.hasValue.lined: [1.0], INF.accessMode.lined: []}])

        assert formatter.deserialize([node]) == [node]


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """A stand-in for httpx.AsyncClient. Class-level state, because the connector under
    test constructs its own client per call -- a test seeds ``get_payloads`` beforehand and
    reads ``instances`` afterward, rather than injecting an instance directly."""

    get_payloads: list = []
    instances: list = []

    def __init__(self, timeout=None):
        self.timeout = timeout
        self.calls = []
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url):
        self.calls.append(("GET", url))
        payload = _FakeAsyncClient.get_payloads.pop(0) if _FakeAsyncClient.get_payloads else []
        return _FakeResponse(payload)

    async def put(self, url, json):
        self.calls.append(("PUT", url, json))
        return _FakeResponse(None)


class TestRESTParameterConnector:
    """Async HTTP mechanics, with httpx replaced by a stub -- no real network."""

    @pytest.fixture(autouse=True)
    def _patch_httpx(self, monkeypatch):
        import kapps_semantic_middleware.connectors.rest_binding as rest_binding

        _FakeAsyncClient.get_payloads = []
        _FakeAsyncClient.instances = []
        fake_module = type("FakeHttpx", (), {"AsyncClient": _FakeAsyncClient})
        monkeypatch.setattr(rest_binding, "httpx", fake_module)
        yield

    def test_url_joins_base_and_path(self):
        connector = RESTParameterConnector("http://10.0.0.5:8010", f"/TransferUnit/x/{SPEED.lined}")

        assert connector.url == f"http://10.0.0.5:8010/TransferUnit/x/{SPEED.lined}"

    def test_url_tolerates_a_trailing_slash_on_the_base(self):
        connector = RESTParameterConnector("http://10.0.0.5:8010/", "/x")

        assert connector.url == "http://10.0.0.5:8010/x"

    @pytest.mark.asyncio
    async def test_provide_gets_the_url_and_returns_parsed_json(self):
        _FakeAsyncClient.get_payloads = [[{"a": 1}]]
        connector = RESTParameterConnector("http://peer", "/x")

        value = await connector.provide()

        assert value == [{"a": 1}]
        assert _FakeAsyncClient.instances[-1].calls == [("GET", "http://peer/x")]

    @pytest.mark.asyncio
    async def test_consume_puts_the_body_to_the_url(self):
        connector = RESTParameterConnector("http://peer", "/x")
        body = [{"a": 1}]

        await connector.consume(body)

        [client] = _FakeAsyncClient.instances
        assert client.calls == [("PUT", "http://peer/x", body)]

    @pytest.mark.asyncio
    async def test_connect_and_disconnect_are_no_ops(self):
        connector = RESTParameterConnector("http://peer", "/x")

        await connector.connect()
        await connector.disconnect()

    @pytest.mark.asyncio
    async def test_a_non_polling_connector_never_yields(self):
        connector = RESTParameterConnector("http://peer", "/x", poll=False, poll_interval=0.01)

        results = []
        async for value in connector.receive():
            results.append(value)  # pragma: no cover - must never run

        assert results == []

    @pytest.mark.asyncio
    async def test_polling_yields_once_per_change_and_skips_repeats(self):
        _FakeAsyncClient.get_payloads = [[{"a": 1}], [{"a": 1}], [{"a": 2}]]
        connector = RESTParameterConnector("http://peer", "/x", poll_interval=0.001)

        seen = []
        async for value in connector.receive():
            seen.append(value)
            if len(seen) == 2:
                break

        # The unchanged second reading (still {"a": 1}) is skipped; the change to {"a": 2} is not.
        assert seen == [[{"a": 1}], [{"a": 2}]]

    @pytest.mark.asyncio
    async def test_a_failed_poll_is_logged_and_does_not_kill_the_loop(self, caplog):
        class _FlakyClient(_FakeAsyncClient):
            async def get(self, url):
                if not _FakeAsyncClient.instances[:-1]:
                    raise RuntimeError("peer unreachable")
                return await super().get(url)

        import kapps_semantic_middleware.connectors.rest_binding as rest_binding

        rest_binding.httpx.AsyncClient = _FlakyClient
        _FakeAsyncClient.get_payloads = [[{"a": 1}]]
        connector = RESTParameterConnector("http://peer", "/x", poll_interval=0.001)

        seen = []
        async for value in connector.receive():
            seen.append(value)
            break

        assert seen == [[{"a": 1}]]
        assert "Poll of" in caplog.text
