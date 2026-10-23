"""Shared seeding helpers for knowledge-graph initialization.

These functions load the published/general ontology modules (Core, svc:,
mes:) into named graphs, set the service ontology's shapes, and clear the
default graph. A productive store already holds its ontologies and shapes;
these helpers seed a demonstration or test store, and the middleware never
calls them. Both the example scenarios and the multi-process factory demo use
this library code, so it lives here rather than in examples/.
"""

from __future__ import annotations

import logging
import os
from importlib import resources
from urllib.parse import urlsplit

from kapps_triplestore_interface import (
    IRI,
    TripleStore,
    TripleStoreNotSupportedError,
    TripleStoreResponseError,
)

from kapps_semantic_middleware import _deprecation
from kapps_semantic_middleware.errors import DeploymentValueError
from kapps_semantic_middleware.vocabulary import CORE_ONTOLOGY, MES_ONTOLOGY, SVC_ONTOLOGY

logger = logging.getLogger(__name__)

# Named-graph IRIs for seeding — same terms as vocabulary.py's *_ONTOLOGY constants,
# aliased here because "graph to load ontology X into" is this module's own concern.
CORE_GRAPH = CORE_ONTOLOGY
SERVICE_GRAPH = SVC_ONTOLOGY
MES_GRAPH = MES_ONTOLOGY


# A wipe is refused unless the target is disposable by construction, not by habit:
#
# - **The host is loopback, or a bare hostname with no dot.** A bare name is a container alias
#   (the service name in the compose file and in CI), reachable only inside the network that
#   defines it. A store that other people share is always reached by a dotted name. The rule needs
#   no allowlist, and it names no private host, so it ships.
# - **``TRIPLESTORE_ALLOW_DESTRUCTIVE=1`` is set**, which is a person saying so in as many words.
#
# The same predicate, name and semantics as ``kapps_ogm`` and ``kapps_triplestore_interface``
# carry in their ``tests/conftest.py``. If the interface one day exports it, the copies collapse
# into one import with no call-site change.
_ALLOW_DESTRUCTIVE_ENV = "TRIPLESTORE_ALLOW_DESTRUCTIVE"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_disposable_target(url: str | None) -> bool:
    """Whether wiping the store at ``url`` is safe by construction rather than by habit.

    A URL this cannot parse a host out of is **not** disposable. Every uncertain case must land
    on refusal: a wrong ``False`` costs a skipped run, a wrong ``True`` costs somebody else's data.
    """
    host = (urlsplit(url or "").hostname or "").lower()
    if not host:
        return False
    return host in _LOOPBACK_HOSTS or "." not in host


def _store_url(store: TripleStore) -> str | None:
    """The URL the store was built for, or ``None`` when the store does not say.

    The ``TripleStore`` contract declares no URL, so this reads the endpoint that the interface's
    own store keeps. A store without one cannot be told to be disposable, and so is refused.
    """
    return getattr(getattr(store, "_endpoint", None), "url", None)


def _refuse_unless_disposable(store: TripleStore, what: str) -> None:
    """Raise ``DeploymentValueError`` before ``what`` wipes a store that is not disposable."""
    url = _store_url(store)
    if is_disposable_target(url) or os.getenv(_ALLOW_DESTRUCTIVE_ENV) == "1":
        return
    raise DeploymentValueError(
        f"Refusing to {what} in dataset {store.dataset!r} on {url!r}.\n"
        f"That host is neither loopback nor a container alias, so nothing here can tell that "
        f"it is disposable.\n"
        f"Point TRIPLESTORE_URL at a local store or a service alias, or set "
        f"{_ALLOW_DESTRUCTIVE_ENV}=1 to state that the target is disposable."
    )


def _read_core_ontology() -> str:
    """Return the vendored cfc: Core ontology Turtle.

    A verbatim copy of the published Core (version 0.9.0,
    https://circularfactory.github.io/Core/latest/ontology.ttl). Core is external and
    superior: imported and specialized, never modified. It is vendored rather than fetched,
    so seeding stays reproducible offline and a scenario stays self-contained.
    """
    return (
        resources.files("kapps_semantic_middleware")
        .joinpath("ontology", "core.ttl")
        .read_text(encoding="utf-8")
    )


def _read_service_ontology() -> str:
    """Return the packaged svc: ontology Turtle."""
    return (
        resources.files("kapps_semantic_middleware")
        .joinpath("ontology", "service.ttl")
        .read_text(encoding="utf-8")
    )


