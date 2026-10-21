# TransferUnit Factory

This module is one of five contexts in this repository. See `/CONTEXT-MAP.md` at the root for the others.

This is a runnable multi-process demonstration. It stands up a small factory of TransferUnits and a
controller, and a person watches it from a browser. Each participant is a separate process.

**The demo observes.** In KAPPS a device changes only through a workflow of its service. This demo
has no unit workflows yet, so nothing in it writes to a device through a unit's served data model:
the controller reads, and the station board shows what it reads. Its control returns with
workflows after the paper.

Its decisions are development-repository material and do not ship; what they decided is
described, without the deliberation, in `docs/mechanics/`.

**Scenario parts live in the demo.** Every part of a scenario lives in this directory. The
library holds generic functions only. The controller and the TransferUnit resource logic are parts
of scenario 3. The monitor is also a part of scenario 3, and it is not built yet (milestone 2).

## Language

**Factory**:
The whole demonstration as it runs: N TransferUnits, a controller, a Watchdog, and the Launcher that
starts them. A monitor joins in milestone 2. It is not built.
_Avoid_: demo, scenario 3, setup

**Unit index**:
This is the integer from 1 to N that identifies one TransferUnit. Every IRI and every MQTT topic of that
unit derives from it.
_Avoid_: unit number, unit id, n

**Launcher**:
This process builds the initial situation and starts every other process. It is plain
infrastructure and it never appears in the knowledge graph.
Its options are `--dataset` and `--no-watchdog`; the control station has its own `--dataset`.
_Avoid_: supervisor, orchestrator, bootstrapper

**Watchdog**:
This is the one watchdog-mode middleware process the Launcher starts beside the control station
(`--no-watchdog` leaves it out). It registers no Service, so it has no address on the index page,
only a state.
_Avoid_: monitor (a separate, unbuilt part), reaper

**Runner**:
This entry point serves exactly one instance of resource-mode middleware for one TransferUnit.
_Avoid_: middleware script, worker

**Control station**:
This is the controller's Resource individual in the graph. It carries no controllable parameter, and
it appears in its own discovery list. It registers a Service, so it stays discoverable, and it
registers no Workflow, so it holds no Capability and an Operation never resolves to it.
The monitoring station will be the same shape for the monitor, in milestone 2.
_Avoid_: controller resource, planner, operator station

**TransferUnit Expert**:
The domain expert who builds the machine and configures a middleware for it. Their job ends when the
unit is discoverable and addressable factory-wide. They never learn what consumes it.
_Avoid_: device vendor, integrator

**Control Expert**:
A domain expert who consumes units they did not build. They write a view as a SPARQL query, fetch
each hit's datamodel, and read it from their own objects, which the controller's REST read legs
keep current. They change no unit: that takes a workflow of the unit's service, which this demo
does not have yet. They know the TransferUnit ontology and nothing about any particular unit.
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
A mock PLC serves this UI for itself. There is one per PLC process. It sets the device's own setpoints
and barriers, as an operator at the machine would, and is the one place a belt moves in this demo.
Distinct from the controller UI, which reads units through the middleware.
_Avoid_: device UI, unit page

**Live factory**:
A state of the graph, not of the host: a Service carries an address and a heartbeat inside the
staleness window. The Launcher refuses to clear a live factory.
_Avoid_: running factory, active factory
