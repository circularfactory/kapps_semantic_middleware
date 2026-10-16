"""Build a store for a dataset that the caller names, and ignore the environment's.

The triple store interface reads ``TRIPLESTORE_DATASET`` when a caller names no dataset, and
that remains correct for consumer code, which connects to whatever dataset the user configured.
It is wrong for this project's tests and demos, because both of them *wipe* the dataset they
connect to (``seeding.clear_dataset``). Inheriting that name from the ambient environment is how
a stray variable gets somebody else's data cleared -- the KIT server is shared, and the dataset
names on it include ``Production``.

So every construction site here passes its dataset explicitly, and this module is the only
sanctioned way to do it. A store built from ``TripleStoreEndpoint.from_env()`` without a dataset
anywhere in tests, ``demo/`` or ``examples/`` is a mistake, and a greppable one.

The store comes from the interface's factory, ``connect()``: the URL from ``TRIPLESTORE_URL``,
the identity from ``TRIPLESTORE_USERNAME`` and ``TRIPLESTORE_PASSWORD``, and the dataset from the
caller. Each variable has a deprecated fallback, which the interface reads with a warning.

This module guards the name of the dataset: "never touch a dataset the caller did not name". The
server is guarded where the data is destroyed, in ``seeding``: ``clear_dataset`` and
``load_shared_ontologies(reload=True)`` refuse a store whose host is neither loopback nor a bare
container alias, unless ``TRIPLESTORE_ALLOW_DESTRUCTIVE=1`` says otherwise. Reading is never
refused. What still does not exist is a disposable-dataset lifecycle, owned by the triple store
interface: the suite clears at the start of a run rather than the end, so it neither starts from a
guaranteed clean slate nor leaves no trace.
"""

from __future__ import annotations

import os
import warnings
from typing import TYPE_CHECKING, Any, cast

from kapps_triplestore_interface import (
    GDBTokenAuth,
    TripleStore,
    TripleStoreEndpoint,
    connect,
)

from kapps_semantic_middleware import _deprecation
from kapps_semantic_middleware.errors import DeploymentValueError

# The variables a store genuinely needs. TRIPLESTORE_DATASET is absent on purpose.
REQUIRED_TRIPLESTORE_ENV = (
    "TRIPLESTORE_URL",
    "TRIPLESTORE_USERNAME",
    "TRIPLESTORE_PASSWORD",
)

# The only dataset examples/docker/docker-compose.yml creates, so the only one a reader
# following the documented setup has. Both the factory demo and the example scenarios
# target it. connect() checks that the dataset exists, so a different literal here
# fails loudly on a fresh install rather than quietly.
DEMO_DATASET = "kapps-demo"


def _missing(names: tuple[str, ...]) -> list[str]:
    """The variables of `names` that are not set, neither themselves nor by their fallback."""
    return [name for name in names if not os.getenv(name) and not os.getenv(_FALLBACKS[name])]


def _require(names: tuple[str, ...]) -> None:
    missing = _missing(names)
    if missing:
        raise DeploymentValueError(
            f"Missing triple store environment variable(s): {', '.join(missing)}. "
            f"Set {' and '.join(REQUIRED_TRIPLESTORE_ENV)} to reach a triple store."
        )


def triplestore_env_present() -> bool:
    """Whether the environment carries the connection details a live store needs."""
    return not _missing(REQUIRED_TRIPLESTORE_ENV)


def endpoint_for(dataset: str) -> TripleStoreEndpoint:
    """The endpoint of ``dataset``, with the store's URL read from the environment.

    Args:
        dataset: The dataset to target. Named by the caller, never by the environment, so
            that a wipe cannot reach a dataset nobody asked for.

    Returns:
        The endpoint carrying ``dataset`` verbatim. It holds no secret.

    Raises:
        DeploymentValueError: If ``TRIPLESTORE_URL`` is missing, naming it (Repair). It is a
            ``ValueError``.
    """
    _require(("TRIPLESTORE_URL",))
    return TripleStoreEndpoint.from_env(dataset=dataset)


