"""A SELECT or ASK over asserted triples only, for the integration tests' reads.

The query is built by the interface, which writes the scope in the form its store understands,
so no test names a store's own graph.
"""

from __future__ import annotations

from kapps_triplestore_interface import SPARQLQuery


def explicit_select(variables: list[str], where: str) -> SPARQLQuery:
    """A SELECT over the asserted triples that match ``where``."""
    return SPARQLQuery.select(
        variables=variables, where_clauses=[where], include_implicit=False
    )


def explicit_ask(where: str) -> SPARQLQuery:
    """An ASK over the asserted triples that match ``where``."""
    return SPARQLQuery.ask(where_clauses=[where], include_implicit=False)
