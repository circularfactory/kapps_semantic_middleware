"""Unit tests for the semantic-connector seam (#40, ADR 0023 / ADR 0028).

Pure logic. No GraphDB, no broker, no network. The seam exists so a binding
can be described and reasoned about without a running middleware. These tests
are the first consumer of that property.
"""

from __future__ import annotations

import logging

import json

import pytest
from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection
from kapps_triplestore_interface import IRI
from pydantic import BaseModel

from kapps_semantic_middleware.connectors.mqtt_binding import (
    MQTTBinding,
    MQTTParameterFormatter,
)
from kapps_semantic_middleware.connectors.semantic import (
    ParameterBinding,
    Registration,
    SemanticConnectorRegistry,
    normalize_metadata,
    resolve_direction,
)
from kapps_semantic_middleware.projection import (
    ProjectionError,
    carries_southbound,
    cross_check,
    prune_southbound,
)
from kapps_semantic_middleware.vocabulary import INF, AccessMode

BELT = IRI("https://example.org/tu#Belt1")
SPEED = IRI("https://example.org/tu#hasConveyorSpeed")
OTHER_NS = "https://example.org/other#"


class _NodeModel(BaseModel):
    """Stand in for the AnonymousClass the OGM generates for a parameter node."""

    model_config = {"extra": "forbid"}


def _node_model():
    """A node model with field names the OGM mangles from the inf: IRIs."""
    fields = {
        INF.hasValue.lined: (list, []),
        INF.accessMode.lined: (list, []),
        IRI("https://example.org/tu#hasUnit").lined: (list, []),
    }
    from pydantic import create_model

    return create_model("AnonymousClass", **{k: (v[0], v[1]) for k, v in fields.items()})


def _binding(
    access_mode=AccessMode.READWRITE, value_path=None, set_topic="belt/speed_set", port=None
):
    metadata = {
        str(INF.accessMode): [access_mode],
        str(IRI("https://example.org/tu#hasUnit")): ["m/s"],
        str(INF.hasMQTTTopic): ["belt/speed"],
        str(INF.hasMQTTBrokerIP): ["127.0.0.1"],
    }
    if set_topic is not None:
        metadata[str(INF.hasMQTTSetTopic)] = [set_topic]
    if value_path is not None:
        metadata[str(INF.hasMQTTValuePath)] = [value_path]
    if port is not None:
        metadata[str(INF.hasMQTTBrokerPort)] = [port]
    return ParameterBinding(
        resource_iri=BELT,
        parameter_property=SPEED,
        field_id=SPEED.lined,
        metadata=normalize_metadata(metadata),
        descriptor=MQTTBinding,
        node_model_type=_node_model(),
    )


class TestRegistry:
    """Resolution is by interface property. The registry knows what each binding reads."""

    def test_resolves_a_registered_descriptor(self):
        registry = SemanticConnectorRegistry([MQTTBinding])
        assert (
            registry.for_interface_property(INF.isInterfaceAccessibleMQTTParameter)
            is MQTTBinding
        )

    def test_unregistered_interface_property_resolves_to_nothing(self):
        registry = SemanticConnectorRegistry([MQTTBinding])
        assert registry.for_interface_property(IRI(f"{OTHER_NS}isSomethingElse")) is None

    def test_a_second_interface_class_registers_without_touching_core(self):
        """#40 acceptance: a stub protocol registers alongside MQTT. No core change."""

        class StubBinding:
            connector_cls = object
            interface_property = IRI(f"{OTHER_NS}isInterfaceAccessibleStubParameter")
            connection_metadata = (IRI(f"{OTHER_NS}hasStubEndpoint"),)

            @staticmethod
            def build(binding, direction):
                return ()

        registry = SemanticConnectorRegistry([MQTTBinding, StubBinding])

        assert len(registry) == 2
        assert registry.for_interface_property(StubBinding.interface_property) is StubBinding
        assert str(IRI(f"{OTHER_NS}hasStubEndpoint")) in registry.declared_connection_metadata()

    def test_declared_metadata_is_the_union_of_every_binding(self):
        registry = SemanticConnectorRegistry([MQTTBinding])
        assert registry.declared_connection_metadata() == {
            str(INF.hasMQTTTopic),
            str(INF.hasMQTTBrokerIP),
            str(INF.hasMQTTBrokerPort),
            str(INF.hasMQTTSetTopic),
            str(INF.hasMQTTValuePath),
        }

    def test_no_binding_declares_hasvalue_or_accessmode(self):
        """Northbound-safe content must survive the projection. The view is useless otherwise."""
        southbound = SemanticConnectorRegistry([MQTTBinding]).declared_connection_metadata()
        assert str(INF.hasValue) not in southbound
        assert str(INF.accessMode) not in southbound