def _read_mes_ontology() -> str:
    """Return the packaged mes: ontology Turtle (handover-ability vocabulary)."""
    return (
        resources.files("kapps_semantic_middleware")
        .joinpath("ontology", "mes.ttl")
        .read_text(encoding="utf-8")
    )


def read_service_shapes() -> str:
    """Return the packaged SHACL shapes of the svc: ontology, as Turtle (interim name).

    One rule today: every Operation has exactly one status, one of the four values of
    ``OperationStatus``. The file ships beside ``service.ttl``. It seeds demonstrations and
    tests and documents the rule; a productive store already holds its shapes, and the
    middleware never calls this.
    """
    return (
        resources.files("kapps_semantic_middleware")
        .joinpath("ontology", "service-shapes.ttl")
        .read_text(encoding="utf-8")
    )


def set_service_shapes(store: TripleStore) -> bool:
    """Set the service ontology's shapes as the shapes of the store's dataset (interim name).

    ``shapes_set`` replaces every shape of the dataset and validates the data already there,
    so a seed calls this after it cleared the dataset. On a dataset that does not validate
    SHACL, the store refuses with ``TripleStoreNotSupportedError``: this logs one line at
    WARNING and returns ``False``, and the seed goes on without the rule. Any other error
    passes on unchanged.

    This seeds a demonstration or test store: a productive store already holds its shapes,
    and the middleware never calls this.

    Returns:
        Whether the store took the shapes.
    """
    try:
        store.shapes_set(read_service_shapes(), "text/turtle")
    except TripleStoreNotSupportedError:
        logger.warning(
            "Dataset %r does not validate SHACL, so the operation-status rule (exactly one "
            "status per Operation) is not checked; the seed goes on without it.",
            store.dataset,
        )
        return False
    return True


def clear_dataset(store: TripleStore) -> None:
    """Clear the default graph of the store's dataset (an authorized test dataset).

    Always run this before seeding, so a scenario never accumulates residual
    triples from a previous run or a different scenario. It serves demonstrations and tests;
    the middleware never calls it.

    The seed must stop where the store refused the clear. Any error that
    ``graph_clear()`` raises passes on unchanged.

    Raises:
        DeploymentValueError: The store is not disposable (see ``is_disposable_target``) and
            ``TRIPLESTORE_ALLOW_DESTRUCTIVE=1`` is not set. Nothing was cleared.
        TripleStoreResponseError: ``graph_clear()`` returned ``False``. The
            store's contract raises instead, so only a store outside it gets here.
            The error names the dataset.
    """
    _refuse_unless_disposable(store, "clear the default graph")
    if not store.graph_clear():
        raise TripleStoreResponseError(
            f"The store did not clear the default graph of dataset "
            f"{store.dataset!r}: graph_clear() returned False. The seed stops "
            f"here, so it does not write over the old data."
        )


def _graph_is_populated(db, graph_iri: IRI) -> bool:
    """Whether a named graph already holds any statement."""
    return bool(
        db.query(f"ASK {{ GRAPH <{graph_iri}> {{ ?s ?p ?o }} }}").get("boolean", False)
    )


def load_shared_ontologies(db, *, reload: bool = False) -> None:
    """Load the published/general ontology modules, one named graph per ontology.

    Every scenario needs Core, svc:, and mes:, and they never change during
    a run. Each gets its own named graph. It is not re-imported into the
    default graph on every seed. The clear_dataset function clears only
    the default graph. So a seed wipes the scenario's own data, and leaves
    these standing. The store reasons across all graphs, so a domain class
    still resolves its cfc: superclass from here.

    This seeds a demonstration or test store: a productive store already holds
    these ontologies, and the middleware never calls this.

    The operation is idempotent. The function skips a present module unless you set reload.
    ``reload`` wipes the three graphs, so it refuses a store that is not disposable, as
    ``clear_dataset`` does.
    """
    if reload:
        _refuse_unless_disposable(db, "reload the shared ontology graphs")
    for graph, turtle in (
        (CORE_GRAPH, _read_core_ontology()),
        (SERVICE_GRAPH, _read_service_ontology()),
        (MES_GRAPH, _read_mes_ontology()),
    ):
        if reload:
            db.graph_clear(graph)
        elif _graph_is_populated(db, graph):
            continue
        db.graph_add(turtle, graph_iri=graph, content_type="application/x-turtle")


# vendor-check: off -- the deprecated alias, until it goes.


@_deprecation.renamed("clear_dataset")
def clear_repository(db: TripleStore) -> None:
    """The deprecated name of :func:`clear_dataset`."""
    clear_dataset(db)


# vendor-check: on
