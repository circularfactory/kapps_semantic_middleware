"""Offline tests for the launcher seeding logic.

This module tests the IRI minting scheme, the MQTT topic construction, and the
environment handling without a live store. This module mirrors the style of
test_recursive_rest_router.py.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from kapps_triplestore_interface import IRI

from demo.transferunits import seed
from demo.transferunits.launcher import _spawn_plc, _strip_store_env

from conftest import requires_store  # noqa: E402


class TestIRIMinting:
    """This class tests the index-derived IRI scheme."""

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
    """This class tests the MQTT topic construction."""

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
    """This class tests broker_port(n) = 18830 + n.

    Not 1883 + n: 1900 is SSDP/UPnP and is live on most Linux desktops.
    """

    def test_unit_1_is_18831(self):
        assert seed.BROKER_PORT_BASE == 18830
        assert seed.broker_port(1) == 18831

    def test_unit_2_is_18832(self):
        assert seed.broker_port(2) == 18832

    def test_follows_the_index_pattern(self):
        assert seed.broker_port(17) == 18847


# vendor-check: off -- the deprecated variables, until they go.
# The deprecated variables, which the PLC must not receive either.
OLD_STORE_ENV = {
    "GRAPHDB_URL": "http://localhost:7200",
    "GRAPHDB_USERNAME": "admin",
    "GRAPHDB_PASSWORD": "secret",
    "GRAPHDB_REPOSITORY": "test",
}
# vendor-check: on


class TestEnvironmentHandling:
    """This class tests that _strip_store_env strips the store's credentials for PLC children."""

    def test_strip_store_env_removes_all_store_vars(self):
        """_strip_store_env removes every TRIPLESTORE_* key, and every deprecated one, and keeps
        the rest."""
        env = {
            "TRIPLESTORE_URL": "http://localhost:7200",
            "TRIPLESTORE_USERNAME": "admin",
            "TRIPLESTORE_PASSWORD": "secret",
            "TRIPLESTORE_DATASET": "test",
            **OLD_STORE_ENV,
            "OTHER_VAR": "kept",
        }
        stripped = _strip_store_env(env)

        assert stripped == {"OTHER_VAR": "kept"}

    def test_strip_store_env_handles_empty(self):
        """_strip_store_env works on an empty dict."""
        assert _strip_store_env({}) == {}

    def test_spawn_plc_passes_stripped_env_to_popen(self, monkeypatch):
        """_spawn_plc's actual Popen call carries no store variable ("asserted, not assumed").

        test_strip_store_env_removes_all_store_vars only exercises the helper in isolation;
        this asserts the wiring between "helper strips" and "spawn uses the stripped copy".
        """
        monkeypatch.setenv("TRIPLESTORE_URL", "http://localhost:7200")
        monkeypatch.setenv("TRIPLESTORE_DATASET", "test")
        for name, value in OLD_STORE_ENV.items():
            monkeypatch.setenv(name, value)

        captured_env: dict = {}

        def fake_popen(cmdline, *, stdout, stderr, env, text, bufsize, creationflags):
            captured_env.update(env)
            proc = MagicMock()
            proc.pid = 12345
            proc.stdout = iter(["Panel running on http://127.0.0.1:54321/\n"])
            proc.wait.return_value = 0  # the stand-in ends cleanly once its line is read
            return proc

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        handle = _spawn_plc(1)

        assert not set(captured_env) & {"TRIPLESTORE_URL", "TRIPLESTORE_DATASET", *OLD_STORE_ENV}
        assert handle.address == "http://127.0.0.1:54321/"

    def test_spawn_plc_echoes_its_panel_line_to_launcher_stdout(self, monkeypatch, capsys):
        """The panel address is announced synchronously (blocking read in _spawn_plc), so it
        needs its own echo point, distinct from the background-thread drain used by
        middleware/controller."""

        def fake_popen(cmdline, *, stdout, stderr, env, text, bufsize, creationflags):
            proc = MagicMock()
            proc.pid = 12345
            proc.stdout = iter(["Panel running on http://127.0.0.1:54321/\n"])
            proc.wait.return_value = 0  # the stand-in ends cleanly once its line is read
            return proc

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        _spawn_plc(1)

        out = capsys.readouterr().out
        assert "plc-1" in out
        assert "http://127.0.0.1:54321/" in out


@requires_store
class TestLiveSeeding:
    """This class tests seed_factory and factory_is_live with a live store."""

    def test_seed_factory_creates_units(self, store):
        """seed_factory creates every requested TransferUnit."""
        from kapps_ogm import OGM

        ogm = OGM(db=store)
        seed.seed_factory(store, ogm, units=2)

        for n in (1, 2):
            result = store.query(
                f'ASK {{ <{seed._mint_transfer_unit_iri(n)}> a <{seed.TRANSFER_UNIT_CLASS}> }}'
            )
            assert result.get("boolean", False), f"TransferUnit{n} should exist"

    def test_seed_writes_each_unit_s_own_broker_port_as_an_integer(self, store):
        """Unit 2's parameters declare port 18832, as xsd:integer -- the
        round trip must hand back 18832, not "18832" (the default-port rule, extended per-unit)."""
        from kapps_ogm import OGM
        from kapps_semantic_middleware.vocabulary import INF

        ogm = OGM(db=store)
        seed.seed_factory(store, ogm, units=2)

        belt = seed._mint_conveyor_belt_iri(2, "left")
        result = store.query(
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
        self, store
    ):
        """Every seeded individual carries an rdfs:label.

        Controller.discover_resources binds ?label straight off rdfs:label; before this
        was seeded, the SPARQL OPTIONAL never bound and discovery returned label=None for
        every unit. The station board renders unit identity on every card, so this
        seeds a real name rather than drop the field from ResourceInfo.
        """
        from kapps_ogm import OGM
        from rdflib.namespace import RDFS

        ogm = OGM(db=store)
        seed.seed_factory(store, ogm, units=2)

        for n in (1, 2):
            result = store.query(
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

        result = store.query(
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

    def test_factory_is_live_detects_fresh_heartbeat(self, store):
        """factory_is_live returns a Service that carries a fresh heartbeat."""
        from datetime import datetime, timezone

        from kapps_ogm import OGM
        from rdflib import XSD, Literal

        from kapps_semantic_middleware.vocabulary import SVC

        ogm = OGM(db=store)
        seed.seed_factory(store, ogm, units=1)

        service_iri = IRI("http://example.org/test_service")
        store.triple_add((service_iri, SVC.isServiceOf, seed._mint_transfer_unit_iri(1)))
        store.triple_add((service_iri, SVC.address, "http://localhost:8000"))
        store.triple_add(
            (
                service_iri,
                SVC.lastHeartbeat,
                Literal(datetime.now(timezone.utc).isoformat(), datatype=XSD.dateTime),
            )
        )

        live = seed.factory_is_live(store)
        assert len(live) >= 1
