"""KAPPS Semantic Middleware.

Extends transitional_sync_middleware.Middleware with knowledge-graph registration, discovery,
and execution capabilities. Supports three modes -- see `modes.Mode`
for what each one means and which are implemented.

Operation execution: an Operation resolves via its implemented
Capability to a Workflow endpoint, which the system then invokes over HTTP.
"""

from __future__ import annotations

import asyncio
import atexit
import collections
import contextlib
import functools
import inspect
import json
import logging
import random
import threading
import time
import warnings
import weakref
from typing import Any, Callable, Deque, Dict, List, Mapping, NamedTuple, Optional, Sequence

import anyio
import httpx
from fastapi import APIRouter
from fastapi.responses import Response
from fastapi.routing import APIRoute
from kapps_ogm import Update
from kapps_ogm.utils.class_scope import ClassScope
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import PreconditionFailedError
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
from kapps_semantic_middleware.commit import (
    Change,
    ErrorTransition,
    ExitTransition,
    _Commit,
    _Creation,
    _nothing_to_join,
    _property,
)
from kapps_semantic_middleware.operation import (
    _implemented_capabilities,
    _received_scope,
    operation,
)
from kapps_semantic_middleware.possession import _switch_conflict, possession_change
from kapps_semantic_middleware.rest_router import generate_recursive_rest_api
from kapps_semantic_middleware.errors import (
    DeploymentRuntimeError,
    DeploymentValueError,
    HandoverPreconditionError,
    NotificationReceiverAmbiguousError,
    NotificationReceiverUnavailableError,
    OperationQueueEmpty,
    OperationResolutionError,
)
from kapps_semantic_middleware.registration import (
    _issuers,
    _operation_status_update,
    _operation_statuses,
    _read_notified,
    _resolve_claim,
    _sweep_stale_services,
    _terminal_status_update,
    build_event_trigger_url,
    build_state_endpoint,
    build_workflow_endpoint,
    counterpart_has_complementary_ability,
    deregister_service,
    find_possession_state,
    find_resource_operations,
    mark_operation_failed,
    mint_capability_iri,
    mint_service_iri,
    mint_state_property_iri,
    mint_workflow_iri,
    reachable_service_addresses,
    register_service,
    register_state_property,
    register_workflow,
    resolve_dispatch_target,
    update_heartbeat,
)
from kapps_semantic_middleware.validation_report import RefusedWrite, _RefusedWrites, _Verb
from kapps_semantic_middleware.vocabulary import SVC, OperationStatus

logger = logging.getLogger(__name__)

# The heartbeat loop's wait and its random draw. A test replaces them here, not the event loop's
# sleep, which the rest of the module also uses.
_heartbeat_wait = asyncio.sleep
_heartbeat_phase = random.uniform

__all__ = ["SemanticMiddleware", "IssuedOperation", "OperationResolutionError", "Mode"]

#: The statuses of an Operation that a resource has claimed: it runs, or it has ended.
_CLAIMED = frozenset({OperationStatus.RUNNING, OperationStatus.DONE, OperationStatus.FAILED})
#: The statuses of an Operation that has ended.
_ENDED = frozenset({OperationStatus.DONE, OperationStatus.FAILED})



class _EventTriggerPayload(BaseModel):
    """REST body of the event trigger: the IRI of the Operation now in the graph."""

    # `str`, not `IRI`, and deliberately so. `IRI.__get_pydantic_core_schema__` returns
    # `any_schema()` — permissive by design, on the grounds that IRI validates itself — so
    # annotating this `IRI` would neither validate nor convert the incoming value: the
    # attribute would hold a plain `str` while claiming to be an `IRI`. Taking `str` at the
    # boundary and converting explicitly in `_handle_event_trigger` puts the validation where
    # a malformed IRI actually raises. The safer-looking annotation is the less safe one.
    operation_iri: str


class _ReadOperation(NamedTuple):
    """A received Operation as the intake read it, with the Capabilities that it implements."""

    #: The Operation as a kapps_ogm ``Node``, materialized under the intake's scope.
    node: Any
    #: The IRIs of the Capabilities that the Operation implements, found in the read.
    capabilities: List[IRI]


class _Received(NamedTuple):
    """An Operation in the intake's FIFO."""

    #: The Operation's IRI.
    iri: IRI
    #: None when it must be read before it runs: it waited, or it was never read.
    read: Optional[_ReadOperation]


class IssuedOperation:
    """The Operation that ``issue_operation()`` issues: its draft in the block, its handle after it.

    ``issue_operation()`` makes it; a caller does not. In the block of ``with mw.issue_operation(...) as op:``,
    ``op.data`` takes the Operation's domain properties, as ``{property IRI: [values]}``.
    ``op.iri`` is the Operation's IRI. After the block, the Operation is in the graph, ``queued``.
    ``op.issuer`` is its issuer: the Resource of the middleware that issued it. ``svc:issuedBy``
    records it. Then ``op`` waits for the Operation, in a coroutine::

        if not await op.claimed(timeout=30):
            ...  # nobody claimed it in time: it is still queued, and still the issuer's
        status = await op.ended()  # "done" or "failed"

    and with ``op.wait_claimed(timeout)`` and ``op.wait_ended(timeout)`` in code that may block,
    with the same results.

    A wait reads the status from the graph first, so a claim that came before the wait is seen.
    Then it waits until a notification of the Operation arrives at the issuer's event trigger, and
    reads the status again. The middleware that claims the Operation, and the one that ends it,
    sends that notification after its commit. When the timeout passes, the wait reads the status
    once more. Nothing else wakes a wait: it does not poll. So a wait ends before its timeout only
    when the issuer's middleware is served, and has exactly one live Service.
    """

    def __init__(self, middleware: "SemanticMiddleware", iri: IRI, issuer: IRI) -> None:
        #: The Operation's IRI.
        self.iri = iri
        #: The issuer: the Resource of the middleware that issued the Operation.
        self.issuer = issuer
        #: The Operation's domain properties, set in the block of ``issue_operation()``.
        self.data: Dict[str, list] = {}
        self._middleware = middleware
        # Whether the Operation is in the graph: only then can a wait read its status.
        self._in_graph = False
        # The notifications of the Operation that the issuer's event trigger took, counted, and
        # the condition on which a blocking wait waits for the next arrival.
        self._arrival = threading.Condition()
        self._arrivals = 0
        # The event of each waiting coroutine, with the event loop that it runs on.
        self._waiters: set = set()

    async def claimed(self, timeout: Optional[float] = 30) -> bool:
        """Wait until the Operation is claimed.

        Returns:
            True once the Operation is claimed: it is ``running``, or it has already ended.
            False when ``timeout`` seconds pass first. ``None`` waits without a limit.
        """
        return await self._until(_CLAIMED, timeout) is not None

    async def ended(self, timeout: Optional[float] = None) -> str:
        """Wait until the Operation has ended.

        Returns:
            ``"done"`` or ``"failed"``.

        Raises:
            TimeoutError: If ``timeout`` seconds pass first. ``None`` waits without a limit.
        """
        status = await self._until(_ENDED, timeout)
        if status is None:
            raise TimeoutError(f"{self.iri} did not end within {timeout} s")
        return status

    def wait_claimed(self, timeout: Optional[float] = 30) -> bool:
        """The blocking twin of :meth:`claimed`, with the same results. It blocks its thread:
        never call it on the event loop of the issuer's middleware, whose event trigger must
        take the notification that ends the wait."""
        return self._wait(_CLAIMED, timeout) is not None

    def wait_ended(self, timeout: Optional[float] = None) -> str:
        """The blocking twin of :meth:`ended`, with the same results and the same error. It
        blocks its thread, as :meth:`wait_claimed` does."""
        status = self._wait(_ENDED, timeout)
        if status is None:
            raise TimeoutError(f"{self.iri} did not end within {timeout} s")
        return status

    def _read(self, wanted: frozenset) -> Optional[str]:
        """The Operation's status, read from the graph, when it is one of ``wanted``; else None.

        Raises:
            RuntimeError: If the Operation is not in the graph yet: the block of ``issue_operation()``
                has not ended.
        """
        if not self._in_graph:
            raise RuntimeError(
                f"{self.iri} is not issued yet: wait for it after the block of issue_operation()"
            )
        found = sorted(_operation_statuses(self._middleware.ogm, self.iri) & wanted)
        return found[0] if found else None

    async def _until(self, wanted: frozenset, timeout: Optional[float]) -> Optional[str]:
        """Wait in a coroutine until the status is one of ``wanted``, and return it; None when
        the timeout passes first. Each read runs in a worker thread."""
        loop = asyncio.get_running_loop()
        arrived = asyncio.Event()
        waiter = (loop, arrived)
        with self._arrival:
            self._waiters.add(waiter)
        try:
            deadline = None if timeout is None else loop.time() + timeout
            while True:
                # Cleared before the read: a notification that arrives during the read sets it,
                # and the status is read again.
                arrived.clear()
                status = await anyio.to_thread.run_sync(self._read, wanted)
                if status is not None:
                    return status
                remaining = None if deadline is None else deadline - loop.time()
                if remaining is not None and remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(arrived.wait(), remaining)
                except asyncio.TimeoutError:
                    return await anyio.to_thread.run_sync(self._read, wanted)
        finally:
            with self._arrival:
                self._waiters.discard(waiter)

    def _wait(self, wanted: frozenset, timeout: Optional[float]) -> Optional[str]:
        """Wait in this thread until the status is one of ``wanted``, and return it; None when
        the timeout passes first."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._arrival:
            seen = self._arrivals
        while True:
            status = self._read(wanted)
            if status is not None:
                return status
            with self._arrival:
                while self._arrivals == seen:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        break
                    self._arrival.wait(remaining)
                timed_out = self._arrivals == seen
                seen = self._arrivals
            if timed_out:
                return self._read(wanted)

    def _wake(self) -> None:
        """A notification of the Operation arrived at the issuer's event trigger: every wait
        reads the status again."""
        with self._arrival:
            self._arrivals += 1
            self._arrival.notify_all()
            waiters = list(self._waiters)
        for loop, arrived in waiters:
            with contextlib.suppress(RuntimeError):  # a loop that has closed waits no more
                loop.call_soon_threadsafe(arrived.set)


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


#: How long a stop of the instance, or the end of the process, waits for the issuers'
#: notifications that are still to send.
_NOTIFICATIONS_STOP_SECONDS = 1.0

#: How long the worker of the issuers' notifications waits idle before its thread ends. The next
#: notification starts a new one.
_NOTIFIER_IDLE_SECONDS = 5.0


class _Notifier:
    """One background worker that sends an instance's notifications of issuers, first in, first
    out, so the claim's notification of an Operation leaves before the end's.

    At most one worker thread runs at a time. It starts with a job and ends after it waited idle
    for a while; a later job starts a new one. It is a daemon thread, so the end of the process
    does not wait for it: an exit handler sends what still waits for a short time first.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._condition = threading.Condition()
        self._jobs: Deque[Callable[[], None]] = collections.deque()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        _NOTIFIERS.add(self)

    def submit(self, job: Callable[[], None]) -> None:
        """Hand ``job`` to the worker, and return at once."""
        with self._condition:
            self._jobs.append(job)
            if self._thread is None:
                self._start()
            self._condition.notify_all()

    def _start(self) -> None:
        """Start the worker thread. The caller holds the condition."""
        self._thread = threading.Thread(target=self._work, name=self._name, daemon=True)
        self._thread.start()

    def _work(self) -> None:
        """Run the jobs one at a time, in order; end after an idle wait with no job."""
        me = threading.current_thread()
        try:
            while True:
                with self._condition:
                    if not self._jobs:
                        self._condition.wait(_NOTIFIER_IDLE_SECONDS)
                    if not self._jobs:
                        # Decided under the condition, so a job that comes now starts a new one.
                        self._thread = None
                        return
                    job = self._jobs.popleft()
                    self._running = True
                try:
                    job()
                except Exception:  # a job logs its own failures; this is the last guard
                    logger.warning("A notification of an issuer raised", exc_info=True)
                finally:
                    with self._condition:
                        self._running = False
                        self._condition.notify_all()
        finally:
            with self._condition:
                if self._thread is me:  # ended by an error, not by idleness: go on in a new one
                    self._thread = None
                    if self._jobs:
                        self._start()
                self._condition.notify_all()

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Wait until every job handed over so far is done.

        Returns:
            True when all are done, False when ``timeout`` seconds passed first.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._jobs or self._running:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def stop(self, timeout: float) -> int:
        """Send what waits for up to ``timeout`` seconds, then drop the jobs that have not
        started. A job that runs at that moment ends in the background.

        Returns:
            How many jobs were dropped.
        """
        self.flush(timeout)
        with self._condition:
            dropped = len(self._jobs)
            self._jobs.clear()
            self._condition.notify_all()
        return dropped


