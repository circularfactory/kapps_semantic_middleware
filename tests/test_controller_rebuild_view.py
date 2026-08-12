"""Controller.rebuild_view: the live differential rebuild (#82).

No real peer process here. ``rebuild_view``'s own work -- ``view()`` (a SPARQL query),
``wire_view()`` (recognition: SPARQL + the graph's own ``svc:address``), and
``_load_one_hit()`` (``ogm.fetch`` + in-memory ``persist``) -- never issues HTTP. Only a
registered connector's own ``provide()``/``consume()`` would, and this file never drives
one. So "live" here means "has a ``svc:address`` in the graph", published by hand
(``_publish_service``, the same helper ``test_controller_view.py`` and
``test_scenario3_wiring_integration.py`` already use) -- not a genuinely reachable
process. The REST round trip itself is covered separately, against a real peer, in
``test_controller_view.py::TestDrivingAView``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from kapps_ogm import OGM

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conftest import requires_graphdb  # noqa: E402
from demo.transferunits import seed  # noqa: E402
from demo.transferunits.controller import Controller  # noqa: E402
from kapps_semantic_middleware.vocabulary import SVC  # noqa: E402


def _publish_service(graphdb, resource_iri, address: str) -> None:
    """Give ``resource_iri`` a live Service by hand -- mirrors
    ``test_controller_view.py``'s own helper of the same name."""
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


def _unpublish_service(graphdb, resource_iri) -> None:
    """Take ``resource_iri`` offline: delete its Service triples entirely, the graph
    state a clean deregistration (ADR 0029) leaves behind -- the live clause then binds
    nothing for it, exactly like an offline unit's absence in ``test_controller_view.py``.
    """
    service_iri = f"{resource_iri}Service"
    graphdb.query(f"DELETE WHERE {{ <{service_iri}> ?p ?o }}", update=True)


def _query_selecting(*indices: int) -> str:
    """A SPARQL view selecting exactly the live TransferUnits whose index is in
    ``indices`` -- the test's own stand-in for the Control Expert's heuristic, built
    from an explicit index list rather than the demo's even/odd filter so one test can
    move units in and out of the view on demand."""
    if not indices:
        # A syntactically valid query that structurally can never bind ?resource --
        # this is "the view selects nothing", not "the heuristic is malformed".
        return f"""
        SELECT ?resource WHERE {{
            ?resource a <{seed.TRANSFER_UNIT_CLASS}> .
            FILTER(false)
        }}
        """
    suffixes = ", ".join(f'"{n}"' for n in indices)
    return f"""
    SELECT ?resource WHERE {{
        ?resource a <{seed.TRANSFER_UNIT_CLASS}> .
        ?svc <{SVC.isServiceOf}> ?resource ;
             <{SVC.address}> ?addr .
        BIND(STRAFTER(STR(?resource), "#TransferUnit") AS ?suffix)
        FILTER(?suffix IN ({suffixes}))
    }}
    """


@pytest.fixture
def ogm(graphdb):
    return OGM(db=graphdb)


@pytest.fixture
def factory3(graphdb, ogm):
    """3 seeded TransferUnits, none live yet."""
    seed.seed_factory(graphdb, ogm, units=3)
    return ogm


