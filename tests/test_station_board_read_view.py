"""The station board's read view, offline: what a unit's card shows, and that it offers no
write.

A real ``Controller`` and the real board routes, over a stub store that answers the four
queries one poll makes (the view, a label, a class, a Service's address and heartbeat). The
unit's loaded datamodel and its wiring are filled in by hand, the shapes ``wire_view`` and
``_load_view_datamodels`` produce, so no fetch and no peer are needed. The same routes
against a real store are in ``test_station_board_state_route.py``.

The board is a read-only view: in KAPPS a device changes only through a workflow of its
service, and this demo has none yet.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kapps_triplestore_interface import IRI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo.transferunits import algorithm, seed, station_board  # noqa: E402
from demo.transferunits.controller import Controller  # noqa: E402
from kapps_semantic_middleware.connectors.rest_binding import RESTBinding  # noqa: E402
from kapps_semantic_middleware.connectors.semantic import ParameterBinding  # noqa: E402
from kapps_semantic_middleware.connectors.wiring import WiringPlan  # noqa: E402
from kapps_semantic_middleware.vocabulary import INF  # noqa: E402

UNIT = str(seed._mint_transfer_unit_iri(2))
BELT = "http://example.org/factory#ConveyorBelt2_left"
SPEED = 1.5
QUERY = "SELECT ?resource WHERE { ?resource a ?c }"


class _StubStore:
    """Answers each query a poll makes by what the query selects. Anything else is a
    query this test did not expect, and fails it."""

    def __init__(self, *, heartbeat: datetime) -> None:
        self.heartbeat = heartbeat

    def query(self, query, **_kwargs):
        # `class_of` passes a query object rather than text; both read as their SPARQL.
        sparql = query.to_string() if hasattr(query, "to_string") else query
        if "SELECT ?resource" in sparql:
            rows = [{"resource": IRI(UNIT)}]
        elif "SELECT ?label" in sparql:
            rows = [{"label": "Transfer unit 2"}]
        elif "?svc ?addr ?hb" in sparql:
            rows = [{"addr": "http://127.0.0.1:1", "hb": self.heartbeat.isoformat()}]
        elif "?c" in sparql:
            rows = [{"c": IRI(str(seed.TRANSFER_UNIT_CLASS))}]
        else:
            raise AssertionError(f"unexpected query: {sparql}")
        return {"results": {"bindings": rows}}


def _speed_binding() -> ParameterBinding:
    field_id = seed.TU_HAS_CONVEYOR_SPEED.lined
    return ParameterBinding(
        resource_iri=IRI(BELT),
        parameter_property=IRI(str(seed.TU_HAS_CONVEYOR_SPEED)),
        field_id=field_id,
        metadata={str(INF.accessMode): ["readwrite"]},
        descriptor=RESTBinding,
        node_model_type=object,
        root_iri=IRI(UNIT),
        root_class_local_name=IRI(str(seed.TRANSFER_UNIT_CLASS)).lined,
        path_steps=((seed.TU_HAS_CONVEYOR_BELT.lined, BELT),),
    )


def _loaded_unit() -> SimpleNamespace:
    """The loaded datamodel's shape: unit -> belt (by ``id``) -> speed node -> value."""
    speed = SimpleNamespace()
    setattr(speed, INF.hasValue.lined, [SPEED])
    belt = SimpleNamespace(id=BELT)
    setattr(belt, seed.TU_HAS_CONVEYOR_SPEED.lined, [speed])
    unit = SimpleNamespace(id=UNIT)
    setattr(unit, seed.TU_HAS_CONVEYOR_BELT.lined, [belt])
    return unit


@pytest.fixture
def board():
    store = _StubStore(heartbeat=datetime.now(timezone.utc))
    controller = Controller(
        resource_iri="http://example.org/CS-read-view", ogm=SimpleNamespace(db=store), port=0
    )
    binding = _speed_binding()
    controller.units[UNIT] = _loaded_unit()
    controller._view_wirings.append(
        (
            IRI(UNIT),
            WiringPlan(
                northbound_spec=None,
                class_scope=None,
                bindings=[binding],
                registrations=[],
                southbound_properties=frozenset(),
                southbound_by_property={},
            ),
        )
    )

    app = FastAPI()
    station_board.mount_onto(
        app,
        controller=controller,
        algorithm_state=algorithm.AlgorithmState(),
        default_query=QUERY,
    )
    return TestClient(app)


class TestTheCardShowsWhatTheUnitReports:
    def test_a_poll_carries_the_unit_and_its_live_value(self, board):
        body = board.get("/api/state").json()

        assert body["view_error"] is None
        [unit] = body["units"]
        assert unit["resource_iri"] == UNIT
        assert unit["label"] == "Transfer unit 2"
        assert unit["class_iri"] == str(seed.TRANSFER_UNIT_CLASS)
        assert unit["unreachable"] is False

        [row] = unit["parameters"]
        assert row["holder_iri"] == BELT
        assert row["value"] == SPEED
        assert row["access_mode"] == "readwrite"
        assert row["value_expression"].endswith(f'["inf:hasValue"][0] == {SPEED!r}')

    def test_a_row_carries_no_write_state(self, board):
        """No commanded value and no write status: there is no write for one to be about."""
        [row] = board.get("/api/state").json()["units"][0]["parameters"]

        assert not {"commanded", "status", "status_error"} & set(row)

    def test_the_algorithm_bar_reports_one_firing_rule(self, board):
        algo = board.get("/api/state").json()["algorithm"]

        assert set(algo) == {"paused", "seconds_since_last_reaction", "waiting_seconds"}


class TestTheBoardOffersNoWrite:
    @pytest.mark.parametrize("route", ["/api/set", "/api/algorithm/mode"])
    def test_the_removed_routes_are_gone(self, board, route):
        response = board.post(route, json={})

        assert response.status_code in (404, 405), (route, response.status_code)

    def test_the_page_has_no_set_control(self, board):
        page = board.get("/").text

        assert "/api/set" not in page
        assert "setParam" not in page
        assert 'type="number"' not in page