#: Every notifier, for the exit handler.
_NOTIFIERS: "weakref.WeakSet[_Notifier]" = weakref.WeakSet()


def _stop_notifiers_at_exit() -> None:
    """At the end of the process, send what still waits for a short time, then drop the rest
    with one WARNING line per notifier."""
    for notifier in list(_NOTIFIERS):
        dropped = notifier.stop(_NOTIFICATIONS_STOP_SECONDS)
        if dropped:
            logger.warning(
                "The process ends and drops %d notification(s) of issuers of %s",
                dropped,
                notifier._name,
            )


atexit.register(_stop_notifiers_at_exit)


class _OwnIris(NamedTuple):
    """The own identity of a resource-mode instance, which no other mode has."""

    resource: IRI
    service_class: IRI
    service: IRI


def _issued_and_claimed(notified: Any, resource_iri: Optional[IRI]) -> bool:
    """Whether a notified Operation is one that ``resource_iri`` issued and that is no longer
    ``queued``: then the notification tells of its claim or its end, and it is no work."""
    return notified.issuer == resource_iri and OperationStatus.QUEUED not in notified.statuses


def _moved_operations(changes: Sequence[Change]) -> Dict[IRI, str]:
    """Each Operation that ``changes`` move to ``running``, ``done`` or ``failed``, with that
    status: an ``Update`` that sets ``svc:operationStatus`` to one value of the three."""
    moved: Dict[IRI, str] = {}
    for change in changes:
        if not isinstance(change, Update) or change.instance_iri is None:
            continue
        for key, values in change.data.items():
            statuses = {str(value) for value in values}
            moves = len(statuses) == 1 and statuses <= _CLAIMED
            if _property(key) == SVC.operationStatus and moves:
                moved[IRI(change.instance_iri)] = statuses.pop()
    return moved


#: The last path segment of each route that runs a workflow: at once, and in the background.
_RUN_ROUTES = frozenset({"execute", "execute_background"})


def _routes_since(
    app: Any, before: int, *, path_endings: Optional[frozenset[str]] = None
) -> List[APIRoute]:
    """The routes mounted on ``app`` since its list of routes held ``before`` entries.

    FastAPI appends each route that it mounts to the end of the app's list, so the routes that
    one registration mounted are those after the length the list had before it. With
    ``path_endings``, only those whose path's last segment is one of them.
    """
    return [
        route
        for route in app.routes[before:]
        if isinstance(route, APIRoute)
        and (path_endings is None or route.path.rsplit("/", 1)[-1] in path_endings)
    ]


