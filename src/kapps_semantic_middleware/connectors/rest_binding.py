"""The REST semantic connector: the connector seam reaches a peer middleware.

A parameter binds a REST connector when two things hold, neither of them a marker on the
parameter itself. First, the parameter is interface-accessible at all — a subproperty of
``inf:isInterfaceAccessibleParameter``, the generic root, with no more specific protocol
marker (a parameter that also carries ``inf:hasMQTTTopic`` etc. resolves to that protocol's
binding instead; ``wiring.py``'s ``_descriptor_for`` already picks the most specific
registered match). Second, the parameter's resource carries a live ``svc:address`` on its
Service, one hop out through ``svc:isServiceOf``. That address is not a marker on the
parameter — it is looked up once per resource in ``wiring.py``'s ``_recognise`` and folded
into every recognised binding's metadata. This binding reads it out exactly like MQTT reads
``inf:hasMQTTBrokerIP``, through ``binding.get``.

**The route is structural, so nothing else is needed.** Address plus the recursive path
derived from the datamodel tree is a complete binding — no REST-specific ontology term is
minted. ``build_parameter_path`` does that derivation. It moved here from
``demo/transferunits/controller.py`` rather than being rewritten (ticket #77): the two
callers — this binding, reaching outward at recognition time, and the ``Controller``,
reaching outward from a fetched JSON tree — need the identical algorithm, and a domain-level
module is the wrong place to own something the middleware itself defines.

**Payload shape.** The payload is a *list* of parameter dicts, not a bare scalar, and a PUT
must send back exactly what a GET returned. Unlike MQTT, which bridges a bare device scalar
and the one-element list the framework holds, a peer's own generated parameter route already
serves and accepts that exact list — there is no scalar to rewrap. So
``RESTParameterFormatter`` is a plain type adapter between the wire JSON and
``node_model_type`` instances, nothing more (contrast ``mqtt_binding.MQTTParameterFormatter``,
which reassembles static facets a bare scalar would otherwise blank).

**REST has no push, so northbound sync polls** (raised while grilling #81, decided here,
ticket #77). Three questions were left open for whoever picked this up:

- *Batched or per-parameter?* One GET per parameter, one ``RESTParameterConnector`` per
  registration — the same granularity MQTT already uses (one connector per topic). A shared
  batch-fetch-and-fan-out would need state shared across bindings that recognition does not
  otherwise couple, for a cost (extra GETs on a resource with few parameters) this ticket has
  no evidence yet justifies the complexity.
- *What interval?* ``DEFAULT_POLL_INTERVAL_SECONDS`` below, currently conservative. Ticket
  #82 measures one full lap (PUT -> unit middleware -> MQTT -> PLC -> MQTT back -> connector
  read) to set an algorithm's tick above every freshness floor in the chain, and this
  connector's read cadence is one term in that lap — that measurement, not this ticket,
  should tune the default.
- *Configurable?* Yes, per connector instance (``poll_interval=``), which
  ``RESTBinding.build`` could source from a future ``inf:`` term if one ever proves
  necessary. None is minted here; the route needs no new term, and a cadence is not
  addressing.

**Lazy import, same rule as MQTT:** importing this module, and recognising and building a
``RESTBinding``, must not require a working network stack, so that an inspecting instance
keeps receiving the projection on a host that cannot reach anything.

**That rule does not currently hold on this module's import path, and the guard below is
unreachable.** Corrected 2026-08-07 on #77, which had recorded the box as merely untested.
Importing this module pulls in ``transitional_sync_middleware.connect.connectors.aas_client_connector``,
which does a bare ``import httpx`` at module level — and does not declare ``httpx`` in its own
manifest at all. With httpx absent the import therefore dies several frames above the ``try``
below, and the ``# pragma: no cover`` on it is accurate rather than lazy. The MQTT half is
genuinely different: ``aiomqtt`` really is an optional extra there, and ``mqtt_binding``
really does degrade to ``MqttClientConnector = None``.

The guard stays. It is correct in isolation, it is what makes the rule true again the moment
the eager import upstream goes away, and its error message is reachable and asserted
(``tests/test_optional_transport_stacks.py``). What is *not* claimed here any more is that
absence has ever been survivable on this path.
"""

