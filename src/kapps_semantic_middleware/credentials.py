"""Build a store for a dataset that the caller names, and ignore the environment's.

The triple store interface reads ``TRIPLESTORE_DATASET`` when a caller names no dataset, and
that remains correct for consumer code, which connects to whatever dataset the user configured.
It is wrong for this project's tests and demos, because both of them *wipe* the dataset they
connect to (``seeding.clear_dataset``). Inheriting that name from the ambient environment is how
a stray variable gets somebody else's data cleared -- a shared server carries datasets that
other people depend on.

So every construction site here passes its dataset explicitly, and this module is the only
sanctioned way to do it. A store built from ``TripleStoreEndpoint.from_env()`` without a dataset
anywhere in tests, ``demo/`` or ``examples/`` is a mistake, and a greppable one.

The store comes from the interface's factory, ``connect()``: the URL from ``TRIPLESTORE_URL``,
the identity from ``TRIPLESTORE_USERNAME`` and ``TRIPLESTORE_PASSWORD``, and the dataset from the
caller. No other variable counts: a store that only an older variable names is not reachable.

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

from kapps_triplestore_interface import (
    GDBTokenAuth,
    TripleStore,
    TripleStoreEndpoint,
    connect,
)

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
    """The variables of `names` that are not set."""
    return [name for name in names if not os.getenv(name)]


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