class TestDefaultRegistry:
    """The default registry populates by import alone.

    A middleware constructed without an explicit ``connector_registry`` gets the
    default one. Nothing imported the binding modules. No protocol is recognised.
    Nothing is wired. Every parameter comes up dead. Every test builds
    ``SemanticConnectorRegistry([MQTTBinding])`` explicitly. It is structurally
    blind to this. This is how it went unnoticed.

    Note the *projection* no longer depends on this being populated. It asks the
    ontology (ADR 0028). An empty registry is now a wiring failure. It is not a leak.
    """

    def test_importing_the_package_registers_the_builtin_bindings(self):
        from kapps_semantic_middleware.connectors.semantic import default_registry

        assert len(default_registry) >= 1

    def test_the_default_registry_declares_the_mqtt_metadata(self):
        from kapps_semantic_middleware.connectors.semantic import default_registry

        southbound = default_registry.declared_connection_metadata()

        assert {
            str(INF.hasMQTTTopic),
            str(INF.hasMQTTSetTopic),
            str(INF.hasMQTTBrokerIP),
            str(INF.hasMQTTBrokerPort),
            str(INF.hasMQTTValuePath),
        } <= southbound

    def test_mqtt_is_reachable_through_the_default_registry(self):
        from kapps_semantic_middleware.connectors.semantic import default_registry

        assert (
            default_registry.for_interface_property(
                INF.isInterfaceAccessibleMQTTParameter
            )
            is MQTTBinding
        )


class TestDirection:
    """Direction is the most restrictive of accessMode x flavour. Neither may widen."""

    @pytest.mark.parametrize(
        "access_mode, flavour, expected",
        [
            # A controller may drive a settable parameter. This is the only writable combination.
            (AccessMode.READWRITE, SyncDirection.BIDIRECTIONAL, SyncDirection.BIDIRECTIONAL),
            # A monitor cannot drive even a settable one.
            (AccessMode.READWRITE, SyncDirection.TO_PERSISTENCE, SyncDirection.TO_PERSISTENCE),
            # A controller cannot write a read-only sensor.
            (AccessMode.READ, SyncDirection.BIDIRECTIONAL, SyncDirection.TO_PERSISTENCE),
            (AccessMode.READ, SyncDirection.TO_PERSISTENCE, SyncDirection.TO_PERSISTENCE),
        ],
    )
    def test_most_restrictive_wins(self, access_mode, flavour, expected):
        assert resolve_direction(access_mode, flavour) is expected

    def test_absent_access_mode_is_read_only(self):
        """A parameter is never writable by accident of omission (ADR 0023)."""
        binding = ParameterBinding(
            resource_iri=BELT,
            parameter_property=SPEED,
            field_id=SPEED.lined,
            metadata={},
            descriptor=MQTTBinding,
            node_model_type=_node_model(),
        )
        assert binding.access_mode == AccessMode.READ
        assert (
            resolve_direction(binding.access_mode, SyncDirection.BIDIRECTIONAL)
            is SyncDirection.TO_PERSISTENCE
        )

    def test_unrecognised_access_mode_is_read_only(self):
        binding = _binding(access_mode="write-whenever-you-like")
        assert binding.access_mode == AccessMode.READ


