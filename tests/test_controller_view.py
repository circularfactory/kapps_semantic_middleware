"""The controller as a middleware instance: SPARQL view, fetched datamodels, REST
connectors.

The Control Expert's five steps, each its own seam:

1. ``view()`` -- a SPARQL query. No live peer needed: seed a factory, mark some units
   live by hand (``_publish_service``, the same pattern
   ``test_scenario3_wiring_integration.py`` already uses), assert the heuristic finds
   exactly the right ones.
2. ``wire_view()`` -- recognition. No live peer needed either: assert every registered
   connector is REST, never MQTT (``_VIEW_REGISTRY`` excludes it on purpose).
3-5. ``_load_view_datamodels()`` / ``push()`` -- these need a real peer over REST, and the
   peer must run as a genuine separate **process**, not just a separate
   thread in this same process: ``transitional_sync_middleware``'s ``connector_sync_manager`` is a
   process-wide singleton keyed only by ``(data_model_name, model_id)``, so a controller
   and its target peer sharing one process would collide on it the moment both persist
   the same unit's IRI under ``data_model_name="resource"`` -- each would fan out to the
   other's own connectors, forever. A real ``demo.transferunits.middleware`` subprocess
   for the peer sidesteps the collision entirely, the same way the real demo's own
   process-per-participant shape does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from kapps_triplestore_interface import IRI
from kapps_ogm import OGM

from kapps_semantic_middleware.connectors.rest_binding import (
    RESTParameterConnector,
    build_parameter_path,
)
from kapps_semantic_middleware.vocabulary import INF, SVC

from conftest import requires_graphdb  # noqa: E402

# Repo root, so `demo.transferunits...` resolves -- pytest's own "prepend" import mode
# already puts this file's own directory (tests/) on sys.path (what makes the plain
# `conftest` import above work), but not the repo root above it. Same pattern
# `test_northbound_sync_integration.py` uses.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo.transferunits import seed  # noqa: E402
from demo.transferunits.controller import Controller  # noqa: E402
from demo.transferunits.plc.transfer_unit import TransferUnit  # noqa: E402


def _publish_service(graphdb, resource_iri, address: str) -> None:
    """Give ``resource_iri`` a live Service by hand -- the way a real middleware's own
    ``_register_service`` would, without needing that middleware to actually run.
    Mirrors ``test_scenario3_wiring_integration.py``'s own helper."""
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


def _view_query() -> str:
    """The Control Expert's own query: every live TransferUnit, narrowed to
    an even unit index. Neither the class name nor the heuristic lives in
    ``controller.py`` -- both are authored here, in the query text, the same way
    ``control_station.py`` builds it for the running demo.

    SPARQL 1.1 has no ``%`` modulo operator, so evenness is arithmetic:
    ``n - 2*floor(n/2)`` is 0 for an even ``n`` and 1 for an odd one.
    """
    return f"""
    PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
    SELECT ?resource WHERE {{
        ?resource a <{seed.TRANSFER_UNIT_CLASS}> .
        ?svc <{SVC.isServiceOf}> ?resource ; <{SVC.address}> ?addr .
        BIND(STRAFTER(STR(?resource), "#TransferUnit") AS ?suffix)
        FILTER(?suffix != "" && (xsd:integer(?suffix) - 2 * FLOOR(xsd:integer(?suffix) / 2)) = 0)
    }}
    """


@pytest.fixture
def ogm(graphdb):
    return OGM(db=graphdb)


@pytest.fixture
def factory4(graphdb, ogm):
    """4 seeded TransferUnits, none live yet."""
    seed.seed_factory(graphdb, ogm, units=4)
    return ogm


