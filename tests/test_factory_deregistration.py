"""A clean stop takes every unit's reachability out of the graph (#66, #106).

This is the box #66 calls "the observable proof that ADR 0029's process shape turned on the
deregistration #65 documents as dead", and until #106 nothing asserted it. The smoke suite
stops its children through the launcher and says in a comment that this is "so every
middleware deregisters", then never asks the graph.

**#66's box is worded wrongly, and this file asserts the design instead.** The box says "the
graph holds no ``svc:Service`` for any unit". It does hold one, on purpose:
``registration.deregister_service`` clears the address and the workflow/state endpoints and
says in its own docstring that "structural triples and rdf:type are preserved (paper:
availability vs. existence)". A Service that stopped answering has not stopped existing. So
what a clean stop must remove is **reachability**, and that is what is checked below.

Its own module rather than a case inside ``test_factory_smoke.py``, for two reasons that are
both about the launcher's fixed port (ADR 0029 -- it is the only bookmarkable address, so it
cannot be dynamic). That file's ``factory`` fixture is module-scoped and holds the port for
the whole file, so a second factory cannot exist beside it, and a test asserting on state
*after* teardown has no fixture left to run in. Modules are torn down before the next one
starts, so a separate file gets the port to itself.

One unit, not two: this proves deregistration happens, and #79's per-unit isolation is
already proven next door.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterator, List

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import requires_graphdb  # noqa: E402
from demo.transferunits import seed  # noqa: E402
from demo.transferunits.__main__ import LAUNCHER_HOST, LAUNCHER_PORT  # noqa: E402
from demo.transferunits.middleware import _listening  # noqa: E402
from kapps_semantic_middleware.vocabulary import SVC  # noqa: E402

# The two waiting helpers come from the smoke suite rather than being written again. They are
# the same helpers because the failure they have to describe is the same one: a factory that
# did not do something within a budget, quoting what the launcher said.
from test_factory_smoke import _await, _url  # noqa: E402

pytestmark = [requires_graphdb, pytest.mark.live, pytest.mark.smoke]

UNITS = 1
"""One unit is enough to show a Service losing its address. Two would only repeat it."""

READY_TIMEOUT_SECONDS = 180.0
"""Matches the smoke suite's own boot budget: a cold GraphDB seed dominates it."""

STOP_TIMEOUT_SECONDS = 60.0
"""A clean stop waits on the ordered teardown of ADR 0029, not on a signal."""


