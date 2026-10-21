"""The Control Expert's own algorithm code for the TransferUnit factory demo.

This module is the only place in the demo's **runtime control path** allowed to name
domain terms like ``tu:TransferUnit``, ``tu:hasConveyorBelt``. The claim is narrower than
"the only place in this demo", which it used to say and which was never true: ``seed.py``
names them throughout, as it must -- it stands in for the TransferUnit Expert writing the
factory into the graph, which is the other half of the two-expert split.
The line this draws is between the *Control* Expert's code and the generic mechanism it
runs on. The generic ``Controller`` mechanism
in ``controller.py`` deliberately knows nothing about any domain class -- it executes
the view, wires connectors, and loads datamodels, but the *what* (which class, which
heuristic) lives here, not there. This separation realizes the connector seam's core claim: the
Control Expert and the TransferUnit Expert never meet, and neither imports the other's
code. The knowledge graph is the entire contract.

The algorithm itself is deliberately meaningless -- it demonstrates reach, not real
control. When a light barrier's reading changes, it reads a barrier on one unit and the
conveyor speed of another, and logs both. **It observes and writes nothing.** In KAPPS a
device changes only through a workflow of its service; this demo has no unit workflows
yet, so it has no control to exercise. Its control returns with workflows after the paper.
A real material flow controller would route
parcels across units by reading barriers and deciding which unit to trigger; building one
is domain work, not middleware work. The graph carries no plant layout, so nothing here
can route anything, and that is the MVP boundary rather than an oversight.

The background loop's one runtime knob -- pause -- is :class:`AlgorithmState`, not module
state: a demo runs one controller per process, but nothing here should assume that, so the
state a station-board route flips lives on an object ``control_station.py`` constructs and
threads through, not on this module.

Usage::

    # In control_station.py's main():
    hits = controller.view(build_view_query())
    controller.wire_view(hits, class_scope=unit_class_scope())
    state = AlgorithmState()
    # ... server starts, datamodels load into controller.units ...
    # Background loop runs run_algorithm_loop(controller, state).
"""

from __future__ import annotations

import asyncio
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
"""How often the loop samples the loaded units' light barriers for a change, and
re-checks whether it may run at all (paused, or a view rebuild in progress). This is a
cheap read of objects already held in memory (``controller.units``), not a network
call."""


@dataclass
class AlgorithmState:
    """Runtime knobs for the background loop, shared between it and every station-board
    route that reads or flips one.

    One instance is constructed in ``control_station.py``'s ``main()`` and threaded
    through to both ``run_algorithm_loop`` and ``station_board.mount_onto`` --
    the single shared object is what lets a page toggle and the loop agree with no
    polling of each other. Pause is a plain mutable field rather than an
    ``asyncio.Event``: it is read once per watch tick (every ``WATCH_INTERVAL_SECONDS``),
    which is already fine-grained enough that a flag the loop notices next tick costs
    nothing a demonstration would ever notice.
    """

    paused: bool = False
    """The human-facing global pause. While true, no reaction fires."""

    last_reaction_at: Optional[float] = None
    """``time.monotonic()`` of the last completed reaction, or ``None`` before the first
    one."""

    waiting_since: Optional[float] = None
    """``time.monotonic()`` of when the loop started watching for a change with none seen
    yet. ``None`` the instant after a reaction fires. Backs the page's explicit "waiting
    for a change" state (idle must not read as broken)."""


def _barrier_snapshot(controller: Controller) -> Dict[str, Tuple[bool, ...]]:
    """``{unit_iri: (occupied_front_or_only_barrier, ...)}`` for every loaded unit, for
    the loop's own change detection.

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

    This realizes the Control Expert's step 1: the view is the query, and the query text supplies
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

    This is the Control Expert's own view of one hit's shape. It must
    match what the unit's own middleware uses (``demo/transferunits/middleware.py``'s own
    ``class_scope``), because the expert needs to see the same nested parameters the unit
    exposes. Built from ``seed``'s constants so the demo package remains self-contained
    ("the duplication ends in its own commit").

    Returns:
        A ClassScope with two property chains: belt->speed and barrier->occupied.
    """
    return ClassScope.from_property_chains(
        [
            [seed.TU_HAS_CONVEYOR_BELT, seed.TU_HAS_CONVEYOR_SPEED],
            [seed.TU_HAS_LIGHT_BARRIER, seed.TU_IS_OCCUPIED],
        ]
    )


