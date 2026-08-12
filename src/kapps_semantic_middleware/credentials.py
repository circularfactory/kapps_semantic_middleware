"""Build a GraphDB client for a repository the caller names, ignoring the environment's.

``GRAPHDB_REPOSITORY`` is the library's own convention and remains correct for consumer
code, which connects to whatever repository the user configured. It is wrong for this
project's tests and demos, because both of them *wipe* the repository they connect to
(``seeding.clear_repository``). Inheriting that name from the ambient environment is how a
stray variable gets somebody else's data cleared — the KIT server is shared, and the
repository names on it include ``Production``.

So every construction site here passes its repository explicitly, and this module is the
only sanctioned way to do it. A bare ``GraphDB.from_env()`` anywhere in tests, ``demo/`` or
``examples/`` is a mistake, and a greppable one.

The helper deliberately does **not** delegate to ``GraphDBCredentials.from_env``: that
classmethod raises when ``GRAPHDB_REPOSITORY`` is unset, which would make a variable
nothing reads into a variable everything requires.

This is a deliberate stopgap for v0.1.0 (issue #146). It buys "never touch a repository
the caller did not name" and nothing more — the suite still clears at the start of a run
rather than the end, so it neither starts from a guaranteed clean slate nor leaves no
trace. A disposable-repository lifecycle owned by the triplestore interface is issue #149.
"""

from __future__ import annotations

import os

from kapps_triplestore_interface import GraphDB, GraphDBCredentials

# The variables a client genuinely needs. GRAPHDB_REPOSITORY is absent on purpose.
REQUIRED_GRAPHDB_ENV = (
    "GRAPHDB_URL",
    "GRAPHDB_USERNAME",
    "GRAPHDB_PASSWORD",
)

# The only repository docker/docker-compose.yml creates, so the only one a reader
# following the documented setup has. Both the factory demo and the example scenarios
# target it. GraphDB validates the name at construction, so a different literal here
# fails loudly on a fresh install rather than quietly.
DEMO_REPOSITORY = "kapps-demo"


def graphdb_env_present() -> bool:
    """Whether the environment carries the connection details a live client needs."""
    return all(os.getenv(name) for name in REQUIRED_GRAPHDB_ENV)


def credentials_for(repository: str) -> GraphDBCredentials:
    """Credentials for ``repository``, with the connection details read from the environment.

    Args:
        repository: The repository to target. Named by the caller, never by the
            environment, so that a wipe cannot reach a repository nobody asked for.

    Returns:
        Credentials carrying ``repository`` verbatim.

    Raises:
        ValueError: If a connection variable is missing, naming the variable.
    """
    missing = [name for name in REQUIRED_GRAPHDB_ENV if not os.getenv(name)]
    if missing:
        raise ValueError(
            f"Missing GraphDB environment variable(s): {', '.join(missing)}. "
            f"Set {' and '.join(REQUIRED_GRAPHDB_ENV)} to reach a triple store."
        )

    return GraphDBCredentials(
        base_url=os.environ["GRAPHDB_URL"],
        username=os.environ["GRAPHDB_USERNAME"],
        password=os.environ["GRAPHDB_PASSWORD"],
        repository=repository,
    )


def graphdb_for(repository: str) -> GraphDB:
    """A live client bound to ``repository``.

    The replacement for ``GraphDB.from_env()`` everywhere in this project that seeds or
    clears. GraphDB validates the repository during construction, so a name that does not
    exist on the server raises here rather than failing silently later.

    Args:
        repository: The repository to target.

    Returns:
        A connected client whose ``repository`` is the one passed in.
    """
    return GraphDB(credentials=credentials_for(repository))
