"""Offline tests for the launcher seeding logic.

This module tests the IRI minting scheme, the MQTT topic construction, and the
environment handling without a live GraphDB. This module mirrors the style of
test_recursive_rest_router.py.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from kapps_triplestore_interface import IRI

from demo.transferunits import seed
from demo.transferunits.launcher import _spawn_plc, _strip_graphdb_env

from conftest import requires_graphdb  # noqa: E402


class TestIRIMinting:
    """This class tests the index-derived IRI scheme (ADR 0030)."""

    def test_unit_1_matches_existing_constants(self):
        """The Unit 1 IRIs must match the frozen constants in examples/seed.py."""
        assert seed._mint_transfer_unit_iri(1) == IRI(
            "https://www.sfb1574.kit.edu/ontologies/TransferUnitInstances#TransferUnit1"
        )
        assert seed._mint_conveyor_belt_iri(1, "left") == IRI(
            "https://www.sfb1574.kit.edu/ontologies/TransferUnitInstances#ConveyorBelt1_left"
        )
        assert seed._mint_conveyor_belt_iri(1, "right") == IRI(
            "https://www.sfb1574.kit.edu/ontologies/TransferUnitInstances#ConveyorBelt1_right"
        )
        assert seed._mint_light_barrier_iri(1, "front") == IRI(
            "https://www.sfb1574.kit.edu/ontologies/TransferUnitInstances#LightBarrier1_front"
        )
        assert seed._mint_light_barrier_iri(1, "back") == IRI(
            "https://www.sfb1574.kit.edu/ontologies/TransferUnitInstances#LightBarrier1_back"
        )

    def test_unit_2_follows_pattern(self):
        """The Unit 2 IRIs follow the same pattern with index 2."""
        assert seed._mint_transfer_unit_iri(2) == IRI(
            "https://www.sfb1574.kit.edu/ontologies/TransferUnitInstances#TransferUnit2"
        )
        assert seed._mint_conveyor_belt_iri(2, "left") == IRI(
            "https://www.sfb1574.kit.edu/ontologies/TransferUnitInstances#ConveyorBelt2_left"
        )
        assert seed._mint_light_barrier_iri(2, "front") == IRI(
            "https://www.sfb1574.kit.edu/ontologies/TransferUnitInstances#LightBarrier2_front"
        )

    def test_control_station_is_fixed(self):
        """The control station IRI stays the same, regardless of N."""
        assert seed.CONTROL_STATION == IRI(
            "https://www.sfb1574.kit.edu/ontologies/FactoryInstances#ControlStation1"
        )


class TestMQTTTopics:
    """This class tests the MQTT topic construction (ADR 0023)."""

    def test_speed_topic(self):
        """The speed topics follow TransferUnit<n>/ConveyorBelt/<position>/speed."""
        assert seed._mqtt_topic(1, "ConveyorBelt", "left", "speed") == "TransferUnit1/ConveyorBelt/left/speed"
        assert seed._mqtt_topic(3, "ConveyorBelt", "right", "speed") == "TransferUnit3/ConveyorBelt/right/speed"

    def test_setpoint_topic(self):
        """The setpoint topics append _set to the param segment."""
        assert seed._mqtt_topic(1, "ConveyorBelt", "left", "speed_set") == "TransferUnit1/ConveyorBelt/left/speed_set"
        assert seed._mqtt_topic(2, "ConveyorBelt", "right", "speed_set") == "TransferUnit2/ConveyorBelt/right/speed_set"

    def test_occupied_topic(self):
        """The light-barrier occupancy topics follow the same scheme."""
        assert seed._mqtt_topic(1, "LightBarrier", "front", "occupied") == "TransferUnit1/LightBarrier/front/occupied"
        assert seed._mqtt_topic(4, "LightBarrier", "back", "occupied") == "TransferUnit4/LightBarrier/back/occupied"


class TestBrokerPort:
    """This class tests broker_port(n) = 18830 + n (#79, ADR 0029/0030 as amended).

    Not 1883 + n: 1900 is SSDP/UPnP and is live on most Linux desktops.
    """

    def test_unit_1_is_18831(self):
        assert seed.BROKER_PORT_BASE == 18830
        assert seed.broker_port(1) == 18831

    def test_unit_2_is_18832(self):
        assert seed.broker_port(2) == 18832

    def test_follows_the_index_pattern(self):
        assert seed.broker_port(17) == 18847


class TestEnvironmentHandling:
    """This class tests that _strip_graphdb_env strips GRAPHDB_* credentials for PLC children (ADR 0029)."""

    def test_strip_graphdb_env_removes_all_graphdb_vars(self):
        """_strip_graphdb_env removes every GRAPHDB_* key and keeps the rest."""
        env = {
            "GRAPHDB_URL": "http://localhost:7200",
            "GRAPHDB_USERNAME": "admin",
            "GRAPHDB_PASSWORD": "secret",
            "GRAPHDB_REPOSITORY": "test",
            "OTHER_VAR": "kept",
        }
        stripped = _strip_graphdb_env(env)

        assert "GRAPHDB_URL" not in stripped
        assert "GRAPHDB_USERNAME" not in stripped
        assert "GRAPHDB_PASSWORD" not in stripped
        assert "GRAPHDB_REPOSITORY" not in stripped
        assert stripped["OTHER_VAR"] == "kept"

    def test_strip_graphdb_env_handles_empty(self):
        """_strip_graphdb_env works on an empty dict."""
        assert _strip_graphdb_env({}) == {}

    def test_spawn_plc_passes_stripped_env_to_popen(self, monkeypatch):
        """_spawn_plc's actual Popen call carries no GRAPHDB_* var (ADR 0029: "asserted, not assumed").

        test_strip_graphdb_env_removes_all_graphdb_vars only exercises the helper in isolation;
        this asserts the wiring between "helper strips" and "spawn uses the stripped copy".
        """
        monkeypatch.setenv("GRAPHDB_URL", "http://localhost:7200")
        monkeypatch.setenv("GRAPHDB_REPOSITORY", "test")

        captured_env: dict = {}

        def fake_popen(cmdline, *, stdout, stderr, env, text, bufsize, creationflags):
            captured_env.update(env)
            proc = MagicMock()
            proc.pid = 12345
            proc.stdout = iter(["Panel running on http://127.0.0.1:54321/\n"])
            return proc

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        handle = _spawn_plc(1)

        assert "GRAPHDB_URL" not in captured_env
        assert "GRAPHDB_REPOSITORY" not in captured_env
        assert handle.address == "http://127.0.0.1:54321/"

    def test_spawn_plc_echoes_its_panel_line_to_launcher_stdout(self, monkeypatch, capsys):
        """The panel address is announced synchronously (blocking read in _spawn_plc), so it
        needs its own echo point, distinct from the background-thread drain used by
        middleware/controller (#85)."""

        def fake_popen(cmdline, *, stdout, stderr, env, text, bufsize, creationflags):
            proc = MagicMock()
            proc.pid = 12345
            proc.stdout = iter(["Panel running on http://127.0.0.1:54321/\n"])
            return proc

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        _spawn_plc(1)

        out = capsys.readouterr().out
        assert "plc-1" in out
        assert "http://127.0.0.1:54321/" in out


@requires_graphdb
class TestLiveSeeding:
    """This class tests seed_factory and factory_is_live with a live GraphDB."""

    def test_seed_factory_creates_units(self, graphdb):
        """seed_factory creates every requested TransferUnit."""
        from kapps_ogm import OGM

        ogm = OGM(db=graphdb)
        seed.seed_factory(graphdb, ogm, units=2)

        for n in (1, 2):
            result = graphdb.query(
                f'ASK {{ <{seed._mint_transfer_unit_iri(n)}> a <{seed.TRANSFER_UNIT_CLASS}> }}'
            )
            assert result.get("boolean", False), f"TransferUnit{n} should exist"

    def test_seed_writes_each_unit_s_own_broker_port_as_an_integer(self, graphdb):
        """#79's acceptance: unit 2's parameters declare port 18832, as xsd:integer -- the
        round trip must hand back 18832, not "18832" (ADR 0031's rule, extended per-unit)."""
        from kapps_ogm import OGM
        from kapps_semantic_middleware.vocabulary import INF

        ogm = OGM(db=graphdb)
        seed.seed_factory(graphdb, ogm, units=2)

        belt = seed._mint_conveyor_belt_iri(2, "left")
        result = graphdb.query(
            f"""
            SELECT ?port WHERE {{
                <{belt}> <{seed.TU_HAS_CONVEYOR_SPEED}> ?param .
                ?param <{INF.hasMQTTBrokerPort}> ?port .
            }}
            """,
            convert_bindings=True,
        )
        bindings = result.get("results", {}).get("bindings", [])
        assert len(bindings) == 1
        assert bindings[0]["port"] == 18832

    def test_seed_writes_a_human_readable_label_for_each_unit_and_the_control_station(
        self, graphdb
    ):
        """Every seeded individual carries an rdfs:label (#89 item 5).

        Controller.discover_resources binds ?label straight off rdfs:label; before this
        was seeded, the SPARQL OPTIONAL never bound and discovery returned label=None for
        every unit. #82 (not yet built) renders unit identity on every card, so this
        seeds a real name rather than drop the field from ResourceInfo.
        """
        from kapps_ogm import OGM
        from rdflib.namespace import RDFS

        ogm = OGM(db=graphdb)
        seed.seed_factory(graphdb, ogm, units=2)

        for n in (1, 2):
            result = graphdb.query(
                f"""
                SELECT ?label WHERE {{
                    <{seed._mint_transfer_unit_iri(n)}> <{RDFS.label}> ?label .
                }}
                """,
                convert_bindings=True,
            )
            bindings = result.get("results", {}).get("bindings", [])
            assert len(bindings) == 1, f"TransferUnit{n} should carry exactly one rdfs:label"
            assert str(bindings[0]["label"]) == f"TransferUnit {n}"

        result = graphdb.query(
            f"""
            SELECT ?label WHERE {{
                <{seed.CONTROL_STATION}> <{RDFS.label}> ?label .
            }}
            """,
            convert_bindings=True,
        )
        bindings = result.get("results", {}).get("bindings", [])
        assert len(bindings) == 1
        assert str(bindings[0]["label"]) == "Control Station"

    def test_factory_is_live_detects_fresh_heartbeat(self, graphdb):
        """factory_is_live returns a Service that carries a fresh heartbeat."""
        from datetime import datetime, timezone

        from kapps_ogm import OGM
        from rdflib import XSD, Literal

        from kapps_semantic_middleware.vocabulary import SVC

        ogm = OGM(db=graphdb)
        seed.seed_factory(graphdb, ogm, units=1)

        service_iri = IRI("http://example.org/test_service")
        graphdb.triple_add((service_iri, SVC.isServiceOf, seed._mint_transfer_unit_iri(1)))
        graphdb.triple_add((service_iri, SVC.address, "http://localhost:8000"))
        graphdb.triple_add(
            (
                service_iri,
                SVC.lastHeartbeat,
                Literal(datetime.now(timezone.utc).isoformat(), datatype=XSD.dateTime),
            )
        )

        live = seed.factory_is_live(graphdb)
        assert len(live) >= 1
