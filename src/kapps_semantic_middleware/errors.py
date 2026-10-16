"""The errors that a caller of the middleware catches.

Import them from here. Errors of the store and of the OGM propagate unchanged, and are imported
from their own modules: ``kapps_triplestore_interface.exceptions`` and ``kapps_ogm.utils.errors``.

The type of an error tells the caller its action, so the caller chooses its branch with ``except``
and does not read the message. Each type below that names an action in its docstring asks for that
one action:

- **Repair:** the middleware or its deployment is broken. The operator repairs it, and the same
  call fails until then. ``DeploymentError`` is the base of these types.
- **Retry:** this attempt failed for a temporary reason, such as a peer that is not live now. The
  same call can succeed later.

Wrong use of the middleware's API raises a built-in ``ValueError``, ``TypeError`` or
``RuntimeError``: the caller fixes the call. A type that replaces a built-in also subclasses it, and
a type that splits an older type also subclasses that type, so an ``except`` written against the
older type still catches it.
"""

from __future__ import annotations

__all__ = [
    "CapabilityNotRealizedError",
    "DeploymentError",
    "DeploymentRuntimeError",
    "DeploymentValueError",
    "HandoverPreconditionError",
    "NotificationReceiverAmbiguousError",
    "NotificationReceiverUnavailableError",
    "NotificationUndelivered",
    "OntologyGroundTruthError",
    "OperationQueueEmpty",
    "OperationReceiverUnavailableError",
    "OperationResolutionError",
    "PossessionSwitchConflict",
]


class DeploymentError(Exception):
    """The middleware or its deployment is broken, and the middleware detected it.

    Action: Repair. The operator repairs the configuration, the installation or the data in the
    store, or the maintainers fix the bug. The same call fails until then.

    The base of the middleware's own Repair types; it is never raised itself. A caller catches
    every one of them with ``except DeploymentError``. ``DeploymentValueError`` and
    ``DeploymentRuntimeError`` replace the built-in that such a condition raised before, and
    ``CapabilityNotRealizedError`` and ``NotificationReceiverAmbiguousError`` are the Repair
    cases of ``OperationResolutionError`` and ``NotificationUndelivered``.
    """


class DeploymentValueError(DeploymentError, ValueError):
    """A value that the deployment gives is missing or wrong: a store variable that is not set,
    an unknown mode, a mode without the arguments that it requires, a Resource with more than one
    possession state, or an instance with no asserted domain type.

    Action: Repair. Set the variable, correct the configuration of the instance, or repair the
    data in the store; the same call fails until then. It is a ``ValueError`` too, as each of
    these conditions raised before.
    """


class DeploymentRuntimeError(DeploymentError, RuntimeError):
    """The middleware's installation or an internal rule is broken: the bundled examples cannot
    be found, or an Operation reached the intake of an instance without a notification handler.

    Action: Repair. Install the package again, or report the bug; the same call fails until
    then. It is a ``RuntimeError`` too, as each of these conditions raised before.
    """


class OntologyGroundTruthError(Exception):
    """A referenced class does not pre-exist as the required kind."""


class OperationResolutionError(Exception):
    """An Operation cannot resolve to a reachable Workflow endpoint.

    The middleware raises one of its two subclasses, which tell the action:
    ``OperationReceiverUnavailableError`` (Retry) and ``CapabilityNotRealizedError`` (Repair).
    """


class OperationReceiverUnavailableError(OperationResolutionError):
    """A Workflow realizes the Capability, but no Service of it is live now: none has an address.

    Action: Retry. Issue the Operation again later, when the receiver is live. Nothing was
    written.
    """


class CapabilityNotRealizedError(OperationResolutionError, DeploymentError):
    """No Workflow realizes the Capability: no receiver for it is registered in the graph.

    Action: Repair. Deploy and start a resource that exposes a Workflow for the Capability; the
    same call fails until then. ``issue_operation()`` raises it before anything is written, and
    ``claim_next()`` raises it before the claim, and the Operation stays ``queued``.
    """


class OperationQueueEmpty(Exception):
    """Raised when `claim_next` finds no `queued` Operation to pull."""


class HandoverPreconditionError(Exception):
    """A handover precondition failed (caller lacks possession, or counterpart lacks the
    complementary handover ability). Rejection occurs before any physical work begins."""


class PossessionSwitchConflict(Exception):
    """The store refused the commit of a change of possession, because the precondition of the
    commit was false, so nothing was written.

    ``handover()`` and ``switch_possession`` raise it for the store's ``PreconditionFailedError``
    (from ``kapps_triplestore_interface.exceptions``), which is its cause and names the triples
    of the precondition. Usually another write changed a value after the commit read it: for
    example, a second handover of the same workpiece landed between the commit's read of the
    workpiece and its write. Nothing switches against values that are no longer there."""


class NotificationUndelivered(Exception):
    """Raised by ``notify`` when the resource did not take the notification, and by
    ``issue_operation()`` when the receiver did not take the notification of the new Operation.

    The middleware raises one of its two subclasses, which tell the action:
    ``NotificationReceiverUnavailableError`` (Retry) and ``NotificationReceiverAmbiguousError``
    (Repair).

    ``notify`` raises it when the resource has no live Service, when it has more than one, and when
    the post to its Service fails. ``issue_operation()`` raises it only when the post fails: its discovery
    has already chosen one Service, and a request that finds none raises
    ``OperationResolutionError`` before anything is written. A post fails when the Service's address
    is no valid URL, when the post cannot connect, or when the Service did not answer in time or
    answered with an error. The notification writes nothing to the graph. The Operation of a
    ``issue_operation()`` stays as its commit wrote it, ``queued``: the receiver takes it up when it starts.
    """


class NotificationReceiverUnavailableError(NotificationUndelivered):
    """The resource has no live Service, or the post to its Service failed: the address is no
    valid URL, the post cannot connect, or the Service did not answer in time or answered with
    an error.

    Action: Retry. Send the notification again later. The notification wrote nothing to the
    graph; the Operation of an ``issue_operation()`` stays ``queued``, and the receiver takes it
    up when it starts. The error of the post, if any, is the cause.
    """


class NotificationReceiverAmbiguousError(NotificationUndelivered, DeploymentError):
    """The resource has more than one live Service, and ``notify`` does not choose between them.

    Action: Repair. Stop the instance that should not be live, or remove the address that it
    left in the graph; the same call fails until then. Nothing was posted.
    """
