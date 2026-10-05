"""The errors that a caller of the middleware catches.

Import them from here. Errors of the store and of the OGM propagate unchanged, and are imported
from their own modules: ``kapps_triplestore_interface.exceptions`` and ``kapps_ogm.utils.errors``.
"""

from __future__ import annotations

__all__ = [
    "HandoverPreconditionError",
    "NotificationUndelivered",
    "OntologyGroundTruthError",
    "OperationQueueEmpty",
    "OperationResolutionError",
    "PossessionSwitchConflict",
]


class OntologyGroundTruthError(Exception):
    """A referenced class does not pre-exist as the required kind."""


class OperationResolutionError(Exception):
    """An Operation cannot resolve to a reachable Workflow endpoint."""


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
    ``request()`` when the receiver did not take the notification of the new Operation.

    ``notify`` raises it when the resource has no live Service, when it has more than one, and when
    the post to its Service fails. ``request()`` raises it only when the post fails: its discovery
    has already chosen one Service, and a request that finds none raises
    ``OperationResolutionError`` before anything is written. A post fails when the Service's address
    is no valid URL, when the post cannot connect, or when the Service did not answer in time or
    answered with an error. The notification writes nothing to the graph. The Operation of a
    ``request()`` stays as its commit wrote it, ``queued``: the receiver takes it up when it starts.
    """
