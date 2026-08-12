"""Workflow and state handlers for the example scenarios.

These live in an importable module (rather than inline in the notebooks) for a
concrete reason: ``@workflow`` runs each function through transitional_sync_middleware
typeguard-based instrumentation, which reads the function *module source*. A
function defined in a Jupyter cell belongs to ``__main__``, which has no source
file, so that instrumentation fails. Definition of the handlers here gives them a real
module source and keeps the shared in-memory door state in one place.
"""

from __future__ import annotations

import threading

# --- Scenario 1 -------------------------------------------------------------- #


def hello_world() -> str:
    """The most basic workflow: return a greeting."""
    return "hello world"


# --- Scenario 2 (door) ------------------------------------------------------- #

# In-memory door state — mutated by the workflows, read by the state getter.
# Deliberately NOT stored in the knowledge graph.
_door = {"status": "closed"}

# The door closes itself 30 s after opening (a safety default). Timers are daemon threads
# (they never block process exit) and are tracked so a script/test can cancel any still-
# pending one on teardown.
DOOR_AUTO_CLOSE_SECONDS = 30.0
_auto_close_timers: list[threading.Timer] = []


def door_open() -> str:
    """Workflow: open the door (mutates in-memory state) and schedule its auto-close."""
    _door["status"] = "opened"
    timer = threading.Timer(
        DOOR_AUTO_CLOSE_SECONDS, lambda: _door.__setitem__("status", "closed")
    )
    timer.daemon = True
    timer.start()
    _auto_close_timers.append(timer)
    return "opened"


def door_close() -> str:
    """Workflow: close the door (mutates in-memory state)."""
    _door["status"] = "closed"
    return "closed"


def door_status() -> str:
    """State getter: the door current status, served live from memory."""
    return _door["status"]


def reset_door() -> None:
    """Reset the door to closed and cancel any pending auto-close timers (setup/teardown)."""
    for timer in _auto_close_timers:
        timer.cancel()
    _auto_close_timers.clear()
    _door["status"] = "closed"