class TestMQTTBindingBuild:
    """4 parameters -> 4 bindings -> 6 connectors. A settable parameter needs two."""

    def test_settable_parameter_yields_read_and_write_registrations(self):
        registrations = list(MQTTBinding.build(_binding(), SyncDirection.BIDIRECTIONAL))

        assert [r.sync_direction for r in registrations] == [
            SyncDirection.TO_PERSISTENCE,
            SyncDirection.FROM_PERSISTENCE,
        ]
        assert [r.connector.topic for r in registrations] == [
            "belt/speed",
            "belt/speed_set",
        ]
        # Both legs share one broker. One ConnectionInfo binds them (ADR 0023).
        assert {r.connector.mqtt_broker_ip for r in registrations} == {"127.0.0.1"}

    def test_read_only_direction_yields_only_the_read_leg(self):
        registrations = list(MQTTBinding.build(_binding(), SyncDirection.TO_PERSISTENCE))

        assert len(registrations) == 1
        assert registrations[0].sync_direction is SyncDirection.TO_PERSISTENCE
        assert registrations[0].connector.topic == "belt/speed"

    def test_readwrite_without_a_set_topic_degrades_to_read_only(self, caplog):
        """The connector publishes where it subscribes. No set topic means no write leg."""
        registrations = list(
            MQTTBinding.build(_binding(set_topic=None), SyncDirection.BIDIRECTIONAL)
        )

        assert len(registrations) == 1
        assert "readwrite but declares no" in caplog.text

    def test_missing_broker_binds_nothing_and_says_why(self, caplog):
        """A silently dead parameter is the failure mode this warning prevents."""
        binding = _binding()
        binding.metadata.pop(str(INF.hasMQTTBrokerIP))

        assert list(MQTTBinding.build(binding, SyncDirection.BIDIRECTIONAL)) == []
        assert "missing a broker" in caplog.text
        assert str(SPEED) in caplog.text


class TestMQTTBindingPort:
    """inf:hasMQTTBrokerPort reaches the connector. Absent means 1883 (ADR 0031)."""

    def test_absent_port_defaults_to_1883(self):
        registrations = list(MQTTBinding.build(_binding(), SyncDirection.TO_PERSISTENCE))

        assert registrations[0].connector.mqtt_broker_port == 1883

    def test_a_declared_port_reaches_the_connector(self):
        registrations = list(
            MQTTBinding.build(_binding(port=18831), SyncDirection.TO_PERSISTENCE)
        )

        assert registrations[0].connector.mqtt_broker_port == 18831

    def test_both_legs_of_a_settable_parameter_share_the_declared_port(self):
        registrations = list(
            MQTTBinding.build(_binding(port=18831), SyncDirection.BIDIRECTIONAL)
        )

        assert {r.connector.mqtt_broker_port for r in registrations} == {18831}

    def test_the_round_trip_preserves_an_integer_not_a_string(self):
        """The seed writes xsd:integer. binding.get() must hand back 18831, not "18831"
        (#69's stated acceptance -- the first non-string literal any seed here writes)."""
        registrations = list(
            MQTTBinding.build(_binding(port=18831), SyncDirection.TO_PERSISTENCE)
        )

        port = registrations[0].connector.mqtt_broker_port
        assert port == 18831
        assert isinstance(port, int)


