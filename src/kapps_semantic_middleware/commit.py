"""The general write: a state transition that the store admits before the work runs, and the
creation of a new instance, built on it.

`SemanticMiddleware.commit()` and `SemanticMiddleware.create()` return the context managers
defined here. They know no vocabulary of their own: the transitions they commit are kapps_ogm
`Create` and `Update` changes, so they serve any datamodel and any state transition. Rules of a
domain enter a creation through a `PredefinedScope`, which a module beside this one gives.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import uuid
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, Callable, Iterator, Mapping, NamedTuple, Optional, Sequence, Union

import anyio
from kapps_ogm import Create, Update
from kapps_ogm.utils.class_scope import ClassScope
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import InvalidIRIError, ShaclValidationError
from rdflib import URIRef

logger = logging.getLogger(__name__)

Change = Union[Create, Update]

#: The exit transition: its changes, or a function that returns them at the clean exit.
ExitTransition = Union[Sequence[Change], Callable[[], Sequence[Change]]]

#: The error transition: its changes, or a function of the exception that returns them.
ErrorTransition = Union[Sequence[Change], Callable[[BaseException], Sequence[Change]]]

#: The pending write of the current body: the innermost commit or creation that is open there
#: and whose write is still to come. A creation opened in that body joins it when it writes
#: through the same OGM into the same named graph. None where no write is to come.
#:
#: It is set and reset on the caller's side of each context manager: the asynchronous form runs
#: its entry in a worker thread, and a value set there would not reach the body.
_pending_write: ContextVar[Optional[Union["_Commit", "_Creation"]]] = ContextVar(
    "kapps_semantic_middleware.pending_write", default=None
)


class _Commit:
    """What ``SemanticMiddleware.commit()`` returns, and what its body is given.

    The body reads ``nodes`` (or ``node``) and changes the datamodel with ``update``. The
    protocol itself is described on ``SemanticMiddleware.commit()``. One object serves ``with``
    and ``async with``: the steps are the same functions, and the asynchronous form runs each of
    them in a worker thread.

    Only ``update`` changes the graph. A change made to a fetched instance (``node``, its
    ``.instance``) is not committed. Nothing set on the transition itself reaches the graph
    either, so an assignment to an attribute it does not have raises ``AttributeError``, instead
    of being lost without a word.
    """

    __slots__ = (
        "_ogm",
        "_instances",
        "_class_scope",
        "_entry",
        "_exit_changes",
        "_error_changes",
        "_named_graph",
        "_staged",
        "_token",
        "_on_admitted",
        "_on_refused",
        "nodes",
    )

    def __init__(
        self,
        ogm: Any,
        *,
        instances: Sequence[str],
        class_scope: Any,
        entry: Sequence[Change],
        exit: ExitTransition,
        error: ErrorTransition,
        named_graph: Optional[IRI],
        on_admitted: Optional[Callable[[Sequence[Change]], None]] = None,
        on_refused: Optional[Callable[..., None]] = None,
    ) -> None:
        self._ogm = ogm
        self._instances = [IRI(iri) for iri in instances]
        self._class_scope = class_scope
        self._entry = list(entry)
        self._exit_changes: Callable[[], Sequence[Change]] = (
            exit if callable(exit) else functools.partial(list, exit)
        )
        self._error_changes: Callable[[BaseException], Sequence[Change]] = (
            error if callable(error) else lambda exc: list(error)
        )
        self._named_graph = named_graph
        self._staged: list[Change] = []
        #: Told of each write that the store admitted, with its changes, after the write. It
        #: cannot change the write's outcome: its error is logged, not raised.
        self._on_admitted = on_admitted
        #: Told of each refusal by the store's shapes, of the entry, of the clean exit or (with
        #: ``error_transition=True``) of the error transition, with the store's error, before
        #: the error is raised. It cannot change the error: its own error is logged, not raised.
        self._on_refused = on_refused
        #: The instances this commit is about, by IRI, as the OGM fetched them after the entry.
        self.nodes: dict[IRI, Any] = {}

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in _Commit.__slots__:
            raise AttributeError(
                f"a transition has no attribute {name!r}, and nothing set on it reaches the "
                "graph. Change an instance with transition.update(instance_iri, "
                "{property: [values]})."
            )
        object.__setattr__(self, name, value)

    @property
    def node(self) -> Any:
        """The one instance this commit is about, as the OGM fetched it after the entry.

        Raises:
            ValueError: If the commit is about no instance or about several; read ``nodes``.
        """
        if len(self.nodes) != 1:
            raise ValueError(f"this commit is about {len(self.nodes)} instances; read nodes")
        return next(iter(self.nodes.values()))

    def update(self, instance_iri: str, data: dict) -> None:
        """Change an existing instance at the clean exit, in the same commit as the exit transition.

        ``data`` is ``{property IRI: [values]}``, as in a kapps_ogm ``Update``: each property it
        names replaces the stored values, and ``[]`` removes the property. The instance need not
        be one the commit was given. A later call for the same property of the same instance
        replaces the earlier value. Nothing is written before the clean exit, and an error drops
        every change.
        """
        self._stage(Update(instance_iri=IRI(instance_iri), data=dict(data)))

    def _stage(self, change: Change) -> None:
        """Register ``change`` with this open commit, for its clean exit.

        This is the one way in for the body's changes: ``update`` uses it, and so can code that
        runs inside the body and adds a ``Create`` that must be admitted together with the rest.
        At the clean exit every staged change to one instance merges with the others and with
        the exit transition's change to it. An error drops them all. A change that could never
        be written correctly raises here, where it is made.
        """
        _check(change)
        self._staged.append(change)

    # ------------------------------------------------------------------ #
    # The steps, shared by the synchronous and the asynchronous form.
    # ------------------------------------------------------------------ #

    def _write(self, changes: Sequence[Change], *, error_transition: bool = False) -> None:
        """One ``OGM.commit`` of ``changes``, each checked first. Nothing is sent when there is
        nothing to write, or when a change fails its check.

        After the store admitted the write, the function given as ``on_admitted`` is told of it.
        The write has happened, so an error of that function is logged and not raised. When the
        store's shapes refuse a write, the function given as ``on_refused`` is told of the
        store's error, and the error is raised unchanged. For the write of the error transition
        (``error_transition``), it is told so by the keyword ``error_transition``, and the error is
        not the caller's: it is only noted on the error that is.
        """
        for change in changes:
            _check(change)
        if not changes:
            return
        sent = list(changes)
        try:
            self._ogm.commit(changes=sent, named_graph=self._named_graph)
        except ShaclValidationError as refusal:
            if self._on_refused is not None:
                try:
                    self._on_refused(refusal, error_transition=error_transition)
                except Exception:
                    logger.warning("A function told of a refused write raised", exc_info=True)
            raise
        if self._on_admitted is None:
            return
        try:
            self._on_admitted(sent)
        except Exception:
            logger.warning("A function told of an admitted write raised", exc_info=True)

    def _fetch(self, iri: IRI) -> Any:
        """One instance for the body: materialized under the class scope, or else a reference."""
        if self._class_scope is not None:
            return self._ogm.fetch(instance_iri=iri, class_scope=self._class_scope, materialize=True)
        return self._ogm.fetch(instance_iri=iri, as_reference=True)

    def _enter(self) -> "_Commit":
        """Commit the entry transition, then fetch the instances the body is given.

        A refused entry raises before anything else happens. A fetch that fails after the entry
        was admitted is an error like one in the body: the error transition records it.
        """
        self._write(self._entry)
        try:
            self.nodes = {iri: self._fetch(iri) for iri in self._instances}
        except Exception as exc:
            self._error(exc)
            raise
        return self

    def _exit(self, exc: Optional[BaseException]) -> None:
        """Finish after the body: the clean exit when ``exc`` is None, else the error path."""
        if exc is None:
            try:
                self._clean_exit()
            except Exception as refused:
                self._error(refused)
                raise
            return
        self._error(exc)

    def _clean_exit(self) -> None:
        """Commit the staged changes and the exit transition as one ``OGM.commit``.

        When a staged change sets a property that the exit transition also sets on the same
        instance, the exit transition is skipped as a whole, and the staged value is committed.
        """
        exit_changes = list(self._exit_changes())
        if _overlap(exit_changes, self._staged):
            exit_changes = []
        self._write(_one_change_per_instance(exit_changes + self._staged))

    def _error(self, exc: BaseException) -> None:
        """Drop the staged changes and commit the error transition for ``exc``.

        ``exc`` is what the caller sees, so a failure of the error transition is logged and
        noted on ``exc`` rather than raised in its place.
        """
        self._staged = []
        try:
            self._write(self._error_changes(exc), error_transition=True)
        except Exception as failure:
            logger.error("The error transition was not committed: %r", failure, exc_info=failure)
            exc.add_note(f"The error transition was not committed: {failure!r}")

    def __enter__(self) -> "_Commit":
        self._enter()
        self._token = _pending_write.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        _pending_write.reset(self._token)
        # An interrupt or an exit (KeyboardInterrupt, SystemExit, GeneratorExit) writes nothing:
        # the entry transition stays, as after a crash.
        if exc is None or isinstance(exc, Exception):
            self._exit(exc)

    async def __aenter__(self) -> "_Commit":
        await anyio.to_thread.run_sync(self._enter)
        self._token = _pending_write.set(self)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        _pending_write.reset(self._token)
        # A cancellation of the body is an error of the work, like an exception: a deadline
        # that ran out or a task that was cancelled leaves the error transition. The exit is
        # shielded, because in a cancelled scope the commit would itself be cancelled before it
        # starts. The cancellation is raised again afterwards. An interrupt or an exit writes
        # nothing, as in the synchronous form.
        if exc is None or isinstance(exc, (Exception, anyio.get_cancelled_exc_class())):
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(functools.partial(self._exit, exc))


class ScopeChanges(NamedTuple):
    """What a predefined scope adds to the creation of an instance, for the same commit."""

    #: The properties that the scope sets in the new instance's ``Create``, as
    #: ``{property IRI: [values]}``.
    data: dict
    #: Further changes, such as the creation of an instance that the new one points at.
    further: list[Change]


class PredefinedScope(ClassScope):
    """A ClassScope that brings the rules for creating an instance of its class.

    ``mw.create()`` takes it where it takes a ClassScope. It calls ``check()`` when it is
    entered, so wrong inputs raise before the body runs and before anything is written. At the
    clean exit it calls ``changes(ogm, instance_iri)``, which gives the :class:`ScopeChanges` of
    the new instance: the data that the scope adds to its ``Create``, and further changes for the
    same commit. ``changes`` may read the store through ``ogm``. A property that the scope adds
    may not be set by the body as well.

    A domain module gives a function that returns one, with the rules filled in. The generic
    core knows only this hook, and no term of the domain.
    """

    def __init__(
        self,
        scope: Optional[Mapping] = None,
        *,
        check: Optional[Callable[[], None]] = None,
        changes: Optional[Callable[[Any, IRI], ScopeChanges]] = None,
    ) -> None:
        super().__init__()
        for prop, nested in (scope or {}).items():
            self[prop] = nested
        self._check_inputs = check or (lambda: None)
        self._changes = changes or (lambda ogm, instance_iri: ScopeChanges({}, []))

    def check(self) -> None:
        """Raise if the inputs of the scope cannot make a correct instance."""
        self._check_inputs()

    def changes(self, ogm: Any, instance_iri: IRI) -> ScopeChanges:
        """What the scope adds to the creation of ``instance_iri``, for the same commit."""
        return ScopeChanges(*self._changes(ogm, instance_iri))


class _Creation:
    """What ``SemanticMiddleware.create()`` returns, and what its body is given.

    The body reads ``iri`` and sets the new instance's properties with ``update``. The protocol
    itself is described on ``SemanticMiddleware.create()``. One object serves ``with`` and
    ``async with``. Nothing set on the creation itself reaches the graph, so an assignment to an
    attribute it does not have raises ``AttributeError``, instead of being lost without a word.
    """

    __slots__ = (
        "_ogm",
        "_class_iri",
        "_scope",
        "_given_iri",
        "_named_graph",
        "_data",
        "_staged",
        "_parent",
        "_token",
        "_written",
        "_on_admitted",
        "_on_refused",
        "iri",
    )

    def __init__(
        self,
        ogm: Any,
        class_iri: str,
        scope: Mapping,
        *,
        instance_iri: Optional[str],
        named_graph: Optional[IRI],
        on_admitted: Optional[Callable[[Sequence[Change]], None]] = None,
        on_refused: Optional[Callable[..., None]] = None,
    ) -> None:
        self._ogm = ogm
        self._class_iri = IRI(class_iri)
        self._scope = scope if isinstance(scope, ClassScope) else _as_class_scope(scope or {})
        self._given_iri = instance_iri
        self._named_graph = named_graph
        self._data: dict = {}
        self._staged: list[Change] = []
        self._parent: Optional[Union[_Commit, _Creation]] = None
        self._written: tuple[Change, ...] = ()
        #: Told of the creation's own commit, when it commits alone, as ``_Commit`` tells it.
        self._on_admitted = on_admitted
        #: Told of a refusal of that commit by the store's shapes, as ``_Commit`` tells it. A
        #: creation that joins a pending write is refused with it, and that write tells its own.
        self._on_refused = on_refused

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in _Creation.__slots__:
            raise AttributeError(
                f"a creation has no attribute {name!r}, and nothing set on it reaches the graph. "
                "Set a property of the new instance with "
                f"creation.{_Creation.update.__name__}({{property: [values]}})."
            )
        object.__setattr__(self, name, value)

    @property
    def changes(self) -> tuple[Change, ...]:
        """The changes that this creation committed, or joined to the write of its outer body.

        Its own ``Create`` comes first, then the changes of its predefined scope, then those of
        the creations nested in it. Empty until the clean exit has committed or joined them, and
        after an error.
        """
        return self._written

    def update(self, data: Mapping) -> None:
        """Set properties of the new instance, for its ``Create`` at the clean exit.

        ``data`` is ``{property IRI: [values]}``, as in a kapps_ogm ``Create``. A later call for
        the same property replaces the earlier value. A value that is an IRI (an ``IRI`` or an
        rdflib ``URIRef``), such as the ``iri`` of a nested creation, or ``{"id": iri}``, is a
        reference to that instance. Any other value goes to the OGM as it is. Nothing is written
        before the clean exit, and an error drops it.

        Raises:
            ValueError: If the values of a property are not a list.
        """
        for prop, values in data.items():
            if not isinstance(values, list):
                raise ValueError(
                    f"the values of {prop} must be a list, as in {{property: [values]}}; "
                    f"got {values!r}"
                )
        references = {
            prop: [_as_reference(value) for value in values] for prop, values in data.items()
        }
        self._data = _merged_data(self._data, references)

    def _stage(self, change: Change) -> None:
        """Register a change of a creation nested in this one, for this one's clean exit."""
        _check(change)
        self._staged.append(change)

    def _prepare(self) -> None:
        """Check the scope's inputs and mint the IRI. Nothing is read or written."""
        if isinstance(self._scope, PredefinedScope):
            self._scope.check()
        self.iri = (
            IRI(self._given_iri) if self._given_iri is not None else _mint(self._class_iri)
        )
        self._data = {}
        self._staged = []
        self._written = ()

    def _changes(self, staged: Sequence[Change]) -> list[Change]:
        """The ``Create`` of the new instance, the scope's changes, and the nested ones."""
        added = ScopeChanges({}, [])
        if isinstance(self._scope, PredefinedScope):
            added = self._scope.changes(self._ogm, self.iri)
        set_by_both = {_property(p) for p in added.data} & {_property(p) for p in self._data}
        if set_by_both:
            named = ", ".join(sorted(map(str, set_by_both)))
            raise ValueError(
                f"the body of the creation of {self.iri} sets {named}, which its scope sets itself"
            )
        data = {**self._data, **added.data}
        chains: list[list[Any]] = self._scope.to_property_chains()
        chains += [[IRI(prop)] for prop in data]
        create = Create(
            class_iri=self._class_iri,
            instance_iri=self.iri,
            data=data,
            class_scope=ClassScope.from_property_chains(chains),
        )
        return [create, *added.further, *staged]

    def _become_pending(self) -> None:
        """Find the write to join, and become the pending write of the body. On the caller's side.

        The pending write is joined only when it goes through the same OGM into the same named
        graph: one of another middleware, such as a planner's beside a resource's in one process,
        would write this creation through the wrong store or graph.
        """
        pending = _pending_write.get()
        joins = (
            pending is not None
            and pending._ogm is self._ogm
            and pending._named_graph == self._named_graph
        )
        self._parent = pending if joins else None
        self._token = _pending_write.set(self)

    def _exit(self, exc: Optional[BaseException]) -> None:
        """Finish after the body: join the outer write or commit alone, or drop everything."""
        staged, self._staged = self._staged, []
        if exc is not None:
            return
        changes = self._changes(staged)
        if self._parent is not None:
            for change in changes:
                self._parent._stage(change)
        else:
            alone = _Commit(
                self._ogm,
                instances=(),
                class_scope=None,
                entry=(),
                exit=changes,
                error=(),
                named_graph=self._named_graph,
                on_admitted=self._on_admitted,
                on_refused=self._on_refused,
            )
            with alone:
                pass
        self._written = tuple(changes)

    def __enter__(self) -> "_Creation":
        self._prepare()
        self._become_pending()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        _pending_write.reset(self._token)
        self._exit(exc)

    async def __aenter__(self) -> "_Creation":
        await anyio.to_thread.run_sync(self._prepare)
        self._become_pending()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        _pending_write.reset(self._token)
        if exc is not None:
            self._exit(exc)
            return
        # Shielded for the reason the commit's exit is: a deadline that runs out now must not
        # stop the write of a body that finished.
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(functools.partial(self._exit, None))


