"""What an instance has seen of its connections and of its graph binding, as typed records.

Nothing in the framework or in the connectors records whether a connector is connected, when it
last moved a value, or when the store last answered. This module records it, in memory, at two
choke points, and adds no traffic of its own:

- **A connection.** The instance hands the framework an observing wrapper in place of each
  connector it wires (``_observed_connector``). One call site covers REST, MQTT and the knowledge
  graph connector, and no connector class changes. The MQTT leg is the framework's own connector,
  which the dependency policy keeps to bugfixes.
- **The graph binding.** Every read and write of the instance reaches the store through its OGM's
  ``db``: the OGM's own methods do, and so do the ~25 direct queries of this package. The instance
  puts an observing store in that place (``_observe_store``), once per OGM.

The records are the public read surface: ``SemanticMiddleware.connections`` and
``SemanticMiddleware.graph_binding`` give them, and a dashboard box and a later exporter read
them the same way. A record is frozen and is made when it is read, with this instance's resource
and service IRI attached then. A field that may carry connection metadata, such as an error
message that names a host, a port, a topic or a URL, is marked as such in the record's type, and
``is_connection_metadata`` reads the mark. A surface that must not show connection metadata leaves
out what that one predicate names, and keeps no list of its own.

The records live as long as the process. A restart empties them.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import inspect
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterator, Optional

from kapps_triplestore_interface import IRI
from kapps_triplestore_interface import exceptions as _store_errors
from kapps_triplestore_interface.exceptions import ShaclValidationError

logger = logging.getLogger(__name__)

#: The key of a record field's metadata that marks the field as connection metadata.
CONNECTION_METADATA = "connection_metadata"


def is_connection_metadata(record: Any, name: str) -> bool:
    """Whether the field ``name`` of a record type, or of a record, carries connection metadata.

    The one predicate that a surface reads to leave connection metadata out, such as the dashboard
    under ``dev=False``. A name that is no field of the record, and anything that is no record,
    is not connection metadata.
    """
    if not dataclasses.is_dataclass(record):
        return False
    for each in dataclasses.fields(record):
        if each.name == name:
            return bool(each.metadata.get(CONNECTION_METADATA, False))
    return False


@dataclass(frozen=True)
class CallError:
    """The last error of a call: when it happened, the type of the error, and its message.

    The message is connection metadata: the error of an HTTP client, a broker client or the store
    names the address that it could not reach.
    """

    time: datetime
    type: str
    message: str = field(metadata={CONNECTION_METADATA: True})


@dataclass(frozen=True)
class ConnectionRecord:
    """What the instance has seen of one connector that it wired, since it started.

    ``resource`` and ``service`` are this instance's own IRIs. ``holder`` is the individual that
    holds the parameter, and ``field`` the parameter's field in the datamodel. ``connector`` names
    the connector's class, and ``direction`` the way it moves data.

    ``connected`` is ``None`` until the framework asks the connector to connect, ``True`` once its
    ``connect()`` has returned, and ``False`` after its ``disconnect()`` or a ``connect()`` that
    raised. It is what the connector's own ``connect()`` reports: a connector whose ``connect()``
    does nothing, such as the knowledge graph connector, is "connected" as soon as it returns.

    ``last_provide`` is the last ``provide()``, ``last_consume`` the last ``consume()`` that
    returned, and ``last_receive`` the last value that ``receive()`` yielded. All times are aware,
    in UTC.
    """

    resource: Optional[IRI]
    service: Optional[IRI]
    connector_id: str
    holder: Any
    field: str
    connector: str
    direction: str
    connected: Optional[bool]
    last_provide: Optional[datetime]
    last_consume: Optional[datetime]
    last_receive: Optional[datetime]
    last_error: Optional[CallError]

    @property
    def last_sync(self) -> Optional[datetime]:
        """When a value last moved: the newer of the last receive and the last consume.

        Not ``provide``: a pushing connector, such as the MQTT one, answers ``provide()`` from a
        value it holds and reads nothing, so a fresh ``provide`` says nothing of the value's age.
        """
        return _newer(self.last_receive, self.last_consume)


@dataclass(frozen=True)
class GraphBindingRecord:
    """What the instance has seen of its graph binding, the store behind its OGM, since it started.

    ``resource`` and ``service`` are this instance's own IRIs. ``dataset`` is the store's dataset,
    and ``named_graph`` the graph that the instance writes to, ``None`` for the default graph.

    ``last_read`` and ``last_write`` are the last read and write that the store answered. A write
    that the store refused, by a shape or by a false precondition, is an answer: the binding works,
    and the refusal shows in the instance's refused writes. ``last_error`` is the last call that
    did not reach the store, or that the store failed itself: no answer, refused credentials or
    access, or a server error.

    Several instances in one process may share one OGM, and so one store. Each sees the same
    reads, writes and error, which are the store's, under its own IRIs.
    """

    resource: Optional[IRI]
    service: Optional[IRI]
    dataset: Optional[str]
    named_graph: Optional[IRI]
    last_read: Optional[datetime]
    last_write: Optional[datetime]
    last_error: Optional[CallError]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _newer(*moments: Optional[datetime]) -> Optional[datetime]:
    """The newest of ``moments`` that is known, or ``None``."""
    known = [m for m in moments if m is not None]
    return max(known) if known else None


def _call_error(error: BaseException) -> CallError:
    """The record of ``error``. It never raises, because it runs in the path of every sync."""
    try:
        message = str(error)
    except Exception:  # noqa: BLE001 - an error whose text cannot be made is still recorded
        message = ""
    return CallError(time=_now(), type=type(error).__name__, message=message)


# --- connections -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Seen:
    """One connector's observation, copied out under its lock."""

    connected: Optional[bool] = None
    last_provide: Optional[datetime] = None
    last_consume: Optional[datetime] = None
    last_receive: Optional[datetime] = None
    last_error: Optional[CallError] = None


