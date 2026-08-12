"""station_board.py's poll and view routes: the acceptance #82 states in terms of the
page, held at the seam a test can actually reach (#82).

Five rules live here, none of which the controller-level tests can hold on their own:

1. **The backend never collapses.** A shut card is a browser-side display state; every
   connector stays live and every value stays current behind it. This is emphatically
   *not* ADR 0032's collapsed monitor row, which genuinely holds no data because it has
   not fetched -- so the payload must never shrink and the server must not track which
   cards are open.
2. **The expert toggle's three jobs** -- full IRI, the real assignment expression, and
   what ``prune_southbound`` stripped -- are three fields on every parameter row. The
   toggle itself is client-side; what is testable is that the data is there, per
   parameter, which is exactly what #78's "discoverable at runtime, per parameter"
   asked for.
3. **The view re-runs on every poll**, so the card set tracks the graph unattended. A
   ``GET /api/state`` *is* one poll, and that is the whole mechanism #35 needs.
4. **A bad heuristic is reported in place**, never as a 500. The controller-level half is
   covered in ``test_controller_rebuild_view.py``; this is the route-level half, where a
   500 would take the page down rather than show a message.
5. **The two deaths look different.** A cleanly stopped unit deregisters and its card
   leaves; a ``kill -9``'d one keeps its address, so it stays selected and its card reads
   ``unreachable`` with its last-known values and an age.

No real peer process here. A ``svc:address`` that nothing listens on is enough to make a
PUT fail for a genuine reason (connection refused) -- the "unit down" case ``rejected``
exists to name -- without paying for a subprocess. A real PUT landing is covered against
a real peer in ``test_controller_view.py::TestDrivingAView``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
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
    """Give ``resource_iri`` a live Service by hand -- mirrors ``test_controller_view.py``'s
    own helper of the same name."""
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
    """Take ``resource_iri`` offline: delete its Service triples entirely, the graph state
    a clean deregistration (ADR 0029) leaves behind -- the live clause then binds nothing
    for it."""
    service_iri = f"{resource_iri}Service"
    graphdb.query(f"DELETE WHERE {{ <{service_iri}> ?p ?o }}", update=True)


def _query_selecting(*indices: int) -> str:
    """A SPARQL view selecting exactly the live TransferUnits whose index is in
    ``indices`` -- this file's stand-in for the Control Expert's heuristic, built from an
    explicit index list rather than the demo's even/odd filter so one test can move units
    in and out of the view on demand. Same helper as ``test_controller_rebuild_view.py``."""
    if not indices:
        # Syntactically valid, structurally unable to bind ?resource -- "the view selects
        # nothing", not "the heuristic is malformed".
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


MALFORMED_QUERY = "SELECT ?resource WHERE { this is not sparql"


@pytest.fixture
def ogm(graphdb):
    return OGM(db=graphdb)


async def _board(graphdb, ogm, unit_scope, *, name: str, live: tuple, query: str):
    """Seed two units, publish a Service for each index in ``live``, wire a controller to
    ``query`` and graft the board onto a bare app.

    Returns ``(client, controller)``. Every test here needs this same eight-line dance,
    and the algorithm is always paused: this file tests the board's own routes, and #82
    disables every set control while the algorithm runs.
    """
    seed.seed_factory(graphdb, ogm, units=2)
    for index in live:
        _publish_service(
            graphdb, seed._mint_transfer_unit_iri(index), f"http://127.0.0.1:{index}"
        )

    controller = Controller(resource_iri=f"http://example.org/{name}", ogm=ogm, port=0)
    hits = controller.view(query)
    controller.wire_view(hits, class_scope=unit_scope)
    await controller._load_view_datamodels()

    app = FastAPI()
    station_board.mount_onto(
        app,
        controller=controller,
        algorithm_state=algorithm.AlgorithmState(tick_seconds=999.0, paused=True),
        default_query=query,
    )
    return TestClient(app), controller


def _rows(body) -> list:
    """Every parameter row in a payload, flattened across units."""
    return [row for unit in body["units"] for row in unit["parameters"]]


def _unit_iris(body) -> set:
    return {unit["resource_iri"] for unit in body["units"]}


@requires_graphdb
@pytest.mark.asyncio
class TestTheBackendNeverCollapses:
    async def test_the_payload_carries_every_row_on_every_poll(self, graphdb, ogm, unit_scope):
        """Collapsing is display-only, so a shut card's values must already be in hand
        when it reopens -- no refetch, no gap. The backend cannot know a card is shut,
        which is what makes that guarantee free."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-collapse1", live=(1,), query=_query_selecting(1)
        )

        for poll in (1, 2):
            body = client.get("/api/state").json()
            assert body["units"], f"poll {poll} returned no units at all"
            for unit in body["units"]:
                assert unit["parameters"], f"poll {poll}: {unit['resource_iri']} lost its rows"
                for row in unit["parameters"]:
                    assert "value" in row, f"poll {poll}: a row arrived with no value key"

    async def test_the_row_set_is_identical_across_polls(self, graphdb, ogm, unit_scope):
        """The payload is the same shape every time. If the backend ever did learn about
        collapsing, this is where the missing rows would show up."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-collapse2", live=(1,), query=_query_selecting(1)
        )

        first = {(r["holder_iri"], r["field_id"]) for r in _rows(client.get("/api/state").json())}
        second = {(r["holder_iri"], r["field_id"]) for r in _rows(client.get("/api/state").json())}

        assert first == second
        assert first, "the fixture wired no parameters, so this proves nothing"

    async def test_the_server_holds_no_per_card_visibility_state(self, graphdb, ogm, unit_scope):
        """The structural half of the same rule: ``_BoardState`` is the board's entire
        server-side memory, and it holds the heuristic text and nothing else. Which cards
        are open lives in the page, in ``expandedCards``.

        Asserted against the dataclass's fields rather than by grepping the source for
        "collapsed" -- the word legitimately appears in this file's own teaching text,
        where #82 explicitly requires it to warn a reader who knows ADR 0032.
        """
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-collapse3", live=(1,), query=_query_selecting(1)
        )

        assert set(station_board._BoardState.__dataclass_fields__) == {"current_query"}

        for unit in client.get("/api/state").json()["units"]:
            assert "collapsed" not in unit
            assert "expanded" not in unit


@requires_graphdb
@pytest.mark.asyncio
class TestTheExpertToggle:
    """#82's "one toggle, three jobs". The toggle is client-side; that all three payloads
    are present per parameter is what a test can hold."""

    async def test_every_row_carries_its_full_iri(self, graphdb, ogm, unit_scope):
        """Job one: ADR 0021 made visible. The operator sees a label, the expert sees the
        IRI it stands for, so the row must carry both."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-toggle1", live=(1,), query=_query_selecting(1)
        )

        rows = _rows(client.get("/api/state").json())

        assert rows, "no parameter rows to check"
        for row in rows:
            assert row["field_iri"].startswith("http"), (
                f"{row['field_iri']!r} is not a full IRI -- a label or CURIE leaked into the field"
            )

    async def test_every_row_carries_its_real_assignment_expression(
        self, graphdb, ogm, unit_scope
    ):
        """Job two: ADR 0033 accepted ADR 0027's awkward shape as-is, and this makes that
        shape inspectable without making the default page a code demo."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-toggle2", live=(1,), query=_query_selecting(1)
        )

        rows = _rows(client.get("/api/state").json())

        assert rows, "no parameter rows to check"
        for row in rows:
            expression = row["assignment_expression"]
            assert '["inf:hasValue"][0] =' in expression, (
                f"{expression!r} does not show the real nested assignment"
            )
            assert row["field_id"] in expression

    async def test_a_speed_row_reports_what_pruning_stripped(self, graphdb, ogm, unit_scope):
        """Job three, and #78's answer: the northbound boundary is the thing this demo
        exists to teach, so what ``prune_southbound`` removed is shown at the parameter it
        applies to -- per parameter, which a per-unit aggregate could not satisfy."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-toggle3", live=(1,), query=_query_selecting(1)
        )

        speed_field = seed.TU_HAS_CONVEYOR_SPEED.lined
        speed_rows = [r for r in _rows(client.get("/api/state").json()) if r["field_id"] == speed_field]

        assert speed_rows, "the seeded belt speed parameter never reached the board"
        for row in speed_rows:
            assert row["pruned"], "the speed row reports nothing stripped, but MQTT topics were"
            assert any("MQTT" in marker for marker in row["pruned"]), (
                f"expected the stripped MQTT markers, got {row['pruned']}"
            )

    async def test_the_payload_never_carries_what_it_reports_as_pruned(
        self, graphdb, ogm, unit_scope
    ):
        """A row that lists a marker under ``pruned`` must not ship its value.

        This was false until #78's scope-down: ``facets`` excluded two keys by name and
        carried everything else, so the same response announced
        ``"pruned": ["hasMQTTBrokerIP", ...]`` beside
        ``"facets": {"hasMQTTBrokerIP": "127.0.0.1", ...}``. The controller drives peers
        over REST and holds no MQTT connector, so a broker address on this page is ADR
        0028's boundary leaking from the consumer side.

        Two assertions, because the marker *names* must still appear: ``pruned`` exists to
        name them. What must not appear is their **values**. So the key check is
        per-row (facets and pruned must not intersect), and the value check is against the
        whole serialised body -- a future field that re-adds the broker address under some
        other key should fail here rather than pass by renaming itself.
        """
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-noleak", live=(1,), query=_query_selecting(1)
        )

        body = client.get("/api/state").json()
        rows = _rows(body)
        assert rows, "no parameter rows to check"

        reported_pruned = {marker for row in rows for marker in row["pruned"]}
        assert reported_pruned, "nothing reported as pruned, so this proves nothing"

        for row in rows:
            leaked = set(row["facets"]) & set(row["pruned"])
            assert not leaked, (
                f"{row['field_id']} reports {sorted(leaked)} as pruned and ships them as facets"
            )

        serialised = json.dumps(body)
        assert seed.MQTT_BROKER_IP not in serialised, (
            "the peer unit's broker address reached the browser"
        )
        assert "/speed_set" not in serialised, (
            "a peer unit's MQTT set topic reached the browser"
        )


