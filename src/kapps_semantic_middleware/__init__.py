# This package exports `SemanticMiddleware` and `Mode`, and nothing else.
#
# A name belongs here when this package or a documented consumer imports it from here. That is the
# test an addition has to pass.
import asyncio
import contextlib
import logging
import os
import sys
import warnings
from collections.abc import Iterator


def _import_framework_leaving_host_state() -> None:
    """Import the framework, then undo what its import does to the process.

    The framework's package init calls `logging.basicConfig(level=INFO)`, which adds a stderr
    handler to an unconfigured root logger and sets it to INFO. On Windows its MQTT module sets
    a process-wide `WindowsSelectorEventLoopPolicy`. This function removes each root handler
    the import added, sets the root level back, and sets the event-loop policy back if the
    import changed it. A handler, a level or a policy that the host set stays. It touches no
    named logger, and off Windows it calls no policy function.

    If the framework was imported before KSM, the import here does nothing, and there is
    nothing to undo. This function goes when KSM stops importing the framework.
    """
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    # The framework's own condition for setting the policy.
    on_windows = sys.platform.startswith("win") or os.name == "nt"
    if on_windows:
        with _policy_calls():
            policy_before = asyncio.get_event_loop_policy()
    try:
        import transitional_sync_middleware  # noqa: F401
    finally:
        for handler in list(root.handlers):
            if handler not in handlers_before:
                root.removeHandler(handler)
        root.setLevel(level_before)
        if on_windows:
            with _policy_calls():
                if asyncio.get_event_loop_policy() is not policy_before:
                    asyncio.set_event_loop_policy(policy_before)


@contextlib.contextmanager
def _policy_calls() -> Iterator[None]:
    """Run the event-loop policy functions, which are deprecated from Python 3.14 on."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield


_import_framework_leaving_host_state()

from kapps_semantic_middleware.middleware import SemanticMiddleware  # noqa: E402
from kapps_semantic_middleware.modes import Mode  # noqa: E402

__all__ = [
    "SemanticMiddleware",
    "Mode",
]