def _describe_routes(routes: Sequence[APIRoute], func: Callable[..., Any]) -> None:
    """Describe each route with the docstring of ``func``, the function that it serves.

    The first line of the docstring becomes the route's summary, and the whole docstring its
    description. The instance's API description, which ``/docs`` renders and the dashboard's
    Services box reads, then says what the function does, and not only the name that FastAPI
    derives from the route's handler. A function without a docstring leaves the route's
    summary as it was, and the route gets no description. Nothing else of the route changes.
    """
    doc = inspect.cleandoc(func.__doc__ or "")
    for route in routes:
        if doc:
            route.summary = doc.splitlines()[0]
        route.description = doc


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
        # The ignore is for a typed transitional_sync_middleware, whose property mypy reads
        # off the class as its getter, which has no `fget`. Untyped, as today, it is unused.
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
            named_graph: The target named graph in the knowledge graph, a string or an ``IRI``.
                The constructor converts it to an ``IRI``; a string that is no IRI raises
                ``InvalidIRIError`` here. None is the default graph.
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

        Raises:
            DeploymentValueError: If ``mode`` is no mode, or the mode lacks an argument that it
                requires (Repair). It is a ``ValueError``.
            NotImplementedError: In server mode.
        """
        super().__init__()
        self._rebrand()

        if mode not in modes.ALL:
            raise DeploymentValueError(
                f"mode must be one of {', '.join(repr(str(m)) for m in modes.ALL)}; "
                f"got {mode!r}"
            )

        self.mode = mode
        self.ogm = ogm
        # The writes of this instance that the store's shapes refused, the newest of them, in
        # memory only. `commit`, `create` and `handover` record each refusal where it reaches
        # the caller, and `refused_writes` reads them.
        self._refused_writes = _RefusedWrites()
        self.host = host
        self.port = port
        self.address = address or f"http://{host}:{port}"

        # Liveness.
        self.heartbeat_interval = heartbeat_interval
        self.staleness_threshold = staleness_threshold
        self.sweep_interval = sweep_interval
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._sweep_task: Optional[asyncio.Task] = None

        if mode == Mode.RESOURCE:
            # The test names each argument, so the type checker knows that none is None below.
            if resource_iri is None or service_class is None or ogm is None:
                arguments = (
                    ("resource_iri", resource_iri),
                    ("service_class", service_class),
                    ("ogm", ogm),
                )
                missing = [name for name, value in arguments if value is None]
                raise DeploymentValueError(f"resource mode requires: {', '.join(missing)}")

            # This instance's own identity, which resource mode alone has: the other modes set
            # all three to None. A resource-only path reads them through `_own_iris`.
            self.resource_iri: Optional[IRI] = IRI(resource_iri)
            self.service_class: Optional[IRI] = IRI(service_class)
            self.service_iri: Optional[IRI] = mint_service_iri(self.resource_iri, self.address)

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
            # queued for a manual claim_next.
            self._callback: Optional[Any] = None
            self._callback_scope: Any = None
            # Optional domain handler (``on_notification``). With one, the instance takes its
            # notifications through the intake instead of the queue, and the handler gets those
            # that match no workflow of the instance. None means notifications are queued.
            self._notification_handler: Optional[Callable[[Any], Any]] = None
            # The subproject's own ClassScope of a received Operation, united with the default
            # part when the intake reads one.
            self._notification_scope: Any = None
            # The function of each exposed workflow of this instance, by the IRI of the
            # Capability that it realizes. The intake's match is a lookup here, never a query,
            # and it knows no workflow of an earlier run of the same resource.
            self._workflow_functions: Dict[IRI, Callable[..., Any]] = {}
            # The intake's FIFO, and whether its one worker is at work.
            self._intake: Deque[_Received] = collections.deque()
            self._intake_working = False
            # The background tasks: the callback's pull-and-runs and the intake's worker. The
            # middleware tracks them so they are not garbage-collected before they finish.
            self._background_tasks: set = set()
            # The handle of each Operation that this instance issued, while its caller keeps it,
            # so a notification of the Operation wakes its waits.
            self._issued_handles: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
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
                raise DeploymentValueError("watchdog mode requires: ogm")
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

        # The named graph enters KSM here and is an `IRI` from here on, as every helper it
        # reaches takes it: a string that is no IRI is refused now, not at the first write. It
        # comes after the checks of the mode's arguments, so a missing argument is still the
        # first error, and nothing above reads it.
        self.named_graph: Optional[IRI] = IRI(named_graph) if named_graph is not None else None

        # The background worker that sends the notifications of issuers, and its stop, after
        # the deregistration so that a stop does not wait for it first.
        self._notifier = _Notifier(f"issuer-notifications {self.resource_iri or self.mode}")
        self.add_callback("on_shutdown", self._stop_notifications)

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
        own = self._own_iris("the Service's registration is only valid in resource mode")
        await anyio.to_thread.run_sync(
            functools.partial(
                register_service,
                self.ogm,
                resource_iri=own.resource,
                service_iri=own.service,
                service_class=own.service_class,
                address=self.address,
                named_graph=self.named_graph,
            )
        )

    async def _deregister_service(self) -> None:
        """Deregister the Service instance (remove reachability) on shutdown."""
        own = self._own_iris("the Service's deregistration is only valid in resource mode")
        await anyio.to_thread.run_sync(
            functools.partial(
                deregister_service,
                self.ogm,
                own.service,
                named_graph=self.named_graph,
            )
        )

    # --- Liveness: per-service heartbeat (resource mode) ------------------- #

    async def _start_heartbeat(self) -> None:
        """Start the background heartbeat loop (resource mode)."""
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """Refresh svc:lastHeartbeat once at startup, after a random phase, then every ``heartbeat_interval`` seconds, until the system cancels it."""
        # The loop is only started when a positive interval was configured.
        interval = self.heartbeat_interval or 30.0
        own = self._own_iris("the heartbeat is only valid in resource mode")

        async def beat() -> None:
            await anyio.to_thread.run_sync(
                functools.partial(
                    update_heartbeat,
                    self.ogm,
                    own.service,
                    named_graph=self.named_graph,
                )
            )

        try:
            # The first beat is at startup, so a reader sees a fresh heartbeat right after
            # registration. The random phase follows it: instances started together would
            # otherwise beat together for life, and a grid's writes would land in one burst.
            await beat()
            await _heartbeat_wait(_heartbeat_phase(0.0, interval))
            while True:
                await beat()
                await _heartbeat_wait(interval)
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
        """Refresh this service's heartbeat once (also useful for tests/manual pings).

        Raises:
            RuntimeError: If called outside resource mode, which has no Service.
        """
        own = self._own_iris("emit_heartbeat() is only valid in resource mode")
        await anyio.to_thread.run_sync(
            functools.partial(update_heartbeat, self.ogm, own.service, named_graph=self.named_graph)
        )
        # One line per interval (30s by default), so this is a pulse rather than a flood --
        # and it is the only outward sign that an idle instance is still alive, which is
        # exactly what someone watching the activity feed wants to see.
        logger.info("Heartbeat written for %s", own.service)

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
        plain method, so a test or operator triggers a sweep directly. The issuer of each
        Operation that the sweep marks ``failed`` is notified after that commit.
        """
        swept = await anyio.to_thread.run_sync(
            functools.partial(
                _sweep_stale_services,
                self.ogm,
                self.staleness_threshold,
                named_graph=self.named_graph,
                failed=lambda op_iri: self._notify_issuers({op_iri: OperationStatus.FAILED}),
            )
        )
        return [str(s) for s in swept]

    # ------------------------------------------------------------------ #
    # The general write and the general read.
    # ------------------------------------------------------------------ #

    def commit(
        self,
        *instances: str,
        class_scope: Any = None,
        entry: Sequence[Change] = (),
        exit: ExitTransition = (),
        error: ErrorTransition = (),
    ) -> _Commit:
        """A state transition that the store admits before the work it describes begins.

        Usage, inside a coroutine::

            running = [Update(instance_iri=op, data={str(SVC.operationStatus): ["running"]})]
            done = [Update(instance_iri=op, data={str(SVC.operationStatus): ["done"]})]
            failed = [Update(instance_iri=op, data={str(SVC.operationStatus): ["failed"]})]

            async with mw.commit(
                op, class_scope=scope, entry=running, exit=done, error=failed
            ) as transition:
                torque = await robot.screw(transition.node.instance)  # the physical work
                transition.update(op, {str(SVC.executionResult): [torque]})

        and the same with ``with`` in code that runs in a worker thread, such as a callback, a
        notification handler or a script. ``entry``, ``exit`` and ``error`` are lists of
        kapps_ogm ``Create`` and ``Update`` changes. Each is written as one ``OGM.commit``, so
        each may span several instances. Nothing here is specific to a vocabulary: any
        datamodel and any state transition fit.

        - **Entry.** The entry transition is committed. If the store refuses it, its error is
          raised, nothing is written, and the body does not run. Then each of ``instances`` is
          fetched for the body: materialized under ``class_scope`` when one is given, else as a
          reference. They are ``transition.nodes`` by IRI, and ``transition.node`` when there is
          one.
        - **Body.** Domain code, such as a robot's move. It holds no transaction and no
          connection of the triple store, so heartbeats and other writes go on while it awaits.
          A body that blocks its thread (``time.sleep``, a blocking driver call) must run in a
          worker thread, with the synchronous ``with`` inside that thread, as callbacks and
          notification handlers run. **Only** ``transition.update(instance, {property:
          [values]})`` **changes the graph**, at the clean exit. A change made to
          ``transition.node`` or to its ``.instance`` is not committed, and an assignment to an
          attribute that the transition does not have raises ``AttributeError``.
        - **Clean exit.** The body's changes and the exit transition are committed as one
          ``OGM.commit``. Changes to one instance that a change names merge into one change,
          because kapps_ogm refuses two changes that write one instance. **A body that sets a
          property that the exit transition also sets, on the same instance, wins:** its value
          is committed and the exit transition is skipped as a whole. So a body that sets the
          status itself keeps it. The merge does not reach a named instance that a change only
          nests with properties: when two changes both write one, kapps_ogm raises
          ``ValueError`` at the exit, which commits the error transition and then raises.
        - **Error in the body.** The body's changes are dropped, the error transition is
          committed, and the error is raised again. An error of the fetch after the entry is
          handled the same way, and the body does not run.
        - **Refused exit.** The error transition is committed, and the store's error is raised.

        ``exit`` may be a function that returns the changes, called at the clean exit, so the
        exit transition can depend on what the body did. ``error`` may be a function that takes
        the exception and returns the changes. If the error transition itself fails, the
        original exception is raised, with a note and a log record that say so.

        **A cancellation of the body** under ``async with`` (a deadline of ``anyio.fail_after``
        or ``move_on_after`` that runs out, a cancelled task) is an error of the work: the error
        transition is committed in a shielded cancel scope, so the cancellation cannot interrupt
        it, and then the cancellation is raised again. ``error`` receives the cancellation
        exception. An interrupt or an exit (``KeyboardInterrupt``, ``SystemExit``,
        ``GeneratorExit``) writes no transition, in either form: the entry transition stays, as
        after a crash. The exit of the asynchronous form is shielded too, so a deadline that
        runs out after the body finished does not stop its exit commit.

        **Inside a coroutine, use** ``async with``. It runs every graph call (the commits, the
        fetch and the transition functions) in a worker thread, so graph I/O never blocks the
        event loop. The synchronous form makes the same calls in the calling thread and blocks
        it. Nothing stops it from being used on the event loop, where it stalls every other
        task, heartbeats included.

        **The write goes to the graph only.** All changes go to this instance's named graph. The
        datamodel that this instance serves over REST does not change, and no synced connector
        hears of the write, so no device is written.

        **An Operation's issuer is notified.** A write can move an Operation that has an issuer
        (``svc:issuedBy``) to ``running``, ``done`` or ``failed``. After the store admits such a
        write, the middleware hands the notification to a background worker of this instance and
        goes on at once. The worker reads the issuer and notifies it, as :meth:`notify` does, in
        the order of the writes. If the issuer cannot be notified, a WARNING is logged and
        nothing is raised, because the write has happened.

        **kapps_ogm's rules apply.** A ``Create`` that carries data must carry its own
        ``class_scope``: otherwise ``ValueError`` is raised before it is written, in the entry
        before any write, and in the exit transition at the exit. The store refuses a commit
        whose precondition is false with ``PreconditionFailedError``, and writes nothing of it:
        a ``Create`` of an instance that the store already holds, or a change that removes a
        value that is no longer there, because another write changed it after kapps_ogm read
        it. The error is not caught: for a ``Create``, it signals a reused IRI.

        Raises:
            kapps_triplestore_interface.exceptions.TripleStoreInterfaceError: If the store
                refuses a transition. A SHACL refusal is ``ShaclValidationError``, whose
                ``report`` is the store's validation report, verbatim. A false precondition is
                ``PreconditionFailedError``.
            ValueError: If a change does not fit its class, two changes of one transition write
                one instance (these from kapps_ogm), or a ``Create`` carries data but no
                ``class_scope``.
            requests.exceptions.RequestException: If the store cannot be reached.
        """
        return self._transition(
            *instances, class_scope=class_scope, entry=entry, exit=exit, error=error
        )

    def _transition(
        self,
        *instances: str,
        class_scope: Any = None,
        entry: Sequence[Change] = (),
        exit: ExitTransition = (),
        error: ErrorTransition = (),
        issuers: Optional[Mapping[IRI, Optional[IRI]]] = None,
        verb: _Verb = "commit",
    ) -> _Commit:
        """The commit of :meth:`commit`, and of a special function that already read the issuers
        of the Operations that it moves: ``issuers`` maps such an Operation to its issuer, or to
        None when it has none, so that it is not read again after the write. ``verb`` names the
        call that writes, for the record of a refusal: ``handover`` commits through here too."""
        return _Commit(
            self.ogm,
            instances=instances,
            class_scope=class_scope,
            entry=entry,
            exit=exit,
            error=error,
            named_graph=self.named_graph,
            on_admitted=functools.partial(self._notify_issuers_of_write, issuers=issuers or {}),
            on_refused=functools.partial(self._refused_writes.record, verb),
        )

    @property
    def refused_writes(self) -> List[RefusedWrite]:
        """The writes of this instance that the store's shapes refused, the newest first.

        :meth:`commit`, :meth:`create` and :meth:`handover` record each such refusal when it
        reaches the caller (``ShaclValidationError``), and raise the error unchanged. So does the
        write of the error transition of :meth:`commit`, which the store's shapes may refuse
        when the body failed: its record has the verb ``commit``, and its first line reads
        ``commit (error transition) refused by ...``. The caller still gets the error of the
        body, with a note that the error transition was not committed. The record holds the
        time, the verb, the rules that the report names, and the report. The instance keeps the
        newest 50 in memory, and nothing in the graph; an older one drops. A refusal that is no
        refusal by the shapes, such as a false precondition, is not recorded.

        Each recorded refusal is also logged, as one record at INFO on the logger
        ``kapps_semantic_middleware.validation_report``. The dashboard's box "Refused writes"
        shows them; ``validation_report.render_refused_write`` renders one.
        """
        return self._refused_writes.newest_first()

    @property
    def resource(self) -> Optional[IRI]:
        """This middleware's own Resource: the short form where a Resource is named, as in
        ``workpiece(scope, possessor=mw.resource)``. None in a mode without a Resource."""
        return self.resource_iri

    def _own_iris(self, refusal: str) -> _OwnIris:
        """This instance's Resource, Service class and Service, for a resource-only path.

        Resource mode sets all three at construction, and the other modes set none, so this
        check is the mode check of the path that reads them. ``refusal`` is the first half of
        its error, which names the path.

        Raises:
            RuntimeError: ``"<refusal>; current mode is <mode>"``, outside resource mode.
        """
        # The tests for None only narrow the types: in resource mode none of the three is None.
        if (
            self.mode != Mode.RESOURCE
            or self.resource_iri is None
            or self.service_class is None
            or self.service_iri is None
        ):
            raise RuntimeError(f"{refusal}; current mode is {self.mode!r}")
        return _OwnIris(self.resource_iri, self.service_class, self.service_iri)

    def create(self, cls: str, scope: Any, instance_iri: Optional[str] = None) -> _Creation:
        """Create one new instance of the class ``cls``, built on :meth:`commit`.

        Usage::

            with mw.create(SNAPSHOT, snapshot_scope) as snapshot:
                snapshot.update({str(EX.decision): ["unscrew"]})

        and ``async with`` in a coroutine, as for :meth:`commit`. For new work for a resource that
        realizes a Capability, use :meth:`issue_operation`: it creates the Operation with this method, and
        then notifies the receiver.

        - **Entry.** The IRI is minted, ``<class>_<12 hex>``, unless ``instance_iri`` is given,
          and the body reads it as ``creation.iri``: a nested creation can be linked before
          anything is written. A predefined scope checks its inputs here; wrong inputs raise
          before the body runs and before any write. Nothing is read or written.
        - **Body.** ``creation.update({property: [values]})`` sets the new instance's properties.
          A value that is an IRI, such as the ``iri`` of a nested creation, is a reference.
        - **Clean exit, alone.** One ``OGM.commit`` writes the ``Create`` of the instance, the
          changes of its predefined scope, and the changes of every creation nested in it. A
          refusal by the store raises its error, and nothing is written.
        - **Clean exit, nested.** Inside the body of a :meth:`commit` or of another
          :meth:`create` of this middleware whose write is still to come, the same changes join
          that write instead: they are admitted or refused together with it, and an error in its
          body drops them. A creation commits on its own inside a body whose commit has already
          happened, such as that of :meth:`handover`, and inside a write of another middleware
          (another OGM or another named graph), such as a planner's beside a resource's in one
          process.
        - **Error in the body.** Nothing is written or joined, and the error is raised again.
          Nothing exists yet that could carry an error transition.

        After the clean exit, ``creation.changes`` are the changes that the creation committed or
        joined: its ``Create`` first, then those of its predefined scope and of its nested
        creations.

        ``scope`` is a ClassScope of the class, or a predefined scope, which brings the rules for
        creating an instance of its class: a domain module gives it, and it may require inputs and
        add changes to the same commit. The ``Create`` names every property that ``scope`` names
        and every property that the body sets, so kapps_ogm drops none of them.

        **kapps_ogm's rules apply.** An ``instance_iri`` that the store already holds is refused
        with ``PreconditionFailedError`` at the commit, and nothing is written.

        Raises:
            ValueError: If a predefined scope's inputs are wrong (at entry), the body sets a
                property that its predefined scope sets itself, or the data does not fit the
                class (from kapps_ogm).
            kapps_triplestore_interface.exceptions.TripleStoreInterfaceError: If the store
                refuses the commit. A SHACL refusal is ``ShaclValidationError``, whose
                ``report`` is the store's validation report, verbatim. An instance that the
                store already holds is ``PreconditionFailedError``.
            requests.exceptions.RequestException: If the store cannot be reached.
        """
        return _Creation(
            self.ogm,
            cls,
            scope,
            instance_iri=instance_iri,
            named_graph=self.named_graph,
            on_admitted=self._notify_issuers_of_write,
            on_refused=functools.partial(self._refused_writes.record, "create"),
        )

    def fetch(
        self,
        instance_iri: str,
        *,
        class_scope: Any = None,
        as_reference: bool = False,
        materialize: bool = False,
    ) -> Any:
        """Read one instance from the graph through the OGM, as a plain call.

        A read has nothing to guard, so this is no context manager. It is ``OGM.fetch`` with
        this instance's OGM: ``class_scope`` selects the properties to load, ``as_reference``
        loads only the IRI, and ``materialize`` builds the datamodel (``node.instance``). It
        blocks while it reads, so asynchronous code calls it in a worker thread, as the
        asynchronous form of :meth:`commit` does.

        Returns:
            The kapps_ogm ``Node`` of the instance.

        Raises:
            ValueError: If the store holds no class for the instance.
            kapps_ogm.utils.errors.AnonymousNodeFetchError: If the IRI is a Skolem IRI of an
                anonymous node, which only its parent carries.
        """
        return self.ogm.fetch(
            instance_iri=IRI(instance_iri),
            class_scope=class_scope,
            as_reference=as_reference,
            materialize=materialize,
        )

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

        The function's docstring describes the routes that run it, ``execute`` and
        ``execute_background``, in the instance's API description (``/docs``): its first line
        is their summary, and the whole docstring their description. A function without one
        keeps the framework's summary and gets no description.

        On an instance with a notification handler (:meth:`on_notification`), a notified
        Operation that implements the Capability this workflow realizes runs it, with the
        Operation as its one argument: a kapps_ogm ``Node``, read and materialized under the
        intake's scope (``operation.instance``). A parameter typed ``kapps_ogm.Node`` registers as
        any other does, because pydantic takes a ``Node`` as it is. Its REST route is mounted too,
        but the framework takes a one-parameter workflow's argument as an optional query parameter
        ``arg``, not as a body: a call passes ``None``, or a string from the query, and the
        framework's type check (typeguard) refuses both, so a call over REST is answered with an
        HTTP 500 and such a workflow is run by the intake::

            @mw.workflow(capability_class=TRANSPORT_CAPABILITY, workflow_class=TRANSPORT_WORKFLOW)
            def box_transport(transport: Node) -> None:
                ...  # the work; it commits its own transitions with mw.commit()

        Raises:
            RuntimeError: If called outside resource mode.
            ValueError: If capability_class or workflow_class is missing.
        """
        own = self._own_iris("@workflow decorator only valid in resource mode")
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
            before = len(self.app.routes)
            wrapped = base_decorator(func)
            # Of the routes that the framework mounts for the workflow, those that run it say
            # what it does. Its description and interrupt routes keep the framework's summaries.
            _describe_routes(_routes_since(self.app, before, path_endings=_RUN_ROUTES), func)
            name = func.__name__
            workflow_iri = mint_workflow_iri(own.service, name)
            capability_iri = mint_capability_iri(own.resource, name)
            endpoint = build_workflow_endpoint(self.address, name)
            # The intake's match: a notified Operation that implements this Capability runs
            # this function.
            self._workflow_functions[capability_iri] = func

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
                        resource_iri=own.resource,
                        service_iri=own.service,
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

        The getter's docstring describes the route in the instance's API description
        (``/docs``): its first line is the summary, and the whole docstring the description. A
        getter without one keeps the default summary and gets no description.

        Raises:
            RuntimeError: If called outside resource mode.
            ValueError: If capability_class or state_property_class is missing.
        """
        own = self._own_iris("@state decorator only valid in resource mode")
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
            state_property_iri = mint_state_property_iri(own.service, state_name)
            capability_iri = mint_capability_iri(own.resource, state_name)
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

            before = len(self.app.routes)
            self.app.include_router(router)
            # The route says what the getter does, not what this handler does.
            _describe_routes(_routes_since(self.app, before), func)

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
                        resource_iri=own.resource,
                        service_iri=own.service,
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
            """Take a notification: the IRI of an Operation for this instance to look at.

            A notification names one Operation in the graph and carries nothing else. It says
            that an Operation for this instance exists, or that one this instance issued ended
            or changed. The instance reads the Operation from the graph and answers at once,
            with no business result. With a notification handler, the Operation goes into the
            instance's intake. Without one, it goes into the instance's queue, unless it is the
            feedback of an Operation that this instance issued. The answer names the Operation,
            and says ``queued`` only when it was queued.
            """
            return await middleware._handle_event_trigger(IRI(payload.operation_iri))

        # Base decorator mounts the route without the KG registration the KAPPS
        # ``self.workflow(...)`` override performs. The routes that run it say what it does,
        # as those of a registered workflow do: the docstring above is what `/docs` shows.
        before = len(self.app.routes)
        Middleware.workflow(self)(event_trigger)
        _describe_routes(_routes_since(self.app, before, path_endings=_RUN_ROUTES), event_trigger)

    async def _handle_event_trigger(self, operation_iri: IRI) -> Dict[str, str]:
        """Receiver-side event trigger: take a notification of an Operation.

        The trigger carries only the Operation IRI; its payload lives in the graph. The
        Operation is read (a read that fails is raised, so the notifier learns that it was not
        taken), and the trigger answers at once, with no business result. What happens next
        depends on whether this instance registered a notification handler:

        - **With one** (``on_notification``), the Operation goes into the **intake**. The read
          is the intake's one read, under its scope. The Operation enters the instance's FIFO,
          and one worker runs one Operation at a time, in arrival order: an Operation whose
          Capability is one of this instance's own goes to the exposed workflow that realizes
          it, any other to the handler, each with the Operation object. An Operation that waited
          is read again before it runs. The middleware writes nothing. The answer names the
          Operation and claims no status.
        - **Without one**, the Operation enters this instance's in-memory queue and stays
          ``queued``. A domain callback (``register_callback``) or a pull-and-run
          (``claim_next``) runs the work later, with the dispatch lifecycle. The answer says
          ``queued``. The read is one query, of the Operation's status and issuer.

        **Feedback.** A notification of an Operation that this instance issued wakes its handle
        (:class:`IssuedOperation`). It wakes it before the read, because the handle reads the
        status itself: a read that fails here then does not leave a wait to its timeout. An
        Operation that this resource issued and that is no longer ``queued`` never enters the
        queue: the notification is the feedback of its claim or of its end, and the answer names
        the Operation and claims no status. With a handler, the handler gets it, and never a
        workflow.
        """

        self._wake_handle(operation_iri)
        if self._notification_handler is not None:
            read = await anyio.to_thread.run_sync(self._read_received, operation_iri)
            self._take_in(operation_iri, read)
            return {"operation": str(operation_iri)}
        notified = await anyio.to_thread.run_sync(
            functools.partial(_read_notified, self.ogm, operation_iri)
        )
        if _issued_and_claimed(notified, self.resource_iri):
            # The claim or the end of an Operation that this resource issued: no work here.
            return {"operation": str(operation_iri)}
        self._operation_queue.append(operation_iri)
        if self._callback is not None:
            # Fire the domain callback in the background. The trigger returns
            # immediately (it does not block on the work). The pull-and-run runs
            # off the event loop, so the blocking graph I/O never stalls the server.
            self._run_in_background(self._run_callback())
        return {"operation": str(operation_iri), "status": OperationStatus.QUEUED}

    def _read_received(self, operation_iri: IRI) -> _ReadOperation:
        """Read a received Operation for the intake, and find the Capabilities it implements.

        One read: the Operation is fetched and materialized under the intake's scope, which is
        the Capability that the Operation implements and the Workflow that realizes it, united
        with the scope given to :meth:`on_notification`. The Capabilities come from that read,
        so the intake's match needs no query of its own. It blocks while it reads.

        Returns:
            The Operation as a kapps_ogm ``Node``, and the IRIs of its Capabilities.
        """
        node = self.fetch(
            operation_iri,
            class_scope=_received_scope(self._notification_scope),
            materialize=True,
        )
        return _ReadOperation(node, _implemented_capabilities(node))

    def _take_in(self, operation_iri: IRI, read: Optional[_ReadOperation] = None) -> None:
        """Put a received Operation into the intake's FIFO, and start the worker when it is idle.

        An Operation that arrives while the worker is at work waits its turn, so its read is
        dropped and it is read again before it runs. Without ``read``, it is read before it
        runs, as after a restart. Runs on the event loop, so the FIFO needs no lock.
        """
        if self._intake_working:
            read = None
        self._intake.append(_Received(operation_iri, read))
        if not self._intake_working:
            self._intake_working = True
            self._run_in_background(self._work_intake())

    async def _work_intake(self) -> None:
        """The intake's one worker: it runs the FIFO's Operations one at a time, in arrival
        order, and ends when the FIFO is empty."""
        try:
            while self._intake:
                await self._run_received(self._intake.popleft())
        finally:
            self._intake_working = False

    async def _run_received(self, received: _Received) -> None:
        """Run one Operation of the intake, and log any exception.

        An Operation that waited is read again first. Then the Operation object goes to the
        exposed workflow that realizes one of its Capabilities, matched in memory, or else to
        the notification handler. A plain function runs in a worker thread, a coroutine
        function on the event loop. The middleware writes nothing, also when the read or the
        function raises: the exception is logged, and the worker goes on with the next one.
        """
        what = "The read before the run"
        try:
            read = received.read
            if read is None:
                read = await anyio.to_thread.run_sync(self._read_received, received.iri)
            own = [c for c in read.capabilities if c in self._workflow_functions]
            if own:
                # A notification of an Operation that this resource issued, and that is no longer
                # queued, tells of its claim or its end: the handler gets it, never the workflow.
                # It is read from the graph, so it holds after a restart too.
                what = "The read of the status and the issuer"
                notified = await anyio.to_thread.run_sync(
                    functools.partial(_read_notified, self.ogm, received.iri)
                )
                if _issued_and_claimed(notified, self.resource_iri):
                    own = []
            if own:
                function: Callable[[Any], Any] = self._workflow_functions[own[0]]
                what = f"The workflow {getattr(function, '__name__', function)}"
            else:
                what = "The intake"
                if self._notification_handler is None:  # the intake runs only with a handler
                    raise DeploymentRuntimeError(
                        f"{self.resource_iri} took an Operation into the intake without a "
                        "notification handler"
                    )
                function = self._notification_handler
                what = "The notification handler"
            if inspect.iscoroutinefunction(function):
                await function(read.node)
            else:
                await anyio.to_thread.run_sync(function, read.node)
        except Exception:
            logger.error(
                "%s of %s raised for the Operation %s",
                what,
                self.resource_iri,
                received.iri,
                exc_info=True,
            )

    def _wake_handle(self, operation_iri: IRI) -> None:
        """Wake the waits of the handle of ``operation_iri``, if this instance issued it and its
        caller keeps the handle."""
        handle = self._issued_handles.get(operation_iri)
        if handle is not None:
            handle._wake()

    def _notify_issuers_of_write(
        self, changes: Sequence[Change], issuers: Mapping[IRI, Optional[IRI]] = {}
    ) -> None:
        """After a write that the store admitted: notify the issuer of each Operation that the
        write moved to ``running``, ``done`` or ``failed``.

        The write's own changes show such a move: an ``Update`` that sets ``svc:operationStatus``
        to one of the three. Its old value is not known without a read, so a write that sets the
        status that the Operation already has notifies too. ``issuers`` holds what the caller has
        already read: an Operation's issuer, or None for one without an issuer. The others are
        read in one query.
        """
        moved = _moved_operations(changes)
        if moved:
            self._notify_issuers(moved, issuers)

    def _notify_issuers(
        self, moved: Mapping[IRI, str], issuers: Mapping[IRI, Optional[IRI]] = {}
    ) -> None:
        """Hand the notification of the issuer of each Operation in ``moved`` to this instance's
        background worker, and return at once. ``moved`` maps an Operation to its new status.

        The worker sends them in the order they were handed over. A slow or hanging issuer
        delays only the worker, never the commit or the work after it.
        """
        self._notifier.submit(
            functools.partial(self._send_to_issuers, dict(moved), dict(issuers))
        )

    def _send_to_issuers(
        self, moved: Mapping[IRI, str], issuers: Mapping[IRI, Optional[IRI]]
    ) -> None:
        """In the background worker: notify the issuer of each Operation in ``moved``.

        An issuer that the caller has not read is read here, in one query. An Operation without
        an issuer notifies nobody. An issuer that cannot be notified (no live Service, more than
        one, or a post that fails) is logged, one line at WARNING: the status is in the graph.
        """
        known = {op: issuers[op] for op in moved if op in issuers}
        unread = [op for op in moved if op not in issuers]
        if unread:
            try:
                known.update(_issuers(self.ogm, unread))
            except Exception as error:
                logger.warning(
                    "The issuers of %s were not read, so none was notified: %r",
                    ", ".join(map(str, unread)),
                    error,
                )
        for op_iri, status in moved.items():
            issuer = known.get(op_iri)
            if issuer is None:
                continue
            try:
                self._notify(op_iri, issuer)
            except Exception as error:
                logger.warning(
                    "The issuer %s of %s was not notified that it is %s: %s",
                    issuer,
                    op_iri,
                    status,
                    error,
                )

    def _flush_notifications(self, timeout: Optional[float] = None) -> bool:
        """Wait until the issuers' notifications handed over so far are sent: True, or False when
        ``timeout`` seconds passed first. For a test that must see them."""
        return self._notifier.flush(timeout)

    async def _stop_notifications(self) -> None:
        """At the stop of the instance: send what waits for up to one second, then drop the rest
        with one WARNING line. The status of each Operation is in the graph either way."""
        dropped = await anyio.to_thread.run_sync(
            self._notifier.stop, _NOTIFICATIONS_STOP_SECONDS
        )
        if dropped:
            logger.warning(
                "%s stopped and dropped %d notification(s) of issuers that were still to send",
                self.resource_iri or self.mode,
                dropped,
            )

    def _run_in_background(self, coroutine: Any) -> None:
        """Run ``coroutine`` as a task, and keep a reference until it ends.

        Without the reference the event loop holds only a weak one, and the task can be
        garbage-collected before it finishes.
        """
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def notify(self, *, operation: Any, resource: Any, timeout: float = 10.0) -> None:
        """Send a notification: tell `resource` to look at `operation` in the graph.

        Usage::

            mw.notify(operation=transport, resource=origin_module)

        A notification is one-way. It names one Operation in the graph and carries nothing
        else: no payload, and no result. It works in both directions: down, "an Operation for
        you exists", and up, "this Operation ended or changed". The middleware writes nothing
        to the graph for it. The resource's middleware takes it through its intake when it
        registered an `on_notification` handler: the exposed workflow that realizes the
        Operation's Capability runs it, or else the handler gets it. It queues it when it
        registered no handler. A notification of an Operation that the resource issued and that
        is no longer `queued` is feedback: it wakes the Operation's handle, the handler gets it,
        and it is never queued and never runs a workflow.

        The call finds the resource's live Service in the graph (one that carries an
        `svc:address`), posts the IRI to that Service's event trigger, and returns when the
        receiver confirms receipt, which it does at once. It raises
        `NotificationUndelivered` when the resource has no live Service, when it has more
        than one (the call does not choose between them), and when the post fails, is
        refused, or takes longer than `timeout` seconds: its subclass
        `NotificationReceiverAmbiguousError` (Repair) for more than one live Service, and
        `NotificationReceiverUnavailableError` (Retry) for each other case.

        This is a plain blocking call. From a coroutine, run it off the event loop::

            await anyio.to_thread.run_sync(
                functools.partial(mw.notify, operation=op, resource=wms)
            )
        """

        if self.mode != Mode.RESOURCE:
            raise RuntimeError(
                f"notify() is only valid in resource mode; current mode is {self.mode!r}"
            )
        self._notify(IRI(operation), IRI(resource), timeout)

    def _notify(self, operation_iri: IRI, resource_iri: IRI, timeout: float = 10.0) -> None:
        """:meth:`notify` without its check of the mode, so a watchdog tells an issuer too.

        Raises:
            NotificationReceiverUnavailableError: If the resource has no live Service, or the
                post fails (Retry).
            NotificationReceiverAmbiguousError: If the resource has more than one live Service
                (Repair).
        """
        reachable = reachable_service_addresses(self.ogm, resource_iri)
        if not reachable:
            raise NotificationReceiverUnavailableError(
                f"{resource_iri} has no live Service to notify of {operation_iri}"
            )
        if len(reachable) > 1:
            listing = ", ".join(sorted(f"{service} at {address}" for service, address in reachable))
            raise NotificationReceiverAmbiguousError(
                f"{resource_iri} has more than one live Service ({listing}); notify() does not "
                f"choose which one to notify of {operation_iri}"
            )
        self._deliver(operation_iri, resource_iri, reachable[0][1], timeout)

    @staticmethod
    def _deliver(operation_iri: IRI, recipient: IRI, address: str, timeout: float) -> None:
        """The delivery of a notification, for :meth:`notify` and :meth:`issue_operation`: post the
        Operation's IRI to the event trigger at `address`, and return when the receiver confirms
        receipt. `recipient` names the receiver in the error.

        Raises:
            NotificationReceiverUnavailableError: If the post fails (Retry): `address` is no
                valid URL, or the post cannot connect, is refused, or takes longer than
                `timeout` seconds. Its cause is the error of the post.
        """
        try:
            httpx.post(
                build_event_trigger_url(address),
                json={"operation_iri": str(operation_iri)},
                timeout=timeout,
            ).raise_for_status()
        # A malformed address is no HTTPError: httpx raises InvalidURL for it, and a host that the
        # IDNA codec refuses, such as one with a label over 63 characters, raises UnicodeError.
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as error:
            raise NotificationReceiverUnavailableError(
                f"{recipient} at {address} did not take the notification of "
                f"{operation_iri}: {error!r}"
            ) from error

    @contextlib.contextmanager
    def issue_operation(
        self,
        *,
        capability_class: Any,
        operation_class: Any,
        operation_iri: Optional[str] = None,
        target_resource: Optional[str] = None,
    ):
        """Issue an Operation: new work for a resource that realizes a Capability.

        Usage::

            with mw.issue_operation(capability_class=cap, operation_class=op_cls) as op:
                op.data[str(EX.hasScrew)] = [{"id": str(screw)}]  # its domain fields, if any
            if await op.claimed(timeout=30):
                status = await op.ended()  # "done" or "failed"

        Use it for a new Operation. It is three steps, each the work of another part of the
        middleware: discovery, then :meth:`create`, then the delivery of :meth:`notify`.

        - **Discovery.** Before the block runs, a live Service is found that realizes a
          Capability of ``capability_class`` (with ``target_resource``, one of that resource).
          If there is none, ``OperationResolutionError`` is raised, and nothing is written:
          ``OperationReceiverUnavailableError`` (Retry) when a Workflow realizes the Capability
          but no Service of it is live now, and ``CapabilityNotRealizedError`` (Repair) when no
          Workflow realizes it.
        - **The block.** It sets the Operation's domain properties in ``op.data``, as
          ``{property IRI: [values]}``, and may read ``op.iri``. An error in the block writes
          nothing.
        - **Create.** At the end of the block, one :meth:`create` of the Operation writes it,
          under the operation scope. It writes the type, the ``cfc:implementsCapability`` link
          to the Capability that discovery found, the status ``queued`` and ``op.data``. It also
          writes ``svc:issuedBy``, the issuer: this middleware's Resource, ``op.issuer``. The
          store admits all of it or none of it. A refusal is raised, and nobody is notified. An
          ``operation_iri`` that the store already holds is refused with
          ``PreconditionFailedError``, and that Operation is not touched. If ``op.data`` sets
          ``svc:issuedBy``, ``ValueError`` is raised before anything is written.
        - **Notify.** With the Operation in the graph, the Service that discovery found is
          notified, through its event trigger, and takes the Operation through its intake or
          its queue (see :meth:`on_notification`). If the notification fails, the Operation
          stays ``queued``, and ``NotificationReceiverUnavailableError`` (Retry), a
          ``NotificationUndelivered``, is raised. Nothing is undone: the
          receiver takes up its ``queued`` Operations when it starts, and the issuer is
          responsible for the Operation while it is ``queued``.

        After the block, ``op`` is the :class:`IssuedOperation`: it waits until the Operation is
        claimed and until it ends. The middleware that claims it, and the one that ends it,
        notifies the issuer after its commit, and that notification wakes the waits.

        The Operation commits on its own, also inside the block of another :meth:`commit` or
        :meth:`create`, because the receiver reads it after the notification. A :meth:`create`
        nested in this block joins the Operation's commit.

        This is the in-process caller face — not REST-exposed.
        """

        own = self._own_iris("issue_operation() is only valid in resource mode")
        capability_class_iri = IRI(capability_class)
        operation_class_iri = IRI(operation_class)

        # Discovery, before the block: an unroutable request fails before any domain work.
        capability_iri, service_iri, address = resolve_dispatch_target(
            self.ogm,
            capability_class_iri,
            target_resource=IRI(target_resource) if target_resource else None,
            named_graph=self.named_graph,
        )

        # The receiver reads the Operation after it is notified, so the Operation commits on its
        # own, also in the body of another commit, whose write would come too late. A creation
        # nested in this block joins the Operation's commit. An error in the block writes nothing.
        with _nothing_to_join(), self.create(
            operation_class_iri,
            operation(ClassScope(), capability=capability_iri),
            instance_iri=operation_iri or None,
        ) as created:
            issued = IssuedOperation(self, created.iri, own.resource)
            yield issued
            if any(_property(key) == SVC.issuedBy for key in issued.data):
                raise ValueError(
                    f"the data of {created.iri} sets {SVC.issuedBy}, which issue_operation() sets "
                    f"itself, to {own.resource}"
                )
            created.update({str(SVC.operationStatus): [OperationStatus.QUEUED]})
            created.update(issued.data)
            created.update({str(SVC.issuedBy): [{"id": str(own.resource)}]})

        # The Operation is in the graph: its handle can wait, and only now is the receiver
        # notified, through the delivery of notify().
        issued._in_graph = True
        self._issued_handles[issued.iri] = issued
        self._deliver(issued.iri, service_iri, address, timeout=30.0)

    def request(
        self,
        *,
        capability_class: Any,
        operation_class: Any,
        operation_iri: Optional[str] = None,
        target_resource: Optional[str] = None,
    ):
        """Deprecated alias of :meth:`issue_operation`; use ``issue_operation()``.

        It warns with a ``DeprecationWarning``, then calls :meth:`issue_operation` with the same
        arguments and returns its context manager. It goes in the release after next.
        """

        warnings.warn(
            "SemanticMiddleware.request() is deprecated and will be removed in the release "
            "after next; use issue_operation(), which takes the same arguments",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.issue_operation(
            capability_class=capability_class,
            operation_class=operation_class,
            operation_iri=operation_iri,
            target_resource=target_resource,
        )

    @contextlib.contextmanager
    def claim_next(self, scope: Any = None):
        """Pull-and-run the next queued Operation as a transaction context manager.

        Usage::

            with mw.claim_next(scope) as claimed:
                claimed.result = do_the_work(claimed.operation)

        It is a :meth:`commit` about the Operation it claims:

        - **Entry.** `__enter__` takes the next `queued` Operation from this instance's
          in-memory queue (FIFO order, raises `OperationQueueEmpty` if none), and commits the
          claim, `queued` to `running`, before the body runs. If the store refuses the claim, its
          error is raised and the body does not run; the Operation has left the in-memory queue,
          and the graph keeps it as it was. Then the Operation is re-fetched under the
          domain-supplied `ClassScope`, so the body gets exactly the object shape it needs.
        - **Body.** It runs the work, and may set `claimed.result`.
        - **Clean exit.** `done` plus provenance (`executedByWorkflow` / `executionTimestamp` /
          `executionResult`) is one commit.
        - **Error.** On a body exception, a fetch that fails after the claim, or a refused
          `done`, `failed` plus provenance and the exception message is one commit, and the
          exception is raised. Provenance is folded into the terminal transition. On failure the
          CM also dumps the resource datamodel and stores it as the Operation's
          ``svc:failureState`` in the same commit, so the state that produced the failure
          survives the exception.
        - **The issuer.** After the claim, and after `done` or `failed`, the Operation's issuer
          (`svc:issuedBy`) is notified, as after any :meth:`commit` that moves an Operation.
          The read before the claim finds the issuer, so the claim reads nothing more for it.

        Two claimants of one Operation are not told apart yet: the claim of an Operation that is
        already `running` changes nothing, and the store admits it.

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
        # stays queued (in the graph and this in-memory queue) for a later retry. The same read
        # finds the Operation's issuer, whom the claim and the end notify.
        read = _resolve_claim(self.ogm, op_iri, named_graph=self.named_graph)
        workflow_iri = read.workflow

        # Preconditions passed: dequeue, then claim it in the commit's entry. A refused claim has
        # therefore left the in-memory queue, as a failed `running` write always has.
        self._operation_queue.pop(0)
        claimed = _ClaimedOperation(op_iri, None)

        def done() -> List[Change]:
            return [
                _terminal_status_update(
                    operation_iri=op_iri,
                    workflow_iri=workflow_iri,
                    status=OperationStatus.DONE,
                    result=claimed.result,
                )
            ]

        def failed(exc: BaseException) -> List[Change]:
            return [
                _terminal_status_update(
                    operation_iri=op_iri,
                    workflow_iri=workflow_iri,
                    status=OperationStatus.FAILED,
                    result=str(exc),
                    failure_state=self._dump_resource_datamodel(),
                )
            ]

        with self._transition(
            op_iri,
            class_scope=scope,
            entry=[_operation_status_update(op_iri, OperationStatus.RUNNING)],
            exit=done,
            error=failed,
            issuers={op_iri: read.issuer},
        ) as transition:
            claimed.operation = transition.node
            yield claimed

    @contextlib.contextmanager
    def handover(
        self,
        *,
        mode: Any,
        workpiece: Any,
        counterpart: Any,
        workpiece_update: Optional[Dict[str, List[Any]]] = None,
        operation: Any = None,
        entry: Optional[Sequence[Change]] = None,
    ):
        """Hand a workpiece to a counterpart: the possession changes first, then the body moves it.

        Usage::

            with mw.handover(mode=MES.Pass, workpiece=box, counterpart=next_module, operation=op):
                conveyor.pass_box()  # the physical move, owned by the domain

        and, with more changes that must be admitted together with the possession change, such as
        a transport that the next module now executes::

            moved_on = Update(
                instance_iri=transport,
                data={
                    str(CFC.executedOn): [{"id": next_module}],
                    str(CFC.implementsCapability): [{"id": next_capability}],
                },
            )
            with mw.handover(
                mode=MES.Pass, workpiece=box, counterpart=next_module, entry=[moved_on]
            ):
                conveyor.pass_box()

        The entry checks two preconditions before anything is written: this resource currently
        possesses the workpiece (the workpiece points at a `cfc:PossessionState` whose possessor
        is this resource), and the counterpart carries the `mes:hasHandoverAbility` complementary
        to `mode`. There is deliberately no check that the counterpart is free: a resource may
        possess any number of workpieces unless a domain rule limits it.

        Then the entry commits the possession change through :meth:`commit`, as one commit. The
        workpiece's `cfc:hasPossessedWorkpiece` points at the counterpart's one possession state;
        `workpiece_update` sets more properties of the workpiece that must change together with
        its possessor, such as its transport state; `operation`, the Operation that this
        handover completes, becomes `done`; and `entry`, the caller's own kapps_ogm changes, is
        written too. When the counterpart has no state yet, the same commit creates
        `<counterpart>_possession` and links the counterpart to it. Nothing writes the
        `cfc:hasPossessor` link of this resource, or of a counterpart that has a state.

        If the store refuses the commit, its error is raised (a SHACL refusal is
        `ShaclValidationError`, with the store's validation report as its `report`), nothing is
        written, neither the possession change nor a change of `entry`, and the body does not run.
        The body is the move: the physical transport, and any coordination with the counterpart, for
        example through an `issue_operation(...)`. A clean exit writes nothing more. An error in the body is
        raised as it is and undoes nothing: the possession has already changed, so the caller
        records what really happened.

        Core says that a resource and a workpiece each have exactly one possession state. That is
        OWL: it licenses inferences and refuses no write. What refuses a handover at commit is the
        domain's SHACL shape, for example the one that allows one box on a conveyor module.

        A first handover to a counterpart without a state can race another write that creates
        that state. Two commits that truly coincide both write the same state triples, and one
        state results. When the other write lands between this handover's read and its commit,
        the store refuses the second creation of the state (`PreconditionFailedError`), and
        writes nothing of it. The handover then checks both preconditions again and commits once
        more, with the state that exists now; if a precondition no longer holds, for example
        because the other write handed this same workpiece, it raises
        `HandoverPreconditionError`, and the body does not run.

        A second handover of the same workpiece at the same moment is refused only in part.
        Both pass the preconditions before either commits. Inside its commit, kapps_ogm compares
        the workpiece with a fresh read of it. If the other handover's commit landed between that
        read and this commit's write, the store refuses this commit and writes nothing of it, and
        the handover raises `PossessionSwitchConflict` before the body runs. Known limit: if the
        other handover's commit landed before that read, this commit overwrites its possession
        change, both succeed, and both bodies run.

        Args:
            mode: This resource's handover ability, such as `MES.Pass`. The counterpart must
                carry the complementary one.
            workpiece: The workpiece that this resource possesses.
            counterpart: The resource that takes the workpiece over.
            workpiece_update: More properties of the workpiece to set in the same commit, as
                `{property IRI: [values]}`; each listed property is replaced. A value that is an
                IRI (an `IRI` or an rdflib `URIRef`), or `{"id": iri}`, is a reference to that
                object. Any other value goes to the OGM as it is: a plain `str` is a literal,
                which the OGM refuses for an object property with `ValueError`, before any write.
            operation: The Operation that this handover completes. Its `svc:operationStatus`
                becomes `done` in the same commit.
            entry: More kapps_ogm `Create` and `Update` changes, built by the caller, that the
                store admits or refuses in the same commit as the possession change, before the
                body. A change of the workpiece or of `operation` belongs in `workpiece_update` or
                is made by `operation`: kapps_ogm refuses two changes of one instance in one
                commit, with `ValueError`.

        Raises:
            RuntimeError: If this instance is not in resource mode.
            HandoverPreconditionError: If a precondition fails. Nothing is written.
            PossessionSwitchConflict: If the store found the precondition of the entry's
                commit false (`PreconditionFailedError`, the cause). Usually another write
                changed what the entry had read, such as a second handover of the workpiece.
                The store does not say which part of the commit failed, so the same error
                covers a change of `entry` that creates an instance that the store holds.
                Nothing is written, and the body does not run.
            ValueError: If `workpiece_update` names `cfc:hasPossessedWorkpiece`, which the
                handover sets itself, or a value that does not fit its property, or a change of
                `entry` does not fit its class or writes an instance that the commit already
                writes. Nothing is written.
            DeploymentValueError: If the counterpart has more than one possession state
                (Repair). It is a ``ValueError``. Nothing is written.
            kapps_triplestore_interface.exceptions.TripleStoreInterfaceError: If the store
                refuses the possession change; a SHACL refusal is `ShaclValidationError`.
                Nothing is written, and the body does not run.
        """

        if self.mode != Mode.RESOURCE:
            raise RuntimeError(
                f"handover() is only valid in resource mode; current mode is {self.mode!r}"
            )
        workpiece_iri = IRI(workpiece)
        counterpart_iri = IRI(counterpart)
        mode_ability_iri = IRI(mode)

        self._check_handover_preconditions(workpiece_iri, counterpart_iri, mode_ability_iri)

        # What the commit carries beside the possession change: the Operation that the handover
        # completes, and the caller's own changes.
        alongside: List[Change] = []
        if operation is not None:
            done = {str(SVC.operationStatus): [OperationStatus.DONE]}
            alongside.append(Update(instance_iri=IRI(operation), data=done))
        alongside.extend(entry or ())

        # The entry commits the possession change; the body is the move; the exit writes nothing.
        change = possession_change(
            self.ogm,
            workpiece_iri=workpiece_iri,
            possessor_iri=counterpart_iri,
            workpiece_update=workpiece_update,
        )
        with contextlib.ExitStack() as admitted:
            try:
                admitted.enter_context(
                    self._transition(entry=[*change.changes, *alongside], verb="handover")
                )
            except PreconditionFailedError as refusal:
                # The store wrote nothing of the entry. When the entry creates the counterpart's
                # state, another write may have created that state after it was read: then the
                # preconditions are checked again, and the entry is committed once more with
                # that state. Any other refusal is a conflict.
                again = None
                if change.creates_state:
                    again = possession_change(
                        self.ogm,
                        workpiece_iri=workpiece_iri,
                        possessor_iri=counterpart_iri,
                        workpiece_update=workpiece_update,
                    )
                if again is None or again.creates_state:
                    raise _switch_conflict(workpiece_iri) from refusal
                self._check_handover_preconditions(workpiece_iri, counterpart_iri, mode_ability_iri)
                try:
                    admitted.enter_context(
                        self._transition(entry=[*again.changes, *alongside], verb="handover")
                    )
                except PreconditionFailedError as second_refusal:
                    raise _switch_conflict(workpiece_iri) from second_refusal
            # The commit has happened, so a creation in the body commits on its own.
            admitted.enter_context(_nothing_to_join())
            yield

    def _check_handover_preconditions(
        self, workpiece_iri: IRI, counterpart_iri: IRI, mode_ability_iri: IRI
    ) -> None:
        """The two preconditions of a handover, read before anything is written.

        Raises:
            HandoverPreconditionError: If this resource does not possess the workpiece, or the
                counterpart lacks the handover ability complementary to the mode.
        """
        resource_iri = self._own_iris("handover() is only valid in resource mode").resource
        current_ps = find_possession_state(
            self.ogm, workpiece_iri, resource_iri, named_graph=self.named_graph
        )
        if current_ps is None:
            raise HandoverPreconditionError(
                f"{resource_iri} does not currently possess {workpiece_iri}"
            )
        if not counterpart_has_complementary_ability(
            self.ogm, counterpart_iri, mode_ability_iri, named_graph=self.named_graph
        ):
            raise HandoverPreconditionError(
                f"counterpart {counterpart_iri} lacks the handover ability complementary "
                f"to {mode_ability_iri}"
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
        Operation. An instance with a notification handler (`on_notification`) takes its
        notifications through the intake, not the queue, so registering both raises
        `RuntimeError`.
        """

        if self.mode != Mode.RESOURCE:
            raise RuntimeError(
                f"register_callback() is only valid in resource mode; current mode is {self.mode!r}"
            )
        if self._notification_handler is not None:
            raise RuntimeError(
                "register_callback() and on_notification() exclude each other: "
                "this instance already takes its notifications through the intake"
            )
        self._callback = callback
        self._callback_scope = scope

    def on_notification(
        self, handler: Callable[[Any], Any], class_scope: Any = None
    ) -> None:
        """Take this instance's notifications through the intake, with ``handler`` as fallback.

        Usage::

            def on_transport_ended(transport):
                ...  # the Operation object; write through mw.commit()

            mw.on_notification(on_transport_ended, class_scope=transport_scope)

        A notification arrives at this resource's event trigger: from a peer's `notify`, from a
        peer's `issue_operation()`, which notifies the resource it dispatched an Operation to, or from
        the middleware that claimed or ended an Operation that this resource issued.
        The trigger reads the Operation once and answers at once. The Operation then enters
        this instance's FIFO, and one worker runs one Operation at a time, in arrival order:

        - An Operation whose Capability is one of this instance's own (matched in memory, by
          IRI) runs the exposed workflow that realizes it (:meth:`workflow`), with the
          Operation object as its argument.
        - Any other Operation goes to `handler(operation)`, the fallback, such as the up
          direction: "this Operation ended". So does an Operation that this instance issued to
          a Capability of its own, once it is no longer `queued`: that notification is the
          feedback of its own claim or end, and the workflow does not run it again.

        The Operation object is a kapps_ogm ``Node``, read and materialized under the scope:
        the Capability that the Operation implements and the Workflow that realizes it, united
        with `class_scope`, this subproject's own ClassScope of the Operation. An Operation that
        waited behind another one is read again before it runs, so it is never stale. The
        middleware writes no status and no provenance: the workflow or the handler commits its
        own transitions with `mw.commit()`.

        A function may be a plain function or a coroutine function. A plain one runs in a
        worker thread, so it may block (graph I/O, a physical move) without stalling the
        server. A coroutine function runs on the event loop and must not block. An exception is
        logged, nothing is written for it, and the next Operation runs.

        At startup, this instance's own `queued` Operations go into the FIFO again, and its own
        orphaned `running` ones become `failed`, as crash recovery.

        An instance takes its notifications either here or through the dispatch lifecycle
        (`register_callback`, `claim_next`), not both: registering one when the other is
        registered raises `RuntimeError`. One handler per instance: a second call replaces the
        first. Register it before the instance starts.
        """

        if self.mode != Mode.RESOURCE:
            raise RuntimeError(
                "on_notification() is only valid in resource mode; "
                f"current mode is {self.mode!r}"
            )
        if self._callback is not None:
            raise RuntimeError(
                "on_notification() and register_callback() exclude each other: "
                "this instance already has a callback for the dispatch lifecycle"
            )
        self._notification_handler = handler
        self._notification_scope = class_scope

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
        """Take up again, at startup, the Operations addressed to this instance.

        Both kinds are found by ontology traversal (Operation -> Capability <- resource), whose
        links persist across a restart. So this is a one-shot startup query, rather than
        steady-state polling.

        Own orphaned ``running`` Operations (this resource crashed mid-execution) transition to
        ``failed`` first, and never auto-rerun, because a half-done physical action must not
        silently replay. The issuer of each is notified after that commit. That is crash
        recovery, on every instance. Only then are the own
        ``queued`` Operations taken up, so none that starts now can be taken for an orphan:

        - **With a notification handler**, they go into the intake's FIFO, as notifications
          do, and each is read before it runs.
        - **Without one**, they are re-enqueued into the in-memory queue for the dispatch
          lifecycle (``register_callback``, ``claim_next``).
        """

        own = self._own_iris("the queue's reconstruction is only valid in resource mode")
        resource_iri = own.resource
        orphaned = await anyio.to_thread.run_sync(
            functools.partial(
                find_resource_operations,
                self.ogm,
                resource_iri,
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
            self._notify_issuers({op_iri: OperationStatus.FAILED})

        queued = await anyio.to_thread.run_sync(
            functools.partial(
                find_resource_operations,
                self.ogm,
                resource_iri,
                [OperationStatus.QUEUED],
                self.named_graph,
            )
        )
        if self._notification_handler is not None:
            for op_iri in queued:
                self._take_in(op_iri)
            return
        if self._callback is None:
            logger.warning(
                "%s has neither a notification handler (on_notification) nor a callback "
                "(register_callback): the Operations it is notified of wait in its queue until "
                "claim_next() takes them",
                resource_iri,
            )
        for op_iri in queued:
            if op_iri not in self._operation_queue:
                self._operation_queue.append(op_iri)

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
        resource_iri = self._own_iris("connector wiring is only valid in resource mode").resource
        self._wiring = plan_wiring(
            ogm=self.ogm,
            resource_iri=resource_iri,
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
                model_id=str(resource_iri),
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
                resource_iri,
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