@requires_graphdb
@pytest.mark.asyncio
class TestTheViewTracksTheGraphUnattended:
    """The view re-runs on every poll, so ``GET /api/state`` alone moves the card set.
    This is the mechanism #35 needs: a unit added to a running factory appears with no
    restart and no configuration."""

    async def test_a_unit_registered_while_the_page_is_open_appears_within_one_poll(
        self, graphdb, ogm, unit_scope
    ):
        """The heuristic already selects both indices; unit 2 is simply not live yet.
        Registering it is the *only* thing that happens between the two polls -- no
        rebuild call, no page interaction."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-arrive1", live=(1,), query=_query_selecting(1, 2)
        )
        unit1 = str(seed._mint_transfer_unit_iri(1))
        unit2 = str(seed._mint_transfer_unit_iri(2))

        before = _unit_iris(client.get("/api/state").json())
        assert before == {unit1}, f"expected only the live unit up front, got {before}"

        _publish_service(graphdb, seed._mint_transfer_unit_iri(2), "http://127.0.0.1:2")

        after = _unit_iris(client.get("/api/state").json())
        assert after == {unit1, unit2}, f"unit 2 did not join within one poll: {after}"

    async def test_a_cleanly_stopped_unit_leaves_within_one_poll(self, graphdb, ogm, unit_scope):
        """ADR 0029 has the launcher SIGTERM the middleware first, so a clean stop takes
        the unit's ``svc:address`` out of the graph. It then stops matching the live
        clause and its card leaves -- the "cleanly stopped" half of #82's two deaths."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-leave1", live=(1, 2), query=_query_selecting(1, 2)
        )
        unit1 = str(seed._mint_transfer_unit_iri(1))
        unit2 = str(seed._mint_transfer_unit_iri(2))

        before = _unit_iris(client.get("/api/state").json())
        assert before == {unit1, unit2}

        _unpublish_service(graphdb, seed._mint_transfer_unit_iri(2))

        after = _unit_iris(client.get("/api/state").json())
        assert after == {unit1}, f"the stopped unit did not leave within one poll: {after}"