def _first_belt_speed(unit, unit_iri: IRI):
    """The speed parameter of a unit's first conveyor belt, or ``None``.

    Reaching a value means ``getattr(getattr(belts[0], ...)[0], INF.hasValue.lined)``;
    this takes the first two steps of that walk and warns when one of them finds nothing.
    """
    belts = getattr(unit, seed.TU_HAS_CONVEYOR_BELT.lined, None) or []
    if not belts:
        logger.warning("Unit %s has no conveyor belts; skipping reaction.", unit_iri)
        return None

    speed_param = getattr(belts[0], seed.TU_HAS_CONVEYOR_SPEED.lined, None) or []
    if not speed_param:
        logger.warning("Belt on %s has no speed parameter; skipping reaction.", unit_iri)
        return None

    return speed_param[0]


async def run_algorithm_once(controller: Controller) -> Optional[IRI]:
    """Execute one reaction of the demonstration algorithm.

    Deliberately meaningless -- it demonstrates reach, not real control. Picks two
    distinct unit IRIs at random from ``controller.units``, reads a barrier on the
    source and the conveyor speed of the target, and logs both. It reads only objects the
    controller's read legs keep current, and it writes nothing: no assignment, no
    connector, no HTTP call.

    If fewer than 2 units are loaded, returns ``None`` and logs at DEBUG that there was
    nothing to observe yet.

    Args:
        controller: The Controller instance whose ``units`` dict holds the loaded
            datamodels.

    Returns:
        The target IRI whose speed was read, or ``None`` if fewer than 2 units were
        loaded or the target's shape was incomplete.
    """
    if len(controller.units) < 2:
        logger.debug("Fewer than 2 units loaded; nothing to observe yet.")
        return None

    unit_iris = list(controller.units.keys())
    source_iri_str, target_iri_str = random.sample(unit_iris, 2)
    source_iri = IRI(source_iri_str)
    target_iri = IRI(target_iri_str)

    source_unit = controller.units[source_iri_str]
    target_unit = controller.units[target_iri_str]

    # Read the source unit's first light barrier's isOccupied value.
    source_barriers = getattr(source_unit, seed.TU_HAS_LIGHT_BARRIER.lined)
    if source_barriers:
        occupied_param = getattr(source_barriers[0], seed.TU_IS_OCCUPIED.lined)
        if occupied_param:
            occupied_value = getattr(occupied_param[0], INF.hasValue.lined)
            logger.info("Barrier on %s reads %s", source_iri, occupied_value)

    # Read the target unit's first conveyor belt's current hasConveyorSpeed value.
    target_speed_param = _first_belt_speed(target_unit, target_iri)
    if target_speed_param is None:
        return None
    speed_value = list(getattr(target_speed_param, INF.hasValue.lined))

    logger.info("Belt on %s runs at %s", target_iri, speed_value)
    return target_iri


async def run_algorithm_loop(controller: Controller, state: AlgorithmState) -> None:
    """Run the algorithm in the background, honouring ``state``'s pause.

    An ``async def`` with ``try: while True: ... except asyncio.CancelledError: pass`` --
    the exact pattern ``SemanticMiddleware`` uses for its own heartbeat (see
    ``middleware.py``'s ``_heartbeat_loop``).

    Samples ``controller.units``' light barriers every ``WATCH_INTERVAL_SECONDS`` and fires
    exactly one reaction the instant a reading differs from the previous sample -- never on
    the very first sample after a startup, since there is nothing yet to compare it
    against.

    Every watch tick is gated on ``state.paused`` and on
    ``controller.rebuild_lock.locked()`` ("the algorithm auto-pauses across a rebuild and
    resumes" -- the lock already marks exactly a rebuild's duration, so this needs no
    second flag to track that separately, and cannot drift out of step with it).

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

            # Quiescent until a barrier reading changes. Idle must show an explicit
            # "waiting for a change" state rather than read as broken.
            if state.waiting_since is None:
                state.waiting_since = time.monotonic()
            snapshot = _barrier_snapshot(controller)
            if previous_barrier_snapshot and snapshot != previous_barrier_snapshot:
                await run_algorithm_once(controller)
                state.last_reaction_at = time.monotonic()
                state.waiting_since = None
            previous_barrier_snapshot = snapshot
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        pass