# ADR: 0017, 0023, 0028, 0033

from __future__ import annotations

import asyncio
import logging
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    ClassVar,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
)

from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware.connectors.semantic import (
    ParameterBinding,
    Registration,
    semantic_connector,
)
from kapps_semantic_middleware.vocabulary import INF, SVC

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 2.0
"""Default cadence of the northbound read leg's poll loop. See the module docstring's
"REST has no push" section for why this value is provisional rather than measured."""

# Sentinel that tracks whether any value exists yet, same rationale as mqtt_binding's: a
# genuine first payload still counts as a change worth logging at INFO. Not shared with
# mqtt_binding on purpose -- each protocol binding stands alone (ADR 0023: a descriptor
# names a connector_cls, it does not inherit from a sibling descriptor).
_UNSET = object()


def _is_a_change(value: Any, previous: Any) -> bool:
    """Whether a polled value is news, for change-only INFO logging.

    Kept as a named function rather than inlined to its two call sites (#93 item 5 offered
    the inlining), for one reason: ``mqtt_binding`` has a function of the same name that is
    **not** a bare ``!=`` -- it treats NaN as equal to itself, because IEEE-754 says
    ``nan != nan`` and a sensor stuck at NaN would otherwise report every republish as a
    change and flood the feed the comparison exists to keep readable.

    The two are deliberately not shared, and the divergence is the point worth seeing: this
    one polls a peer middleware's REST route, which serves JSON, so a NaN cannot survive the
    encoding to reach here. Inlining ``value != previous`` would erase the question of
    whether that reasoning still holds the next time this file grows a value type.
    """
    return value != previous


try:
    import httpx
except ImportError:  # pragma: no cover - depends on httpx being installed
    httpx = None  # type: ignore[assignment]


def _httpx_module():
    """The ``httpx`` module, or an actionable error if it is absent.

    Called from inside the connector's async methods, never at import or recognition time, so
    that the failure is deferred to the moment something actually tries to talk to a peer.

    **The deferral is real; the absence it defers is not reachable here.** See the module
    docstring: an eager, undeclared ``import httpx`` upstream in ``transitional_sync_middleware`` means this
    module cannot be imported without httpx in the first place. The message below is
    nonetheless asserted, by swapping the module global -- the strongest check available while
    that stays true, and weaker than #77's box asked for.
    """

    # ADR: 0023, 0028
    if httpx is None:  # pragma: no cover - depends on the optional install
        raise ImportError(
            "The REST semantic connector needs httpx. Install it with `uv add httpx`, or "
            "construct the middleware with autoregister_connectors=False to run as an "
            "inspector."
        )
    return httpx


def build_parameter_path(
    root_class_local_name: str,
    root_iri: IRI,
    steps: Sequence[Tuple[str, str]],
    terminal_field: str,
) -> str:
    """Build the structural REST path for a parameter from known (field, child_id) hops.

    Mirrors ``rest_router.py``'s ``_accumulate_routes`` path shape exactly:
    ``/{Model}/{lined_root}/{field}/{lined_child}/.../{field}``. Field names stay literal —
    the caller (recognition, here; a fetched JSON tree, for ``Controller``) already carries
    them in the mangled form the served route uses. Only individual IDs are mangled here, via
    ``IRI.lined``.

    Moved from ``demo/transferunits/controller.py`` (ticket #77): the algorithm has no
    domain term and two callers now need it, one of them in ``src/``.

    Args:
        root_class_local_name: The root resource's class IRI, mangled (``IRI(...).lined``)
            -- what ``kapps_ogm`` names the materialized pydantic class, and so the
            ``{Model}`` segment the served route actually mounts under. Not the bare
            fragment: see ``ParameterBinding.root_class_local_name``'s docstring.
        root_iri: The root resource's own IRI.
        steps: Sequence of (field_name, child_id) hops from the root to the parameter's owner.
        terminal_field: The final field name (the parameter itself).

    Returns:
        The structural URL path.
    """
    # ADR: 0017, 0021
    path_parts = ["", root_class_local_name, IRI(root_iri).lined]
    for field_name, child_id in steps:
        path_parts.append(field_name)
        path_parts.append(IRI(child_id).lined)
    path_parts.append(terminal_field)
    return "/".join(path_parts)


