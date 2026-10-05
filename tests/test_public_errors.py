"""The errors a caller catches have a public home that is not `registration`.

`kapps_semantic_middleware.errors` is where a subproject imports them from. Their earlier home
stays importable, and both paths name the same class, so an `except` written against either
catches what the middleware raises.
"""

from __future__ import annotations

import pytest

from kapps_semantic_middleware import errors, registration

# The errors that moved here from `registration`, which still names them.
MOVED = [
    "HandoverPreconditionError",
    "OntologyGroundTruthError",
    "OperationQueueEmpty",
    "OperationResolutionError",
    "PossessionSwitchConflict",
]

# Every error the module exports: the moved ones, and the one that was born here.
NAMES = sorted(MOVED + ["NotificationUndelivered"])


@pytest.mark.parametrize("name", MOVED)
def test_an_error_has_one_class_under_both_import_paths(name):
    assert getattr(errors, name) is getattr(registration, name)
    assert getattr(errors, name).__module__ == "kapps_semantic_middleware.errors"


def test_the_errors_module_exports_exactly_these_names():
    assert sorted(errors.__all__) == NAMES