def triplestore_for(dataset: str) -> TripleStore:
    """A live store bound to ``dataset``, from the interface's factory, ``connect()``.

    The replacement for building a store from the environment's dataset everywhere in this
    project that seeds or clears. The identity logs in for a token, as a store may refuse
    Basic authentication. ``connect()`` logs in and probes the dataset now, so a dataset that
    does not exist raises here rather than failing silently later.

    Args:
        dataset: The dataset to target.

    Returns:
        A connected store whose ``dataset`` is the one passed in.

    Raises:
        DeploymentValueError: If a connection variable is missing, naming each one that is
            (Repair).
        TripleStoreInterfaceError: The error of ``connect()``, unchanged: the store cannot be
            reached, refuses the identity, or does not have the dataset.
    """
    _require(REQUIRED_TRIPLESTORE_ENV)
    return connect(endpoint_for(dataset), GDBTokenAuth.from_env())


# vendor-check: off -- the deprecated aliases and the deprecated variables, until they go.

# Each variable, and the deprecated variable that the interface reads when it is not set.
_FALLBACKS = {
    "TRIPLESTORE_URL": "GRAPHDB_URL",
    "TRIPLESTORE_USERNAME": "GRAPHDB_USERNAME",
    "TRIPLESTORE_PASSWORD": "GRAPHDB_PASSWORD",
}

# Each deprecated constant, and the constant that replaces it.
_RENAMED = {
    "REQUIRED_GRAPHDB_ENV": "REQUIRED_TRIPLESTORE_ENV",
    "DEMO_REPOSITORY": "DEMO_DATASET",
}

if TYPE_CHECKING:
    from kapps_triplestore_interface import GraphDB, GraphDBCredentials

    # A type checker reads each deprecated constant as its successor.
    REQUIRED_GRAPHDB_ENV = REQUIRED_TRIPLESTORE_ENV
    DEMO_REPOSITORY = DEMO_DATASET


def __getattr__(name: str) -> Any:
    # A deprecated constant is not an attribute of the module, so each use of it comes here and
    # warns: an import of it and a read of it as an attribute.
    return _deprecation.constant(__name__, _RENAMED, globals(), name)


def _value_or_fallback(name: str) -> str:
    """The value of `name`, or of its deprecated fallback, with the interface's warning."""
    value = os.getenv(name)
    if value:
        return value
    fallback = _FALLBACKS[name]
    # The caller's line: this function, credentials_for, its alias wrapper, the caller.
    warnings.warn(
        f"{fallback} is deprecated; set {name}, which replaces it.",
        DeprecationWarning,
        stacklevel=4,
    )
    return os.environ[fallback]


@_deprecation.renamed("triplestore_env_present")
def graphdb_env_present() -> bool:
    """The deprecated name of :func:`triplestore_env_present`."""
    return triplestore_env_present()


@_deprecation.renamed("endpoint_for")
def credentials_for(repository: str) -> GraphDBCredentials:
    """The deprecated name of :func:`endpoint_for`.

    It returns the interface's deprecated credentials, as before: the endpoint's URL and
    dataset, and the identity read from the environment.

    Raises:
        DeploymentValueError: If a connection variable is missing, naming each one that is
            (Repair).
    """
    _require(REQUIRED_TRIPLESTORE_ENV)
    endpoint = endpoint_for(repository)
    # The one warning of a use is this alias's own, so the interface's deprecated name is read
    # without its warning.
    with warnings.catch_warnings(action="ignore", category=DeprecationWarning):
        from kapps_triplestore_interface import GraphDBCredentials

    return GraphDBCredentials(
        base_url=endpoint.url,
        username=_value_or_fallback("TRIPLESTORE_USERNAME"),
        password=_value_or_fallback("TRIPLESTORE_PASSWORD"),
        repository=endpoint.dataset,
    )


@_deprecation.renamed("triplestore_for")
def graphdb_for(repository: str) -> GraphDB:
    """The deprecated name of :func:`triplestore_for`.

    It returns the store of ``triplestore_for``, typed as the interface's adapter, as before.
    """
    return cast("GraphDB", triplestore_for(repository))


# vendor-check: on