class _ConnectorObservation:
    """What one wrapper has seen. The framework calls a connector from the event loop and the
    dashboard reads from a worker thread, so both take one lock."""

    def __init__(self) -> None:
        self._seen = _Seen()
        self._lock = threading.Lock()

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._seen = dataclasses.replace(self._seen, **changes)

    def stamp(self, leg: str) -> None:
        """Record that ``leg``, ``provide``, ``consume`` or ``receive``, moved a value now."""
        self._set(**{f"last_{leg}": _now()})

    def connected(self, state: bool) -> None:
        self._set(connected=state)

    def failed(self, error: BaseException, *, connected: Optional[bool] = None) -> None:
        changes: Dict[str, Any] = {"last_error": _call_error(error)}
        if connected is not None:
            changes["connected"] = connected
        self._set(**changes)

    def seen(self) -> _Seen:
        with self._lock:
            return self._seen


#: What is no error of a connection: the end of a task or of a generator.
_NOT_AN_ERROR = (asyncio.CancelledError, GeneratorExit)


@contextlib.contextmanager
def _failures_to(observation: _ConnectorObservation, **on_failure: Any) -> Iterator[None]:
    """Record an error of the block in ``observation`` and raise it unchanged.

    The end of a task or of a generator is no error. ``on_failure`` is set with the error, such as
    ``connected=False`` for a ``connect()`` that failed.
    """
    try:
        yield
    except _NOT_AN_ERROR:
        raise
    except Exception as error:
        observation.failed(error, **on_failure)
        raise


class _ObservedConnector:
    """The connector that the framework gets in place of a wired one.

    It defines ``connect`` and ``disconnect`` itself, and its subclasses define each of
    ``provide``, ``consume`` and ``receive`` that the wrapped connector has, and no other. The
    framework decides what to run by ``isinstance`` against its ``@runtime_checkable`` protocols
    ``Provider``, ``Consumer`` and ``Receiver``, which look for the methods on the class: a
    wrapper that only forwarded attributes would match none of them, and one that defined all
    five would turn a connector without ``receive`` into a receiver, whose receive loop then dies.
    Every other attribute is read from the wrapped connector.

    An error is recorded and raised unchanged; the end of a task is no error. Recording never
    raises, because a defect here would stop the sync and not only the page.
    """

    def __init__(self, inner: Any, observation: _ConnectorObservation) -> None:
        self._inner = inner
        self._observation = observation

    def __getattr__(self, name: str) -> Any:
        # Called only for a name the wrapper has not got itself.
        return getattr(self.__dict__["_inner"], name)

    def __repr__(self) -> str:
        return f"<observed {self._inner!r}>"

    async def connect(self) -> None:
        with _failures_to(self._observation, connected=False):
            await self._inner.connect()
        self._observation.connected(True)

    async def disconnect(self) -> None:
        with _failures_to(self._observation):
            await self._inner.disconnect()
        self._observation.connected(False)


async def _provide(self: _ObservedConnector) -> Any:
    with _failures_to(self._observation):
        value = await self._inner.provide()
    self._observation.stamp("provide")
    return value


async def _consume(self: _ObservedConnector, body: Any) -> None:
    with _failures_to(self._observation):
        await self._inner.consume(body)
    self._observation.stamp("consume")