@requires_graphdb
@pytest.mark.asyncio
class TestABadHeuristicIsReportedInPlace:
    """The frame guarantees the ``?resource`` binding and the class, so the failure modes
    left to the editable box are malformed syntax and zero hits. Both are reported on the
    page; neither may 500 it."""

    async def test_a_malformed_heuristic_reports_its_error_without_500ing(
        self, graphdb, ogm, unit_scope
    ):
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-bad1", live=(1,), query=_query_selecting(1)
        )

        response = client.post("/api/view/run", json={"query": MALFORMED_QUERY})

        assert response.status_code == 200, "a typo in the box must not take the page down"
        body = response.json()
        assert body["ok"] is False
        assert body["error"], "a rejected heuristic must say what was wrong with it"

    async def test_a_malformed_heuristic_leaves_the_running_cards_alone(
        self, graphdb, ogm, unit_scope
    ):
        """A typo must cost nothing: the units already wired keep running, with their
        connectors up, exactly as they were."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-bad2", live=(1,), query=_query_selecting(1)
        )
        before = _unit_iris(client.get("/api/state").json())

        client.post("/api/view/run", json={"query": MALFORMED_QUERY})

        response = client.get("/api/state")
        assert response.status_code == 200
        assert _unit_iris(response.json()) == before

    async def test_the_poll_keeps_reporting_a_malformed_heuristic_in_place(
        self, graphdb, ogm, unit_scope
    ):
        """The board keeps the bad text as its current heuristic -- so the Control Expert
        can see and fix it -- which means every following poll re-runs it. Each one has to
        report the failure in the payload rather than raise."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-bad3", live=(1,), query=_query_selecting(1)
        )

        client.post("/api/view/run", json={"query": MALFORMED_QUERY})

        for _ in range(2):
            response = client.get("/api/state")
            assert response.status_code == 200
            assert response.json()["view_error"], "the poll dropped the error silently"

    async def test_a_zero_hit_heuristic_is_reported_but_is_not_an_error(
        self, graphdb, ogm, unit_scope
    ):
        """Selecting nothing is a legitimate answer, not a failure -- an empty board with
        no error message, distinct from the malformed case above."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-bad4", live=(1,), query=_query_selecting(1)
        )

        response = client.post("/api/view/run", json={"query": _query_selecting()})

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.json()["error"] is None

        body = client.get("/api/state").json()
        assert body["units"] == []
        assert body["view_error"] is None, "an empty view is not an error"

    async def test_reset_restores_the_default_heuristic_and_its_cards(
        self, graphdb, ogm, unit_scope
    ):
        """``reset`` is the way back from any edit, including one that emptied the board:
        the default heuristic runs again and its units rejoin as a normal rebuild."""
        default_query = _query_selecting(1)
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-bad5", live=(1,), query=default_query
        )
        unit1 = str(seed._mint_transfer_unit_iri(1))
        client.post("/api/view/run", json={"query": _query_selecting()})
        assert client.get("/api/state").json()["units"] == []

        response = client.post("/api/view/reset")

        assert response.status_code == 200
        body = client.get("/api/state").json()
        assert body["query"] == default_query
        assert _unit_iris(body) == {unit1}, "reset did not bring the default's units back"


@requires_graphdb
@pytest.mark.asyncio
class TestAWriteStatusReachesTheBoard:
    """The classifier itself is unit-tested in ``test_write_status.py``; what matters here
    is that a rejection recorded by the set route is on the very next poll, which is what
    makes ``rejected`` visible on screen and distinct from ``diverged``."""

    async def test_a_rejected_write_shows_as_rejected_on_the_next_poll(
        self, graphdb, ogm, unit_scope
    ):
        """Nothing listens on the unit's address, so the PUT genuinely fails -- the "unit
        down" case. The reason is recorded on the controller rather than in the page, so
        it survives a reload and is visible here."""
        client, controller = await _board(
            graphdb, ogm, unit_scope, name="CS-reject1", live=(1,), query=_query_selecting(1)
        )
        unit1 = str(seed._mint_transfer_unit_iri(1))
        speed_field = seed.TU_HAS_CONVEYOR_SPEED.lined
        belt_iri = str(getattr(controller.units[unit1], seed.TU_HAS_CONVEYOR_BELT.lined)[0].id)

        set_response = client.post(
            "/api/set",
            json={
                "resource_iri": unit1,
                "holder_iri": belt_iri,
                "field_id": speed_field,
                "value": 4.2,
            },
        )
        assert set_response.json()["ok"] is False

        rows = [
            r
            for r in _rows(client.get("/api/state").json())
            if r["holder_iri"] == belt_iri and r["field_id"] == speed_field
        ]

        assert rows, "the belt speed row vanished from the board after a failed write"
        assert rows[0]["status"] == "rejected"
        assert rows[0]["status_error"], "a rejected row must carry the reason on screen"


def _set_heartbeat(graphdb, resource_iri, *, seconds_ago: float) -> None:
    """Give ``resource_iri``'s Service a ``svc:lastHeartbeat`` that many seconds in the
    past -- the one thing a ``kill -9``'d unit stops doing.

    Deletes any existing heartbeat first, so a test can bring a unit to life and then
    kill it without minting a second Service.
    """
    service_iri = f"{resource_iri}Service"
    graphdb.query(
        f"DELETE WHERE {{ <{service_iri}> <{SVC.lastHeartbeat}> ?o }}",
        update=True,
    )
    heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    graphdb.query(
        f"""
        INSERT DATA {{
          <{service_iri}> <{SVC.lastHeartbeat}>
              "{heartbeat_at.isoformat()}"^^<http://www.w3.org/2001/XMLSchema#dateTime> .
        }}
        """,
        update=True,
    )


def _find_unit(body, resource_iri: str):
    """The payload entry for one unit, or ``None`` if the view is not carrying it."""
    for unit in body["units"]:
        if unit["resource_iri"] == resource_iri:
            return unit
    return None


@requires_graphdb
@pytest.mark.asyncio
class TestAKilledUnitStaysAndReadsUnreachable:
    """#82's second death. A ``kill -9``'d unit never gets to deregister, so its
    ``svc:address`` stays in the graph and the view's live clause goes on matching it --
    it is not a leaver, and dropping its card would be a lie about what the graph says.
    What stops is its heartbeat, and that is what the board reports.

    Driven entirely by writing ``svc:lastHeartbeat`` at chosen ages against the real
    90 s ``staleness_threshold``: no process is killed here, because ``liveness_of``
    reads the graph and nothing else, and a subprocess would only make the test slower
    and flakier without testing anything more.
    """

    async def test_a_unit_that_never_reported_a_heartbeat_reads_unreachable(
        self, graphdb, ogm, unit_scope
    ):
        """Nothing has ever reported in, so there is no age to show -- distinct from a
        unit that reported and then stopped, which has one."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-kill1", live=(1,), query=_query_selecting(1)
        )
        unit1 = str(seed._mint_transfer_unit_iri(1))

        unit = _find_unit(client.get("/api/state").json(), unit1)

        assert unit is not None, "the unit must still be selected -- it has an address"
        assert unit["unreachable"] is True
        assert unit["age_seconds"] is None, "no heartbeat means no age to display"

    async def test_a_fresh_heartbeat_reads_reachable(self, graphdb, ogm, unit_scope):
        """The control. Without it, every assertion below would pass just as well against
        a liveness check that was simply broken and always said "unreachable"."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-kill2", live=(1,), query=_query_selecting(1)
        )
        unit1 = str(seed._mint_transfer_unit_iri(1))
        _set_heartbeat(graphdb, seed._mint_transfer_unit_iri(1), seconds_ago=0)

        unit = _find_unit(client.get("/api/state").json(), unit1)

        assert unit is not None
        assert unit["unreachable"] is False, "a unit that just reported in is not dead"
        assert 0 <= unit["age_seconds"] < 60, f"expected a fresh age, got {unit['age_seconds']}"

    async def test_a_heartbeat_older_than_the_staleness_threshold_reads_unreachable(
        self, graphdb, ogm, unit_scope
    ):
        """The ``kill -9`` itself: nothing refreshed the heartbeat, and the board works
        that out on its own. Nobody tells it the process died -- there is no one left to.

        600 s is comfortably past the 90 s default rather than a hair over it, so this
        cannot fail on a slow machine.
        """
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-kill3", live=(1,), query=_query_selecting(1)
        )
        unit1 = str(seed._mint_transfer_unit_iri(1))
        _set_heartbeat(graphdb, seed._mint_transfer_unit_iri(1), seconds_ago=600)

        unit = _find_unit(client.get("/api/state").json(), unit1)

        assert unit is not None, "a killed unit keeps its address, so it stays selected"
        assert unit["unreachable"] is True
        assert unit["age_seconds"] >= 600, f"expected the written age, got {unit['age_seconds']}"

    async def test_a_killed_unit_keeps_its_card_and_its_last_known_values(
        self, graphdb, ogm, unit_scope
    ):
        """#82 asks for last-known values *greyed*, not gone -- so the payload has to
        still carry them. Greying is the page's job; the backend's job is not to drop the
        last thing the unit managed to say."""
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-kill4", live=(1,), query=_query_selecting(1)
        )
        unit1 = str(seed._mint_transfer_unit_iri(1))
        _set_heartbeat(graphdb, seed._mint_transfer_unit_iri(1), seconds_ago=600)

        body = client.get("/api/state").json()
        unit = _find_unit(body, unit1)

        assert unit is not None, f"the killed unit's card left the board: {_unit_iris(body)}"
        assert unit["parameters"], "the killed unit lost its rows instead of greying them"
        for row in unit["parameters"]:
            assert "value" in row

    async def test_the_two_deaths_look_different(self, graphdb, ogm, unit_scope):
        """The heart of #82's liveness section, and the reason the two are not unified: a
        unit that deregistered (ADR 0029's clean stop) said goodbye and its card leaves; a
        unit that was killed never got the chance, so its card stays and says so.

        Both deaths happen between the same two polls, so this cannot pass by one of them
        simply taking longer to show up.
        """
        client, _ = await _board(
            graphdb, ogm, unit_scope, name="CS-kill5", live=(1, 2), query=_query_selecting(1, 2)
        )
        unit1 = str(seed._mint_transfer_unit_iri(1))
        unit2 = str(seed._mint_transfer_unit_iri(2))
        _set_heartbeat(graphdb, seed._mint_transfer_unit_iri(1), seconds_ago=0)
        _set_heartbeat(graphdb, seed._mint_transfer_unit_iri(2), seconds_ago=0)

        before = client.get("/api/state").json()
        assert _unit_iris(before) == {unit1, unit2}
        assert [u["unreachable"] for u in before["units"]] == [False, False], (
            "both units must start alive, or the comparison below proves nothing"
        )

        _set_heartbeat(graphdb, seed._mint_transfer_unit_iri(1), seconds_ago=600)  # kill -9
        _unpublish_service(graphdb, seed._mint_transfer_unit_iri(2))  # clean stop

        after = client.get("/api/state").json()

        assert _unit_iris(after) == {unit1}, f"expected only the killed unit, got {_unit_iris(after)}"
        assert _find_unit(after, unit1)["unreachable"] is True, (
            "the killed unit stayed but did not report itself unreachable"
        )
