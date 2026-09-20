"""KAPPS Semantic Middleware.

Extends transitional_sync_middleware.Middleware with knowledge-graph registration, discovery,
and execution capabilities. Supports three modes -- see `modes.Mode`
for what each one means and which are implemented.

Operation execution: an Operation resolves via its implemented
Capability to a Workflow endpoint, which the system then invokes over HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import logging
from typing import Any, Callable, Dict, List, Optional

import anyio
import httpx
from fastapi import APIRouter
from fastapi.responses import Response
from kapps_triplestore_interface import IRI
from pydantic import BaseModel

from transitional_sync_middleware import Middleware
from transitional_sync_middleware.connect.connectors.model_connector import ModelConnector
from transitional_sync_middleware.middleware.persistence_factory import PersistenceFactory
from transitional_sync_middleware.middleware.registries import ConnectionInfo
from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection

from kapps_semantic_middleware.connectors.semantic import (
    SemanticConnectorRegistry,
    default_registry,
)
from kapps_semantic_middleware.connectors.wiring import WiringPlan, plan_wiring
from kapps_semantic_middleware import modes
from kapps_semantic_middleware.modes import Mode
from kapps_semantic_middleware.activity import ActivityFeed, enable_activity_feed
from kapps_semantic_middleware.rest_router import generate_recursive_rest_api
from kapps_semantic_middleware.registration import (
    HandoverPreconditionError,
    OperationQueueEmpty,
    OperationResolutionError,
    build_event_trigger_url,
    build_state_endpoint,
    build_workflow_endpoint,
    counterpart_has_complementary_ability,
    create_operation,
    deregister_service,
    find_possession_state,
    find_resource_operations,
    mark_operation_failed,
    mint_capability_iri,
    mint_operation_iri,
    mint_service_iri,
    mint_state_property_iri,
    mint_workflow_iri,
    record_terminal_status,
    register_service,
    register_state_property,
    register_workflow,
    resolve_dispatch_target,
    resolve_operation_workflow,
    revert_operation,
    set_operation_status,
    switch_possession,
    sweep_stale_services,
    update_heartbeat,
)
from kapps_semantic_middleware.vocabulary import OperationStatus

logger = logging.getLogger(__name__)

__all__ = ["SemanticMiddleware", "OperationResolutionError", "Mode"]


class _EventTriggerPayload(BaseModel):
    """REST body of the event trigger: the IRI of the Operation now in the graph."""

    # `str`, not `IRI`, and deliberately so. `IRI.__get_pydantic_core_schema__` returns
    # `any_schema()` — permissive by design, on the grounds that IRI validates itself — so
    # annotating this `IRI` would neither validate nor convert the incoming value: the
    # attribute would hold a plain `str` while claiming to be an `IRI`. Taking `str` at the
    # boundary and converting explicitly in `_handle_event_trigger` puts the validation where
    # a malformed IRI actually raises. The safer-looking annotation is the less safe one.
    operation_iri: str


class _OperationDraft:
    """Mutable draft yielded by ``request(...)``.

    The dispatch body populates ``data`` (domain property-IRI str -> list of values) and
    can read ``iri`` (the minted Operation IRI). The atomic exit turns it into a graph
    Operation and fires the event trigger.
    """

    def __init__(self, iri: IRI) -> None:
        self.iri = iri
        self.data: Dict[str, list] = {}


class _ClaimedOperation:
    """Handle yielded by ``claim_next(...)`` for pull-and-run execution.

    ``operation`` is the re-fetched Operation (a `kapps_ogm` Node — hydrated under the
    domain-supplied ClassScope, or a bare reference when no scope was given). The body reads
    it to decide what work to do. It sets ``result`` to the outcome, which is recorded as
    ``svc:executionResult`` in the terminal transition.
    """

    def __init__(self, iri: IRI, operation: Any) -> None:
        self.iri = iri
        self.operation = operation
        self.result: Optional[str] = None


class SemanticMiddleware(Middleware):
    """KAPPS Semantic Middleware extending transitional_sync_middleware.Middleware.

    Adds knowledge-graph registration, discovery, and execution. It supports the
    three modes of :class:`~kapps_semantic_middleware.modes.Mode`. Resource mode
    registers the Service/Workflow/Capability instances on startup, and deregisters
    them on shutdown. It is the only mode with runtime consequence today.

    ``mode`` accepts a ``Mode`` constant or the equivalent bare string -- ``Mode`` is a
    ``str`` subclass, so existing ``mode="resource"`` callers are unaffected.

    Operation execution: an Operation resolves via its implemented
    Capability to a Workflow endpoint, which is then invoked over HTTP.
    """

    #: What this product calls itself. The base class names the framework it is built on,
    #: which is accurate for the framework and wrong for anything hitting *our* address --
    #: a visitor landing on a middleware instance should be told what it reached. The
    #: dependency policy allows only bugfixes in sibling repos, so the name is corrected here rather
    #: than there: the base class stays as it is, and every override lives on this side.
    APP_TITLE = "semantic-middleware"
    APP_DESCRIPTION = (
        "The KAPPS Semantic Middleware. It reads a resource out of a knowledge graph, "
        "wires its connectors from what it finds there, and serves the resulting datamodel."
    )
    APP_WELCOME = "Welcome to semantic middleware!"
    APP_CONTACT = {"name": "Etienne Hoffmann", "email": "etienne.hoffmann@kit.edu"}

    @staticmethod
    def _app_version() -> str:
        """This package's version, not the framework's.

        The base class defaults to ``transitional_sync_middleware.VERSION``, so an unmodified header
        advertises the framework release as though it were this product's. Read from
        installed metadata rather than hardcoded, so it tracks ``pyproject.toml`` and
        cannot drift. Falls back when the package is run from a tree that was never
        installed.
        """
        try:
            from importlib.metadata import version

            return version("kapps-semantic-middleware")
        except Exception:  # pragma: no cover - only when run uninstalled
            return "0+unknown"

    def _rebrand(self) -> None:
        """Name this product rather than the framework, in the OpenAPI metadata.

        Every field the ``/docs`` header renders comes from here: the title (which is also
        the browser-tab name), the description, the version badge and the contact link. The
        base class fills all four in for the framework it ships, which is accurate for the
        framework and wrong on *our* address.

        It has to run before anything touches ``self.app``, because the base class builds
        the FastAPI object once and caches it, reading the metadata at that moment.
        """
        self.set_meta_data(
            title=self.APP_TITLE,
            description=self.APP_DESCRIPTION,
            version=self._app_version(),
            contact=self.APP_CONTACT,
        )

    @property
    def app(self):
        """The FastAPI app, with its root route renamed and a quiet favicon route added.

        The base class registers ``GET /`` inside its own ``app`` property, so the route
        exists the moment the app does. Starlette matches routes in order and a second
        registration would never be reached, so the original is removed rather than
        shadowed. Guarded by a flag: the base property caches, but this one is consulted
        on every access, and neither patch below may run twice on the same app object.

        The favicon route answers ``GET /favicon.ico`` with a bare 204: this
        product ships no icon asset, and every browser that requests one otherwise logs
        a 404 -- the only console error a control station or a unit middleware's page
        would otherwise show on a clean load.

        The nested ``root`` function handles ``GET /`` requests. It returns the welcome
        message defined in ``APP_WELCOME``.

        The nested ``favicon`` function handles ``GET /favicon.ico`` requests. It returns
        an empty 204 response to suppress browser console errors.
        """
        app = Middleware.app.fget(self)  # type: ignore[attr-defined]
        if not getattr(app, "_kapps_root_replaced", False):
            app._kapps_root_replaced = True
            for route in list(app.routes):
                methods = getattr(route, "methods", None) or set()
                if getattr(route, "path", None) == "/" and "GET" in methods:
                    app.routes.remove(route)

            welcome = self.APP_WELCOME

            @app.get("/", response_model=str)
            async def root() -> str:
                """Handle GET / requests. Return the welcome message."""
                return welcome

            @app.get("/favicon.ico", include_in_schema=False)
            async def favicon() -> Response:
                """Handle GET /favicon.ico requests. Return an empty 204 response."""
                return Response(status_code=204)

            # The schema is generated lazily and cached. Drop any copy built before the
            # swap, so /docs and /openapi.json describe the routes that actually exist.
            app.openapi_schema = None
        return app

    def __init__(
        self,
        *,
        mode: Mode = Mode.RESOURCE,
        resource_iri: Optional[str] = None,
        service_class: Optional[str] = None,
        ogm: Any = None,
        host: str = "127.0.0.1",
        port: int = 8000,
        address: Optional[str] = None,
        named_graph: Optional[str] = None,
        heartbeat_interval: Optional[float] = 30.0,
        staleness_threshold: float = 90.0,
        sweep_interval: float = 30.0,
        class_scope: Any = None,
        autoregister_connectors: bool = True,
        connector_sync_direction: SyncDirection = SyncDirection.BIDIRECTIONAL,
        connector_registry: Optional[SemanticConnectorRegistry] = None,
        ensure_transport: Optional[Callable[[str, int], None]] = None,
        activity_feed: bool = False,
        activity_capacity: int = 200,
    ) -> None:
        """Initialize the KAPPS Semantic Middleware instance.

        Configure the middleware for one of three modes: RESOURCE, WATCHDOG, or SERVER.
        Resource mode registers services in the knowledge graph and exposes REST APIs.
        Watchdog mode monitors service liveness and sweeps stale registrations.
        Server mode is not yet implemented.

        Parameters:
            mode: The operating mode. Accepts a Mode constant or equivalent string.
            resource_iri: The IRI of the resource this middleware represents (resource mode).
            service_class: The ontology class IRI for the service (resource mode).
            ogm: The ontology graph manager for knowledge graph operations.
            host: The HTTP server host address. Default is "127.0.0.1".
            port: The HTTP server port. Default is 8000.
            address: The full service address. Defaults to http://{host}:{port}.
            named_graph: The target named graph in the knowledge graph.
            heartbeat_interval: Seconds between heartbeat updates (resource mode).
            staleness_threshold: Seconds before a service is considered stale (watchdog mode).
            sweep_interval: Seconds between staleness sweeps (watchdog mode).
            class_scope: The consumer's view for parameter recognition and wiring.
            autoregister_connectors: Enable automatic connector registration. Default is True.
            connector_sync_direction: The sync direction for connectors. Default is BIDIRECTIONAL.
            connector_registry: The semantic connector registry. Uses default_registry if None.
            ensure_transport: A callback to start transport brokers before connector creation.
            activity_feed: Enable the activity feed for observability. Default is False.
            activity_capacity: Maximum events to retain in the activity feed ring buffer.

        Resource mode requires resource_iri, service_class, and ogm. Watchdog mode requires ogm.
        The constructor registers startup and shutdown callbacks for service lifecycle management.
        It also configures liveness monitoring, connector wiring, and optional activity feeds.
        """
        super().__init__()
        self._rebrand()

        if mode not in modes.ALL:
            raise ValueError(
                f"mode must be one of {', '.join(repr(str(m)) for m in modes.ALL)}; "
                f"got {mode!r}"
            )

        self.mode = mode
        self.ogm = ogm
        self.host = host
        self.port = port
        self.named_graph = named_graph
        self.address = address or f"http://{host}:{port}"

        # Liveness.
        self.heartbeat_interval = heartbeat_interval
        self.staleness_threshold = staleness_threshold
        self.sweep_interval = sweep_interval
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._sweep_task: Optional[asyncio.Task] = None

        if mode == Mode.RESOURCE:
            missing = []
            if resource_iri is None:
                missing.append("resource_iri")
            if service_class is None:
                missing.append("service_class")
            if ogm is None:
                missing.append("ogm")
            if missing:
                raise ValueError(f"resource mode requires: {', '.join(missing)}")

            self.resource_iri = IRI(resource_iri)
            self.service_class = IRI(service_class)
            self.service_iri = mint_service_iri(self.resource_iri, self.address)

            # Connector wiring. The class_scope is the consumer's view,
            # rooted at this resource and configured in embedding code rather than in the
            # ontology -- a TransferUnit's parameters hang off its belts and
            # barriers, so reaching them needs a two-level chain the ontology cannot guess.
            self.class_scope = class_scope
            self.autoregister_connectors = autoregister_connectors
            self.connector_sync_direction = connector_sync_direction
            self.connector_registry = connector_registry or default_registry
            # The transport seam a binding calls before building its first connector for a
            # declared address. None means exactly today's behaviour: the library
            # never starts, probes, or stops a broker on its own.
            self.ensure_transport = ensure_transport
            # None until a class_scope is given. Without a consumer's view, there is no
            # root for recognition, and the datamodel fetch stays unscoped, the same way
            # it does for scenarios 1 and 2.
            self._wiring: Optional[WiringPlan] = None
            # The parameter routes the recursive router generated, in tree order.
            # Populated at startup. Kept because the question "what did I actually expose?"
            # the user can otherwise answer only by filtering `app.routes` and re-deriving which
            # of them are parameters. A silently empty list is also the symptom of a tree
            # walk that missed a branch.
            self._parameter_routes: List[str] = []

            # Event-trigger coordination: an in-memory operation queue
            # (the graph is the source of truth, and `_reconstruct_queue` rebuilds it on
            # start-up) and
            # the receiver-side event-trigger REST route that other resources ring to dispatch.
            self._operation_queue: List[IRI] = []
            # Optional domain callback fired on enqueue. None means leave the Operation
            # queued for a manual claim_next. The middleware tracks background tasks so they are not
            # garbage-collected before they finish.
            self._callback: Optional[Any] = None
            self._callback_scope: Any = None
            self._callback_tasks: set = set()
            self._register_event_trigger()

            self.add_callback("on_start_up", self._register_service)
            self.add_callback("on_shutdown", self._deregister_service)
            # Stand up the resource's REST interface from graph ground truth
            # (generate_rest_interface: OGM-fetch the resource instance -> datamodel -> API).
            # This registers the "resource" persistence connector that
            # _wire_semantic_connectors's synced connectors look up at their own startup,
            # below. That lookup runs from an on_start_up callback too, and callbacks run in
            # registration order -- so this one must land in the list first, or connector
            # sync starts against a persistence connector that does not exist yet.
            self.add_callback("on_start_up", self._load_resource_datamodel)
            # Reconstruct the operation queue from the graph (queue durability): a one-shot
            # startup query, so no work is lost across a restart and no steady-state polling.
            self.add_callback("on_start_up", self._reconstruct_queue)
            if heartbeat_interval and heartbeat_interval > 0:
                self.add_callback("on_start_up", self._start_heartbeat)
                self.add_callback("on_shutdown", self._stop_heartbeat)

            if class_scope is not None:
                self._wire_semantic_connectors()

        elif mode == Mode.WATCHDOG:
            if ogm is None:
                raise ValueError("watchdog mode requires: ogm")
            self.resource_iri = None
            self.service_class = None
            self.service_iri = None
            self.add_callback("on_start_up", self._start_sweep)
            self.add_callback("on_shutdown", self._stop_sweep)

        elif mode == Mode.SERVER:
            self.resource_iri = None
            self.service_class = None
            self.service_iri = None
            raise NotImplementedError("mode 'server' is not implemented yet")

        # Opt-in, and deliberately *outside* the mode branches. A controller and a
        # monitor this library configures them differently, so they inherit the feed
        # from the same code -- no `if mode ==` here, and none in `activity.py` either. That
        # the flavors compose out of one library is the claim the demo is built to make, and
        # a mode-specific observability surface would quietly undercut it.
        #
        # Off means off: no ring buffer allocated, no handler attached, no route mounted.
        self.activity_feed: Optional[ActivityFeed] = None
        if activity_feed:
            self.activity_feed = enable_activity_feed(self, capacity=activity_capacity)

    async def _register_service(self) -> None:
        """Register the Service instance in the knowledge graph on startup."""
        await anyio.to_thread.run_sync(
            functools.partial(
                register_service,
                self.ogm,
                resource_iri=self.resource_iri,
                service_iri=self.service_iri,
                service_class=self.service_class,
                address=self.address,
                named_graph=self.named_graph,
            )
        )

    async def _deregister_service(self) -> None:
        """Deregister the Service instance (remove reachability) on shutdown."""
        await anyio.to_thread.run_sync(
            functools.partial(
                deregister_service,
                self.ogm,
                self.service_iri,
                named_graph=self.named_graph,
            )
        )

    # --- Liveness: per-service heartbeat (resource mode) ------------------- #

    async def _start_heartbeat(self) -> None:
        """Start the background heartbeat loop (resource mode)."""
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """Refresh svc:lastHeartbeat every ``heartbeat_interval`` seconds, until the system cancels it."""
        # The loop is only started when a positive interval was configured.
        interval = self.heartbeat_interval or 30.0
        try:
            while True:
                await anyio.to_thread.run_sync(
                    functools.partial(
                        update_heartbeat,
                        self.ogm,
                        self.service_iri,
                        named_graph=self.named_graph,
                    )
                )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    async def _stop_heartbeat(self) -> None:
        """Cancel the heartbeat loop on shutdown."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

    async def emit_heartbeat(self) -> None:
        """Refresh this service's heartbeat once (also useful for tests/manual pings)."""
        await anyio.to_thread.run_sync(
            functools.partial(
                update_heartbeat, self.ogm, self.service_iri, named_graph=self.named_graph
            )
        )
        # One line per interval (30s by default), so this is a pulse rather than a flood --
        # and it is the only outward sign that an idle instance is still alive, which is
        # exactly what someone watching the activity feed wants to see.
        logger.info("Heartbeat written for %s", self.service_iri)

    # --- Liveness: centralized watchdog sweep (watchdog mode) -------------- #

    async def _start_sweep(self) -> None:
        """Start the background staleness-sweep loop (watchdog mode)."""
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        """Sweep stale services every ``sweep_interval`` seconds, until canceled."""
        try:
            while True:
                await self.sweep()
                await asyncio.sleep(self.sweep_interval)
        except asyncio.CancelledError:
            pass

    async def _stop_sweep(self) -> None:
        """Cancel the sweep loop on shutdown."""
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass

    async def sweep(self) -> List[str]:
        """Deregister every stale service once. Returns the swept service IRIs (as str).

        A watchdog-mode instance calls this on its ``sweep_interval``. It is also a
        plain method, so a test or operator triggers a sweep directly.
        """
        swept = await anyio.to_thread.run_sync(
            functools.partial(
                sweep_stale_services,
                self.ogm,
                self.staleness_threshold,
                named_graph=self.named_graph,
            )
        )
        return [str(s) for s in swept]

    def workflow(
        self,
        *args,
        capability_class: Any = None,
        workflow_class: Any = None,
        **kwargs,
    ):
        """Register a function as a REST-invokable workflow with KG registration.

        In resource mode, requires ``capability_class`` and ``workflow_class`` as
        keyword-only IRIs (both classes must pre-exist in the ontology). Registers the
        REST endpoint via the base class, then schedules KG registration of the
        Workflow and Capability instances on startup (after the service is
        registered).

        Raises:
            RuntimeError: If called outside resource mode.
            ValueError: If capability_class or workflow_class is missing.
        """
        if self.mode != Mode.RESOURCE:
            raise RuntimeError(
                f"@workflow decorator only valid in resource mode; current mode is {self.mode!r}"
            )
        if capability_class is None:
            raise ValueError("capability_class is required in resource mode")
        if workflow_class is None:
            raise ValueError("workflow_class is required in resource mode")

        capability_class_iri = IRI(capability_class)
        workflow_class_iri = IRI(workflow_class)

        base_decorator = super().workflow(*args, **kwargs)

        def decorator(func):
            """Wrap the workflow function and schedule its KG registration on startup.

            The decorator creates IRIs for the Workflow and Capability instances.
            It builds the REST endpoint URL from the service address and function name.
            The wrapped function becomes a REST endpoint via the base class decorator.
            Registration occurs during the on_start_up callback phase.
            """
            wrapped = base_decorator(func)
            name = func.__name__
            workflow_iri = mint_workflow_iri(self.service_iri, name)
            capability_iri = mint_capability_iri(self.resource_iri, name)
            endpoint = build_workflow_endpoint(self.address, name)

            async def register_workflow_callback() -> None:
                """Register the Workflow and Capability instances in the knowledge graph.

                This callback runs during startup after the Service registration completes.
                It creates the Workflow, Capability, and their class associations in the graph.
                The endpoint URL links the Workflow to its REST handler.
                """
                await anyio.to_thread.run_sync(
                    functools.partial(
                        register_workflow,
                        self.ogm,
                        resource_iri=self.resource_iri,
                        service_iri=self.service_iri,
                        workflow_iri=workflow_iri,
                        workflow_class=workflow_class_iri,
                        capability_iri=capability_iri,
                        capability_class=capability_class_iri,
                        endpoint=endpoint,
                        named_graph=self.named_graph,
                    )
                )

            self.add_callback("on_start_up", register_workflow_callback)
            return wrapped

        return decorator

    def state(
        self,
        *,
        capability_class: Any = None,
        state_property_class: Any = None,
        name: Optional[str] = None,
    ):
        """Expose a getter as a GET-readable state property with KG registration.

        Parallel to :meth:`workflow` but GET-only, for a readable, potentially
        high-frequency-changing value (e.g. a door's status). In resource mode,
        it requires ``capability_class`` and ``state_property_class`` as
        keyword-only IRIs (both classes must pre-exist in the ontology). Registers a
        GET endpoint at ``/state/{name}`` that calls the decorated getter on
        demand. The live value is NEVER written to the graph. Only the stable
        endpoint triple is written, at registration.

        Raises:
            RuntimeError: If called outside resource mode.
            ValueError: If capability_class or state_property_class is missing.
        """
        if self.mode != Mode.RESOURCE:
            raise RuntimeError(
                f"@state decorator only valid in resource mode; current mode is {self.mode!r}"
            )
        if capability_class is None:
            raise ValueError("capability_class is required in resource mode")
        if state_property_class is None:
            raise ValueError("state_property_class is required in resource mode")

        capability_class_iri = IRI(capability_class)
        state_property_class_iri = IRI(state_property_class)

        def decorator(func):
            """Wrap the state getter function and register its REST endpoint.

            The decorator creates IRIs for the StateProperty and Capability instances.
            It builds a GET endpoint at /state/{name} that invokes the decorated getter.
            The endpoint returns the current value without writing to the knowledge graph.
            Registration of the endpoint metadata occurs during the on_start_up callback.
            """
            state_name = name or func.__name__
            state_property_iri = mint_state_property_iri(self.service_iri, state_name)
            capability_iri = mint_capability_iri(self.resource_iri, state_name)
            endpoint = build_state_endpoint(self.address, state_name)

            router = APIRouter(prefix=f"/state/{state_name}", tags=["state"])

            @router.get("")
            async def get_state():
                """Handle GET /state/{name} requests. Call the decorated getter and return its value.

                The handler invokes the decorated function to fetch the current state value.
                It awaits the result if the function returns an awaitable.
                The response contains the raw value, not wrapped in any envelope.
                """
                result = func()
                if inspect.isawaitable(result):
                    result = await result
                return result

            self.app.include_router(router)

            async def register_state_property_callback() -> None:
                """Register the StateProperty and Capability instances in the knowledge graph.

                This callback runs during startup after the Service registration completes.
                It creates the StateProperty, Capability, and their class associations in the graph.
                The endpoint URL links the StateProperty to its REST handler.
                """
                await anyio.to_thread.run_sync(
                    functools.partial(
                        register_state_property,
                        self.ogm,
                        resource_iri=self.resource_iri,
                        service_iri=self.service_iri,
                        state_property_iri=state_property_iri,
                        state_property_class=state_property_class_iri,
                        capability_iri=capability_iri,
                        capability_class=capability_class_iri,
                        endpoint=endpoint,
                        named_graph=self.named_graph,
                    )
                )

            self.add_callback("on_start_up", register_state_property_callback)
            return func

        return decorator

    # ------------------------------------------------------------------ #
    # Event-trigger coordination: receiver intake + caller dispatch, as transaction
    # context managers.
    # ------------------------------------------------------------------ #

    def _register_event_trigger(self) -> None:
        """Expose the built-in ``execute`` event trigger on the REST API.

        Mounted via the base transitional_sync_middleware workflow mechanism, as a plain REST
        route (``POST /workflows/event_trigger/execute``). This is deliberately
        NOT a KG-registered ``svc:Workflow``. The event trigger is framework
        plumbing every resource-mode instance exposes, so peers can call it. It
        is not a domain capability.
        """

        middleware = self

        async def event_trigger(payload: _EventTriggerPayload) -> Dict[str, str]:
            """Handle POST /workflows/event_trigger/execute requests. Enqueue an Operation for execution.

            The handler extracts the Operation IRI from the request payload.
            It delegates to _handle_event_trigger to fetch and queue the Operation.
            The response contains the Operation IRI and its initial QUEUED status.
            """
            return await middleware._handle_event_trigger(IRI(payload.operation_iri))

        # Base decorator mounts the route without the KG registration the KAPPS
        # ``self.workflow(...)`` override performs.
        Middleware.workflow(self)(event_trigger)

    async def _handle_event_trigger(self, operation_iri: IRI) -> Dict[str, str]:
        """Receiver-side event-trigger intake.

        The trigger carries only the Operation IRI — its payload lives in the graph. We
        ``ogm.fetch`` the Operation (confirming it exists) and enqueue it into this
        instance's in-memory queue, leaving it ``queued`` and returning immediately with
        no business result. A domain callback (``register_callback``) or a pull-and-run
        (``claim_next``) runs the work
        later. This slice only enqueues.
        """

        await anyio.to_thread.run_sync(
            functools.partial(self.ogm.fetch, instance_iri=operation_iri)
        )
        self._operation_queue.append(operation_iri)
        if self._callback is not None:
            # Fire the domain callback in the background. The trigger returns
            # immediately (it does not block on the work). The pull-and-run runs
            # off the event loop, so the blocking graph I/O never stalls the server. Keep a
            # reference so the middleware does not garbage-collect the task before it finishes.
            task = asyncio.create_task(self._run_callback())
            self._callback_tasks.add(task)
            task.add_done_callback(self._callback_tasks.discard)
        return {"operation": str(operation_iri), "status": OperationStatus.QUEUED}

    @contextlib.contextmanager
    def request(
        self,
        *,
        capability_class: Any,
        operation_class: Any,
        operation_iri: Optional[str] = None,
        target_resource: Optional[str] = None,
    ):
        """Caller-side dispatch as a transaction context manager.

        Usage::

            with mw.request(capability_class=cap, operation_class=op_cls) as op:
                ...  # populate op.data with the operation's domain fields (if any)

        The body populates the yielded draft. On clean exit the middleware **atomically**
        creates the Operation (status ``queued``, addressed to a reachable Service via its
        Capability through discovery), and triggers that Service's event trigger over
        REST. If the trigger delivery fails, the system reverts the created Operation
        atomically. A body exception aborts before anything is written.

        This is the in-process caller face — not REST-exposed.
        """

        if self.mode != Mode.RESOURCE:
            raise RuntimeError(
                f"request() is only valid in resource mode; current mode is {self.mode!r}"
            )
        capability_class_iri = IRI(capability_class)
        operation_class_iri = IRI(operation_class)
        op_iri = (
            IRI(operation_iri) if operation_iri else mint_operation_iri(operation_class_iri)
        )

        # __enter__ precondition, OUTSIDE the transaction: resolve a reachable
        # receiver for the capability BEFORE the body runs, so an unroutable dispatch fails
        # before any domain work rather than at commit.
        capability_iri, _service_iri, address = resolve_dispatch_target(
            self.ogm,
            capability_class_iri,
            target_resource=IRI(target_resource) if target_resource else None,
            named_graph=self.named_graph,
        )
        draft = _OperationDraft(op_iri)

        # Body runs here. An exception occurs and the system writes nothing.
        yield draft

        # __exit__ (clean): atomically create the Operation and fire the event trigger.
        create_operation(
            self.ogm,
            operation_iri=op_iri,
            operation_class=operation_class_iri,
            capability_iri=capability_iri,
            status=OperationStatus.QUEUED,
            data=draft.data or None,
            named_graph=self.named_graph,
        )
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    build_event_trigger_url(address),
                    json={"operation_iri": str(op_iri)},
                )
                response.raise_for_status()
        except Exception:
            revert_operation(
                self.ogm,
                op_iri,
                operation_class=operation_class_iri,
                capability_iri=capability_iri,
                data=draft.data or None,
                named_graph=self.named_graph,
            )
            raise

    @contextlib.contextmanager
    def claim_next(self, scope: Any = None):
        """Pull-and-run the next queued Operation as a transaction context manager.

        Usage::

            with mw.claim_next(scope) as claimed:
                claimed.result = do_the_work(claimed.operation)

        `__enter__` pops the next `queued` Operation from this instance's in-memory queue
        (FIFO order, raises `OperationQueueEmpty` if none). It marks the Operation `running`, and
        re-fetches it under the domain-supplied `ClassScope`, so the body gets exactly the
        object shape it needs. The body runs the work, and may set `claimed.result`. On
        clean exit the CM **atomically** records `done` plus provenance
        (`executedByWorkflow` / `executionTimestamp` / `executionResult`). On a body
        exception it records `failed` plus the exception message, and re-raises. Provenance
        is folded into the terminal transition. On failure the CM also dumps the
        resource datamodel and stores it as the Operation's ``svc:failureState``, so the
        state that produced the failure survives the exception.

        This is the in-process receiver face — not REST-exposed.
        """

        if self.mode != Mode.RESOURCE:
            raise RuntimeError(
                f"claim_next() is only valid in resource mode; current mode is {self.mode!r}"
            )
        if not self._operation_queue:
            raise OperationQueueEmpty("no queued Operation to pull")
        op_iri = self._operation_queue[0]  # peek FIFO, dequeue only once preconditions pass

        # Precondition, BEFORE any mutation: resolve the Workflow for provenance
        # without requiring a live endpoint, so `executedByWorkflow` is recorded even if the
        # workflow's `svc:endpoint` the system deregistered it mid-run. If this raises, the Operation
        # stays queued (in the graph and this in-memory queue) for a later retry.
        workflow_iri = resolve_operation_workflow(
            self.ogm, op_iri, named_graph=self.named_graph
        )

        # Preconditions passed — claim it: dequeue, mark running, then re-fetch under scope.
        self._operation_queue.pop(0)
        set_operation_status(
            self.ogm,
            operation_iri=op_iri,
            status=OperationStatus.RUNNING,
            named_graph=self.named_graph,
        )
        if scope is not None:
            operation = self.ogm.fetch(
                instance_iri=op_iri, class_scope=scope, materialize=True
            )
        else:
            operation = self.ogm.fetch(instance_iri=op_iri, as_reference=True)
        claimed = _ClaimedOperation(op_iri, operation)

        try:
            yield claimed
        except Exception as exc:
            record_terminal_status(
                self.ogm,
                operation_iri=op_iri,
                workflow_iri=workflow_iri,
                status=OperationStatus.FAILED,
                result=str(exc),
                failure_state=self._dump_resource_datamodel(),
                named_graph=self.named_graph,
            )
            raise
        else:
            record_terminal_status(
                self.ogm,
                operation_iri=op_iri,
                workflow_iri=workflow_iri,
                status=OperationStatus.DONE,
                result=claimed.result,
                named_graph=self.named_graph,
            )

    @contextlib.contextmanager
    def handover(self, *, mode: Any, workpiece: Any, counterpart: Any):
        """Change-of-possession primitive as a transaction context manager.

        Usage::

            with mw.handover(mode=MES.Pass, workpiece=wp, counterpart=other):
                ...  # domain-owned physical transport / counterpart coordination

        `__enter__` runs exactly two precondition checks OUTSIDE the transaction. First, the
        caller currently possesses the workpiece (a `cfc:PossessionState` the workpiece points
        to, with this resource as possessor). Second, the counterpart carries the
        `mes:hasHandoverAbility` COMPLEMENTARY to `mode`. There is deliberately no
        destination-free, universal maxCount-1 check. Possession is not always single.
        The body is domain-owned: physical transport, and any counterpart coordination, for
        example through a `request(...)` dispatch. The Core never references the event
        trigger. On clean exit, `__exit__` switches possession to the counterpart. The middleware appends
        the counterpart's `cfc:hasPossessor` to a fresh PossessionState (an atomic insertion,
        because a resource can possess several workpieces). The middleware then re-points the workpiece's
        cardinality-1 `cfc:hasPossessedWorkpiece`, in a single OGM DELETE/INSERT along the update
        path. The workpiece thus points to exactly one PossessionState throughout the process.
        On an exception, the middleware aborts with no switch. Possession is Core's reified model
        (`cfc:PossessionState`). The "possessed by exactly one" cardinality is Core's own
        Workpiece restriction, the commit-time SHACL backstop.
        """

        if self.mode != Mode.RESOURCE:
            raise RuntimeError(
                f"handover() is only valid in resource mode; current mode is {self.mode!r}"
            )
        workpiece_iri = IRI(workpiece)
        counterpart_iri = IRI(counterpart)
        mode_ability_iri = IRI(mode)

        # __enter__ preconditions, OUTSIDE the transaction.
        current_ps = find_possession_state(
            self.ogm, workpiece_iri, self.resource_iri, named_graph=self.named_graph
        )
        if current_ps is None:
            raise HandoverPreconditionError(
                f"{self.resource_iri} does not currently possess {workpiece_iri}"
            )
        if not counterpart_has_complementary_ability(
            self.ogm, counterpart_iri, mode_ability_iri, named_graph=self.named_graph
        ):
            raise HandoverPreconditionError(
                f"counterpart {counterpart_iri} lacks the handover ability complementary "
                f"to {mode_ability_iri}"
            )

        # Body runs here. An exception aborts before any possession switch.
        yield

        # __exit__ (clean): switch possession to the counterpart (append possessor link, then
        # atomically re-point the workpiece's single possession).
        switch_possession(
            self.ogm,
            workpiece_iri=workpiece_iri,
            new_possessor_iri=counterpart_iri,
            named_graph=self.named_graph,
        )

    def register_callback(self, callback: Any, scope: Any = None) -> None:
        """Register a domain work callback fired on enqueue.

        `callback` is the work function `callback(operation) -> result`. It runs the
        Operation's work, and returns the value recorded as `svc:executionResult`. On each
        enqueue, The middleware drives a background pull-and-run around it:
        `with claim_next(scope) as c: c.result = callback(c.operation)`. The middleware
        handles status (`running`->`done`/`failed`) and provenance. The domain writes only
        the work. Opt-in: with no callback registered, an enqueued Operation stays `queued`
        for a manual `claim_next`. `scope` is the domain ClassScope used to re-fetch the
        Operation.
        """

        if self.mode != Mode.RESOURCE:
            raise RuntimeError(
                f"register_callback() is only valid in resource mode; current mode is {self.mode!r}"
            )
        self._callback = callback
        self._callback_scope = scope

    async def _run_callback(self) -> None:
        """Drive the domain callback's pull-and-run off the event loop.

        The blocking pull-and-run runs in a worker thread, so it never stalls the server. A
        failure inside the domain work is a normal terminal outcome. `claim_next` has
        already recorded it as `failed`, with provenance in the graph. This logs it at
        warning level, not error. The middleware catches the exception so this fire-and-forget
        task does not leak an unretrieved-exception warning. A drained-queue race
        (`OperationQueueEmpty`, another callback task claimed the op first) is benign.
        """
        try:
            await anyio.to_thread.run_sync(self._drive_callback)
        except OperationQueueEmpty:
            pass  # another callback task already claimed the operation, nothing to do
        except Exception:
            logger.warning(
                "Domain callback did not complete cleanly for a queued operation on %s",
                self.resource_iri,
                exc_info=True,
            )

    def _drive_callback(self) -> None:
        """Run one pull-and-run wrapping the registered domain callback."""
        callback = self._callback
        if callback is None:  # defensive: only scheduled when the middleware registers a callback
            return
        with self.claim_next(self._callback_scope) as claimed:
            claimed.result = callback(claimed.operation)

    async def _reconstruct_queue(self) -> None:
        """Reconstruct the operation queue from the graph on startup for durability.

        Own ``queued`` Operations are re-enqueued into the in-memory cache. Own orphaned
        ``running`` Operations (this resource crashed mid-execution) transition to
        ``failed``, and never auto-rerun, because a half-done physical action must not
        silently replay. Both are found by ontology traversal (Operation -> Capability <-
        resource), whose links persist across a restart. So this is a one-shot startup
        query, rather than steady-state polling.
        """

        queued = await anyio.to_thread.run_sync(
            functools.partial(
                find_resource_operations,
                self.ogm,
                self.resource_iri,
                [OperationStatus.QUEUED],
                self.named_graph,
            )
        )
        for op_iri in queued:
            if op_iri not in self._operation_queue:
                self._operation_queue.append(op_iri)

        orphaned = await anyio.to_thread.run_sync(
            functools.partial(
                find_resource_operations,
                self.ogm,
                self.resource_iri,
                [OperationStatus.RUNNING],
                self.named_graph,
            )
        )
        for op_iri in orphaned:
            await anyio.to_thread.run_sync(
                functools.partial(
                    mark_operation_failed,
                    self.ogm,
                    op_iri,
                    "orphaned running operation reclaimed at startup, not auto-rerun",
                    self.named_graph,
                )
            )

    def _wire_semantic_connectors(self) -> None:
        """Recognize this resource's interface parameters, and connect what the wiring allows.

        Called from ``__init__`` rather than from an ``on_start_up`` callback. That is not
        a stylistic choice. ``lifespan`` calls ``connect()`` on everything in the connection
        registry *before* running ``on_start_up``. ``initiate_sync`` (what
        ``add_synced_connector`` defers) starts ``run_receive()`` but never calls
        ``connect()``. A connector registered later does not connect. Its listener never
        starts. ``receive()`` blocks forever, while outbound limps on through
        ``consume()``'s reconnect. Silent, one-directional failure.

        Recognition and the northbound projection run identically for every connector
        wiring. ``autoregister_connectors`` controls only the connection. An
        inspecting instance that skipped recognition would treat each parameter node as
        ordinary data, and serve its broker address northbound. The least-privileged
        instance would leak the most.
        """

        # Deliberately not wrapped in a try/except. A class_scope request means the
        # parameters under it must wire, and a resource that cannot resolve them has not
        # "come up with a smaller surface" — it has come up unable to reach its device, with
        # the projection that keeps broker addresses off the wire never computed.
        # Failing construction is the honest outcome. A warning here would produce exactly
        # the silent half-alive resource the binding design is written to avoid.
        self._wiring = plan_wiring(
            ogm=self.ogm,
            resource_iri=self.resource_iri,
            class_scope=self.class_scope,
            registry=self.connector_registry,
            flavour=self.connector_sync_direction,
            autoregister=self.autoregister_connectors,
            ensure_transport=self.ensure_transport,
        )

        for binding, registration in self._wiring.registrations:
            self.add_synced_connector(
                connector_id=f"{binding.resource_iri}#{binding.field_id}#{registration.suffix}",
                connector=registration.connector,
                model_type=registration.model_type,
                data_model_name="resource",
                model_id=str(self.resource_iri),
                # The COMPLEX property is the deepest addressable thing: ConnectionInfo has
                # three levels and field_id is a plain getattr, so inf:hasValue is out of
                # reach. Same atomic unit the recursive router reaches from the routing side.
                contained_model_id=str(binding.resource_iri),
                field_id=binding.field_id,
                formatter=registration.formatter,
                sync_role=registration.sync_role,
                sync_direction=registration.sync_direction,
            )

        if self._wiring.registrations:
            logger.info(
                "Wired %d connector(s) across %d parameter(s) on %s",
                len(self._wiring.registrations),
                len(self._wiring.bindings),
                self.resource_iri,
            )

    def _suppress_default_persistence_warning(self, data_model_name: str) -> None:
        """Pre-register the exact fallback ``persist()`` would build anyway, so
        ``transitional_sync_middleware``'s "No persistence factory found ... Using default persistence
        factory" warning never fires for ``data_model_name``.

        ``persist()`` (the base class) calls ``add_to_persistence`` with no
        ``persistence_factory``, which asks the registry for the default one. Nothing in
        this codebase ever calls the base class's own opt-in
        (``add_default_persistence``) to register one ahead of time, so every call falls
        into the registry's "not found" branch, logs the warning, and constructs
        ``PersistenceFactory(ModelConnector)`` -- benign, since that is the only
        connector kind ``persist()`` ever needed, but a warning nobody had explained
        trains people to ignore warnings, and a connector that silently stops syncing is
        exactly the kind of bug that hides behind one.

        Registering that identical factory here, before the first ``persist()`` call,
        changes no behaviour -- the connector constructed is the same either way -- it
        only tells the registry the fallback was chosen on purpose. Called directly on
        ``persistence_registry`` rather than through ``add_default_persistence``: that
        wrapper additionally requires ``data_model_name`` already present in
        ``self.data_models``, a bookkeeping step ``Controller._load_view_datamodels``
        (``demo/transferunits/controller.py``) has no other reason to perform for each
        view hit it loads. ``object`` stands in for the wrapper's own ``typing.Any``
        default: the registry's lookup does ``issubclass(persisted_model_type,
        model_type)``, and ``typing.Any`` is not a class ``issubclass`` accepts.

        Idempotent -- harmless to call more than once, but skipped once this
        ``data_model_name`` is registered, so a caller need not track whether it already
        ran.

        **Kept here rather than fixed upstream, knowingly.** Every fact this method uses -- that the fallback is
        ``PersistenceFactory(ModelConnector)``, that the lookup does
        ``issubclass(persisted_model_type, model_type)`` -- belongs to ``transitional_sync_middleware``,
        so this is a reimplementation of a sibling's internals in a downstream repo, and it
        drifts silently the moment that fallback changes. Root architecture rules would permit fixing
        it there, but the sibling is not *defective*: it warns about a real choice it made,
        and suppressing that warning for every consumer is a feature change, not a bugfix.
        The failure mode if it drifts is benign and loud enough -- the warning returns --
        rather than a wrong value, which is why this is a recorded bet rather than a fix.
        Revisit it whenever ``transitional_sync_middleware`` changes that fallback.
        """

        connection_info = ConnectionInfo(data_model_name=data_model_name)
        if connection_info in self.persistence_registry.persistence_factories:
            return
        self.persistence_registry.add_persistence_factory(
            connection_info, object, PersistenceFactory(ModelConnector)
        )

    async def _load_resource_datamodel(self) -> None:
        """Expose the resource's REST interface generated from graph ground truth.

        Resource mode builds its REST surface from the graph. It fetches the resource
        individual through the OGM, materializes its transitional_sync_middleware datamodel, and
        generates the CRUD REST API (``generate_rest_api_for_data_model``). This is
        best-effort and additive. The event-trigger and workflow routes are the
        load-bearing surface for this slice. So a resource with no materializable data is
        skipped, with a warning, instead of failing startup.

        When a ``class_scope`` was given, the fetch goes through the **pruned** spec. So
        the served datamodel has no field that could carry a broker address or a topic.
        Without one, the middleware fetches unscoped, and returns the individual.
        This is what scenarios 1 and 2 rely on, and why the projection cannot leak there.
        """

        try:
            fetch = functools.partial(
                self.ogm.fetch, instance_iri=self.resource_iri, materialize=True
            )
            if self._wiring is not None:
                fetch = functools.partial(
                    self.ogm.fetch,
                    instance_iri=self.resource_iri,
                    **self._wiring.northbound_fetch_kwargs(),
                )
            node = await anyio.to_thread.run_sync(fetch)
            instance = getattr(node, "instance", None)
            if instance is None:
                return
            self.load_model_instances("resource", [instance])
            if self._wiring is not None:
                # load_model_instances does not persist (it calls load_data_model with its
                # persist_instances default, False), so no "resource" persistence connector
                # exists unless we create one here. A synced connector's initiate_sync
                # (registered by _wire_semantic_connectors, and run after this callback --
                # see the on_start_up registration order in __init__) looks this connector
                # up by data_model_name="resource" and this exact resource_iri, and raises
                # a bare KeyError if it is missing. Scenarios 1 and 2 pass no class_scope,
                # so self._wiring remains None and they keep the behavior unchanged.
                self._suppress_default_persistence_warning("resource")
                await self.persist("resource", instance)
            # The local recursive router, not the framework generator. It still
            # produces the framework's top-level CRUD -- it calls it -- and then descends the
            # datamodel tree to give every interface-accessible parameter its own address, so a
            # setpoint is a PUT to one belt rather than a read-modify-write of the whole list.
            # With no wiring, no bindings exist, so the system adds nothing and scenarios 1 and 2
            # keep exactly the surface they have today.
            self._parameter_routes = generate_recursive_rest_api(
                self,
                "resource",
                root_id=str(self.resource_iri),
                instance=instance,
                bindings=self._wiring.bindings if self._wiring is not None else (),
            )
        except Exception as exc:  # noqa: BLE001 - additive convenience surface, never fatal
            logger.warning(
                "Could not generate the resource datamodel REST API for %s: %s",
                self.resource_iri,
                exc,
            )

    def _dump_resource_datamodel(self) -> Optional[str]:
        """Serialize this resource's transitional_sync_middleware datamodel to a JSON string, or None.

        This is the failed Operation state snapshot. On a body exception,
        `claim_next` records this alongside `operationStatus=failed` (`svc:failureState`),
        so the user can diagnose the resource state from the
        graph. This is best-effort. A resource with no materializable datamodel, or a
        serialization error, yields None. The `failed` status and error string are still
        recorded either way. The snapshot is additive.
        """

        try:
            node = self.ogm.fetch(instance_iri=self.resource_iri, materialize=True)
            instance = getattr(node, "instance", None)
            if instance is None:
                return None
            if isinstance(instance, BaseModel):
                return instance.model_dump_json()
            return json.dumps(instance, default=str)
        except Exception as exc:  # noqa: BLE001 - additive diagnostic, never fatal to failure recording
            logger.warning(
                "Could not serialize resource datamodel for failure dump of %s: %s",
                self.resource_iri,
                exc,
            )
            return None