@contextlib.contextmanager
def _nothing_to_join() -> Iterator[None]:
    """Mark a body in which a creation finds no pending write to join, so it commits on its own.

    Two bodies need it. One is a body whose commit has already happened, such as that of
    ``handover()``: its clean exit writes nothing more. The other is a creation that must commit
    on its own although a write of its caller is still to come, such as the Operation of
    ``issue_operation()``, whose receiver reads it after the notification. A creation opened in a
    ``mw.commit()`` or a ``mw.create()`` inside the marked body still joins that one.
    """
    token = _pending_write.set(None)
    try:
        yield
    finally:
        _pending_write.reset(token)


def _require_iri(value: Any, *, scope: Callable[..., Any], keyword: str) -> None:
    """The check of a required input of a predefined scope, for its ``check()`` at entry.

    ``scope`` is the function that makes the predefined scope, and ``keyword`` the name of the
    input; both name the call in the error, so a rename of either changes the message too.

    Raises:
        ValueError: If ``value`` is missing, empty, or no IRI.
    """
    call = f"{scope.__name__}(scope, {keyword}=...)"
    if value is None or not str(value).strip():
        raise ValueError(f"{call} requires its {keyword}, an IRI; got {value!r}")
    try:
        IRI(value)
    except InvalidIRIError as exc:
        raise ValueError(f"{call} requires its {keyword} as an IRI; got {value!r}") from exc


