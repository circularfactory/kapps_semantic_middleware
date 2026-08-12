"""The controller is a middleware instance: SPARQL view, fetched datamodels, REST
connectors (#80, ADR 0033).

The controller is a resource-mode middleware instance that holds its own datamodels. It
never speaks raw HTTP itself. Its own driving connectors do that, exactly the way a
device-facing instance's MQTT connectors do (ADR 0022/0023). The Control Expert's five
steps (ADR 0033, ``CONTEXT.md``):

1. ``view()`` runs the caller's own SPARQL query -- the view -- and returns every IRI it
   binds to ``?resource``. The query is the whole view: which class, which liveness join
   (a live resource's Service carries ``svc:address``), and any heuristic that narrows it
   further are authored by the caller. Nothing here assumes a domain class.
2. ``wire_view()`` runs ``ogm.fetch`` per hit and recognizes its interface-accessible
   parameters, exactly like ``SemanticMiddleware.__init__`` does for its own resource --
   except every one of these roots is *someone else's* resource, reached over REST
   (ADR 0033), never MQTT: the registry it recognizes against excludes MQTT on purpose,
   so a unit's own broker stays that unit's own middleware's business.
3. Loading happens in an ``on_start_up`` callback (``_load_view_datamodels``): each hit's
   northbound (pruned) datamodel materializes into ``self.units``, keyed by resource IRI.
   No ``inf:hasMQTT*`` property survives the load (ticket #78's projection).
4. Registration happens in step 2, via ``wire_view()`` -- a plain call the caller makes
   after construction and before the app's lifespan starts (before ``run_server``).
   Connectors must exist before the lifespan connects them (ADR 0023), so wiring cannot
   wait for an async hook.
5. ``push()`` drives an in-place assignment on a loaded datamodel out to its owner. An
   algorithm reads and writes ``self.units[...]`` directly; nothing here or in the
   algorithm's body issues an HTTP call.

The controller still discovers resources by class IRI via ``discover_resources`` (ticket
#43), independent of the view mechanism above, and still derives REST parameter paths
via ``rest_binding.build_parameter_path`` (moved there by ticket #77). It registers
itself as a ControlStationService so it appears in its own discovery list.
"""

from __future__ import annotations

import asyncio
import enum
import functools
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import anyio
from transitional_sync_middleware.middleware.registries import ConnectionInfo
from kapps_triplestore_interface import IRI

from kapps_semantic_middleware import Mode, SemanticMiddleware
from kapps_semantic_middleware.connectors.rest_binding import RESTBinding
from kapps_semantic_middleware.connectors.rest_binding import (
    build_parameter_path as _rest_build_parameter_path,
)
from kapps_semantic_middleware.connectors.semantic import SemanticConnectorRegistry
from kapps_semantic_middleware.connectors.wiring import WiringPlan, plan_wiring
from kapps_semantic_middleware.vocabulary import RDFS, SVC

logger = logging.getLogger(__name__)

_VIEW_REGISTRY = SemanticConnectorRegistry([RESTBinding])
"""REST only, deliberately excluding MQTT (ADR 0033).

The ontology declares a TransferUnit's parameters MQTT-specific
(``tu:hasConveyorSpeed rdfs:subPropertyOf inf:isInterfaceAccessibleMQTTParameter``), and
recognition always resolves to the *most specific* registered descriptor
(``wiring._descriptor_for``) -- that is a TBox fact, and pruning the fetched instance
data cannot change it. A controller that recognized against ``default_registry`` (which
carries both bindings) would therefore try to dial a unit's own broker directly, exactly
the ad-hoc reach ADR 0033 exists to replace with REST. Excluding MQTT from the registry
used for view wiring is what makes REST the only match; the northbound projection then
independently guarantees no broker metadata survives the fetch either (belt and braces,
not two names for the same mechanism)."""


@dataclass
class ResourceInfo:
    """Metadata about a discovered resource, for listing in discovery.

    Attributes:
        resource_iri: The individual's IRI.
        resource_type: The class IRI (e.g. tu:TransferUnit).
        label: Human-readable label, if present.
        address: The service's svc:address — presence means live, absence means offline.
        last_heartbeat: The service's svc:lastHeartbeat timestamp, if present.
    """

    resource_iri: IRI
    """The individual's IRI."""

    resource_type: IRI
    """The class IRI (e.g. tu:TransferUnit)."""

    label: Optional[str]
    """Human-readable label, if present."""

    address: Optional[str]
    """The service's svc:address. Presence means live. Absence means offline."""

    last_heartbeat: Optional[str]
    """The service's svc:lastHeartbeat timestamp, if present."""

    @property
    def is_live(self) -> bool:
        """Live iff svc:address is present (MVP liveness, ticket #43)."""
        return self.address is not None


@dataclass
class ViewDiff:
    """What one ``rebuild_view()`` call changed, or why it changed nothing (#82).

    A malformed heuristic and a zero-hit heuristic are both reported here rather than
    raised: ``error`` set is the malformed case (the caller shows it in place, never a
    500); every list empty with ``error`` unset is the legitimate "selected nothing"
    outcome, not a failure. ``station_board.py``'s ``/api/view/run`` turns this into the
    page's inline message either way.
    """

    joiners: List[IRI] = field(default_factory=list)
    """Hits new to this rebuild -- fetched, pruned, wired and loaded before return."""

    leavers: List[IRI] = field(default_factory=list)
    """Hits the previous rebuild had that this one does not -- torn down before return."""

    unchanged: List[IRI] = field(default_factory=list)
    """Hits present both times -- left alone, per #82's "leave the unchanged alone"."""

    error: Optional[str] = None
    """Set when the heuristic itself failed (malformed SPARQL). None otherwise."""


