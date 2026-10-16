"""Unit tests for KnowledgeGraphConnector.

Pure logic: the OGM is a mock, so no store is needed. The connector runs ``OGM.fetch`` and
``OGM.commit`` on a worker thread. An error from either reaches the caller as the same object,
with its own type and message: a store that refuses a write is not reported as a network failure.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from kapps_triplestore_interface import IRI
from kapps_triplestore_interface.exceptions import TripleStoreResponseError

from kapps_semantic_middleware.connectors import KnowledgeGraphConnector

INSTANCE = IRI("https://example.org/instances/belt1")
NAMED_GRAPH = "https://example.org/graphs/state"


class StoreRefusal(TripleStoreResponseError):
    """A subclass of the store's error, as a typed SHACL refusal is. It keeps its own type."""


def errors() -> list[BaseException]:
    """One error of each kind the OGM can raise: a refusal, bad data, and an untyped failure."""
    return [
        StoreRefusal("Error while querying the store (500) - <validation report>"),
        ValueError("data does not fit the class"),
        Exception("Failed to update instance in database."),
    ]


def connector(ogm: Mock) -> KnowledgeGraphConnector:
    return KnowledgeGraphConnector(ogm, INSTANCE, named_graph=NAMED_GRAPH)


class TestErrorsReachTheCallerUnchanged:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", errors(), ids=lambda e: type(e).__name__)
    async def test_provide(self, error: BaseException):
        ogm = Mock()
        ogm.fetch.side_effect = error

        with pytest.raises(type(error)) as raised:
            await connector(ogm).provide()

        assert raised.value is error
        assert not isinstance(raised.value, ConnectionError)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", errors(), ids=lambda e: type(e).__name__)
    async def test_consume(self, error: BaseException):
        ogm = Mock()
        ogm.commit.side_effect = error

        with pytest.raises(type(error)) as raised:
            await connector(ogm).consume({"key": ["value"]})

        assert raised.value is error
        assert not isinstance(raised.value, ConnectionError)


class TestCallsReachTheOgm:
    @pytest.mark.asyncio
    async def test_provide_returns_what_fetch_returns(self):
        ogm = Mock()
        ogm.fetch.return_value = node = object()

        assert await connector(ogm).provide() is node
        ogm.fetch.assert_called_once_with(
            instance_iri=INSTANCE, class_spec=None, class_scope=None, materialize=True
        )

    @pytest.mark.asyncio
    async def test_consume_commits_the_body(self):
        ogm = Mock()
        body = {"key": ["value"]}

        await connector(ogm).consume(body)

        ogm.commit.assert_called_once_with(
            instance_iri=INSTANCE, data=body, named_graph=NAMED_GRAPH
        )