def _mint(class_iri: IRI) -> IRI:
    """A new IRI for an instance of ``class_iri``: ``<class>_<12 hex>``."""
    return IRI(f"{class_iri}_{uuid.uuid4().hex[:12]}")


def _as_class_scope(scope: Mapping) -> ClassScope:
    """``scope``, a mapping of property IRIs to nested scopes, as a ``ClassScope``."""
    result = ClassScope()
    for prop, nested in scope.items():
        result[prop] = nested
    return result


def _as_reference(value: Any) -> Any:
    """An IRI (an ``IRI`` or an rdflib ``URIRef``) as the OGM's reference ``{"id": iri}``.

    Any other value stays as it is: ``{"id": iri}`` is a reference already, and the OGM checks
    the rest against the property.
    """
    if isinstance(value, URIRef):
        return {"id": str(value)}
    return value


def _check(change: Change) -> None:
    """Refuse a change that kapps_ogm would write wrongly, without an error of its own.

    A ``Create`` that carries data needs a ``class_scope``: without one, on an OGM without a
    loader, kapps_ogm drops a data property that the class does not know, with only a warning.

    Raises:
        ValueError: If ``change`` is a ``Create`` with data and no ``class_scope``.
    """
    if isinstance(change, Create) and change.data and change.class_scope is None:
        name = change.instance_iri or f"a new {change.class_iri}"
        raise ValueError(
            f"the Create of {name} carries data but no class_scope, so properties that the "
            "class does not know would be dropped; give the Create a class_scope"
        )