@requires_graphdb
@pytest.mark.asyncio
class TestDifferentialRebuild:
    async def test_one_rebuild_covers_a_joiner_a_leaver_and_an_unchanged_hit(
        self, graphdb, factory3, unit_scope
    ):
        """#82's own acceptance criterion: "a test covers joiner, leaver and unchanged
        in one rebuild." Units 1 and 2 start live and loaded; unit 1 goes offline and
        unit 3 comes online in the same moment; one rebuild_view call must report all
        three categories and leave self.units matching the new hit set exactly."""
        unit1, unit2, unit3 = (seed._mint_transfer_unit_iri(n) for n in (1, 2, 3))
        _publish_service(graphdb, unit1, "http://127.0.0.1:19201")
        _publish_service(graphdb, unit2, "http://127.0.0.1:19202")

        controller = Controller(resource_iri="http://example.org/CS-rebuild1", ogm=factory3, port=0)
        hits = controller.view(_query_selecting(1, 2))
        controller.wire_view(hits, class_scope=unit_scope)
        await controller._load_view_datamodels()

        assert set(controller.units) == {str(unit1), str(unit2)}
        unit2_instance_before = controller.units[str(unit2)]

        # The world changes: unit 1 deregisters, unit 3 registers.
        _unpublish_service(graphdb, unit1)
        _publish_service(graphdb, unit3, "http://127.0.0.1:19203")

        diff = await controller.rebuild_view(_query_selecting(2, 3))

        assert [str(iri) for iri in diff.joiners] == [str(unit3)]
        assert [str(iri) for iri in diff.leavers] == [str(unit1)]
        assert [str(iri) for iri in diff.unchanged] == [str(unit2)]
        assert diff.error is None

        assert set(controller.units) == {str(unit2), str(unit3)}
        # "Leave the unchanged alone": the same Python object, not re-fetched.
        assert controller.units[str(unit2)] is unit2_instance_before

    async def test_a_leaver_has_its_connectors_torn_down(self, graphdb, factory3, unit_scope):
        """"Close connectors and drop the leavers" -- not just drop the dict entry.
        Every connector wire_view registered for the departed hit must be gone from the
        connection registry, and its wiring must no longer be findable."""
        unit1 = seed._mint_transfer_unit_iri(1)
        _publish_service(graphdb, unit1, "http://127.0.0.1:19301")

        controller = Controller(resource_iri="http://example.org/CS-rebuild2", ogm=factory3, port=0)
        hits = controller.view(_query_selecting(1))
        controller.wire_view(hits, class_scope=unit_scope)
        await controller._load_view_datamodels()

        [(_, wiring)] = controller._view_wirings
        connector_ids_before = [
            f"{binding.resource_iri}#{binding.field_id}#{registration.suffix}"
            for binding, registration in wiring.registrations
        ]
        assert connector_ids_before, "expected at least one connector for unit 1"
        assert all(
            cid in controller.connection_registry.connectors for cid in connector_ids_before
        )

        _unpublish_service(graphdb, unit1)
        diff = await controller.rebuild_view(_query_selecting())

        assert [str(iri) for iri in diff.leavers] == [str(unit1)]
        assert controller.wiring_for(unit1) is None
        assert not any(cid in controller.connection_registry.connectors for cid in connector_ids_before)

    async def test_a_malformed_heuristic_reports_in_place_and_leaves_units_untouched(
        self, graphdb, factory3, unit_scope
    ):
        """Neither a malformed heuristic nor a zero-hit one may 500 -- both are
        reported in the returned ViewDiff, and a malformed one must not disturb
        whatever was already loaded."""
        unit1 = seed._mint_transfer_unit_iri(1)
        _publish_service(graphdb, unit1, "http://127.0.0.1:19401")

        controller = Controller(resource_iri="http://example.org/CS-rebuild3", ogm=factory3, port=0)
        hits = controller.view(_query_selecting(1))
        controller.wire_view(hits, class_scope=unit_scope)
        await controller._load_view_datamodels()
        units_before = dict(controller.units)

        diff = await controller.rebuild_view("SELECT ?resource WHERE { this is not sparql")

        assert diff.error is not None
        assert diff.joiners == [] and diff.leavers == [] and diff.unchanged == []
        assert controller.units == units_before

    async def test_a_zero_hit_heuristic_is_not_an_error(self, graphdb, factory3, unit_scope):
        """A heuristic that matches nothing is a legitimate outcome, not a failure --
        every previously-loaded hit becomes a leaver, and no error is reported."""
        unit1 = seed._mint_transfer_unit_iri(1)
        _publish_service(graphdb, unit1, "http://127.0.0.1:19501")

        controller = Controller(resource_iri="http://example.org/CS-rebuild4", ogm=factory3, port=0)
        hits = controller.view(_query_selecting(1))
        controller.wire_view(hits, class_scope=unit_scope)
        await controller._load_view_datamodels()

        diff = await controller.rebuild_view(_query_selecting())

        assert diff.error is None
        assert [str(iri) for iri in diff.leavers] == [str(unit1)]
        assert controller.units == {}