@dataclass
class CommandedValue:
    """What was last written to one parameter, and who wrote it (#82).

    The served datamodel carries only the observed value (ADR 0024's locator pattern
    means the graph, and so the fetched tree, holds no separate setpoint field), so this
    is the *only* place "what did we ask for" is knowable at all -- not a convenience
    cache of something visible elsewhere. Both write paths must fill it: a human via
    ``station_board.py``'s set route, and the algorithm via ``algorithm.run_algorithm_once``,
    each calling ``controller.writes.record_commanded`` immediately before ``push()``.
    """

    value: Any
    at: float
    """``time.monotonic()`` at the moment this was recorded, for a UI's "how long has
    this been converging" display -- monotonic rather than wall-clock because nothing
    here compares it across a process restart."""

    origin: str
    """"operator" or "algorithm" -- distinguishes who last commanded this parameter."""


class WriteStatus(str, enum.Enum):
    """What became of the last write to one parameter (#82).

    A plain ``str`` subclass for the same reason ``AlgorithmMode`` is one: a status
    round-trips through JSON with no translation layer between the tracker, the page's
    badge and a test's assertion.

    #82 words the progression ``sending`` -> ``settled`` | ``rejected`` | ``diverged``.
    ``sending`` is not a member here because it is not a *judgement* -- it is simply the
    moment between the click and the first observation, and the page shows it locally.
    :attr:`CONVERGING` is what that moment becomes as soon as there is something to
    observe, and under #83's ramp it is where a healthy write spends most of its life.
    """

    SETTLED = "settled"
    """The actual value came back matching what was commanded."""

    CONVERGING = "converging"
    """Accepted, still moving toward the command -- the normal state during #83's ramp."""

    DIVERGED = "diverged"
    """Accepted, but the actual value has *stopped converging*. Not "unequal": #83's ramp
    makes commanded and actual unequal during every set by design (#81, amending #31), so
    an equality test would fire on every write."""

    REJECTED = "rejected"
    """The PUT failed outright -- unit down, 4xx, bad payload. Carries a reason."""


_SETTLED_RELATIVE_TOLERANCE = 1e-2
"""A value that has made a round trip through MQTT, JSON and a float is never bit-exact,
so an exact comparison would leave every write reading ``converging`` forever."""

_SETTLED_ABSOLUTE_TOLERANCE = 1e-9

DEFAULT_STILL_SECONDS = 6.0
"""How long a value may sit unmoved, short of its command, before the write is called
diverged.

Measured in *seconds*, not in polls: a poll is a browser asking, so counting polls would
make two open tabs declare divergence in half the time and a closed page never declare it
at all. One quiet moment is a slow lap -- #82's "the tick must exceed one lap" constraint
says a lap can straddle a poll -- so this must exceed a lap while staying under the
default tick (8.0 s), or the algorithm would overwrite a stuck value before the board
ever reported it.
"""


def _is_close(a: Any, b: Any) -> bool:
    """Whether two observed values count as the same value."""
    if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return a == b
    difference = abs(a - b)
    if difference < _SETTLED_ABSOLUTE_TOLERANCE:
        return True
    return difference / max(abs(a), abs(b), 1.0) < _SETTLED_RELATIVE_TOLERANCE


@dataclass
class _Observation:
    """The last value seen for one parameter, and when it last actually moved."""

    last_value: Any
    unchanged_since: float


