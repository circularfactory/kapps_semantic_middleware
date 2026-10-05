"""Offline tests for `seeding.clear_repository`: a seed stops where the store refused the clear.

A fake client stands in for `GraphDB`. It answers `clear_graph()` the way one of the two
interface versions does when the store refuses: the released interface returns `False` and logs
a warning, a later one raises the store's error.
"""

from __future__ import annotations

import pytest
from kapps_triplestore_interface.exceptions import GraphDbException
from requests.exceptions import RequestException

from kapps_semantic_middleware.seeding import clear_repository


class _FakeClient:
    """A `GraphDB` with one repository whose `clear_graph()` answers what it is told."""

    def __init__(self, repository: str, answer):
        self.repository = repository
        self._answer = answer
        self.cleared: list[tuple] = []

    def clear_graph(self, *args):
        self.cleared.append(args)
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer


def test_a_clear_that_returns_false_raises_naming_the_repository():
    db = _FakeClient("scenario-seeds", answer=False)

    with pytest.raises(GraphDbException, match="'scenario-seeds'"):
        clear_repository(db)


# Any error passes on, whatever its type: the store's error answer, and a store out of reach.
@pytest.mark.parametrize(
    "error",
    [
        GraphDbException("HTTP 500: Internal error while trying to validate SHACL Shape"),
        RequestException("the store cannot be reached"),
    ],
    ids=["error-answer", "unreachable"],
)
def test_a_clear_that_raises_passes_the_error_on_unchanged(error):
    db = _FakeClient("scenario-seeds", answer=error)

    with pytest.raises(type(error)) as raised:
        clear_repository(db)

    assert raised.value is error


def test_a_clear_that_succeeds_clears_the_default_graph_once_and_returns():
    db = _FakeClient("scenario-seeds", answer=True)

    clear_repository(db)

    assert db.cleared == [()]
