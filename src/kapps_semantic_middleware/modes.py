"""The three middleware modes as constants. Do not use bare strings (#21).

``SemanticMiddleware(mode=...)`` took a plain ``str``. It validated the string against a tuple.
The tuple was written out at the call site. A typo produced a ``ValueError`` at construction. This
is at least loud. Nothing made the valid values discoverable. Every comparison in the class restated
one of the literals.

Use a ``str`` subclass. Do not use a plain ``Enum``. This is deliberate. Every existing caller
passes ``mode="resource"``. The scenarios, tests and notebooks are full of it. Subclass ``str``.
This keeps all of that working unchanged. ``Mode.RESOURCE == "resource"`` is true. So is
``mode in (Mode.RESOURCE, Mode.WATCHDOG)`` for a caller who passed the bare string. The constant
is the better way to say it. The string does not stop being a way to say it.
"""

# ADR: 0005

from __future__ import annotations

from enum import Enum


class Mode(str, Enum):
    """A middleware instance mode. Each member below says what it means."""

    # ADR: 0005, 0007

    RESOURCE = "resource"
    """Wrap one ``resource_iri``. Register a Service. Serve its workflows and parameters.
    Hold a heartbeat. This is the only mode with runtime consequence today."""

    SERVER = "server"
    """Reserved. Serve data with no physical resource. Not implemented. Construct one
    raises. Out of scope for the scenario-3 controller: a controller consumes a graph, and it
    does not serve one."""

    WATCHDOG = "watchdog"
    """Reserved. Sweep liveness from a central point. Sweep stale Services. Register
    nothing of its own."""

    def __str__(self) -> str:
        # Without this, an f-string renders "Mode.RESOURCE". It does not render "resource". This
        # would change every log line and error message that interpolates a mode.
        return self.value


ALL = tuple(Mode)
"""Every valid mode. Use for validation. Use for error messages that list the alternatives."""
