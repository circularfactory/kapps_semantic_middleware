"""The MQTT semantic connector: the first instance of the binding seam.

A parameter is reachable over MQTT when its domain property is a subproperty of
``inf:isInterfaceAccessibleMQTTParameter``. The parameter node then carries a broker
address and a read topic. A settable parameter node also carries a set topic.

**Two connectors per settable parameter, one ConnectionInfo.** ``MqttClientConnector`` takes
a single topic. Its ``consume()`` publishes to the topic it subscribed to. So it physically
cannot serve a read topic plus a distinct set topic. ``ConnectionRegistry.connections`` is
``Dict[ConnectionInfo, List[str]]``. So both bind to one ``ConnectionInfo``, and differ only
in ``sync_direction`` — precisely what ``SyncDirection`` is for. For the scenario-3
TransferUnit that is 4 parameters, 4 bindings, 6 connectors, 6 topics.

``aiomqtt`` is an optional extra of ``transitional_sync_middleware`` (its ``industrial`` group). So the
connector class is imported lazily. Importing this module must not require a working MQTT
stack. The registry must exist for every connector wiring, including one that wires nothing.
An inspecting instance with no ``aiomqtt`` installed must still receive the projection.
"""

from __future__ import annotations

import json
import math
import logging
from typing import Any, Callable, ClassVar, Dict, Iterable, List, Optional, Tuple

from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware.connectors.semantic import (
    ParameterBinding,
    Registration,
    semantic_connector,
)
from kapps_semantic_middleware.vocabulary import INF

logger = logging.getLogger(__name__)

# Sentinel that tracks whether any value exists yet. Distinct from None, so a
# genuine first None payload still counts as a change worth logging at INFO.
_UNSET = object()


def _is_a_change(value: Any, previous: Any) -> bool:
    """Whether an inbound value is news. Treat NaN as equal to itself.

    ``float("nan") != float("nan")`` is true by IEEE-754. So a sensor stuck at NaN (a
    failed probe, a divide-by-zero in the PLC) would register every republish as a
    change, and flood the very feed this comparison exists to keep readable. That is
    precisely the case where the operator least needs a scrolling wall of identical
    lines.
    """
    if isinstance(value, float) and isinstance(previous, float):
        if math.isnan(value) and math.isnan(previous):
            return False
    return value != previous


try:
    from transitional_sync_middleware.connect.connectors.mqtt_client_connector import (
        MqttClientConnector,
    )
except ImportError:  # pragma: no cover - depends on the optional extra being installed
    # Importing this module must not require a working MQTT stack. The registry must exist
    # for every flavour, including one that wires nothing, so an inspector on a
    # host without aiomqtt must still receive recognition and the projection. The failure is
    # deferred to the moment something actually tries to build a connector.
    MqttClientConnector = None  # type: ignore[assignment]


def _mqtt_client_connector_cls():
    """The framework connector class, or an actionable error if the extra is absent."""
    if MqttClientConnector is None:  # pragma: no cover - depends on the optional extra
        raise ImportError(
            "The MQTT semantic connector needs aiomqtt, an optional extra of "
            "transitional_sync_middleware. Install it with `uv add aiomqtt`, or construct the middleware "
            "with autoregister_connectors=False to run as an inspector."
        )
    return MqttClientConnector


