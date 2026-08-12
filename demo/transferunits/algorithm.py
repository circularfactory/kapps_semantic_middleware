"""The Control Expert's own algorithm code for the TransferUnit factory demo.

This module is the only place in the demo's **runtime control path** allowed to name
domain terms like ``tu:TransferUnit``, ``tu:hasConveyorBelt``. The claim is narrower than
"the only place in this demo", which it used to say and which was never true: ``seed.py``
names them throughout, as it must -- it stands in for the TransferUnit Expert writing the
factory into the graph, which is the other half of ADR 0033's two experts (#93 item 5).
The line this draws is between the *Control* Expert's code and the generic mechanism it
runs on. The generic ``Controller`` mechanism
in ``controller.py`` deliberately knows nothing about any domain class -- it executes
the view, wires connectors, and loads datamodels, but the *what* (which class, which
heuristic) lives here, not there. This separation realizes ADR 0033's core claim: the
Control Expert and the TransferUnit Expert never meet, and neither imports the other's
code. The knowledge graph is the entire contract.

The algorithm itself is deliberately meaningless -- it demonstrates reach, not real
control. It reads a barrier on one unit and echoes that unit's conveyor speed onto
another unit's conveyor ("set a speed from a random other unit's speed", per ADR 0033
step 5). A real material flow controller would route parcels across units by reading
barriers and deciding which unit to trigger; this demo does none of that because
building a real one is domain work, not middleware work. The graph carries no plant
layout, so nothing here can route anything, and that is the MVP boundary rather than
an oversight.

The background loop's own runtime knobs -- mode, pause, tick length (#82) -- are
:class:`AlgorithmState`, not module state: a demo runs one controller per process, but
nothing here should assume that, so the state a station-board route flips lives on an
object ``control_station.py`` constructs and threads through, not on this module.

Usage::

    # In control_station.py's main():
    hits = controller.view(build_view_query())
    controller.wire_view(hits, class_scope=unit_class_scope())
    state = AlgorithmState(tick_seconds=args.tick)
    # ... server starts, datamodels load into controller.units ...
    # Background loop runs run_algorithm_loop(controller, state).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from kapps_triplestore_interface import IRI
from kapps_ogm.utils.class_scope import ClassScope

from kapps_semantic_middleware.vocabulary import INF, SVC

from . import seed
from .controller import Controller

logger = logging.getLogger(__name__)

WATCH_INTERVAL_SECONDS = 0.5
"""How often event-driven mode samples the loaded units' light barriers for a change,
and how often either mode re-checks whether it may run at all (paused, or a view
rebuild in progress). Deliberately much finer than any tick -- this is a cheap read of
objects already held in memory (``controller.units``), not a network call."""


class AlgorithmMode(str, enum.Enum):
    """The two ways the demonstration algorithm's background loop can fire (#82).

    A plain ``str`` subclass so a mode round-trips through JSON with no translation
    layer between the loop, the page's toggle and a test's assertion.
    """

    TIMED = "timed"
    """A tick every ``AlgorithmState.tick_seconds``, visible and periodic."""

    EVENT_DRIVEN = "event_driven"
    """Quiescent until a light barrier's reading changes, then exactly one reaction."""


@dataclass
class AlgorithmState:
    """Runtime knobs for the background loop, shared between it and every station-board
    route that reads or flips one (#82).

    One instance is constructed in ``control_station.py``'s ``main()`` and threaded
    through to both ``run_algorithm_loop`` and ``station_board.configure_board`` --
    the single shared object is what lets a page toggle and the loop agree with no
    polling of each other. Pause/mode are plain mutable fields rather than
    ``asyncio.Event``s: both are read once per watch tick (every
    ``WATCH_INTERVAL_SECONDS``), which is already fine-grained enough that a flag the
    loop notices next tick costs nothing a demonstration would ever notice.
    """

    tick_seconds: float
    """The timed mode's interval. Must exceed one lap of PUT -> unit middleware -> MQTT
    -> PLC -> MQTT back -> connector read (#82's own acceptance criterion), or the
    algorithm writes again before it can observe its last write and the board
    oscillates.

    The default is still *reasoned* rather than measured -- see ``control_station.py``'s
    ``--tick``. #94 (a belt's setpoint overwritten mid-ramp by a sibling's fan-out echo)
    stops a belt short of its command, so no honest lap time can be taken until it is
    fixed; #82's "measure one lap" criterion stays open on that."""

    mode: AlgorithmMode = AlgorithmMode.TIMED
    """Which of the two firing rules is currently active. Switchable at runtime."""

    paused: bool = False
    """The human-facing global pause (#82: "pause to drive"). While true, neither mode
    fires a tick, and every set control on the page is enabled -- station_board.py
    reads this one field to decide which state to show; there is no second flag to keep
    in sync with it."""

    last_tick_at: Optional[float] = None
    """``time.monotonic()`` of the last completed tick, or ``None`` before the first
    one. Lets the page distinguish "about to tick" from "never ticked yet"."""

    waiting_since: Optional[float] = None
    """Event-driven mode only: ``time.monotonic()`` of when it started watching for a
    change with none seen yet. ``None`` whenever timed mode is active, or the instant
    after a reaction fires. Backs the page's explicit "waiting for a change" state
    (#82: idle must not read as broken)."""