class TestEnsureTransport:
    """The deployment's transport hook (ADR 0034). The library only ever calls it -- it
    never probes, never starts anything, and never reads a return value."""

    def test_absent_hook_calls_nothing(self):
        """An instance constructed without ensure_transport calls nothing."""
        list(MQTTBinding.build(_binding(), SyncDirection.BIDIRECTIONAL))
        # No exception, and nothing to assert on -- the point is there is no hook to call.

    def test_the_hook_is_called_with_the_declared_address(self):
        calls = []
        list(
            MQTTBinding.build(
                _binding(port=18831),
                SyncDirection.TO_PERSISTENCE,
                ensure_transport=lambda host, port: calls.append((host, port)),
            )
        )

        assert calls == [("127.0.0.1", 18831)]

    def test_the_hook_defaults_to_1883_the_same_as_the_connector(self):
        calls = []
        list(
            MQTTBinding.build(
                _binding(),
                SyncDirection.TO_PERSISTENCE,
                ensure_transport=lambda host, port: calls.append((host, port)),
            )
        )

        assert calls == [("127.0.0.1", 1883)]

    def test_a_settable_parameter_calls_the_hook_once_not_once_per_leg(self):
        """One binding, two connectors (read + write), one declared address."""
        calls = []
        list(
            MQTTBinding.build(
                _binding(port=18831),
                SyncDirection.BIDIRECTIONAL,
                ensure_transport=lambda host, port: calls.append((host, port)),
            )
        )

        assert calls == [("127.0.0.1", 18831)]

    def test_no_broker_binds_nothing_and_calls_no_hook(self):
        """A parameter that binds nothing has no address to ensure transport for."""
        binding = _binding()
        binding.metadata.pop(str(INF.hasMQTTBrokerIP))
        calls = []

        list(
            MQTTBinding.build(
                binding,
                SyncDirection.BIDIRECTIONAL,
                ensure_transport=lambda host, port: calls.append((host, port)),
            )
        )

        assert calls == []


class TestMQTTFormatter:
    """The formatter bridges a device scalar and the one-element list the framework holds."""

    def _formatter(self, value_path=None):
        model = _node_model()
        return MQTTParameterFormatter(
            model_type=model,
            northbound_facets={
                INF.accessMode.lined: ["readwrite"],
                IRI("https://example.org/tu#hasUnit").lined: ["m/s"],
            },
            value_field=INF.hasValue.lined,
            value_path=value_path,
        )

    def test_deserialize_wraps_a_scalar_into_the_node(self):
        [node] = self._formatter().deserialize(12.1)

        assert getattr(node, INF.hasValue.lined) == [12.1]

    def test_deserialize_preserves_the_static_facets(self):
        """setattr replaces the whole list. A bare value blanks the unit in the
        model served over REST (ADR 0023, the graph half is ADR 0027)."""
        [node] = self._formatter().deserialize(12.1)

        assert getattr(node, IRI("https://example.org/tu#hasUnit").lined) == ["m/s"]
        assert getattr(node, INF.accessMode.lined) == ["readwrite"]

    def test_serialize_produces_a_raw_scalar_payload(self):
        """consume() publishes its argument raw. The formatter encodes it."""
        formatter = self._formatter()
        [node] = formatter.deserialize(3.5)

        assert json.loads(formatter.serialize([node])) == 3.5

    def test_round_trips_a_scalar(self):
        formatter = self._formatter()

        assert json.loads(formatter.serialize(formatter.deserialize(7.25))) == 7.25

    def test_round_trips_a_json_envelope(self):
        """inf:hasMQTTValuePath is one property. Honour it symmetrically (ADR 0023)."""
        formatter = self._formatter(value_path="payload.speed")

        [node] = formatter.deserialize({"payload": {"speed": 4.5}, "ts": 123})
        assert getattr(node, INF.hasValue.lined) == [4.5]

        assert json.loads(formatter.serialize([node])) == {"payload": {"speed": 4.5}}

    def test_envelope_missing_the_path_reads_as_unobserved(self, caplog):
        formatter = self._formatter(value_path="payload.speed")

        [node] = formatter.deserialize({"other": 1})
        assert getattr(node, INF.hasValue.lined) == []
        assert "value path" in caplog.text

    def test_an_unobserved_parameter_refuses_to_serialize(self):
        """Scenario 3 is a locator. A parameter has no value until the device publishes.

        Serializing it used to encode as the JSON scalar ``null`` -- not a valid device
        value, and a receiver expecting a number or boolean chokes on it (#80's
        cross-field notify fan-out makes exactly this reachable: any change on a
        resource asks every write leg on it to re-derive its own slice, including a
        sibling parameter nothing has ever set). Refusing loudly here, rather than
        publishing ``null``, is the fix.
        """
        formatter = self._formatter()
        [node] = formatter.deserialize(None)

        with pytest.raises(ValueError, match="no observed value"):
            formatter.serialize([node])