class MQTTParameterFormatter:
    """Translates between an MQTT payload and the persistence value of a parameter node.

    The persistence value of a COMPLEX property is a **list of one** generated model, not a
    scalar: ``[AnonymousClass(hasValue=[12.1], hasUnit=['m/s'], accessMode=['readwrite'])]``.
    ``ConnectionInfo`` bottoms out at that property. ``field_id`` is a plain ``getattr`` and
    the node's ``inf:hasValue`` is one level further down. The formatter bridges
    the device scalar and the node the middleware persists.

    **Payload shape.** Raw scalar by default. If the parameter declares
    ``inf:hasMQTTValuePath``, use a JSON envelope with the value at that dotted path. The path is
    one property and is honoured symmetrically on both read and write.

    **Symmetry.** ``MqttClientConnector`` is asymmetric. Its listener runs
    ``json.loads(payload)`` on everything it receives. ``consume()`` publishes its argument
    raw. So ``deserialize`` receives an already-parsed value. ``serialize`` must produce the
    encoded bytes.

    **Why the node is reassembled rather than replaced by a bare value.**
    ``update_persistence_with_value`` does ``setattr(contained_model, field_id, value)``,
    replacing the whole list. ``Formatter.deserialize`` sees only the payload. It has no
    access to the current persistence value. A bare scalar would therefore blank the unit and
    the access mode *in the model served over REST*. The graph layer now diffs per triple, so
    unchanged facets are preserved there. The in-memory half remains, and it is what
    this reassembly is for. The northbound payload keeps its unit after the first device
    message. The facets come from the same metadata the binding already read. So this is a
    pure function per message, with no read of current state. ``_last_value`` exists solely
    for change-detection in logging, and never affects the returned value.
    """

    def __init__(
        self,
        model_type: type,
        northbound_facets: Dict[str, Any],
        value_field: str,
        value_path: Optional[str] = None,
        *,
        parameter_label: str = "",
        topic: str = "",
        set_topic: str = "",
    ) -> None:
        self.model_type = model_type
        self.northbound_facets = northbound_facets
        self.value_field = value_field
        self.value_path = value_path
        self.parameter_label = parameter_label
        self.topic = topic
        self.set_topic = set_topic
        self._last_value: Any = _UNSET

    def deserialize(self, data: Any) -> List[Any]:
        """Device payload to the persistence value (a one-element list holding the node)."""
        value = self._extract(data)
        fields = dict(self.northbound_facets)
        fields[self.value_field] = [] if value is None else [value]
        # Log inbound values, but only at INFO when they change. The mock PLC republishes
        # every value on a fixed interval (0.2–0.5s per topic), so logging every message at
        # INFO makes the feed unreadable within seconds. A periodic republish of an unchanged
        # value is noise. A change is news.
        #
        # The topic never appears at INFO. `enable_activity_feed` serves this package's INFO
        # records over HTTP, and a topic is southbound metadata the northbound projection
        # deletes from the served datamodel -- so an INFO line carrying it would hand a peer,
        # over the same web server, the bypass route the projection exists to withhold. The
        # topic stays at DEBUG, which the feed's handler filters out at its own level whatever
        # the logger is set to.
        label = self.parameter_label or "parameter"
        topic = self.topic or "topic"
        changed = _is_a_change(value, self._last_value)
        logger.debug("%s <- %s = %r%s", label, topic, value, "" if changed else " (unchanged)")
        if changed:
            logger.info("%s <- %r", label, value)
            self._last_value = value
        return [self.model_type(**fields)]

    def serialize(self, data: Any) -> bytes:
        """Persistence value to the device payload, encoded for ``consume``.

        Raises if nothing has ever been observed for this parameter. Under the
        locator pattern the graph holds no ``inf:hasValue`` until a connector
        fills it. A bare ``None`` would encode as the JSON scalar ``null``, not a
        valid device value, and a receiver expecting a number or boolean chokes on
        it. The caller's best-effort fan-out (``persisted_connector.py``) catches
        and logs a per-connector failure, so raising here is the same "nothing safe
        to send" outcome that silently accepting ``None`` would have hidden as a
        network-level failure at the device.

        This used to be the ordinary case rather than the guard it reads as. The
        fan-out asked *every* write leg on a resource to re-derive its slice
        whenever *any* field changed, so a parameter nothing had ever set was
        routinely asked to publish. The fan-out is now scoped to the field that
        actually moved (``changed``), so a write leg is now only asked when its own
        parameter was written -- and a write carries the value with it. The guard
        stays because "asked to publish something never observed" is still a real
        error state, and one worth naming rather than encoding as ``null``.
        """

        value = self._value_of(data)
        if value is None:
            raise ValueError(
                f"{self.parameter_label or 'parameter'} has no observed value yet; "
                f"refusing to publish null to {self.set_topic or 'its set topic'}"
            )
        if self.value_path is None:
            result = json.dumps(value).encode()
        else:
            envelope: Dict[str, Any] = {}
            cursor = envelope
            *branches, leaf = self.value_path.split(".")
            for part in branches:
                cursor = cursor.setdefault(part, {})
            cursor[leaf] = value
            result = json.dumps(envelope).encode()
        # Log outbound setpoints unconditionally at INFO. A setpoint is always news. It is
        # an operator or a controller acting, so no change-detection here. The set topic
        # stays at DEBUG for the same reason the inbound topic does -- see `deserialize`.
        label = self.parameter_label or "parameter"
        topic = self.set_topic or "set_topic"
        logger.debug("%s -> %s = %r", label, topic, value)
        logger.info("%s -> %r", label, value)
        return result

    def _extract(self, data: Any) -> Any:
        """Pull the scalar from an inbound payload. Honor the envelope path."""
        if self.value_path is None:
            return data
        cursor = data
        for part in self.value_path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                logger.warning(
                    "MQTT payload has no %r at value path %r; treat it as unobserved",
                    part,
                    self.value_path,
                )
                return None
            cursor = cursor[part]
        return cursor

    def _value_of(self, data: Any) -> Any:
        """Pull the scalar from a persistence value, whatever depth it arrives at.

        Accept the one-element list the framework holds, a bare node, or an
        already-plain scalar. An outbound value can reach here from a PUT route as
        easily as from the persistence model.
        """
        if isinstance(data, (list, tuple)):
            if not data:
                return None
            data = data[0]
        node_value = getattr(data, self.value_field, None)
        if node_value is None and isinstance(data, dict):
            node_value = data.get(self.value_field)
        if node_value is None:
            return data
        if isinstance(node_value, (list, tuple)):
            return node_value[0] if node_value else None
        return node_value