def _barrier_snapshot(controller: Controller) -> Dict[str, Tuple[bool, ...]]:
    """``{unit_iri: (occupied_front_or_only_barrier, ...)}`` for every loaded unit, for
    event-driven mode's own change detection.

    Reads ``controller.units`` directly -- the same objects the REST connectors' own
    background poll keeps current -- rather than issuing any request of its own; this
    function makes no network call and touches no domain object the algorithm itself
    could not already reach.
    """
    snapshot: Dict[str, Tuple[bool, ...]] = {}
    for unit_iri, unit in controller.units.items():
        barriers = getattr(unit, seed.TU_HAS_LIGHT_BARRIER.lined, None) or []
        readings = []
        for barrier in barriers:
            occupied_param = getattr(barrier, seed.TU_IS_OCCUPIED.lined, None) or []
            if occupied_param:
                value = getattr(occupied_param[0], INF.hasValue.lined, None) or []
                readings.append(bool(value[0]) if value else False)
        snapshot[unit_iri] = tuple(readings)
    return snapshot


def build_view_query() -> str:
    """Build the SPARQL SELECT query that finds live, even-indexed TransferUnits.

    This realizes ADR 0033 step 1: the view is the query, and the query text supplies
    the domain class and any heuristic. ``Controller.view()`` runs this verbatim and
    returns every IRI it binds to ``?resource``. Nothing in ``controller.py`` assumes
    a domain class or a column beyond ``?resource`` itself.

    The heuristic here -- unit index is even -- is deliberately arbitrary. It is chosen
    because it visibly returns roughly half the factory when N units are seeded, making
    the demo's behavior easy to verify by inspection. A real algorithm would narrow by
    area of the plant, topology, or workload instead.

    Live means the resource's Service carries ``svc:address``. The query joins from
    ``?resource`` to its Service via ``svc:isServiceOf``, then requires ``?addr`` to be
    bound. The unit index is extracted from the IRI's local name (e.g.
    ``...#TransferUnit4`` -> ``4``) and tested for evenness arithmetically: SPARQL 1.1
    has no ``%`` modulo operator, so this computes
    ``xsd:integer(?suffix) - 2 * FLOOR(xsd:integer(?suffix) / 2)``, which is 0 for an
    even suffix and 1 for an odd one.

    Returns:
        A SPARQL ``SELECT ?resource`` query string.
    """
    return f"""
    PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
    SELECT ?resource WHERE {{
        ?resource a <{seed.TRANSFER_UNIT_CLASS}> .
        ?svc <{SVC.isServiceOf}> ?resource .
        ?svc <{SVC.address}> ?addr .
        BIND(STRAFTER(STR(?resource), "#TransferUnit") AS ?suffix)
        FILTER(?suffix != "" &&
               (xsd:integer(?suffix) - 2 * FLOOR(xsd:integer(?suffix) / 2)) = 0)
    }}
    """


def unit_class_scope() -> ClassScope:
    """Return the ClassScope that roots at tu:TransferUnit and reaches belt/barrier parameters.

    This is the Control Expert's own view of one hit's shape (ADR 0018, ADR 0033). It must
    match what the unit's own middleware uses (``demo/transferunits/middleware.py``'s own
    ``class_scope``), because the expert needs to see the same nested parameters the unit
    exposes. Built from ``seed``'s constants so the demo package remains self-contained
    (ADR 0030: "the duplication ends in its own commit").

    Returns:
        A ClassScope with two property chains: belt->speed and barrier->occupied.
    """
    return ClassScope.from_property_chains(
        [
            [seed.TU_HAS_CONVEYOR_BELT, seed.TU_HAS_CONVEYOR_SPEED],
            [seed.TU_HAS_LIGHT_BARRIER, seed.TU_IS_OCCUPIED],
        ]
    )


