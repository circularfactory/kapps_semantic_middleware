# TransferUnit Factory

This module is one of five contexts in this repository. See `/CONTEXT-MAP.md` at the root for the others.

This is a runnable multi-process demonstration. It stands up a small factory of TransferUnits and a
controller, and a person drives it from a browser. Each participant is a separate process
(ADR 0029).

Its decisions are **ADR 0029, ADR 0030 and ADR 0032**. Those records are
development-repository material and do not ship; what they decided is described, without
the deliberation, in `docs/mechanics/`.

**Root ADR 0004 governs what belongs here.** Every part of a scenario lives in this directory. The
library holds generic functions only. The controller and the TransferUnit resource logic are parts
of scenario 3. The monitor is also a part of scenario 3, and it is not built yet (milestone 2).

## Language

**Factory**:
The whole demonstration as it runs: N TransferUnits, a controller, and the Launcher that starts
them. A monitor joins in milestone 2 (ADR 0032). It is not built.
_Avoid_: demo, scenario 3, setup

**Unit index**:
This is the integer from 1 to N that identifies one TransferUnit. Every IRI and every MQTT topic of that
unit derives from it (ADR 0030).
_Avoid_: unit number, unit id, n

**Launcher**:
This process builds the initial situation and starts every other process. It is plain
infrastructure and it never appears in the knowledge graph (ADR 0029).
_Avoid_: supervisor, orchestrator, bootstrapper

**Runner**:
This entry point serves exactly one instance of resource-mode middleware for one TransferUnit.
_Avoid_: middleware script, worker

**Control station**:
This is the controller's Resource individual in the graph. It carries no controllable parameter, and
it appears in its own discovery list. It registers a Service, so it stays discoverable, and it
registers no Workflow, so it holds no Capability and an Operation never resolves to it (ADR 0002).
The monitoring station will be the same shape for the monitor, in milestone 2.
_Avoid_: controller resource, planner, operator station

**TransferUnit Expert**:
The domain expert who builds the machine and configures a middleware for it. Their job ends when the
unit is discoverable, addressable and drivable factory-wide. They never learn what consumes it.
_Avoid_: device vendor, integrator

**Control Expert**:
A domain expert who consumes units they did not build. They write a view as a SPARQL query, fetch
each hit's datamodel, and drive it by assigning to their own objects (ADR 0033). They know the
TransferUnit ontology and nothing about any particular unit.
_Avoid_: operator, controller author

**View** (of a Control Expert):
The set of resources one consumer works on, defined by that expert's own SPARQL query and whatever
heuristic it carries. It is authored, never a class name, and two experts querying the same factory
hold different views.
_Avoid_: selection, scope (a **ClassScope** is the shape of one resource, not which resources)

**Material flow controller (MFC)**:
The specialised algorithm that routes material across many units by reading barriers and deciding
which to trigger next — in German, *Materialflussrechner*. **Not built here.** It names the kind of
thing that could be wired to the controller's interface. The demo runs a deliberately meaningless
algorithm, because the graph carries no plant layout and building a real one is domain work.
_Avoid_: describing the demo controller as an MFC

**Panel**:
A mock PLC serves this UI for itself. There is one per PLC process. Distinct from the controller UI, which
reaches units through the middleware.
_Avoid_: device UI, unit page

**Live factory**:
A state of the graph, not of the host: a Service carries an address and a heartbeat inside the
staleness window. The Launcher refuses to clear a live factory (ADR 0030).
_Avoid_: running factory, active factory