@semantic_connector
class MQTTBinding:
    """Binds an MQTT-reachable parameter to one or two ``MqttClientConnector`` instances."""

    connector_cls: ClassVar[Any] = MqttClientConnector
    interface_property: ClassVar[IRI] = INF.isInterfaceAccessibleMQTTParameter
    connection_metadata: ClassVar[Tuple[IRI, ...]] = (
        INF.hasMQTTTopic,
        INF.hasMQTTBrokerIP,
        INF.hasMQTTBrokerPort,
        INF.hasMQTTSetTopic,
        INF.hasMQTTValuePath,
    )

    @staticmethod
    def build(
        binding: ParameterBinding,
        direction: SyncDirection,
        *,
        ensure_transport: Optional[Callable[[str, int], None]] = None,
    ) -> Iterable[Registration]:
        """One read registration always. One write registration when both sides permit it.

        ``direction`` has already been reduced to the most restrictive of the parameter's
        ``inf:accessMode`` and the instance's flavour, so this only honours
        it. It must never re-widen.

        ``ensure_transport``, when given, is called once with the declared ``(host, port)``,
        immediately before the first connector for it is built. Deduping across
        several parameters that share one address is the caller's job (``wiring.py``'s
        ``plan_wiring``), not this method's -- a single ``build`` call sees only its own
        parameter, never its siblings.
        """

        connector_cls = _mqtt_client_connector_cls()

        broker = binding.get(INF.hasMQTTBrokerIP)
        topic = binding.get(INF.hasMQTTTopic)
        set_topic = binding.get(INF.hasMQTTSetTopic)
        value_path = binding.get(INF.hasMQTTValuePath)
        port = binding.get(INF.hasMQTTBrokerPort)
        if port is None:
            port = 1883  # Absent means 1883, so every existing ABox keeps its meaning.

        if not broker or not topic:
            # A parameter marked MQTT-accessible whose metadata is incomplete would come up
            # silently dead: no listener, no value, and nothing to say why. Name the property,
            # and name what is absent.
            logger.warning(
                "Do not bind %s on %s over MQTT. The metadata is missing %s. The parameter will be served "
                "but no value will flow.",
                binding.parameter_property,
                binding.resource_iri,
                " and ".join(
                    name
                    for name, present in (("a broker", broker), ("a topic", topic))
                    if not present
                ),
            )
            return

        if ensure_transport is not None:
            ensure_transport(broker, port)

        formatter = _formatter_for(binding, value_path, topic=topic, set_topic=set_topic or "")

        yield Registration(
            connector=connector_cls(broker, topic, port),
            sync_direction=SyncDirection.TO_PERSISTENCE,
            model_type=list,
            formatter=formatter,
            suffix="read",
        )

        if direction is not SyncDirection.BIDIRECTIONAL:
            return

        if not set_topic:
            logger.warning(
                "%s on %s is readwrite but declares no %s. Serve it read-only.",
                binding.parameter_property,
                binding.resource_iri,
                INF.hasMQTTSetTopic,
            )
            return

        yield Registration(
            connector=connector_cls(broker, set_topic, port),
            sync_direction=SyncDirection.FROM_PERSISTENCE,
            model_type=list,
            formatter=formatter,
            suffix="write",
        )


def _formatter_for(
    binding: ParameterBinding,
    value_path: Optional[str],
    topic: str = "",
    set_topic: str = "",
) -> MQTTParameterFormatter:
    """Build the formatter for one parameter from its already-resolved northbound facets.

    The facets are the parameter node's properties minus the connection metadata. These are exactly the
    northbound projection of the node, reached here from the binding side rather than
    from the spec side.
    """

    southbound = {str(prop) for prop in MQTTBinding.connection_metadata}
    value_field = INF.hasValue.lined
    facets = {
        IRI(prop).lined: value
        for prop, value in binding.metadata.items()
        if prop not in southbound and IRI(prop).lined != value_field
    }
    return MQTTParameterFormatter(
        model_type=binding.node_model_type,
        northbound_facets=facets,
        value_field=value_field,
        value_path=value_path,
        parameter_label=binding.label,
        topic=topic,
        set_topic=set_topic,
    )