def _one_change_per_instance(changes: Sequence[Change]) -> list[Change]:
    """``changes`` with all changes to one instance merged into one, in first-seen order.

    kapps_ogm refuses a call in which two changes write one instance. A later value of a
    property replaces an earlier one. Updates to an instance that a ``Create`` makes fold into
    that ``Create``. Two equal ``Create``s of one instance are one, as when two new Workpieces
    create their possessor's state. Two different ``Create``s of one instance stay apart, so
    kapps_ogm refuses them.
    """
    merged: list[Change] = []
    position: dict[IRI, int] = {}
    for change in changes:
        iri = None if change.instance_iri is None else IRI(change.instance_iri)
        index = position.get(iri) if iri is not None else None
        if index is None:
            if iri is not None:
                position[iri] = len(merged)
            merged.append(change)
            continue
        earlier = merged[index]
        if isinstance(earlier, Create) and isinstance(change, Create):
            if change != earlier:
                merged.append(change)
            continue
        data = _merged_data(earlier.data, change.data)
        if isinstance(change, Create):
            merged[index] = replace(change, data=data)
        else:
            merged[index] = replace(earlier, data=data)
    return merged


def _overlap(transition: Sequence[Change], staged: Sequence[Change]) -> bool:
    """Whether ``staged`` sets a property of an instance that ``transition`` also sets."""
    return not _properties_set(transition).isdisjoint(_properties_set(staged))


def _properties_set(changes: Sequence[Change]) -> set[tuple[IRI, Any]]:
    """Each (instance, property) that ``changes`` set, for the instances that they name."""
    return {
        (IRI(change.instance_iri), _property(key))
        for change in changes
        if change.instance_iri is not None
        for key in change.data
    }


def _merged_data(earlier: dict, later: dict) -> dict:
    """``earlier`` with each property of ``later`` replacing the same property, however spelled."""
    data = dict(earlier)
    spelled = {_property(key): key for key in data}
    for key, values in later.items():
        same = spelled.get(_property(key))
        if same is not None:
            del data[same]
        data[key] = values
        spelled[_property(key)] = key
    return data


def _property(key: Any) -> Any:
    """A data key in one spelling, so a prefixed and a full IRI of one property compare equal."""
    try:
        return IRI(key)
    except (InvalidIRIError, TypeError):  # a key that is no IRI is compared as it is
        return key