@requires_graphdb
class TestView:
    """The Control Expert's step 1: the query is the whole view."""

    def test_even_unit_index_heuristic_yields_exactly_units_2_and_4_of_4(
        self, graphdb, factory4
    ):
        for n in (1, 2, 3, 4):
            _publish_service(
                graphdb, seed._mint_transfer_unit_iri(n), f"http://127.0.0.1:1900{n}"
            )
        controller = Controller(resource_iri="http://example.org/CS-view1", ogm=factory4, port=0)

        result = {str(r) for r in controller.view(_view_query())}

        assert result == {
            str(seed._mint_transfer_unit_iri(2)),
            str(seed._mint_transfer_unit_iri(4)),
        }

    def test_offline_unit_is_absent_from_the_view(self, graphdb, factory4):
        """Unit 2 never gets a Service -- offline at query time, so it is simply not a
        hit, rather than a hit the caller must separately filter out."""
        _publish_service(graphdb, seed._mint_transfer_unit_iri(4), "http://127.0.0.1:19004")
        controller = Controller(resource_iri="http://example.org/CS-view2", ogm=factory4, port=0)

        result = {str(r) for r in controller.view(_view_query())}

        assert str(seed._mint_transfer_unit_iri(2)) not in result
        assert result == {str(seed._mint_transfer_unit_iri(4))}

    def test_names_no_domain_class(self):
        """view() runs whatever query the caller hands it -- nothing about
        tu:TransferUnit or the even-index heuristic is compiled into the controller."""
        import inspect

        from demo.transferunits import controller as controller_module

        source = inspect.getsource(controller_module.Controller.view)
        assert "TransferUnit" not in source
        assert "%" not in source  # no heuristic math either


@requires_graphdb
class TestWireView:
    """The Control Expert's steps 2-4: recognition registers REST connectors, never MQTT."""

    def test_registers_only_rest_connectors(self, graphdb, factory4, unit_scope):
        _publish_service(graphdb, seed._mint_transfer_unit_iri(2), "http://127.0.0.1:19102")
        controller = Controller(resource_iri="http://example.org/CS-wire1", ogm=factory4, port=0)
        hits = controller.view(_view_query())

        controller.wire_view(hits, class_scope=unit_scope)

        assert controller._view_wirings, "expected the even unit to be wired"
        registrations = [
            registration
            for _, wiring in controller._view_wirings
            for _, registration in wiring.registrations
        ]
        assert registrations, "expected at least one connector registration"
        assert all(
            isinstance(registration.connector, RESTParameterConnector)
            for registration in registrations
        )

    def test_six_connectors_for_one_units_four_parameters(self, graphdb, factory4, unit_scope):
        """2 belts (readwrite -> 2 connectors each) + 2 barriers (read -> 1 each) = 6,
        the same count MQTT wiring reaches for one unit
        (``test_scenario3_wiring_integration.py::TestRegistrationCount``)."""
        _publish_service(graphdb, seed._mint_transfer_unit_iri(2), "http://127.0.0.1:19103")
        controller = Controller(resource_iri="http://example.org/CS-wire2", ogm=factory4, port=0)
        hits = controller.view(_view_query())

        controller.wire_view(hits, class_scope=unit_scope)

        [(_, wiring)] = controller._view_wirings
        assert len(wiring.bindings) == 4
        assert len(wiring.registrations) == 6


def _bind_free_socket(host: str) -> socket.socket:
    """Bind and listen on an OS-assigned free port, without releasing it.

    Same rationale as ``demo/transferunits/launcher.py``'s own helper: reading back a
    discovered port, closing the socket, and letting uvicorn bind a fresh one reopens an
    allocate-hand-off-bind race -- another process could take the port in between. A
    still-listening socket handed straight to uvicorn closes that gap.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(1)
    return sock


def _start_server(app, host: str, sock: socket.socket):
    import uvicorn

    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    return server, thread


async def _await_true(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


async def _read(url: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=5.0)
        response.raise_for_status()
        return response.json()


async def _wait_for_field(url: str, value_field: str, expected, timeout: float = 10.0):
    """Poll ``url`` until its ``value_field`` equals ``expected``, or fail."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            [body] = await _read(url)
            if body[value_field] == expected:
                return body
        except httpx.HTTPError as exc:
            last_error = exc
        await asyncio.sleep(0.2)
    raise AssertionError(f"{url} never reported {value_field}={expected!r} (last error: {last_error})")