def _kill_factory(proc: subprocess.Popen) -> None:
    """Take down the launcher *and everything it spawned*, whatever state the test left.

    Killing only the launcher is not enough. It is started with ``start_new_session=True``,
    so its five children sit in their own process group and outlive it -- they keep unit 1's
    broker port bound, and that port is also ``conftest.py``'s ``MQTT_TEST_PORT``. Orphans
    therefore do not merely linger: they make twelve unrelated tests in the *non-live* tier
    fail with "Broker startup failed" on the next run. Measured, after a run of this file
    that deliberately skipped the clean stop.

    So the process group is the fallback, and it fires on every path out of the fixture --
    including the paths where ``/api/stop`` was never reached because a wait timed out.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass

    try:
        proc.wait(timeout=30.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            pass


def _unit_service_addresses(db) -> List[str]:
    """Every `svc:address` currently attached to a Service of one of this factory's units.

    Asks by unit IRI rather than by Service IRI, because the Service IRI is minted per
    middleware instance (ADR 0022) and this test never learns it. Empty means no unit of
    this factory is reachable.
    """
    found: List[str] = []
    for index in range(1, UNITS + 1):
        unit_iri = seed._mint_transfer_unit_iri(index)
        rows = db.query(
            f"""
            SELECT ?addr WHERE {{
                ?service <{SVC.isServiceOf}> <{unit_iri}> .
                ?service <{SVC.address}> ?addr .
            }}
            """,
            convert_bindings=True,
        )
        bindings = rows.get("results", {}).get("bindings", []) if isinstance(rows, dict) else []
        found.extend(str(row["addr"]) for row in bindings)
    return found


@pytest.fixture
def stopped_factory(graphdb) -> Iterator[None]:
    """Boot a factory, wait until its unit is reachable, then stop it through the launcher.

    Yields once the stop has returned. The test that follows reads the graph, so the point of
    this fixture is the *transition*: it asserts the unit was reachable first, which is what
    makes the absence afterwards mean something rather than being the state of an empty graph.
    """
    launcher_url = f"http://{LAUNCHER_HOST}:{LAUNCHER_PORT}/"

    if _listening(LAUNCHER_HOST, LAUNCHER_PORT):
        pytest.skip(
            f"Something already answers on {LAUNCHER_HOST}:{LAUNCHER_PORT}, the launcher's "
            "fixed port. This test cannot mean anything against a factory it did not start."
        )

    proc = subprocess.Popen(
        [sys.executable, "-m", "demo.transferunits", "--units", str(UNITS), "--force"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(Path(__file__).resolve().parents[1]),
        env=os.environ.copy(),
        start_new_session=True,
    )

    lines: List[str] = []

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))

    threading.Thread(target=_drain, daemon=True).start()

    def _tail() -> str:
        return "\n".join(lines[-25:]) or "(the launcher said nothing)"

    try:
        _await(
            lambda: _listening(LAUNCHER_HOST, LAUNCHER_PORT),
            READY_TIMEOUT_SECONDS,
            f"The launcher never bound its port. It said:\n{_tail()}",
        )

        # The unit has to be reachable *before* the stop, or the assertion afterwards would
        # pass against a factory that never registered at all.
        _await(
            lambda: bool(_unit_service_addresses(graphdb)),
            READY_TIMEOUT_SECONDS,
            f"No unit ever registered an svc:address, so a later absence would prove "
            f"nothing. The launcher said:\n{_tail()}",
        )

        httpx.post(_url(launcher_url, "api/stop"), timeout=STOP_TIMEOUT_SECONDS)
        yield
    finally:
        _kill_factory(proc)


def test_a_clean_stop_leaves_no_unit_reachable(stopped_factory, graphdb):
    """#66's "observable proof": after a clean stop, no unit advertises an address.

    The fixture has already established that a unit *was* reachable, so this is a
    transition and not a statement about an empty graph.

    A short retry rather than a single read: the stop route returns when the children have
    been told to go, and each middleware's own deregistration is a graph write that lands
    just after. Nothing here waits on a heartbeat sweep -- that is ADR 0007's separate
    mechanism, and a clean stop must not need it.
    """
    _await(
        lambda: _unit_service_addresses(graphdb) == [],
        STOP_TIMEOUT_SECONDS,
        "A unit still advertises an svc:address after a clean stop through the launcher. "
        "ADR 0029's ordered teardown exists so every middleware deregisters while its PLC "
        "still answers; something in that order did not happen.",
    )


def test_the_service_individual_survives_its_deregistration(stopped_factory, graphdb):
    """Availability is not existence: the Service is still there, it just answers nowhere.

    Guards the correction this file was written to make. #66's acceptance box asks for "no
    `svc:Service` for any unit", and satisfying that literally would mean deleting the
    individual -- which `deregister_service` deliberately does not do. If someone later
    "fixes" the box by deleting Services, this test fails and says why.
    """
    unit_iri = seed._mint_transfer_unit_iri(1)
    rows = graphdb.query(
        f"SELECT ?service WHERE {{ ?service <{SVC.isServiceOf}> <{unit_iri}> . }}",
        convert_bindings=True,
    )
    bindings = rows.get("results", {}).get("bindings", []) if isinstance(rows, dict) else []

    assert bindings, (
        "The unit's Service individual is gone from the graph. Deregistration removes "
        "reachability, not existence -- registration.deregister_service preserves the "
        "structural triples and rdf:type on purpose. A consumer must still be able to see "
        "that this resource has a Service which is currently unreachable."
    )
