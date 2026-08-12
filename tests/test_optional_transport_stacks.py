"""The connector modules import and recognise with their transport stacks absent (#93 item 4).

Three acceptance claims from #69 and #77 were true by inspection and held by nothing:

- an instance with ``autoregister_connectors=False`` starts with no ``aiomqtt`` installed
- an instance with no REST stack installed still constructs
- ``ensure_transport`` runs *before* the first connector for that address is built

The first two are the ADR 0028 rule that recognition and the northbound projection must not
depend on a transport: the least-privileged wiring would otherwise be the one that could not
run. Both mechanisms are marked ``# pragma: no cover`` because they depend on an optional
install, and the existing REST test patches ``httpx`` *in* and never out -- so the absent-stack
path had never executed in this suite.

**The absence checks run in a subprocess, and that is not incidental.** The first version of
this file hid the modules in-process and reloaded them afterwards. Reloading builds *new class
objects*, so ``test_rest_binding.py`` -- imported earlier, holding the old ``RESTBinding`` and
``RESTParameterConnector``, and patching ``httpx`` onto whichever module object it captured --
began failing three tests and then hung the run outright on the polling loop. A test that
proves something about *import time* cannot do it by mutating the import table of a process
that has already imported everything. A fresh interpreter is the only honest way to ask the
question, and it costs about a second.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


def _run_with_hidden(hidden: tuple[str, ...], body: str) -> subprocess.CompletedProcess:
    """Run ``body`` in a new interpreter where ``hidden`` top-level packages are absent,
    and hand back the result for the caller to judge.

    Absence is enforced by a ``sys.meta_path`` finder rather than by deleting from
    ``sys.modules``, so it holds for the whole run rather than until something imports the
    package again.

    Separate from :func:`_in_a_fresh_interpreter` because one test here *expects* the
    subprocess to fail, and needs to read the failure rather than be failed by it.
    """
    script = textwrap.dedent(f"""
        import sys

        HIDDEN = {hidden!r}

        class _Blocker:
            def find_module(self, name, path=None):
                return self.find_spec(name, path)

            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in HIDDEN:
                    raise ImportError(f"No module named {{name!r}} (hidden by this test)")
                return None

        sys.meta_path.insert(0, _Blocker())

    """) + textwrap.dedent(body)

    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _in_a_fresh_interpreter(hidden: tuple[str, ...], body: str) -> None:
    """As :func:`_run_with_hidden`, but the subprocess is required to succeed. Its own
    traceback becomes the failure message."""
    result = _run_with_hidden(hidden, body)
    if result.returncode != 0:
        pytest.fail(
            f"subprocess with {hidden} hidden failed:\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


class TestTheModulesImportWithoutTheirStack:
    def test_mqtt_binding_imports_with_no_aiomqtt(self):
        """#69's box. The descriptor must still exist, so recognition and the ADR 0028
        projection run on a host that cannot talk MQTT at all."""
        _in_a_fresh_interpreter(
            ("aiomqtt",),
            """
            from kapps_semantic_middleware.connectors import mqtt_binding

            assert mqtt_binding.MqttClientConnector is None, (
                "aiomqtt was not actually hidden, so this proves nothing"
            )
            assert mqtt_binding.MQTTBinding.interface_property is not None
            assert mqtt_binding.MQTTBinding.connection_metadata, (
                "the descriptor lost its declared connection metadata without its stack"
            )
            """,
        )

    def test_httpx_is_not_actually_optional_so_77s_box_cannot_be_met(self):
        """**#77's acceptance box is false, not merely unasserted** -- #93 item 4 recorded it
        as "believed true, nothing pins it", and writing the pin is what showed it is not
        true.

        *"An instance with no REST stack installed still constructs."* It does not.
        ``rest_binding``'s own guard is correct in isolation, but importing the module pulls
        in ``transitional_sync_middleware.connect.connectors.aas_client_connector``, which does a bare
        ``import httpx`` at module level. So with httpx absent the import fails several
        frames before ``rest_binding``'s ``try`` is reached, and the ``# pragma: no cover``
        on that guard is honest: the branch is unreachable while ``transitional_sync_middleware`` is a
        dependency.

        **And that upstream import is undeclared** (found 2026-08-07, while correcting #77's
        record). ``httpx`` appears nowhere in ``transitional_sync_middleware``'s own manifest -- not as a
        dependency, not as an extra -- while ``import transitional_sync_middleware`` reaches that module
        eagerly. It is the same class of sibling defect this project's ``pyproject.toml``
        already names for ``aiomqtt``: importing a module the manifest never mentions. Filed
        rather than fixed here: declaring it would make the manifest honest without making
        #77's box any more meetable, and making the import lazy upstream is a feature change
        rather than the bugfix root ADR 0001 permits.

        This test pins the *actual* state, so that making httpx genuinely optional upstream
        turns this red and sends the reader back to #77's box rather than leaving a claim
        nobody rechecks. Contrast the MQTT case above, which really is optional and really
        does degrade the way its ticket says.
        """
        result = _run_with_hidden(
            ("httpx",),
            "from kapps_semantic_middleware.connectors import rest_binding\n",
        )

        assert result.returncode != 0, (
            "importing rest_binding with httpx hidden now succeeds -- httpx has become "
            "genuinely optional, so #77's acceptance box is newly meetable and this test "
            "should be replaced by the real absence check"
        )
        assert "import httpx" in result.stderr, (
            f"expected the failure to be a bare httpx import, got:\n{result.stderr}"
        )
        assert "transitional_sync_middleware" in result.stderr, (
            "the import no longer fails inside transitional_sync_middleware, so the reason recorded in "
            f"this test's docstring has changed:\n{result.stderr}"
        )

    def test_the_rest_failure_is_deferred_and_actionable(self, monkeypatch):
        """Absent is not silent. The error names the package and the way out, because the
        alternative is a connector that fails at first contact with no explanation.

        Exercised by swapping the module global rather than by hiding the package, since
        the test above establishes that hiding it never reaches this code. That is a weaker
        test than #77 wanted and it is the strongest one available: it proves the message,
        not the import.
        """
        from kapps_semantic_middleware.connectors import rest_binding

        monkeypatch.setattr(rest_binding, "httpx", None)

        with pytest.raises(ImportError) as excinfo:
            rest_binding._httpx_module()

        message = str(excinfo.value)
        assert "httpx" in message
        assert "autoregister_connectors=False" in message


class TestTransportComesUpBeforeItsConnectors:
    """In-process on purpose: this one swaps a module attribute and puts it back, which is
    safe, unlike reloading the module."""

    def test_ensure_transport_runs_before_the_connector_is_built(self):
        """#69's ordering box. The tests in `test_scenario3_wiring_integration.py` prove the
        call *count* and the address; nothing proved the order, which is the entire reason
        ADR 0034 made the hook synchronous -- `plan_wiring` runs in the constructor, before
        any event loop exists, so a connector built first would find no broker listening.

        Asserted on one shared log, so "before" is a fact about sequence rather than two
        independent counts that happen to agree.
        """
        from kapps_semantic_middleware.connectors import mqtt_binding

        events: list[tuple[str, object]] = []

        class _RecordingConnector:
            def __init__(self, broker, topic, port=None, **kwargs):
                events.append(("connector", (broker, port)))

        def ensure_transport(host: str, port: int) -> None:
            events.append(("transport", (host, port)))

        original = mqtt_binding.MqttClientConnector
        mqtt_binding.MqttClientConnector = _RecordingConnector
        try:
            binding = _mqtt_binding_for(host="127.0.0.1", port=18831)
            list(
                mqtt_binding.MQTTBinding.build(
                    binding,
                    mqtt_binding.SyncDirection.BIDIRECTIONAL,
                    ensure_transport=ensure_transport,
                )
            )
        finally:
            mqtt_binding.MqttClientConnector = original

        assert events, "nothing was recorded, so the ordering claim is untested"
        assert events[0][0] == "transport", (
            f"a connector was built before its transport came up: {events}"
        )
        first_connector = next(
            (i for i, (kind, _) in enumerate(events) if kind == "connector"), None
        )
        assert first_connector is not None, "no connector was built"
        assert all(
            kind == "transport" for kind, _ in events[:first_connector]
        ), f"expected every transport call before the first connector: {events}"


def _mqtt_binding_for(*, host: str, port: int):
    """A minimal recognised MQTT parameter, built by hand.

    By hand rather than off the graph because this test is about call ordering inside
    ``build``, and a live fixture would drag GraphDB into a question that has nothing to do
    with it. ``root_iri`` is left ``None``, which ``ParameterBinding`` documents as the
    by-hand case.
    """
    from kapps_triplestore_interface import IRI

    from kapps_semantic_middleware.connectors.semantic import ParameterBinding
    from kapps_semantic_middleware.connectors.mqtt_binding import MQTTBinding
    from kapps_semantic_middleware.vocabulary import INF

    return ParameterBinding(
        resource_iri=IRI("http://example.org/Belt"),
        parameter_property=IRI("http://example.org/hasSpeed"),
        field_id="hasSpeed",
        metadata={
            str(INF.hasMQTTTopic): ["unit/belt/speed"],
            str(INF.hasMQTTSetTopic): ["unit/belt/speed_set"],
            str(INF.hasMQTTBrokerIP): [host],
            str(INF.hasMQTTBrokerPort): [port],
            str(INF.accessMode): ["readwrite"],
        },
        descriptor=MQTTBinding,
        node_model_type=dict,
    )