class _FakeOGM:
    """Answer the two SPARQL shapes that `projection` issues from a declared hierarchy.

    The projection reads the ontology. A unit test supplies one. Mock the
    *store*. The queries stay real. A mistake in one shows up here.
    """

    def __init__(self, markers, declares):
        self.markers = markers  # parameter property -> [protocol marker, ...]
        self.declares = declares  # marker -> [property, ...]
        self.db = self

    def query(self, q, convert_bindings=False):
        if "?marker" in q and "onProperty" not in q:
            param = q.split("<", 1)[1].split(">", 1)[0]
            rows = [{"marker": m} for m in self.markers.get(param, [])]
            return {"results": {"bindings": rows}}
        wanted = [m for m in self.declares if f"<{m}>" in q]
        props = {p for m in wanted for p in self.declares[m]}
        return {"results": {"bindings": [{"p": p} for p in sorted(props)]}}


MQTT_MARKER = str(INF.isInterfaceAccessibleMQTTParameter)
OPCUA_MARKER = f"{OTHER_NS}isInterfaceAccessibleOPCUAParameter"
OPCUA_ENDPOINT = f"{OTHER_NS}hasOPCUAEndpoint"


def _ogm(*markers):
    return _FakeOGM(
        markers={str(SPEED): list(markers)},
        declares={
            MQTT_MARKER: [
                str(INF.hasMQTTTopic),
                str(INF.hasMQTTBrokerIP),
                str(INF.hasMQTTSetTopic),
            ],
            OPCUA_MARKER: [OPCUA_ENDPOINT],
        },
    )


class TestProjection:
    """The northbound view is the pruned spec. The ontology decides what is pruned."""

    class _Spec:
        """A minimal stand-in with two attributes the prune walks."""

        def __init__(self, properties):
            self.properties = properties

    class _Prop:
        def __init__(self, nested=None):
            self.nested = nested

    def _resource_spec(self, extra=()):
        fields = {
            INF.hasValue: self._Prop(),
            INF.accessMode: self._Prop(),
            INF.hasMQTTTopic: self._Prop(),
            INF.hasMQTTBrokerIP: self._Prop(),
            INF.hasMQTTSetTopic: self._Prop(),
        }
        for name in extra:
            fields[IRI(name)] = self._Prop()
        return self._Spec({SPEED: self._Prop(nested=self._Spec(fields))})

    def test_prune_removes_what_the_ontology_calls_protocol_metadata(self):
        pruned = prune_southbound(self._resource_spec(), ogm=_ogm(MQTT_MARKER))

        assert set(pruned.properties[SPEED].nested.properties) == {
            INF.hasValue,
            INF.accessMode,
        }

    def test_an_unregistered_protocol_is_pruned_too(self):
        """The regression this whole mechanism prevents.

        A belt reachable over MQTT *and* OPC-UA. No OPC-UA binding registered
        anywhere. The old registry-derived prune set removed the MQTT metadata.
        It served the OPC-UA endpoint. A set built from registered code knows
        only the protocols we wrote. Ask the ontology. Find it.
        """
        spec = self._resource_spec(extra=[OPCUA_ENDPOINT])

        pruned = prune_southbound(spec, ogm=_ogm(MQTT_MARKER, OPCUA_MARKER))

        assert set(pruned.properties[SPEED].nested.properties) == {
            INF.hasValue,
            INF.accessMode,
        }
        assert IRI(OPCUA_ENDPOINT) not in pruned.properties[SPEED].nested.properties

    def test_a_property_with_no_protocol_marker_is_untouched(self):
        """An ordinary object property is not interface-accessible. It loses nothing."""
        pruned = prune_southbound(self._resource_spec(), ogm=_ogm())

        assert set(pruned.properties[SPEED].nested.properties) == {
            INF.hasValue,
            INF.accessMode,
            INF.hasMQTTTopic,
            INF.hasMQTTBrokerIP,
            INF.hasMQTTSetTopic,
        }

    def test_prune_does_not_mutate_the_input_spec(self):
        """Both shapes are needed at once. The full one wires. The pruned one serves."""
        spec = self._resource_spec()

        prune_southbound(spec, ogm=_ogm(MQTT_MARKER))

        assert INF.hasMQTTBrokerIP in spec.properties[SPEED].nested.properties

    def test_unreadable_protocol_ranges_refuse_to_serve(self):
        """A projection proves payload unsafe. Do not serve it.

        The ontology says the parameter is reached over a protocol. Nothing can
        be read from that protocol range. A TBox does not exist. A range shape is
        not understood. Continue. Serve fields we have not classified.
        """
        blind = _FakeOGM(markers={str(SPEED): [MQTT_MARKER]}, declares={})

        with pytest.raises(ProjectionError) as exc:
            prune_southbound(self._resource_spec(), ogm=blind)

        assert "cannot prove what is safe" in str(exc.value)


