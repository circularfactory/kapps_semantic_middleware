"""A SELECT or ASK over asserted triples only, for the integration tests' reads.

The query is built by the interface, which writes the scope in the form its store understands,
so no test names a store's own graph.
"""

from __future__ import annotations

from kapps_triplestore_interface import SPARQLQuery
from kapps_triplestore_interface.queries.checked_write import RECEIPT_PREDICATE


def explicit_select(variables: list[str], where: str) -> SPARQLQuery:
    """A SELECT over the asserted triples that match ``where``."""
    return SPARQLQuery.select(
        variables=variables, where_clauses=[where], include_implicit=False
    )


def explicit_ask(where: str) -> SPARQLQuery:
    """An ASK over the asserted triples that match ``where``."""
    return SPARQLQuery.ask(where_clauses=[where], include_implicit=False)


def explicit_statements(db) -> set[tuple[str, str, str]]:
    """Every asserted statement as (s, p, o) strings, to compare before and after.

    The interface's checked writes leave a receipt (``urn:x-kapps:appliedAt``) that the client
    removes with its next write or its close, so a snapshot taken in between would differ by
    the receipt's time; the receipts are not part of the state under test.
    """
    result = db.query(explicit_select(["?s", "?p", "?o"], "?s ?p ?o"), convert_bindings=True)
    return {
        (str(b["s"]), str(b["p"]), str(b["o"]))
        for b in result["results"]["bindings"]
        if str(b["p"]) != str(RECEIPT_PREDICATE)
    }