def _first_belt_speed(unit, unit_iri: IRI, role: str):
    """``(belt, speed_parameter)`` for a unit's first conveyor belt, or ``(None, None)``.

    Collapses what were four near-identical ``getattr`` -> ``if not`` -> ``warning`` ->
    ``return None`` blocks, twice over for source and target (#93 item 5). It also takes
    the three-deep walk with it: reaching a value means
    ``getattr(getattr(belts[0], ...)[0], INF.hasValue.lined)``, and repeating that by hand
    is how the two copies drifted in the first place.

    ``role`` is the word the warning uses ("Source" / "Target"), so a skipped tick still
    says *which* unit was incomplete -- the whole value of the four separate messages.
    """
    belts = getattr(unit, seed.TU_HAS_CONVEYOR_BELT.lined, None) or []
    if not belts:
        logger.warning("%s unit %s has no conveyor belts; skipping tick.", role, unit_iri)
        return None, None

    speed_param = getattr(belts[0], seed.TU_HAS_CONVEYOR_SPEED.lined, None) or []
    if not speed_param:
        logger.warning(
            "%s belt on %s has no speed parameter; skipping tick.", role, unit_iri
        )
        return None, None

    return belts[0], speed_param[0]


async def run_algorithm_once(controller: Controller) -> Optional[IRI]:
    """Execute one tick of the demonstration algorithm (ADR 0033 step 5).

    Deliberately meaningless -- it demonstrates reach, not real control. Picks two
    distinct unit IRIs at random from ``controller.units``, reads a barrier on the
    source (proving it was reachable), reads the source's conveyor speed, and echoes
    that speed onto the target's conveyor ("set a speed from a random other unit's
    speed", per ADR 0033). No HTTP call happens in this function's body -- only
    ``controller.push`` drives the assignment out.

    If fewer than 2 units are loaded, returns ``None`` and logs at DEBUG that there was
    nothing to drive yet. Otherwise logs at INFO the barrier reading and the
    source/target IRIs and speed value pushed.

    Args:
        controller: The Controller instance whose ``units`` dict holds the loaded
            datamodels.

    Returns:
        The target IRI that was pushed, or ``None`` if fewer than 2 units were loaded
        or either unit's shape was incomplete.
    """
    if len(controller.units) < 2:
        logger.debug("Fewer than 2 units loaded; nothing to drive yet.")
        return None

    unit_iris = list(controller.units.keys())
    source_iri_str, target_iri_str = random.sample(unit_iris, 2)
    source_iri = IRI(source_iri_str)
    target_iri = IRI(target_iri_str)

    source_unit = controller.units[source_iri_str]
    target_unit = controller.units[target_iri_str]

    # Read the source unit's first light barrier's isOccupied value (read-only, to
    # prove reachability per ADR 0033 step 4's "read a barrier").
    source_barriers = getattr(source_unit, seed.TU_HAS_LIGHT_BARRIER.lined)
    if source_barriers:
        occupied_param = getattr(source_barriers[0], seed.TU_IS_OCCUPIED.lined)
        if occupied_param:
            occupied_value = getattr(occupied_param[0], INF.hasValue.lined)
            logger.info("Barrier on %s reads %s", source_iri, occupied_value)

    # Read the source unit's first conveyor belt's current hasConveyorSpeed value.
    _, source_speed_param = _first_belt_speed(source_unit, source_iri, "Source")
    if source_speed_param is None:
        return None
    speed_value = list(getattr(source_speed_param, INF.hasValue.lined))

    # Echo the source's speed onto the target unit's first conveyor belt. A fresh list
    # (not the source's own) -- the two units must not end up sharing one mutable list.
    target_belt, target_speed_param = _first_belt_speed(target_unit, target_iri, "Target")
    if target_speed_param is None:
        return None
    setattr(target_speed_param, INF.hasValue.lined, speed_value)

    # Record what this write is asking for before driving it out (#82): the served
    # datamodel carries only the observed value (ADR 0024's locator pattern), so this is
    # the only place the station board could ever learn "commanded" from, for a write
    # the algorithm made and no human's browser initiated.
    controller.writes.record_commanded(
        target_belt.id, seed.TU_HAS_CONVEYOR_SPEED.lined, speed_value, origin="algorithm"
    )

    # Drive the assignment out -- no HTTP call here, push() does the plumbing.
    #
    # A failed write is recorded and swallowed, not raised: the algorithm's tick runs in a
    # background loop that catches only CancelledError, so letting a dead unit's PUT
    # propagate would silently end the loop while the board went on counting down to a
    # tick that will never come. Recording it puts the same `rejected` badge on the row a
    # human's failed write gets (#82), which is the visible half of the same event.
    try:
        await controller.push(target_iri)
    except Exception as exc:
        controller.writes.record_rejected(
            target_belt.id, seed.TU_HAS_CONVEYOR_SPEED.lined, str(exc)
        )
        logger.warning("Algorithm write to %s was rejected: %s", target_iri, exc)
        return target_iri

    logger.info("Echoed speed %s from %s onto %s", speed_value, source_iri, target_iri)
    return target_iri