class TestCrossCheck:
    """The ontology governs. Compare a binding declaration against it. Report."""

    def test_agreement_is_silent(self, caplog):
        cross_check(SPEED, [INF.hasMQTTTopic], [INF.hasMQTTTopic])

        assert not caplog.text

    def test_a_term_only_the_ontology_declares_is_reported(self, caplog):
        cross_check(SPEED, [INF.hasMQTTTopic, INF.hasMQTTValuePath], [INF.hasMQTTTopic])

        assert "hasMQTTValuePath" in caplog.text

    def test_a_term_only_the_binding_declares_is_reported(self, caplog):
        """The direction costs debugging time. It will not survive a write."""
        cross_check(SPEED, [INF.hasMQTTTopic], [INF.hasMQTTTopic, INF.hasMQTTSetTopic])

        assert "hasMQTTSetTopic" in caplog.text
        assert "may come up with no value flowing" in caplog.text

    def test_a_binding_that_declares_nothing_is_not_cross_checked(self, caplog):
        cross_check(SPEED, [INF.hasMQTTTopic], None)

        assert not caplog.text


class TestLeakDetector:
    def test_carries_southbound_detects_a_raw_iri(self):
        southbound = SemanticConnectorRegistry([MQTTBinding]).declared_connection_metadata()
        payload = {str(INF.hasMQTTBrokerIP): ["127.0.0.1"]}

        assert carries_southbound(payload, southbound) == {str(INF.hasMQTTBrokerIP)}

    def test_carries_southbound_detects_a_mangled_field_name(self):
        """A served JSON body carries IRI-mangled field names. Not raw IRIs."""
        southbound = SemanticConnectorRegistry([MQTTBinding]).declared_connection_metadata()
        payload = {INF.hasMQTTBrokerIP.lined: ["127.0.0.1"]}

        assert carries_southbound(payload, southbound) == {str(INF.hasMQTTBrokerIP)}

    def test_a_clean_payload_carries_nothing(self):
        southbound = SemanticConnectorRegistry([MQTTBinding]).declared_connection_metadata()
        payload = {INF.hasValue.lined: [12.1], INF.accessMode.lined: ["readwrite"]}

        assert carries_southbound(payload, southbound) == set()


class TestRegistrationShape:
    def test_registration_is_hashable_and_frozen(self):
        """Registrations are described. Not performed. They must be safe to pass around."""
        registration = Registration(
            connector=object(),
            sync_direction=SyncDirection.TO_PERSISTENCE,
            model_type=list,
        )
        with pytest.raises(Exception):
            registration.sync_direction = SyncDirection.BIDIRECTIONAL


