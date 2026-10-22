"""The station board's prewritten view queries: parity options, order, and the default."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.transferunits import algorithm  # noqa: E402


def test_default_query_has_no_parity_filter():
    assert algorithm.build_view_query() == algorithm.build_view_query("all")
    assert "FILTER" not in algorithm.build_view_query()


@pytest.mark.parametrize("parity,target", [("even", "= 0"), ("odd", "= 1")])
def test_parity_queries_filter_on_the_unit_index(parity, target):
    query = algorithm.build_view_query(parity)
    assert "#TransferUnit" in query
    assert query.rstrip().rstrip("}").rstrip().endswith(target + ")")


def test_unknown_parity_is_rejected():
    with pytest.raises(ValueError):
        algorithm.build_view_query("prime")


def test_named_queries_list_all_first_and_end_with_a_custom_starter():
    entries = algorithm.named_view_queries()
    assert [e["name"] for e in entries] == [
        "All TransferUnits",
        "Even-indexed TransferUnits",
        "Odd-indexed TransferUnits",
        "Custom — edit your own",
    ]
    assert entries[0]["query"] == algorithm.build_view_query()
    assert all("SELECT ?resource" in e["query"] for e in entries)