async def run_algorithm_loop(controller: Controller, state: AlgorithmState) -> None:
    """Run the algorithm in the background, honouring ``state``'s mode and pause (#82).

    An ``async def`` with ``try: while True: ... except asyncio.CancelledError: pass`` --
    the exact pattern ``SemanticMiddleware`` uses for its own heartbeat (see
    ``middleware.py``'s ``_heartbeat_loop``).

    Two firing rules:

    - **Timed**: one tick every ``state.tick_seconds``, waited out in
      ``WATCH_INTERVAL_SECONDS``-sized chunks so a pause, a rebuild or a runtime mode
      switch is noticed well inside one tick rather than only after it.
    - **Event-driven**: samples ``controller.units``' light barriers every
      ``WATCH_INTERVAL_SECONDS`` and fires exactly one tick the instant a reading
      differs from the previous sample -- never on the very first sample after a mode
      switch or a startup, since there is nothing yet to compare it against.

    Both rules are gated the same way on every watch tick: ``state.paused`` (the
    human's "pause to drive") and ``controller.rebuild_lock.locked()`` (#82: "the
    algorithm auto-pauses across a rebuild and resumes" -- the lock already marks
    exactly a rebuild's duration, so this needs no second flag to track that
    separately, and cannot drift out of step with it).

    Args:
        controller: The Controller instance to run the algorithm against.
        state: The shared runtime knobs -- constructed once in ``control_station.py``.
    """
    previous_barrier_snapshot: Dict[str, Tuple[bool, ...]] = {}
    try:
        while True:
            if state.paused or controller.rebuild_lock.locked():
                await asyncio.sleep(WATCH_INTERVAL_SECONDS)
                continue

            if state.mode is AlgorithmMode.TIMED:
                state.waiting_since = None
                # Sleep BEFORE ticking, not after: entering timed mode (at startup, or on
                # a runtime mode switch) must not fire an immediate reaction -- a viewer
                # who just switched to timed mode watches one full, visible tick_seconds
                # elapse first, the same way the interval reads on every tick after the
                # first.
                #
                # Sleep in WATCH_INTERVAL_SECONDS-sized chunks rather than one bare
                # `asyncio.sleep(state.tick_seconds)`: a single long sleep is exactly as
                # slow to notice a pause, a rebuild or a runtime mode switch as
                # `tick_seconds` itself, which is 8s by default and the issue's own
                # example widens it to 15s for a presentation -- "the toggle switches
                # between them at runtime" (#82's own acceptance criterion) must not mean
                # "eventually, once the current tick finishes". Chunking below
                # WATCH_INTERVAL_SECONDS costs nothing when tick_seconds is itself
                # shorter (the `min` collapses to one chunk, unchanged from a bare sleep).
                elapsed = 0.0
                interrupted = False
                while elapsed < state.tick_seconds:
                    chunk = min(WATCH_INTERVAL_SECONDS, state.tick_seconds - elapsed)
                    await asyncio.sleep(chunk)
                    elapsed += chunk
                    if (
                        state.paused
                        or controller.rebuild_lock.locked()
                        or state.mode is not AlgorithmMode.TIMED
                    ):
                        interrupted = True
                        break
                if interrupted:
                    continue
                await run_algorithm_once(controller)
                state.last_tick_at = time.monotonic()
                continue

            # Event-driven: quiescent until a barrier reading changes (#82: idle must
            # show an explicit "waiting for a change" state rather than read as broken).
            if state.waiting_since is None:
                state.waiting_since = time.monotonic()
            snapshot = _barrier_snapshot(controller)
            if previous_barrier_snapshot and snapshot != previous_barrier_snapshot:
                await run_algorithm_once(controller)
                state.last_tick_at = time.monotonic()
                state.waiting_since = None
            previous_barrier_snapshot = snapshot
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        pass