async def _receive(self: _ObservedConnector) -> Any:
    received = self._inner.receive()
    try:
        with _failures_to(self._observation):
            async for item in received:
                self._observation.stamp("receive")
                yield item
    finally:
        close = getattr(received, "aclose", None)
        if close is not None:
            await close()


#: The legs a connector may have, each with the wrapper's method for it.
_LEGS: Dict[str, Callable[..., Any]] = {
    "provide": _provide,
    "consume": _consume,
    "receive": _receive,
}


@functools.lru_cache(maxsize=None)
def _wrapper_class(legs: FrozenSet[str]) -> type:
    """The wrapper class with exactly ``legs``: one class per shape, made once."""
    name = "ObservedConnector" + "".join(sorted(leg.title() for leg in legs))
    return type(name, (_ObservedConnector,), {leg: _LEGS[leg] for leg in legs})


def _observed_connector(inner: Any, observation: _ConnectorObservation) -> Any:
    """``inner``, wrapped so that ``observation`` sees its calls, with the same protocols.

    A connector without ``connect`` and ``disconnect`` is no connector the framework can run, and
    is handed on unwrapped: observing it must never be the reason that wiring fails.
    """
    if not all(callable(getattr(inner, name, None)) for name in ("connect", "disconnect")):
        logger.debug("Not observing %r: it has no connect() and disconnect()", inner)
        return inner
    legs = frozenset(leg for leg in _LEGS if callable(getattr(inner, leg, None)))
    return _wrapper_class(legs)(inner, observation)


def _connection_record(
    *,
    resource: Optional[IRI],
    service: Optional[IRI],
    connector_id: str,
    binding: Any,
    registration: Any,
    observation: _ConnectorObservation,
) -> ConnectionRecord:
    seen = observation.seen()
    direction = registration.sync_direction
    return ConnectionRecord(
        resource=resource,
        service=service,
        connector_id=connector_id,
        holder=binding.resource_iri,
        field=binding.field_id,
        connector=type(registration.connector).__name__,
        direction=getattr(direction, "name", str(direction)),
        connected=seen.connected,
        last_provide=seen.last_provide,
        last_consume=seen.last_consume,
        last_receive=seen.last_receive,
        last_error=seen.last_error,
    )


# --- the graph binding -------------------------------------------------------------------------

#: The store's methods that read, by the triple store interface's protocol. `query` is a read
#: unless its `update` is true.
_READS = frozenset(
    {
        "query",
        "triple_exists",
        "triples_get",
        "iri_exists",
        "new_iri",
        "is_subclass",
        "owl_get_classes_of_individual",
        "graph_get",
        "prefixes_get",
    }
)

#: The store's methods that write. A method in neither set, such as `close`, is not recorded.
_WRITES = frozenset(
    {
        "triple_add",
        "triple_delete",
        "triples_add",
        "triples_delete",
        "triples_update",
        "graph_add",
        "graph_clear",
        "prefix_set",
        "prefix_delete",
        "shapes_set",
    }
)


def _store_error_types(*names: str) -> tuple:
    """The interface's error types of ``names`` that this version of it has.

    The released interface does not yet have every type that its development version has. A name
    it lacks is left out, so this module imports against both, and an error of the older store,
    which raises its HTTP client's errors, is caught as such below.
    """
    return tuple(t for t in (getattr(_store_errors, n, None) for n in names) if t is not None)


#: An answer of the store with an error status, which has a `status`.
# vendor-check: off
_RESPONSE_ERRORS = _store_error_types("TripleStoreResponseError") or _store_error_types(
    "GraphDbException"  # its older name, in the released interface
)
# vendor-check: on
#: A call that got no answer.
_UNAVAILABLE_ERRORS = _store_error_types("TripleStoreUnavailableError")


def _fails_the_binding(error: BaseException) -> bool:
    """Whether ``error`` says the binding failed: no answer, or a failure of the store itself.

    A refusal by a shape, a false precondition, and a malformed query are answers: the store was
    reached and did its work. They are not the binding's error.
    """
    if isinstance(error, ShaclValidationError):
        return False
    if _RESPONSE_ERRORS and isinstance(error, _RESPONSE_ERRORS):
        status = getattr(error, "status", None)
        return status is None or status in (401, 403) or status >= 500
    if _UNAVAILABLE_ERRORS and isinstance(error, _UNAVAILABLE_ERRORS):
        return True
    # The HTTP client's own errors, when a store lets one through. `requests` is the store's
    # client, reached through the interface, and not a dependency of this package.
    try:
        from requests.exceptions import RequestException
    except ImportError:  # pragma: no cover - the interface depends on requests
        return isinstance(error, OSError)
    return isinstance(error, (RequestException, OSError))