class TestActivityLogging:
    """Inbound logs at INFO only on change. Outbound logs every setpoint.

    Keep the activity feed readable. The mock PLC republishes every value every
    0.2 s across four topics. Log each arrival at INFO. Bury the feed in
    seconds. Measure on a live run. Suppress 97.9 % of arrivals. 240 messages
    became 5 INFO lines.
    """

    def _formatter(self, caplog):
        """Build a formatter with caplog configured for the binding logger."""
        caplog.set_level(logging.DEBUG, logger="kapps_semantic_middleware.connectors.mqtt_binding")

        model = _node_model()
        return MQTTParameterFormatter(
            model_type=model,
            northbound_facets={
                INF.accessMode.lined: ["readwrite"],
                IRI("https://example.org/tu#hasUnit").lined: ["m/s"],
            },
            value_field=INF.hasValue.lined,
            value_path=None,
            parameter_label="Belt1 hasConveyorSpeed",
            topic="belt/speed",
            set_topic="belt/speed_set",
        )

    def test_a_changed_inbound_value_logs_at_info(self, caplog):
        """A changed value is news. Appear at INFO, naming the parameter and the value."""
        formatter = self._formatter(caplog)

        formatter.deserialize(1.5)
        formatter.deserialize(2.5)

        records = [r for r in caplog.records if r.levelname == "INFO"]
        assert len(records) == 2
        assert all("Belt1 hasConveyorSpeed" in r.message for r in records)
        assert [r.message for r in records] == [
            "Belt1 hasConveyorSpeed <- 1.5",
            "Belt1 hasConveyorSpeed <- 2.5",
        ]

    def test_a_repeated_inbound_value_drops_to_debug(self, caplog):
        """A periodic republish of an unchanged value is noise. A change is news.

        Without this split the feed is unreadable within seconds.
        """
        formatter = self._formatter(caplog)

        formatter.deserialize(1.5)
        formatter.deserialize(1.5)

        info_records = [r for r in caplog.records if r.levelname == "INFO"]
        debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]

        assert len(info_records) == 1
        # Every arrival is traced at DEBUG, changed or not. Only the unchanged one says so.
        assert len(debug_records) == 2
        assert "(unchanged)" not in debug_records[0].message
        assert "(unchanged)" in debug_records[1].message

    def test_the_first_value_is_always_news_even_if_it_is_none(self, caplog):
        """The _UNSET sentinel ensures the first reading logs even when None.

        Initialize last value to None instead. Silently swallow the first
        reading of a parameter that legitimately starts unobserved.
        """
        formatter = self._formatter(caplog)

        formatter.deserialize(None)

        records = [r for r in caplog.records if r.levelname == "INFO"]
        assert len(records) == 1
        assert "Belt1 hasConveyorSpeed" in records[0].message

    def test_no_topic_reaches_an_info_record(self, caplog):
        """#76. The activity feed serves this package's INFO records over HTTP.

        A topic is southbound metadata that ADR 0028 deletes from the served
        datamodel. An INFO line carrying it hands a peer the bypass route the
        projection withholds, over the same web server, one URL away. The topic
        belongs at DEBUG, which the feed's handler never buffers.
        """
        formatter = self._formatter(caplog)

        [node] = formatter.deserialize(1.5)
        formatter.deserialize(2.5)
        formatter.serialize([node])

        info = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
        assert info, "the test proves nothing if nothing logged at INFO"
        assert not any("belt/speed" in message for message in info)
        # The topic is not lost, only relevelled: a developer who asks for DEBUG still gets it.
        debug = [r.getMessage() for r in caplog.records if r.levelname == "DEBUG"]
        assert any("belt/speed" in message for message in debug)
        assert any("belt/speed_set" in message for message in debug)

    def test_an_outbound_setpoint_always_logs_at_info(self, caplog):
        """Outbound logs at INFO every time. The asymmetry with inbound is deliberate.

        A setpoint is always news. Something chose to act. No change-detection
        applies here.
        """
        formatter = self._formatter(caplog)
        # Build a node to send. Cost one inbound line. Drop it. The count below is
        # about the setpoints alone. Not about how the fixture got its value.
        [node] = formatter.deserialize(3.5)
        caplog.clear()

        formatter.serialize([node])
        formatter.serialize([node])

        records = [r for r in caplog.records if r.levelname == "INFO"]
        assert len(records) == 2
        assert all("Belt1 hasConveyorSpeed" in r.message for r in records)