def _spawn_peer_middleware(unit_index: int, repository: str) -> subprocess.Popen:
    """Spawn a real unit middleware as a genuine OS process, never a thread
    in this test process -- see the module docstring for why that distinction is
    load-bearing here, not just a style preference. Wires real MQTT connectors, on its
    own in-process broker; nothing here needs a PLC, since these tests only
    exercise the REST side.

    ``repository`` must be passed explicitly, and must be the one this test seeded. The
    child names its repository in code and ignores GRAPHDB_REPOSITORY, so
    inheriting the environment is no longer enough to put both ends in the same graph --
    without this the peer would join the demo's repository and publish its svc:address
    where the test is not looking.
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "demo.transferunits.middleware",
            "--unit-index",
            str(unit_index),
            "--port",
            "0",
            "--repository",
            repository,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
        text=True,
        bufsize=1,
    )


async def _await_service_address(graphdb, resource_iri, timeout: float = 30.0) -> str:
    """Poll the graph for ``resource_iri``'s ``svc:address`` -- the same signal
    ``_register_service`` publishes once the middleware's own lifespan has actually
    started, reachable across a process boundary because it goes through
    the graph rather than any pipe or shared memory."""
    deadline = time.monotonic() + timeout
    sparql = f"""
    SELECT ?addr WHERE {{
        ?svc <{SVC.isServiceOf}> <{resource_iri}> ; <{SVC.address}> ?addr .
    }}
    """
    while time.monotonic() < deadline:
        result = graphdb.query(sparql, convert_bindings=True)
        bindings = (
            result.get("results", {}).get("bindings", []) if isinstance(result, dict) else []
        )
        if bindings:
            return str(bindings[0]["addr"])
        await asyncio.sleep(0.2)
    raise AssertionError(f"{resource_iri} never published a svc:address in time")


def _terminate(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)


@pytest_asyncio.fixture
async def running_peer_and_controller(graphdb, unit_scope):
    """A real unit middleware, in its own OS process, plus a real Controller wired onto
    it through the view mechanism, on a genuine uvicorn thread in *this* process (see
    the module docstring for why the peer specifically cannot share this process).
    """
    seeding_ogm = OGM(db=graphdb)
    seed.seed_factory(graphdb, seeding_ogm, units=2)
    unit_iri = seed._mint_transfer_unit_iri(2)

    peer_proc = _spawn_peer_middleware(2, graphdb.repository)
    try:
        peer_address = await _await_service_address(graphdb, unit_iri)

        controller_sock = _bind_free_socket("127.0.0.1")
        controller_port = controller_sock.getsockname()[1]
        controller = Controller(
            resource_iri="http://example.org/CS-e2e",
            service_class=str(seed.CONTROL_STATION_SERVICE_CLASS),
            ogm=OGM(db=graphdb),
            host="127.0.0.1",
            port=controller_port,
            heartbeat_interval=None,
        )
        hits = controller.view(_view_query())
        assert [str(h) for h in hits] == [
            str(unit_iri)
        ], "the view must find exactly the live peer"
        controller.wire_view(hits, class_scope=unit_scope)

        c_server, c_thread = _start_server(controller.app, "127.0.0.1", controller_sock)
        await _await_true(lambda: c_server.started, 30.0, "controller did not start in time")
        await _await_true(
            lambda: str(unit_iri) in controller.units, 10.0, "the view hit was never loaded"
        )

        try:
            yield controller, peer_address, unit_iri, peer_proc
        finally:
            c_server.should_exit = True
            try:
                await _await_true(
                    lambda: not c_thread.is_alive(), 20.0, "controller thread did not stop in time"
                )
            except AssertionError:
                pass
    finally:
        _terminate(peer_proc)


@requires_graphdb
@pytest.mark.asyncio
class TestDrivingAView:
    """The Control Expert's steps 3-5, over real REST between two real middleware processes."""

    async def test_assigning_a_speed_moves_the_peers_belt_with_no_http_in_the_algorithm(
        self, running_peer_and_controller
    ):
        controller, peer_address, unit_iri, _ = running_peer_and_controller
        unit = controller.units[str(unit_iri)]

        belt_field = seed.TU_HAS_CONVEYOR_BELT.lined
        speed_field = seed.TU_HAS_CONVEYOR_SPEED.lined
        value_field = INF.hasValue.lined

        # "The algorithm's body": a plain attribute assignment plus one framework call.
        # No httpx import, no network call, anywhere in this block.
        belt = getattr(unit, belt_field)[0]
        speed_param = getattr(belt, speed_field)[0]
        setattr(speed_param, value_field, [77.7])
        await controller.push(unit_iri)

        path = build_parameter_path(
            IRI(str(seed.TRANSFER_UNIT_CLASS)).lined,
            unit_iri,
            [(belt_field, str(belt.id))],
            speed_field,
        )
        await _wait_for_field(f"{peer_address}{path}", value_field, [77.7])

    async def test_holds_no_mqtt_properties_anywhere(self, running_peer_and_controller):
        """Acceptance criterion 5: "The controller holds no inf:hasMQTT* property
        anywhere (pruning ticket)." Pruning (via
        ``WiringPlan.northbound_fetch_kwargs``) and the REST-only ``_VIEW_REGISTRY`` are
        belt-and-braces -- this asserts the belt-and-braces actually held on a
        real loaded datamodel, fetched from a peer whose seed data genuinely carries
        MQTT broker metadata, rather than only that the two mechanisms exist.
        """
        controller, _peer_address, unit_iri, _ = running_peer_and_controller
        unit = controller.units[str(unit_iri)]

        dumped = unit.model_dump_json()

        mqtt_markers = (
            INF.hasMQTTTopic,
            INF.hasMQTTSetTopic,
            INF.hasMQTTBrokerIP,
            INF.hasMQTTBrokerPort,
            INF.hasMQTTValuePath,
        )
        for marker in mqtt_markers:
            assert IRI(str(marker)).lined not in dumped, f"{marker} leaked into the loaded datamodel"
        # Independent of the mangled field-name check above: the actual broker address
        # and port this demo seeds for unit 2 must not appear as bare values either.
        assert seed.MQTT_BROKER_IP not in dumped
        assert str(seed.broker_port(2)) not in dumped

    async def test_a_barrier_toggled_on_the_peer_becomes_readable_on_the_controller(
        self, running_peer_and_controller
    ):
        """"The unit's own panel" toggles the barrier the same way a real one would --
        a PLC publishing over MQTT to the *peer's* own southbound connector -- so this
        exercises the controller's northbound REST read against a value that actually
        arrived through the whole southbound chain, not a value poked into the peer's
        persistence by hand.
        """
        controller, _peer_address, unit_iri, _ = running_peer_and_controller

        barrier_field = seed.TU_HAS_LIGHT_BARRIER.lined
        occupied_field = seed.TU_IS_OCCUPIED.lined
        value_field = INF.hasValue.lined

        async with TransferUnit(
            broker=seed.MQTT_BROKER_IP, port=seed.broker_port(2), publish_interval=5.0
        ) as plc:
            await plc.set_occupied("front", True)

            async def _controller_sees_it() -> bool:
                current_barrier = getattr(controller.units[str(unit_iri)], barrier_field)[0]
                current_value = getattr(
                    getattr(current_barrier, occupied_field)[0], value_field
                )
                return current_value == [True]

            await _await_true(
                _controller_sees_it,
                10.0,
                "the barrier flip on the peer never reached the controller",
            )

    async def test_a_peer_that_dies_after_wiring_fails_visibly(
        self, running_peer_and_controller, caplog
    ):
        """Once wired, a unit that goes offline must not go silently unnoticed: the
        REST connector's own poll loop logs a warning on every failed GET
        (``rest_binding.py``'s ``receive()``) rather than hanging or dying quietly."""
        _controller, _peer_address, _unit_iri, peer_proc = running_peer_and_controller
        caplog.set_level(
            logging.WARNING, logger="kapps_semantic_middleware.connectors.rest_binding"
        )

        _terminate(peer_proc)

        def _warned() -> bool:
            return any(
                "Poll of" in record.message and "failed" in record.message
                for record in caplog.records
            )

        await _await_true(_warned, 15.0, "a dead peer's poll failure was never logged")