class RESTParameterFormatter:
    """Adapts between a peer's parameter-route JSON body and the persistence value.

    Unlike a bare MQTT scalar, the REST payload already carries the whole parameter node --
    value, unit, access mode, whatever the range declares -- because the peer's own GET/PUT
    routes serve and accept exactly that shape. There is no scalar to rewrap, so this is a
    type adapter between a JSON list of dicts and ``model_type`` instances, nothing more.
    """

    # ADR: 0017

    def __init__(
        self,
        model_type: Type[Any],
        *,
        parameter_label: str = "",
        url: str = "",
    ) -> None:
        self.model_type = model_type
        self.parameter_label = parameter_label
        self.url = url
        self._last_value: Any = _UNSET

    def deserialize(self, data: Any) -> List[Any]:
        """A peer's GET body (or a poll's reading) to the persistence value."""
        items = data or []
        nodes = [
            item if isinstance(item, self.model_type) else self.model_type(**item)
            for item in items
        ]
        label = self.parameter_label or "parameter"
        target = self.url or "peer"
        dumped = [node.model_dump() for node in nodes]
        if _is_a_change(dumped, self._last_value):
            logger.info("%s <- GET %s = %r", label, target, dumped)
            self._last_value = dumped
        else:
            logger.debug("%s <- GET %s = %r (unchanged)", label, target, dumped)
        return nodes

    def serialize(self, data: Any) -> List[Dict[str, Any]]:
        """The persistence value to the JSON body of an outbound PUT.

        Echo semantics: this is the same shape ``deserialize`` accepted, so a read-modify-write
        round trips through this pair with no reshaping in between.
        """

        # ADR: 0017
        items = data if isinstance(data, (list, tuple)) else [data]
        body = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in items
        ]
        logger.info(
            "%s -> PUT %s = %r", self.parameter_label or "parameter", self.url or "peer", body
        )
        return body