class _StoreObservation:
    """What the observing store has seen. Store calls run in worker threads, so a lock."""

    def __init__(self) -> None:
        self.last_read: Optional[datetime] = None
        self.last_write: Optional[datetime] = None
        self.last_error: Optional[CallError] = None
        self._lock = threading.Lock()

    def stamp(self, write: bool) -> None:
        now = _now()
        with self._lock:
            if write:
                self.last_write = now
            else:
                self.last_read = now

    def failed(self, error: BaseException) -> None:
        recorded = _call_error(error)
        with self._lock:
            self.last_error = recorded

    def seen(self) -> tuple:
        with self._lock:
            return self.last_read, self.last_write, self.last_error


def _is_update(args: tuple, kwargs: Dict[str, Any]) -> bool:
    """Whether a call of ``query(query, update=False, ...)`` is an update."""
    if "update" in kwargs:
        return bool(kwargs["update"])
    return len(args) > 1 and bool(args[1])


class _ObservedStore:
    """The store an OGM gets in place of its own, so that each read and write is seen.

    Every attribute is the store's own: a method the tables name is wrapped, anything else, such
    as ``dataset``, is passed through, and an attribute set or deleted here is set or deleted on
    the store, so that a ``patch.object`` on this object is undone on the store. A call that the
    store makes on itself, such as a ``triples_update`` that runs ``query``, is seen once, as the
    outer call: the store's methods are bound to the store, not to this object.
    """

    __slots__ = ("_inner", "_observation")

    def __init__(self, inner: Any, observation: _StoreObservation) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_observation", observation)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._inner, name)
        if not callable(value) or (name not in _READS and name not in _WRITES):
            return value
        observation = self._observation

        @functools.wraps(value)
        def observed(*args: Any, **kwargs: Any) -> Any:
            try:
                result = value(*args, **kwargs)
            except Exception as error:
                if _fails_the_binding(error):
                    observation.failed(error)
                raise
            write = name in _WRITES or (name == "query" and _is_update(args, kwargs))
            observation.stamp(write)
            return result

        return observed

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._inner, name)

    def __repr__(self) -> str:
        return f"<observed {self._inner!r}>"


def _store_of(ogm: Any) -> Any:
    """The ``db`` that ``ogm`` holds, or ``None``, read without running any code of the OGM.

    The OGM keeps its store as a plain attribute. A static read sees it, and sees nothing of an
    object that makes its attributes up on access, so constructing an instance or reading a record
    never makes an OGM do anything.
    """
    store = inspect.getattr_static(ogm, "db", None)
    return None if isinstance(store, (property, staticmethod, classmethod)) else store


def _observe_store(ogm: Any) -> Optional[_StoreObservation]:
    """Observe the store of ``ogm``, once, and give its observation.

    The OGM's ``db`` is replaced by the observing store, in place, so that the OGM's own calls are
    seen too. An OGM that another instance observes already keeps its store, and its observation
    is shared: the binding is the store's. An OGM without a ``db``, or one that refuses the
    replacement, is left alone, and ``None`` says that nothing is observed. A later
    ``ogm.db = other`` ends the observation.
    """
    store = _store_of(ogm)
    if store is None:
        return None
    if isinstance(store, _ObservedStore):
        return object.__getattribute__(store, "_observation")
    observation = _StoreObservation()
    try:
        ogm.db = _ObservedStore(store, observation)
    except (AttributeError, TypeError):
        logger.debug("Not observing the store of %r: its db cannot be replaced", ogm)
        return None
    return observation


def _graph_binding_record(
    *,
    resource: Optional[IRI],
    service: Optional[IRI],
    ogm: Any,
    named_graph: Optional[IRI],
    observation: Optional[_StoreObservation],
) -> GraphBindingRecord:
    last_read, last_write, last_error = (
        observation.seen() if observation is not None else (None, None, None)
    )
    store = _store_of(ogm)
    try:
        dataset = getattr(store, "dataset", None) if store is not None else None
    except Exception:  # noqa: BLE001 - a record is read from memory and never fails on a store
        dataset = None
    return GraphBindingRecord(
        resource=resource,
        service=service,
        dataset=dataset if isinstance(dataset, str) else None,
        named_graph=named_graph,
        last_read=last_read,
        last_write=last_write,
        last_error=last_error,
    )
