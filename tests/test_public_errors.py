"""The errors a caller catches have a public home that is not `registration`.

`kapps_semantic_middleware.errors` is where a subproject imports them from. Their earlier home
stays importable, and both paths name the same class, so an `except` written against either
catches what the middleware raises. The errors of the handover differ: the handover and its
helpers left `registration` for the possession extension, and `registration` names no possession
term. Their old path is a deprecated alias that warns on each use and goes in the next release.
"""

from __future__ import annotations

import warnings

import pytest

from kapps_semantic_middleware import errors, registration

# The errors that moved here from `registration`, which still names them.
MOVED = [
    "OntologyGroundTruthError",
    "OperationQueueEmpty",
    "OperationResolutionError",
]

# The errors of the handover, which moved here from `registration`, whose old names are deprecated.
HANDOVER = [
    "HandoverPreconditionError",
    "PossessionSwitchConflict",
]

# The errors that were born here: each tells one action, except the two older types that the
# action types split.
BORN_HERE = [
    "CapabilityNotRealizedError",
    "DeploymentError",
    "DeploymentRuntimeError",
    "DeploymentValueError",
    "NotificationReceiverAmbiguousError",
    "NotificationReceiverUnavailableError",
    "NotificationUndelivered",
    "OperationReceiverUnavailableError",
]

# Every error the module exports: the moved ones, those of the handover, and those born here.
NAMES = sorted(MOVED + HANDOVER + BORN_HERE)


@pytest.mark.parametrize("name", MOVED)
def test_an_error_has_one_class_under_both_import_paths(name):
    assert getattr(errors, name) is getattr(registration, name)
    assert getattr(errors, name).__module__ == "kapps_semantic_middleware.errors"


@pytest.mark.parametrize("name", HANDOVER)
def test_an_error_of_the_handover_has_its_home_in_errors_and_a_deprecated_old_path(name):
    assert getattr(errors, name).__module__ == "kapps_semantic_middleware.errors"
    assert name not in vars(registration)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert getattr(registration, name) is getattr(errors, name)
    assert [w.category for w in caught] == [DeprecationWarning]
    assert f"kapps_semantic_middleware.errors.{name}" in str(caught[0].message)


def test_the_errors_module_exports_exactly_these_names():
    assert sorted(errors.__all__) == NAMES