class RESTParameterConnector:
    """Reaches one generated parameter route on a peer middleware over HTTP.

    ``connect``/``disconnect`` are no-ops. HTTP is stateless here, one ``httpx.AsyncClient``
    per call, the same style ``Controller`` and ``transitional_sync_middleware``'s own
    ``HttpRequestConnector`` already use -- there is no persistent connection worth pooling
    across the polling interval this runs at.

    ``poll`` gates whether ``receive()`` actually polls. The write leg of a bidirectional
    parameter gets ``poll=False``: its ``sync_direction`` is ``FROM_PERSISTENCE``, so the
    framework's ``SyncedConnector.receive()`` would never act on anything it yielded anyway
    (see ``synced_connector.py``), and polling the same URL a second time from the write leg
    would only double the request rate for no effect.
    """

    def __init__(
        self,
        base_url: str,
        path: str,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        poll: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.path = path if path.startswith("/") else f"/{path}"
        self.poll_interval = poll_interval
        self.poll = poll
        self.timeout = timeout

    @property
    def url(self) -> str:
        return f"{self.base_url}{self.path}"

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def provide(self) -> Any:
        """GET the parameter route. Returns the parsed JSON list of parameter dicts."""
        httpx_module = _httpx_module()
        async with httpx_module.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            return response.json()

    async def consume(self, body: Any) -> None:
        """PUT ``body`` to the parameter route. ``body`` already has the list shape it wants."""
        # ADR: 0017
        httpx_module = _httpx_module()
        async with httpx_module.AsyncClient(timeout=self.timeout) as client:
            response = await client.put(self.url, json=body)
            response.raise_for_status()

    async def receive(self) -> AsyncGenerator[Any, None]:
        """Poll the parameter route, yielding a reading each time it changes.

        REST has no push (see the module docstring). This is the polled substitute for
        MQTT's subscription queue, and what the framework's background sync task
        (``run_receive`` in ``transitional_sync_middleware``'s ``middleware.py``) actually drives northbound
        sync from. A connector with ``poll=False`` never yields, so the framework spawns and
        immediately retires its receive task with no network traffic.
        """
        if not self.poll:
            return
        last_value: Any = _UNSET
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                value = await self.provide()
            except Exception as e:  # noqa: BLE001 - a transient peer outage must not kill the loop
                logger.warning("Poll of %s failed: %s", self.url, e)
                continue
            if _is_a_change(value, last_value):
                last_value = value
                yield value


@semantic_connector
class RESTBinding:
    """Binds a live, generically interface-accessible parameter to a REST connector.

    Registers at the interface **root**, not a protocol-specific subproperty -- REST has no
    parameter-local marker (see the module docstring). ``wiring.py``'s ``_descriptor_for``
    already resolves the *most specific* registered match, and a protocol marker such as
    ``inf:isInterfaceAccessibleMQTTParameter`` is itself a subproperty of this root, so a
    parameter that also declares a specific protocol keeps resolving to that protocol's
    binding. This is the fallback for everything else -- any interface-accessible parameter
    whose resource happens to be live.
    """

    # ADR: 0023, 0028

    connector_cls: ClassVar[Any] = RESTParameterConnector
    interface_property: ClassVar[IRI] = INF.isInterfaceAccessibleParameter
    connection_metadata: ClassVar[Tuple[IRI, ...]] = ()
    """Empty on purpose. This binding reads nothing declared on the *parameter* -- its
    evidence is the resource's Service, one hop out (see the module docstring). The
    projection's cross-check compares this against what the ontology declares *between the
    parameter property and the interface root*, which is likewise nothing for a parameter with
    no protocol-specific marker, so the two agree."""

    # ADR: 0028

    @staticmethod
    def build(
        binding: ParameterBinding,
        direction: SyncDirection,
        *,
        ensure_transport: Optional[Callable[[str, int], None]] = None,
    ) -> Iterable[Registration]:
        """One read registration when the resource is live. One write registration too,
        when ``direction`` permits it.

        ``direction`` has already been reduced to the most restrictive of the parameter's
        ``inf:accessMode`` and the instance's connector wiring, so this only honours it.

        ``ensure_transport`` is unused. REST reaches a peer middleware, not a broker this
        deployment needs to bring up -- there is nothing here to ensure.
        """

        # ADR: 0023, 0034
        address = binding.get(SVC.address)
        if not address:
            # A generically interface-accessible parameter whose resource has no live
            # Service is not a REST-recognition failure -- it is the ordinary state of a
            # resource that has not (yet) registered itself. Name it, the same way MQTT
            # names a missing broker, so a parameter does not come up silently dead.
            logger.warning(
                "Do not bind %s on %s over REST. The resource carries no live %s. The "
                "parameter will be served but no value will flow.",
                binding.parameter_property,
                binding.resource_iri,
                SVC.address,
            )
            return
        if binding.root_iri is None or binding.root_class_local_name is None:
            # Recognition always fills these in (wiring.py's _recognise). Absence means a
            # ParameterBinding was built by hand outside that path -- nothing to derive a
            # route from.
            logger.warning(
                "Do not bind %s on %s over REST. No root resource context to derive a "
                "structural route from.",
                binding.parameter_property,
                binding.resource_iri,
            )
            return

        path = build_parameter_path(
            binding.root_class_local_name,
            binding.root_iri,
            binding.path_steps,
            binding.field_id,
        )
        formatter = _formatter_for(binding, url=f"{address}{path}")

        yield Registration(
            connector=RESTParameterConnector(address, path),
            sync_direction=SyncDirection.TO_PERSISTENCE,
            model_type=list,
            formatter=formatter,
            suffix="read",
        )

        if direction is not SyncDirection.BIDIRECTIONAL:
            return

        yield Registration(
            connector=RESTParameterConnector(address, path, poll=False),
            sync_direction=SyncDirection.FROM_PERSISTENCE,
            model_type=list,
            formatter=formatter,
            suffix="write",
        )


def _formatter_for(binding: ParameterBinding, url: str) -> RESTParameterFormatter:
    """Build the formatter for one parameter, labelled for its log lines."""
    return RESTParameterFormatter(
        model_type=binding.node_model_type,
        parameter_label=binding.label,
        url=url,
    )
