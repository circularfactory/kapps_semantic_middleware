"""station_board.py's POST /api/set route: rejected is distinguishable from a settled
write, and the algorithm's pause gates it server-side (#82).

No real peer process here. A ``svc:address`` that nothing listens on is enough to make
``controller.push()`` fail for a genuine reason (connection refused) -- exactly the
"unit down" case ``rejected`` exists to name -- without paying for a subprocess. The
happy path (a real PUT actually landing) is covered against a real peer in
``test_controller_view.py::TestDrivingAView``; this file is about station_board.py's own
routing of push()'s two outcomes, not about the REST connector underneath it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kapps_ogm import OGM

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conftest import requires_graphdb  # noqa: E402
from demo.transferunits import algorithm, seed, station_board  # noqa: E402
from demo.transferunits.controller import Controller  # noqa: E402
from kapps_semantic_middleware.vocabulary import SVC  # noqa: E402


def _publish_service(graphdb, resource_iri, address: str) -> None:
    service_iri = f"{resource_iri}Service"
    graphdb.query(
        f"""
        INSERT DATA {{
          <{service_iri}> a <{SVC.Service}> ;
              <{SVC.isServiceOf}> <{resource_iri}> ;
              <{SVC.address}> "{address}" .
        }}
        """,
        update=True,
    )


@pytest.fixture
def ogm(graphdb):
    return OGM(db=graphdb)


@requires_graphdb
@pytest.mark.asyncio
class TestSetRoute:
    async def test_a_write_to_an_unreachable_unit_is_rejected_distinguishably(
        self, graphdb, ogm, unit_scope
    ):
        seed.seed_factory(graphdb, ogm, units=1)
        unit1 = seed._mint_transfer_unit_iri(1)
        # Nothing listens on this port -- the PUT genuinely fails, the same "unit down"
        # case #82's own text names for `rejected`, with no subprocess required.
        _publish_service(graphdb, unit1, "http://127.0.0.1:1")

        controller = Controller(resource_iri="http://example.org/CS-set1", ogm=ogm, port=0)
        query = f"""
        SELECT ?resource WHERE {{
            ?resource a <{seed.TRANSFER_UNIT_CLASS}> .
            ?svc <{SVC.isServiceOf}> ?resource ; <{SVC.address}> ?addr .
        }}
        """
        hits = controller.view(query)
        controller.wire_view(hits, class_scope=unit_scope)
        await controller._load_view_datamodels()
        assert str(unit1) in controller.units, "the hit must have loaded despite the dead address"

        state = algorithm.AlgorithmState(tick_seconds=999.0, paused=True)
        app = FastAPI()
        station_board.mount_onto(
            app, controller=controller, algorithm_state=state, default_query=query
        )
        client = TestClient(app)

        belt_field = seed.TU_HAS_CONVEYOR_BELT.lined
        speed_field = seed.TU_HAS_CONVEYOR_SPEED.lined
        belt_iri = str(getattr(controller.units[str(unit1)], belt_field)[0].id)

        response = client.post(
            "/api/set",
            json={
                "resource_iri": str(unit1),
                "holder_iri": belt_iri,
                "field_id": speed_field,
                "value": 4.2,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body.get("error"), "a rejected write must carry a reason, per #82"

    async def test_set_is_refused_while_the_algorithm_is_not_paused(
        self, graphdb, ogm, unit_scope
    ):
        """Server-side half of "set controls are inert while the algorithm runs" --
        never trust the frontend's disabled button alone."""
        seed.seed_factory(graphdb, ogm, units=1)
        unit1 = seed._mint_transfer_unit_iri(1)
        _publish_service(graphdb, unit1, "http://127.0.0.1:1")

        controller = Controller(resource_iri="http://example.org/CS-set2", ogm=ogm, port=0)
        query = f"""
        SELECT ?resource WHERE {{
            ?resource a <{seed.TRANSFER_UNIT_CLASS}> .
            ?svc <{SVC.isServiceOf}> ?resource ; <{SVC.address}> ?addr .
        }}
        """
        hits = controller.view(query)
        controller.wire_view(hits, class_scope=unit_scope)
        await controller._load_view_datamodels()

        state = algorithm.AlgorithmState(tick_seconds=999.0, paused=False)  # running
        app = FastAPI()
        station_board.mount_onto(
            app, controller=controller, algorithm_state=state, default_query=query
        )
        client = TestClient(app)

        belt_field = seed.TU_HAS_CONVEYOR_BELT.lined
        speed_field = seed.TU_HAS_CONVEYOR_SPEED.lined
        belt_iri = str(getattr(controller.units[str(unit1)], belt_field)[0].id)

        response = client.post(
            "/api/set",
            json={
                "resource_iri": str(unit1),
                "holder_iri": belt_iri,
                "field_id": speed_field,
                "value": 4.2,
            },
        )

        assert response.status_code == 409
