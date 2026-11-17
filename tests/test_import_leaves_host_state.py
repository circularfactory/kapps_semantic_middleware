"""An import of KSM leaves the root logger and the event-loop policy as the host set them.

The framework that KSM still imports configures the root logger when it is imported, and on
Windows its MQTT module sets a process-wide event-loop policy. KSM's package init undoes both.

Each check runs in a fresh interpreter, because this process imported KSM long ago. Each runs
twice: once with the package as the first import, once with a submodule, because Python runs the
package init for either.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

ENTRIES = ["kapps_semantic_middleware", "kapps_semantic_middleware.connectors"]


def _run(code: str) -> dict[str, object]:
    """Run `code` in a fresh interpreter and return the JSON object that its last line prints."""
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result: dict[str, object] = json.loads(completed.stdout.strip().splitlines()[-1])
    return result


@pytest.mark.parametrize("entry", ENTRIES)
def test_an_unconfigured_root_logger_stays_unconfigured(entry: str) -> None:
    result = _run(
        f"""
        import json, logging
        root = logging.getLogger()
        before = (list(root.handlers), root.level)
        import {entry}
        print(json.dumps({{"same": (list(root.handlers), root.level) == before,
                           "handlers": len(root.handlers), "level": root.level}}))
        """
    )
    assert result == {"same": True, "handlers": 0, "level": 30}


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("level", ["DEBUG", "WARNING"])
def test_a_root_logger_configured_before_the_import_keeps_its_configuration(
    entry: str, level: str
) -> None:
    result = _run(
        f"""
        import json, logging, sys
        root = logging.getLogger()
        handler = logging.StreamHandler(sys.stdout)
        root.addHandler(handler)
        root.setLevel(logging.{level})
        import {entry}
        print(json.dumps({{"only_own": root.handlers == [handler],
                           "level": logging.getLevelName(root.level)}}))
        """
    )
    assert result == {"only_own": True, "level": level}


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_configuration_made_after_the_import_holds_as_made(entry: str) -> None:
    result = _run(
        f"""
        import io, json, logging
        import {entry}
        stream = io.StringIO()
        logging.basicConfig(level=logging.DEBUG, stream=stream, format="%(name)s %(message)s")
        root = logging.getLogger()
        logging.getLogger("kapps_semantic_middleware.anything").debug("seen")
        print(json.dumps({{"handlers": len(root.handlers), "level": root.level,
                           "text": stream.getvalue()}}))
        """
    )
    assert result == {
        "handlers": 1,
        "level": 10,
        "text": "kapps_semantic_middleware.anything seen\n",
    }


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_ksm_record_reaches_the_hosts_root_handler_at_the_hosts_level(entry: str) -> None:
    result = _run(
        f"""
        import io, json, logging
        stream = io.StringIO()
        logging.basicConfig(level=logging.INFO, stream=stream, format="%(levelname)s %(message)s")
        import {entry}
        log = logging.getLogger("kapps_semantic_middleware.anything")
        log.debug("below the host's level")
        log.info("at the host's level")
        print(json.dumps({{"text": stream.getvalue()}}))
        """
    )
    assert result == {"text": "INFO at the host's level\n"}


@pytest.mark.parametrize("entry", ENTRIES)
def test_on_windows_the_import_leaves_the_event_loop_policy_in_effect(entry: str) -> None:
    """The platform is made to look like Windows, and `asyncio` gets the Windows policy class.

    The modules KSM imports are imported first on the real platform, except KSM and the
    framework, so that nothing else branches on the faked platform. The framework's MQTT
    module imports its Windows names from `asyncio` when it runs, so the stand-ins it finds
    there are the ones it uses; it swallows an import error, so a missing name would only hide
    the effect, and the check that the stand-in was set guards against that.
    """
    result = _run(
        f"""
        import json, subprocess, sys
        listing = subprocess.run(
            [sys.executable, "-c",
             "import sys, {entry}; print('\\\\n'.join(sorted(sys.modules)))"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        import importlib
        for name in listing:
            if name.startswith(("kapps_semantic_middleware", "transitional_sync_middleware")):
                continue
            try:
                importlib.import_module(name)
            except Exception:
                pass

        import asyncio
        class StandIn(asyncio.DefaultEventLoopPolicy):
            pass
        real_set = asyncio.set_event_loop_policy
        set_calls = []
        def recording_set(policy):
            set_calls.append(type(policy).__name__)
            real_set(policy)
        asyncio.WindowsSelectorEventLoopPolicy = StandIn
        asyncio.set_event_loop_policy = recording_set

        hosts_policy = asyncio.DefaultEventLoopPolicy()
        real_set(hosts_policy)
        sys.platform = "win32"
        import {entry}
        sys.platform = "linux"
        print(json.dumps({{"framework_set_stand_in": "StandIn" in set_calls,
                           "kept": asyncio.get_event_loop_policy() is hosts_policy}}))
        """
    )
    assert result == {"framework_set_stand_in": True, "kept": True}


@pytest.mark.parametrize("entry", ENTRIES)
def test_on_the_real_platform_the_import_calls_no_policy_function(entry: str) -> None:
    result = _run(
        f"""
        import asyncio, json
        calls = []
        real_get, real_set = asyncio.get_event_loop_policy, asyncio.set_event_loop_policy
        asyncio.get_event_loop_policy = lambda: (calls.append("get"), real_get())[1]
        asyncio.set_event_loop_policy = lambda p: (calls.append("set"), real_set(p))[1]
        import {entry}
        print(json.dumps({{"calls": calls}}))
        """
    )
    assert result == {"calls": []}