class WriteTracker:
    """Per parameter: what was last commanded, and whether the device is converging on it
    (#82's write states -- see :class:`WriteStatus`).

    This is its own object, not a handful of dicts on :class:`Controller`, because the
    judgement it makes is the one #82 requires to be provable *in a test*: the
    classification began in ``station_board.html``'s script, where pytest cannot reach
    it. Nothing here touches the graph, a connector or the network -- it is a state
    machine over observations, which is what lets its tests run with no fixtures at all.

    The served datamodel carries only the observed value (ADR 0024's locator pattern
    means the graph holds no separate setpoint field), so this is the *only* place "what
    did we ask for" is knowable at all. Both write paths must record: a human via
    ``station_board.py``'s set route, and the algorithm via ``run_algorithm_once``.

    Args:
        still_seconds: How long a value may sit unmoved, short of its command, before
            the write is called diverged. See :data:`DEFAULT_STILL_SECONDS`.
        clock: Monotonic seconds source. Injectable so a test can age a write without
            sleeping through it.
    """

    def __init__(
        self,
        *,
        still_seconds: float = DEFAULT_STILL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._still_seconds = still_seconds
        self._clock = clock
        self._commanded: Dict[str, CommandedValue] = {}
        self._rejected: Dict[str, str] = {}
        self._observations: Dict[str, _Observation] = {}

    @staticmethod
    def _key(component_iri: Union[str, IRI], field_name: str) -> str:
        return f"{component_iri}#{field_name}"

    @staticmethod
    def _scalar(value: Any) -> Any:
        """The comparable form of a written value.

        ``inf:hasValue`` is a one-element list on the datamodel, so both write paths hand
        over ``[4.2]`` while every observation reads back the scalar ``4.2``. Normalising
        here rather than at each caller is what keeps the two paths honest: the operator's
        route and the algorithm's tick disagreed on this exact point, and the algorithm's
        writes could never reach ``settled`` as a result.
        """
        if isinstance(value, (list, tuple)) and len(value) == 1:
            return value[0]
        return value

    def record_commanded(
        self, component_iri: Union[str, IRI], field_name: str, value: Any, *, origin: str
    ) -> None:
        """Record what was just written, and who wrote it. Clears any earlier rejection
        and restarts the convergence watch -- a retry after the unit came back must not
        read as rejected forever, nor inherit the previous write's stillness."""
        key = self._key(component_iri, field_name)
        self._commanded[key] = CommandedValue(
            value=self._scalar(value), at=self._clock(), origin=origin
        )
        self._rejected.pop(key, None)
        self._observations.pop(key, None)

    def record_rejected(
        self, component_iri: Union[str, IRI], field_name: str, error: str
    ) -> None:
        """Record that a write failed outright, with the reason to show. Recorded here
        rather than in the page so it survives a reload -- and so a test can see it."""
        self._rejected[self._key(component_iri, field_name)] = error

    def commanded_for(
        self, component_iri: Union[str, IRI], field_name: str
    ) -> Optional[CommandedValue]:
        """The last :class:`CommandedValue` recorded, or ``None`` if this controller has
        never written to this parameter."""
        return self._commanded.get(self._key(component_iri, field_name))

    def error_for(
        self, component_iri: Union[str, IRI], field_name: str
    ) -> Optional[str]:
        """The reason the last write was rejected, or ``None``."""
        return self._rejected.get(self._key(component_iri, field_name))

    def observe(
        self, component_iri: Union[str, IRI], field_name: str, actual: Any
    ) -> Optional[WriteStatus]:
        """Record what was just observed for one parameter and return the write's status,
        or ``None`` if this controller has never commanded it (a value nobody here drove
        gets no badge -- there is nothing to compare it against).

        Called once per parameter per poll. Only *movement* is recorded, and divergence is
        judged against the clock, so calling this more often -- a second browser tab, a
        manual refresh -- cannot make a write diverge any sooner.
        """
        key = self._key(component_iri, field_name)

        if key in self._rejected:
            return WriteStatus.REJECTED

        commanded = self._commanded.get(key)
        if commanded is None:
            return None

        now = self._clock()
        observation = self._observations.get(key)
        if observation is None or not _is_close(actual, observation.last_value):
            observation = _Observation(last_value=actual, unchanged_since=now)
            self._observations[key] = observation

        if _is_close(actual, commanded.value):
            return WriteStatus.SETTLED
        if now - observation.unchanged_since >= self._still_seconds:
            return WriteStatus.DIVERGED
        return WriteStatus.CONVERGING

    def drop(self, parameters: Sequence[Tuple[Union[str, IRI], str]]) -> None:
        """Forget every named parameter -- a departed view hit's state must not outlive
        it, or a unit that leaves and rejoins shows a stale command.

        Takes the parameters explicitly rather than matching an IRI prefix: the keys are
        *component* IRIs (``ConveyorBelt1_left``), not the unit's own (``TransferUnit1``),
        so a prefix match on the unit IRI silently matches nothing at all. The caller
        reads them off the leaver's own :class:`WiringPlan`.
        """
        for component_iri, field_name in parameters:
            key = self._key(component_iri, field_name)
            self._commanded.pop(key, None)
            self._rejected.pop(key, None)
            self._observations.pop(key, None)


class Controller(SemanticMiddleware):
    """A resource-mode middleware that holds its own datamodels (ADR 0033, ticket #80).

    ``view()`` runs the caller's SPARQL query and returns the resource IRIs it binds.
    ``wire_view()`` recognizes each hit's interface-accessible parameters and registers
    a driving REST connector for every one it finds -- reached over REST, never MQTT
    (``_VIEW_REGISTRY`` excludes it). Loading happens on startup: each hit's pruned
    datamodel materializes into ``self.units``, keyed by resource IRI string. An
    algorithm reads and writes those loaded objects directly and calls ``push()`` to
    drive an assignment out -- no HTTP call anywhere in its body; the registered
    connector's own ``consume()`` performs that.

    It also discovers resources by class IRI (``discover_resources``, ticket #43) and
    registers itself as a ControlStationService, so it appears in its own discovery
    list, independent of whatever view it wires.

    Usage::

        controller = Controller(
            resource_iri="http://example.org/ControlStation1",
            service_class=CFC.Resource,
            ogm=ogm,
            host="127.0.0.1",
            port=8080,
        )

        # Step 1: the view is the query. No domain class is named here.
        hits = controller.view(VIEW_QUERY)

        # Steps 2-4: recognize and register a driving connector for every hit. Must
        # run before the app's lifespan starts (before uvicorn's serve()).
        controller.wire_view(hits, class_scope=UNIT_SCOPE)

        # ... uvicorn serves the app; on_start_up loads every hit into self.units ...

        # Step 5: the algorithm assigns to its own objects, then pushes.
        unit = controller.units[str(hits[0])]
        belt = getattr(unit, TU_HAS_CONVEYOR_BELT.lined)[0]
        speed = getattr(belt, TU_HAS_CONVEYOR_SPEED.lined)[0]
        setattr(speed, INF.hasValue.lined, [50.0])
        await controller.push(hits[0])
    """

    def __init__(
        self,
        *,
        resource_iri: str,
        service_class: Optional[str] = None,
        ogm: Any = None,
        host: str = "127.0.0.1",
        port: int = 8000,
        named_graph: Optional[str] = None,
        heartbeat_interval: Optional[float] = 30.0,
        staleness_threshold: float = 90.0,
        activity_feed: bool = False,
        activity_capacity: int = 200,
    ) -> None:
        """Initialize the controller middleware.

        Args:
            resource_iri: The IRI of this control station resource.
            service_class: The Service class IRI. It must be svc:Service or
                a subclass. It defaults to svc:Service itself. Ticket #66's
                fac:ControlStationService is not yet an ontology class in
                this repo, so pass it explicitly once that class exists.
            ogm: The OGM instance for graph interactions.
            host: Host to bind the REST API on.
            port: Port to bind the REST API on.
            named_graph: Named graph for resource data.
            heartbeat_interval: Interval for heartbeat updates.
            staleness_threshold: Threshold for considering a resource stale.
            activity_feed: Whether to enable activity feed.
            activity_capacity: Capacity of the activity feed ring buffer.
        """
        # The controller is a resource-mode instance with its own Service registration.
        # It deliberately does NOT use class_scope or autoregister_connectors because
        # it has no physical device to connect to — it talks REST to other resources.
        #
        # service_class must be svc:Service or a subclass (register_service enforces
        # this against the graph). cfc:Resource, the original default here, is not
        # one, and register_service rejected it outright. svc:Service always passes,
        # since assert_class_registered short-circuits when class_iri == base_iri.
        super().__init__(
            mode=Mode.RESOURCE,
            resource_iri=resource_iri,
            service_class=service_class or str(SVC.Service),
            ogm=ogm,
            host=host,
            port=port,
            named_graph=named_graph,
            heartbeat_interval=heartbeat_interval,
            staleness_threshold=staleness_threshold,
            class_scope=None,  # No device interface to prune
            # Not "no connectors" -- `wire_view` registers a REST connector per driveable
            # parameter of every view hit, six per unit in this demo. What this flag turns
            # off is recognition *at construction*, against this controller's own resource:
            # a control station is not a device and has no parameters of its own to bind.
            # Its connectors come from the view, which does not exist yet at this point.
            autoregister_connectors=False,
            activity_feed=activity_feed,
            activity_capacity=activity_capacity,
        )

        # The view mechanism (#80, ADR 0033). `units` holds each view hit's loaded
        # northbound datamodel, keyed by `str(resource_iri)` -- what an algorithm reads
        # and mutates directly. `_view_wirings` is the recognition this instance ran for
        # each hit, kept so the on_start_up callback below knows what to fetch.
        # `_view_load_scheduled` guards against registering that callback twice, should
        # `wire_view` ever be called again.
        self.units: Dict[str, Any] = {}
        self._view_wirings: List[Tuple[IRI, WiringPlan]] = []
        self._view_load_scheduled = False

        # Filled by the first wire_view() call and reused by every later rebuild_view()
        # call, so a caller re-running the heuristic never has to repeat the class scope
        # or the registry it wired with the first time (#82).
        self._view_class_scope: Any = None
        self._view_resource_class: Optional[IRI] = None
        self._view_registry: Optional[SemanticConnectorRegistry] = None

        # Held for the duration of one rebuild_view() call. algorithm.run_algorithm_once
        # checks `.locked()` and skips its tick rather than racing a rebuild in progress
        # -- the lock itself *is* #82's "the algorithm auto-pauses across a rebuild and
        # resumes", not a separate flag that could drift out of step with it.
        self.rebuild_lock = asyncio.Lock()

        # What was last commanded on each parameter and whether the device is converging
        # on it (see WriteTracker's docstring for why this exists at all).
        self.writes = WriteTracker()

    def view(self, sparql_query: str) -> List[IRI]:
        """Run the caller's SPARQL query -- the view (ADR 0033 step 1) -- and return
        every IRI it binds to ``?resource``, in result order, deduplicated.

        The query is the whole view. Which class, the liveness join (a live resource's
        Service carries ``svc:address``), and any heuristic that narrows the result
        further are all authored by the caller, in the query text. Nothing here assumes
        a domain class or a column beyond ``?resource`` itself, so no rework here can
        ever compile a domain class into the controller.

        Args:
            sparql_query: A SPARQL ``SELECT`` binding at least ``?resource``.

        Returns:
            The distinct ``?resource`` bindings, in the order the query returned them.
        """
        result = self.ogm.db.query(sparql_query, convert_bindings=True)
        bindings = (
            result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
        )

        resource_iris: List[IRI] = []
        seen: set = set()
        for binding in bindings:
            if "resource" not in binding:
                continue
            iri = IRI(str(binding["resource"]))
            key = str(iri)
            if key in seen:
                continue
            seen.add(key)
            resource_iris.append(iri)
        return resource_iris

    def wire_view(
        self,
        resource_iris: Sequence[Union[str, IRI]],
        *,
        class_scope: Any,
        resource_class: Optional[Union[str, IRI]] = None,
        registry: Optional[SemanticConnectorRegistry] = None,
    ) -> None:
        """Recognize every view hit and register its driving REST connectors (ADR 0033
        steps 2-4).

        Call this once, after ``view()`` and before the app's lifespan starts (before
        ``run_server`` / uvicorn's own ``serve()``). ``SemanticMiddleware.__init__``
        documents why this cannot wait for an ``on_start_up`` hook: ``lifespan`` calls
        ``connect()`` on every registered connector *before* running ``on_start_up``,
        so a connector registered afterwards never connects and its listener never
        starts (ADR 0023). ``_wire_semantic_connectors`` runs from ``__init__`` for
        exactly this reason; this mirrors it for N foreign roots instead of one.

        ``class_scope`` is the Control Expert's own view of one hit's shape (ADR 0018) --
        domain-specific, and deliberately not this method's business to construct.
        ``resource_class`` defaults to ``None``, which lets ``plan_wiring`` read each
        hit's own ``rdf:type`` from the graph rather than take one class for every hit
        on faith.

        ``registry`` defaults to :data:`_VIEW_REGISTRY` (REST only, no MQTT) -- see its
        docstring for why. Fetching and persisting each hit's datamodel is deferred to
        an ``on_start_up`` callback (``_load_view_datamodels``): unlike connector
        registration, nothing about ``ogm.fetch``/``persist`` needs to run before the
        lifespan connects anything.
        """
        registry = registry or _VIEW_REGISTRY

        # Recorded so a later rebuild_view() call -- which only ever receives new query
        # text, per #82's editable-heuristic box -- can re-wire a joiner exactly the way
        # this call wired its own hits, with no second copy of these arguments anywhere.
        self._view_class_scope = class_scope
        self._view_resource_class = (
            IRI(str(resource_class)) if resource_class is not None else None
        )
        self._view_registry = registry

        # Register the loader BEFORE any add_synced_connector call below, and only
        # once, ever. Each add_synced_connector call schedules its own on_start_up
        # callback (`initiate_sync`) that looks up the "resource" persistence connector
        # for this hit's model_id -- a KeyError if it does not exist yet. on_start_up
        # callbacks run in registration order (the same constraint
        # SemanticMiddleware.__init__ documents for its own single-resource wiring), so
        # _load_view_datamodels (which calls persist(), creating that connector) must
        # land in the list first.
        if not self._view_load_scheduled:
            self.add_callback("on_start_up", self._load_view_datamodels)
            self._view_load_scheduled = True

        for resource_iri in resource_iris:
            resource_iri = IRI(str(resource_iri))
            wiring = plan_wiring(
                ogm=self.ogm,
                resource_iri=resource_iri,
                class_scope=class_scope,
                resource_class=IRI(str(resource_class)) if resource_class is not None else None,
                registry=registry,
                flavour=self.connector_sync_direction,
                autoregister=True,
                ensure_transport=self.ensure_transport,
            )
            self._view_wirings.append((resource_iri, wiring))

            for binding, registration in wiring.registrations:
                self.add_synced_connector(
                    connector_id=f"{binding.resource_iri}#{binding.field_id}#{registration.suffix}",
                    connector=registration.connector,
                    model_type=registration.model_type,
                    data_model_name="resource",
                    model_id=str(resource_iri),
                    contained_model_id=str(binding.resource_iri),
                    field_id=binding.field_id,
                    formatter=registration.formatter,
                    sync_role=registration.sync_role,
                    sync_direction=registration.sync_direction,
                )

            logger.info(
                "Wired %d connector(s) across %d parameter(s) on view hit %s",
                sum(1 for _ in wiring.registrations),
                len(wiring.bindings),
                resource_iri,
            )

    async def _load_one_hit(self, resource_iri: IRI, wiring: WiringPlan) -> bool:
        """Fetch, prune and persist one view hit's northbound datamodel into ``self.units``
        (ADR 0033 step 3).

        Extracted from what used to be ``_load_view_datamodels``'s only loop body (#82):
        the startup bulk loader below and ``rebuild_view``'s joiner path both need this
        exact fetch-and-persist step, and a second copy could drift from the first.
        ``WiringPlan.northbound_fetch_kwargs`` is the same pruned fetch
        ``SemanticMiddleware._load_resource_datamodel`` runs for its own resource (ticket
        #78): the materialized instance carries no ``inf:hasMQTT*`` property, regardless
        of what the graph holds for the unit's own middleware.

        ``persist`` registers the "resource" persistence connector each connector
        ``wire_view`` added was built against (keyed by this hit's own IRI as
        ``model_id``), which is what makes ``push()`` and the background read poll able
        to find it.

        A fetch failure (the resource vanished between ``wire_view()`` and this call, or
        a transient graph error) is caught and logged rather than left to propagate: an
        uncaught exception here would abort the caller's whole loop, and take down every
        *other* hit's loading with it -- one dead unit must not sink the whole factory's
        view (the same "fails visibly rather than silently" standard ADR 0033's
        acceptance criteria hold an already-wired unit to).

        Returns:
            Whether the hit was actually loaded. ``False`` on a fetch failure or an empty
            tree; both are logged, neither raises.
        """
        fetch = functools.partial(
            self.ogm.fetch, instance_iri=resource_iri, **wiring.northbound_fetch_kwargs()
        )
        try:
            node = await anyio.to_thread.run_sync(fetch)
        except Exception:
            logger.exception("View hit %s could not be fetched; not loaded.", resource_iri)
            return False
        instance = getattr(node, "instance", None)
        if instance is None:
            logger.warning(
                "View hit %s has no materializable datamodel; not loaded.", resource_iri
            )
            return False
        await self.persist("resource", instance)
        self.units[str(resource_iri)] = instance
        return True

    async def _load_view_datamodels(self) -> None:
        """Fetch and persist every view hit's northbound datamodel (ADR 0033 step 3).

        Runs once, from ``on_start_up``, after ``wire_view`` has already registered
        each hit's connectors. See ``_load_one_hit`` for the per-hit mechanics this
        delegates to.
        """
        # Pre-register the fallback persist() would build anyway, once, before the loop
        # below calls persist() per hit -- silences the base class's "No persistence
        # factory found" warning without changing which connector gets constructed (#89
        # item 6; see SemanticMiddleware._suppress_default_persistence_warning).
        self._suppress_default_persistence_warning("resource")

        loaded = 0
        for resource_iri, wiring in self._view_wirings:
            if await self._load_one_hit(resource_iri, wiring):
                loaded += 1

        if loaded:
            logger.info("Loaded %d resource(s) from the view onto this controller.", loaded)

    async def rebuild_view(self, sparql_query: str) -> ViewDiff:
        """Re-run the view and reconcile ``self.units`` against the new hit set: fetch +
        prune + load the joiners, close connectors and drop the leavers, leave the
        unchanged alone (#82's "live differential rebuild").

        Call this on every poll and on an explicit "run" -- both are the same operation
        here, so the card set tracks the graph whether or not anyone presses anything.
        ``sparql_query`` replaces whatever heuristic the previous call ran; there is no
        separate "the query changed" branch, because comparing the new hit set against
        ``self.units`` already produces the right answer whether the query text moved or
        only the graph did.

        A malformed heuristic raises inside ``view()`` (bad SPARQL syntax); caught here
        and returned as ``ViewDiff(error=...)`` rather than propagated, so a route calling
        this never 500s on a typo in the editable box. A zero-hit heuristic is not an
        error -- it is a ``ViewDiff`` whose ``joiners``/``unchanged`` are both empty and
        whose ``leavers`` may be everything, and the caller reports that in place too.

        Guarded by ``self.rebuild_lock`` for its wiring/teardown section: see the lock's
        own docstring for why nothing else pauses the algorithm separately.

        Args:
            sparql_query: A SPARQL ``SELECT`` binding at least ``?resource`` -- the same
                shape ``view()`` already expects.

        Returns:
            What changed, or why nothing did.
        """
        try:
            hits = self.view(sparql_query)
        except Exception as e:
            logger.warning("View heuristic failed; not rebuilding: %s", e)
            return ViewDiff(error=str(e))

        hit_by_key = {str(iri): iri for iri in hits}
        new_keys = set(hit_by_key)
        current_keys = set(self.units)
        joiner_keys = new_keys - current_keys
        leaver_keys = current_keys - new_keys
        unchanged_keys = new_keys & current_keys

        async with self.rebuild_lock:
            for key in leaver_keys:
                await self._unwire_hit(IRI(key))

            if joiner_keys:
                joiner_iris = [hit_by_key[key] for key in joiner_keys]
                self.wire_view(
                    joiner_iris,
                    class_scope=self._view_class_scope,
                    resource_class=self._view_resource_class,
                    registry=self._view_registry,
                )
                for resource_iri, wiring in self._view_wirings:
                    if str(resource_iri) in joiner_keys and str(resource_iri) not in self.units:
                        await self._load_one_hit(resource_iri, wiring)

        self._current_view_query = sparql_query
        logger.info(
            "View rebuilt: %d joiner(s), %d leaver(s), %d unchanged.",
            len(joiner_keys),
            len(leaver_keys),
            len(unchanged_keys),
        )
        return ViewDiff(
            joiners=[hit_by_key[k] for k in joiner_keys],
            leavers=[IRI(k) for k in leaver_keys],
            unchanged=[hit_by_key[k] for k in unchanged_keys],
        )

    async def _unwire_hit(self, resource_iri: IRI) -> None:
        """Tear down one departed view hit: cancel its connectors' receive loops,
        disconnect them, and drop every trace of it so a later rebuild's diff sees it as
        gone rather than unchanged (#82: "close connectors and drop the leavers").

        ``transitional_sync_middleware``'s own ``ConnectionRegistry.remove_connection`` is explicitly a
        partial cleanup -- its own source comment says "also delete connector and
        connection type" as a TODO -- so this pops all three of its dicts itself, for
        both ``connection_registry`` (the REST read/write connectors ``wire_view``
        registered for this hit) and ``persistence_registry`` (the "resource" connector
        ``persist()`` built for it). Root ADR 0001 keeps this fix here rather than
        patched into the sibling: it is a missing feature the base class's own TODO
        already names, not a correctness bug blocking anything else this project builds
        on that library. What it does not reach: the FastAPI routes
        ``add_synced_connector`` mounted for this hit's connectors stay registered --
        Starlette has no route-removal API at all, so a departed unit's parameter routes
        stay mounted but pointless. Accepted; the board's own display comes from
        ``self.units`` and never from route introspection.
        """
        key = str(resource_iri)
        wiring: Optional[WiringPlan] = None
        remaining: List[Tuple[IRI, WiringPlan]] = []
        for iri, w in self._view_wirings:
            if str(iri) == key:
                wiring = w
            else:
                remaining.append((iri, w))
        self._view_wirings = remaining

        if wiring is not None:
            for binding, registration in wiring.registrations:
                connector_id = (
                    f"{binding.resource_iri}#{binding.field_id}#{registration.suffix}"
                )
                connector = self.connection_registry.connectors.get(connector_id)
                if connector is not None:
                    for task in getattr(connector, "_background_tasks", []):
                        task.cancel()
                    try:
                        await connector.disconnect()
                    except Exception:
                        logger.debug(
                            "Disconnect of %s raised on teardown; ignoring.", connector_id
                        )
                self.connection_registry.connectors.pop(connector_id, None)
                self.connection_registry.connection_types.pop(connector_id, None)
                self.connection_registry.connections.pop(
                    ConnectionInfo(
                        data_model_name="resource",
                        model_id=key,
                        contained_model_id=str(binding.resource_iri),
                        field_id=binding.field_id,
                    ),
                    None,
                )

        persist_info = ConnectionInfo(data_model_name="resource", model_id=key)
        persist_connector_id = self.persistence_registry.connections.pop(persist_info, None)
        if persist_connector_id is not None:
            self.persistence_registry.connectors.pop(persist_connector_id, None)
            self.persistence_registry.connection_types.pop(persist_connector_id, None)

        self.units.pop(key, None)
        if wiring is not None:
            # Driven off the leaver's own bindings: the tracker's keys are *component*
            # IRIs (ConveyorBelt1_left), never the unit's own, so a prefix match on
            # `key` here would silently forget nothing at all.
            self.writes.drop(
                [(binding.resource_iri, binding.field_id) for binding in wiring.bindings]
            )
        logger.info("Dropped departed view hit %s.", resource_iri)

    def wiring_for(self, resource_iri: Union[str, IRI]) -> Optional[WiringPlan]:
        """The :class:`WiringPlan` recognized for one loaded view hit, or ``None`` if it
        was never wired (or has since been dropped by ``rebuild_view``).

        A display-only accessor for a station-board-style consumer that needs each
        parameter's access mode, human label, or what ``prune_southbound`` stripped for
        it (``WiringPlan.southbound_by_property``) -- reading ``self._view_wirings``
        directly from outside this class would reach into a private list this class is
        free to reshape; this is the seam such a caller should use instead.
        """
        key = str(resource_iri)
        for iri, wiring in self._view_wirings:
            if str(iri) == key:
                return wiring
        return None

    def liveness_of(self, resource_iri: Union[str, IRI]) -> Tuple[bool, Optional[float]]:
        """Whether a still-selected view hit is reachable, and the age of its last
        heartbeat in seconds (#82's two deaths).

        A cleanly stopped unit deregisters and drops its ``svc:address`` entirely, so
        ``view()`` stops selecting it -- ``rebuild_view`` sees it as a leaver, and its
        card leaves within one poll. A ``kill -9``'d unit keeps its address: ``view()``
        keeps selecting it, so it stays in ``self.units`` ("leave the unchanged alone"),
        but nothing refreshes its heartbeat. This reuses ``self.staleness_threshold`` --
        the same window ``SemanticMiddleware`` already tracks for its own watchdog (ADR
        0007) -- rather than a second threshold invented for the board.

        Returns:
            ``(unreachable, age_seconds)``. ``age_seconds`` is ``None`` when no heartbeat
            was ever recorded (unreachable is then ``True``).
        """
        info = self._get_service_info(IRI(str(resource_iri)))
        last_heartbeat = info.get("lastHeartbeat")
        if not last_heartbeat:
            return True, None
        try:
            heartbeat_at = datetime.fromisoformat(str(last_heartbeat))
        except ValueError:
            return True, None
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - heartbeat_at).total_seconds()
        return age_seconds > self.staleness_threshold, age_seconds

    async def push(self, resource_iri: Union[str, IRI]) -> None:
        """Drive an in-place assignment on a loaded view datamodel out to its owner
        (ADR 0033 step 5).

        The algorithm mutates ``self.units[str(resource_iri)]`` directly -- ``setattr``
        on the loaded pydantic tree, the ADR 0033 shape
        (``unit.conveyor_belt_left.speed[0]["inf:hasValue"][0] = 12.4``, mangled field
        names notwithstanding: see the class docstring for the real attribute names). A
        plain Python mutation calls out to nothing on its own, so this re-consumes the
        hit's own persistence connector, which fans the new value out to every synced
        connector sharing it (``PersistedConnector._notify_synced_connectors``) --
        among them the REST connector ``wire_view`` registered, whose own ``consume()``
        performs the PUT. No HTTP call happens here or needs to happen in the caller.

        Args:
            resource_iri: One of the IRIs ``view()`` returned and ``wire_view()`` wired.

        Raises:
            KeyError: ``resource_iri`` was never wired (or the app has not started yet,
                so nothing has been loaded and persisted).
        """
        connection_info = ConnectionInfo(data_model_name="resource", model_id=str(resource_iri))
        connector = self.persistence_registry.get_connection(connection_info)
        model = await connector.provide()
        await connector.consume(model)

    async def push_parameter(
        self,
        resource_iri: Union[str, IRI],
        component_iri: Union[str, IRI],
        field_name: str,
        node: Any,
    ) -> None:
        """Drive one specific parameter's write leg directly, bypassing ``push()``'s
        persistence-level fan-out (#82).

        ``push()`` re-consumes the whole resource through its persistence connector,
        which fans the new value out to every synced connector sharing it
        (``PersistedConnector._notify_synced_connectors``). That fan-out catches and
        only logs a sibling connector's failure, by the base framework's own design --
        its docstring's own reasoning is that one connector's failure must not silently
        end every other connector's sync (kapps_semantic_middleware#86). Correct for the
        fan-out's own purpose, but it means ``push()`` can never actually tell its
        caller a PUT failed: the exception never reaches this far. That is exactly what
        #82's "rejected" state needs to know, so a human-initiated write goes through
        this method instead, straight to the one connector responsible for this exact
        parameter -- a genuine failure (the peer down, a 4xx) then propagates to the
        caller rather than being swallowed and merely logged.

        Only the operator's own set control (``station_board.py``'s ``/api/set`` route)
        uses this. The algorithm keeps using ``push()``: a background loop's own write
        has nowhere useful to report a failure to, and ``push()``'s silent-fan-out
        behaviour costs it nothing it was relying on.

        Args:
            resource_iri: The root view hit that owns ``component_iri``.
            component_iri: The IRI of the belt/barrier/etc. that holds the parameter.
            field_name: The parameter's mangled field name (``IRI(...).lined``).
            node: The parameter's own current pydantic node (already mutated by the
                caller's ``setattr`` -- this method sends exactly this value, it does
                not re-derive one).

        Raises:
            KeyError: No write connector is registered for this exact parameter --
                either it is read-only, or ``resource_iri`` was never wired.
        """
        wiring = self.wiring_for(resource_iri)
        if wiring is None:
            raise KeyError(f"{resource_iri} is not a wired view hit")

        for binding, registration in wiring.registrations:
            if (
                str(binding.resource_iri) == str(component_iri)
                and binding.field_id == field_name
                and registration.suffix == "write"
            ):
                body = (
                    registration.formatter.serialize([node])
                    if registration.formatter is not None
                    else [node]
                )
                await registration.connector.consume(body)
                return

        raise KeyError(f"No write connector for {component_iri}#{field_name}")

    def discover_resources(self, resource_class: Any) -> List[ResourceInfo]:
        """List all individuals of resource_class with their service metadata.

        This is the standard discovery path. It uses ontology traversal, so the
        caller writes no SPARQL. Internally, it walks the ontology graph and finds
        each individual and its linked Service node.

        Args:
            resource_class: The resource class IRI or class object (e.g. tu:TransferUnit).

        Returns:
            List of ResourceInfo with address, heartbeat, and liveness status.
        """
        resource_class_iri = IRI(resource_class) if not isinstance(resource_class, IRI) else resource_class

        results: List[ResourceInfo] = []

        try:
            # Query for all individuals of the resource class that have a Service linked.
            sparql = f"""
            SELECT ?resource ?label ?svc ?addr ?hb WHERE {{
                ?resource a <{resource_class_iri}> .
                OPTIONAL {{ ?svc <{SVC.isServiceOf}> ?resource . }}
                OPTIONAL {{ ?svc <{SVC.address}> ?addr . }}
                OPTIONAL {{ ?svc <{SVC.lastHeartbeat}> ?hb . }}
                OPTIONAL {{ ?resource <{RDFS.label}> ?label . }}
            }}
            """
            result = self.ogm.db.query(sparql, convert_bindings=True)
            bindings = (
                result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
            )

            seen_resources = set()
            for binding in bindings:
                # With convert_bindings=True, each binding value is already a converted
                # Python object, such as an IRI or a str. It is not a raw {"value": ...}
                # dict. Index it directly. This matches registration.py's own pattern,
                # for example find_resource_operations.
                resource_iri_str = str(binding["resource"])
                if resource_iri_str in seen_resources:
                    continue
                seen_resources.add(resource_iri_str)

                resource_iri = IRI(resource_iri_str)
                label = binding.get("label")
                address = binding.get("addr")
                last_heartbeat = binding.get("hb")

                results.append(ResourceInfo(
                    resource_iri=resource_iri,
                    resource_type=resource_class_iri,
                    label=str(label) if label is not None else None,
                    address=str(address) if address is not None else None,
                    last_heartbeat=str(last_heartbeat) if last_heartbeat is not None else None,
                ))

        except Exception as e:
            logger.warning("Failed to discover resources of class %s: %s", resource_class_iri, e)
            # Return empty list on error. Discovery is best-effort.

        return results

    def _get_service_info(self, resource_iri: IRI) -> Dict[str, Optional[str]]:
        """Get the service metadata for a resource.

        Queries the graph for the Service linked to this resource via svc:isServiceOf,
        returning address and lastHeartbeat.

        Args:
            resource_iri: The resource's IRI.

        Returns:
            Dict with 'address' and 'lastHeartbeat' keys, both possibly None.
        """
        result: Dict[str, Optional[str]] = {"address": None, "lastHeartbeat": None}

        try:
            # Query for the Service linked to this resource via svc:isServiceOf.
            sparql = f"""
            SELECT ?svc ?addr ?hb WHERE {{
                ?svc <{SVC.isServiceOf}> <{resource_iri}> .
                OPTIONAL {{ ?svc <{SVC.address}> ?addr . }}
                OPTIONAL {{ ?svc <{SVC.lastHeartbeat}> ?hb . }}
            }}
            """
            db_result = self.ogm.db.query(sparql, convert_bindings=True)
            bindings = (
                db_result.get("results", {}).get("bindings", []) if isinstance(db_result, dict) else []
            )

            if bindings:
                binding = bindings[0]
                # Same convert_bindings=True shape as discover_resources. Index directly.
                addr_val = binding.get("addr")
                hb_val = binding.get("hb")

                if addr_val is not None:
                    result["address"] = str(addr_val)
                if hb_val is not None:
                    result["lastHeartbeat"] = str(hb_val)

        except Exception as e:
            logger.debug("Failed to get service info for %s: %s", resource_iri, e)

        return result


    @staticmethod
    def _derive_parameter_path(
        datamodel_tree: Dict[str, Any],
        resource_class_local_name: str,
        resource_iri: IRI,
        steps: Sequence[Tuple[str, str]],
        terminal_field: str,
    ) -> str:
        """Derive a structural REST URL for a parameter by traversing a fetched datamodel tree.

        Mirrors rest_router.py's _accumulate_routes logic. It operates on a plain
        dict/list JSON tree, not on pydantic models.
        This method checks that every hop's child id exists in the tree, then
        builds the path. Use this method, not rest_binding.build_parameter_path, when the ids
        come from outside the tree and need a check.

        A terminal parameter is a field whose value is a list of dicts WITHOUT an "id" key
        (they carry hasValue/hasUnit/accessMode instead). A non-terminal field's value is a
        list of dicts WITH "id" keys, and recursion continues into the matching child.

        Args:
            datamodel_tree: The full JSON tree of the resource's datamodel.
            resource_class_local_name: Fragment of the resource's class IRI (e.g. "TransferUnit").
            resource_iri: The root resource's own IRI.
            steps: Sequence of (field_name, child_id) hops to navigate the tree.
            terminal_field: The final field name (the parameter itself).

        Returns:
            The structural URL path.

        Raises:
            ValueError: A step's field is not a list, or its child_id is not present.
        """
        current_level = datamodel_tree
        for field_name, child_id in steps:
            field_value = current_level.get(field_name, [])
            if not isinstance(field_value, list):
                raise ValueError(f"Field {field_name} is not a list in the datamodel tree")

            child_dict = next(
                (item for item in field_value if isinstance(item, dict) and item.get("id") == child_id),
                None,
            )
            if child_dict is None:
                raise ValueError(f"Child with id {child_id} not found under field {field_name}")

            current_level = child_dict

        return _rest_build_parameter_path(
            resource_class_local_name, resource_iri, steps, terminal_field
        )
