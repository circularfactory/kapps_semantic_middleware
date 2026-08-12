# The scenario-3 TransferUnit ontology

Design notes and change history for `examples/transferunit.ttl`. The Turtle file itself is **plain RDF
with no comment blocks, and classes only** — everything that explains *why* it looks the way it does
lives here, and its instances are created through the OGM by `seed.seed_scenario3`.

## How scenario 3 is seeded

Three layers, deliberately separated:

| Layer | Where it lives | Why |
|---|---|---|
| Published / general ontologies — Core, `svc:`, `mes:` | **one named graph per ontology** (`…/Core`, `…/Service`, `…/MES`) | needed by every scenario and never changed during a run. `clear_repository` clears only the default graph, so a seed wipes the scenario own data and leaves these standing. GraphDB reasons across all graphs, so `tu:TransferUnit rdfs:subClassOf cfc:Resource` still resolves |
| TransferUnit **classes** | `examples/transferunit.ttl`, default graph | the domain TBox: the `inf:` interface vocabulary and the `tu:` terms |
| TransferUnit **instances** | created by `seed.seed_scenario3` via `ogm.create` | the seed then exercises the same validated write path a running middleware uses (root ADR 0008) instead of asserting Turtle behind the OGM back |

Core is **vendored** at `src/kapps_semantic_middleware/ontology/core.ttl` — a verbatim copy of the
published 0.9.0 release — rather than fetched at seed time, so seeding stays reproducible offline per
examples ADR 0001. It is imported and specialized, never modified (ADR 0012). The local `cfc:` class
stubs the Turtle used to carry are gone: Core now loads in full.

**No exceptions to root ADR 0008.** The seed writes every triple through `ogm.create`, connection
metadata included. This was not true until `SAWeindel/kapps_ogm#7` landed: the write path serialized
only what a property *own* range restriction declared, so a topic, set topic or broker passed to
`ogm.create` was silently dropped (#52), and the seed had to fall back to a targeted SPARQL
`INSERT … WHERE` in a helper called `_attach_connection_metadata` — a documented exception to root
ADR 0008. `#7` made `PropertySpec.specify` walk `rdfs:subPropertyOf*` and merge the anonymous
restriction ranges it finds, so the effective shape of `tu:hasConveyorSpeed` now resolves all six of
`inf:hasValue`, `tu:hasUnit`, `inf:accessMode`, `inf:hasMQTTTopic`, `inf:hasMQTTSetTopic` and
`inf:hasMQTTBrokerIP`. The helper and the exception were deleted on 2026-07-29. The metadata is now
passed in `ogm.create` `data`, and `tests/test_scenario3_seed_integration.py` — unchanged, because it
always asserted the resulting graph rather than the mechanism — verifies it live.

## What it is

Self-contained ground-truth ontology for **scenario 3 (TransferUnit) only**: a mock PLC to middleware to
controller loop over a TransferUnit with two conveyor belts and two light barriers (map #24). The
conveyor speeds are settable control variables. The light barriers are read-only occupancy sensors.
All four are reached over MQTT.

**Self-contained by design.** `seed.py` clears the repository before every seed and loads only the
scenario own Turtle, and GraphDB does not dereference `owl:imports`. So the file declares every term
the scenario needs and imports nothing.

## Structure

| Block | Contents |
|---|---|
| Interface vocabulary (`inf:`) | The two interface marker properties with their range restrictions, plus the parameter-node metadata terms. |
| Resource types (`tu:`) | `TransferUnit`, `ConveyorBelt`, `LightBarrier` — topology carried over from the sfb1574 `tu:` example. |
| Composition properties | `hasConveyorBelt`, `hasLightBarrier`. |
| Parameter properties | `hasConveyorSpeed`, `isOccupied`, `hasUnit`. |
| Service type | `tu:TransferUnitService`. |

## The two TBoxes stay separate and are joined at runtime

The domain ontology says exactly two things about a parameter:

1. **What it is** — its `rdfs:range`: a value, a unit. Domain content.
2. **That it is interface-accessible** — `rdfs:subPropertyOf` a protocol interface property. A marker,
   and an **optional** one (ADR 0026).

It says nothing about what MQTT connection metadata looks like. That TBox lives on the `inf:` interface
properties, and the two are joined **at runtime** by the middleware: walking `rdfs:subPropertyOf*` and
merging the anonymous ranges it finds. That is the entailment the reasoner does not materialise —
rdfs7 then rdfs3 place the value node in *both* range classes, hence in their intersection. Not by
inheritance: RDFS entails no range triple for a subproperty, and GraphDB materialises none (measured:
0 inherited ranges with `include_implicit=True` on both repositories).

This keeps the authoritative sfb1574 shape intact — `tu:hasConveyorSpeed` own restriction still
declares `inf:hasValue` + `tu:hasUnit` and nothing else — while giving the connector contract a home
that twenty domain engineers never have to restate.

## The projection is merge depth

A consumer merging only up to `inf:isInterfaceAccessibleParameter` should get value, unit and access
mode. One that also merges the protocol subproperty gets topic, set topic and broker.

**The ordering is the design. The merge is not selectable.** `PropertySpec._resolve_effective_ranges`
walks the *entire* `rdfs:subPropertyOf*` chain, and no merge-depth parameter exists on `specify`,
`get_class_spec` or `fetch` — so every ClassSpec is the deep one, and a materialized parameter *does*
carry the broker address. Measured live, 2026-07-29. The middleware therefore realizes the shallow view
itself, by pruning the southbound properties out of the ClassSpec before fetching (**ADR 0028**). An
earlier reading — "the restriction is the projection, no filtering step needed" — held only while the
`inf:` interface properties had no ranges of their own, which the 2026-07-28 change below deliberately
ended. `SAWeindel/kapps_ogm#8` would let the OGM produce the shallow shape directly and retire the
prune.

**The ordering is load-bearing:** northbound-safe content on the parent property, connection details on
the protocol subproperty, never the other way round. Putting a topic or broker on the parent property
would leak it to every peer that GETs the resource.

## Every restriction uses `owl:allValuesFrom`

Under the Open World Assumption an existential says a value exists *in the world*, not that the triple
is in this graph. `kapps_ogm` turns `someValuesFrom` into a **required** pydantic field, which is a
closed-world reading of an open-world axiom. `allValuesFrom` types the property without requiring it.
Requiredness belongs to SHACL `sh:minCount` (ADR 0025; `SAWeindel/kapps_ogm#3`, `#11`).

The `inf:` metadata terms carry **no `rdfs:domain`**: they appear only inside range restrictions, from
which `kapps_ogm` derives their types directly. Giving them a domain would make them separately
specifiable, and their missing global range would then raise.

## The subproperty marker is optional

It is stated in this file because scenario 3 hardware has a fixed protocol, which is exactly the case
it is for. Own-built hardware whose protocol is not known at deployment time, or two machines of one
class on different protocols, simply omit it — the interface is then resolved from the instance own
metadata or from embedding code (ADR 0026).

## Instance data

**Topic scheme** (an instance convention, not baked into the classes — ADR 0023):
`TransferUnit<n>/<component>/<position>/<param>`, with a setpoint appending `_set`. The
mock PLC (`demo/transferunits/plc/transfer_unit.py`) publishes 4 topics and subscribes to 2. Broker
`127.0.0.1`, no auth — a local test
broker. A real deployed broker with auth and TLS is out of scope for map #24.

**Lifecycle.** In production these parameter nodes are written by the **middleware** when the resource
is first set up: that is the moment the interface connection metadata is joined to the domain
parameter, and from then on the middleware works ABox-only. Seeding them here stands in for "this
TransferUnit was instantiated by a previous run", which is what lets the scenario exercise the
connector self-registration path from a cold start. #54 replaces the stand-in with the real flow.

**No values — this scenario is a locator** (ADR 0024). The graph records *where* a value lives (unit,
access mode, topic, broker) and never the value itself. The live value exists only in the datamodel and
over REST. The `inf:hasValue` literals in the upstream example were test scaffolding from before the
middleware existed. A parameter that has not been observed yet materialises as an empty list, which
`NodeValidator` accepts under the OWA. A domain whose data changes slowly may instead *commit* its
values. The middleware is agnostic between the two patterns.

**No SHACL shapes.** Deferred with the rest of SHACL Interop. The setter payload shape (e.g. speed
within `[0, maxSpeed]`) is explicitly out of map #24 scope.

## Namespace (provisional)

The `inf:` terms are authored under the existing CrcInterfaces IRI, which already defines `hasValue`,
`hasMQTTTopic`, `hasMQTTBrokerIP` and the `isInterfaceAccessible*` hierarchy — so the TransferUnit stays
vocabulary-compatible with the minimal example shared across `kapps_triplestore_interface` and `kapps_ogm`.
CrcInterfaces is deprecated; the consolidation capstone (#39) re-homes these terms under the `inf:` name
it mints, which is one constant in `vocabulary.py` plus this file (ADR 0021).

## A TransferUnit is a "unit" because of the view, not the ontology

A belt is a part of a unit because the middleware user view is **rooted** at the unit and reaches the
belt — not because of any part-of semantics here. Rootedness is a property of the view (ADR 0018).
Likewise the Service: one Service per **middleware instance**, not per resource, since several
instances may be bound to one TransferUnit, each owning its own Service node, address and heartbeat
(ADR 0022). The Service, its endpoint and its heartbeat are minted at runtime by registration and are
never authored here (root ADR 0003 of the core context: the ontology is ground truth for types, not for
runtime state).

## Change history

Root ADR 0001 requires a detailed changelog for ontology changes. It lives here now rather than in the
Turtle.

### 2026-07-29 — the seed last raw-SPARQL write is gone

No change to the Turtle. This records the seed catching up to it. The 2026-07-28 entry below noted that
the declared MQTT metadata "takes effect only once `SAWeindel/kapps_ogm#7` lands". It landed, and the
effect was measured against the live store before anything was deleted: with the metadata passed in
`ogm.create` `data`, a settable belt parameter comes back carrying `hasUnit`, `accessMode`,
`hasMQTTTopic`, `hasMQTTSetTopic` and `hasMQTTBrokerIP`, and a read-only barrier parameter carries
`accessMode`, `hasMQTTTopic` and `hasMQTTBrokerIP` and no set topic — exactly what the old SPARQL
`INSERT` produced. `_attach_connection_metadata` was then deleted along with its root-ADR-0008
exception, and `examples/seed.py` now has no raw write path at all.

The scenario-3 integration tests needed no edit, which is the useful part: they assert the shape of the
seeded graph, never how the triples got there, so they were already the acceptance test for this
change.

This is an enabling step for **#54**, not #54 itself. The seed still *authors* the connection metadata;
#54 is the middleware provisioning it at first setup and writing back what it wired. What has changed is
that the write path #54 needs now exists and is proven.

### 2026-07-28 (#53) — the interface TBox gets its own ranges, the domain TBox gets smaller

From ADR 0025/0026, grilled under #52.

- **`inf:isInterfaceAccessibleParameter` and `inf:isInterfaceAccessibleMQTTParameter` gained
  `rdfs:range` restrictions.** The 2026-07-27 correction below declared the MQTT terms "vocabulary
  only … they must never enter a range restriction", reasoning that a restriction would materialise a
  broker address into the served datamodel. That reasoning assumed one shape per property. ADR 0026
  replaces it with merge depth, so the broker is still physically absent northbound while the metadata
  is now **declared** — which is what lets provisioning write it through the OGM at all. Without this,
  no OGM write path can put a topic on a parameter: it is dropped at materialisation, silently, with
  one warning. Takes effect only once `SAWeindel/kapps_ogm#7` lands. The file is correct ahead of it,
  the merged shape is simply not computed yet.
- **`inf:accessMode` moved out** of both parameter restrictions onto
  `inf:isInterfaceAccessibleParameter`. It is generic interface content — every interface-accessible
  parameter has an access mode — so restating it per parameter made twenty domain engineers carry a
  fact that is not theirs.
- **Every `owl:someValuesFrom` became `owl:allValuesFrom`.** Since the 2026-07-27 locator rewrite
  removed every `inf:hasValue` literal, materialising a seeded belt raises
  `ValidationError: inf-hasValue Field required` — the file and the code disagreed. Verified offline
  against the pinned pydantic; **not** reproduced end-to-end, because the file cannot currently be
  loaded into GraphDB (#55).

The `rdfs:subPropertyOf` markers stay and are now documented as optional. Instance data is unchanged:
parameter nodes keep their metadata and still carry no `inf:hasValue` literals.

### 2026-07-28 — split into classes, instances and shared modules

The file became **classes only**. Its instance data moved into `seed.seed_scenario3`, which creates the
TransferUnit, two belts and two barriers through `ogm.create`; the local `cfc:` stubs were deleted in
favour of the full published Core; and Core, `svc:` and `mes:` moved into one named graph each. See
*How scenario 3 is seeded* above. Covered by `tests/test_scenario3_seed_integration.py`, which is the
first thing to exercise this ontology against a live store.

### 2026-07-28 — comment blocks removed

All `#` comment blocks were stripped from the Turtle and moved into this document. The file is now
plain RDF. `rdfs:comment` triples remain, since they are data the OGM reads into `ClassSpec.comment`.

### 2026-07-27 (#25) — corrected: the TBoxes must not be connected

The first rewrite put the whole MQTT contract (topic, set topic, broker) into each parameter property
`rdfs:range`. That was wrong on two counts: it duplicated protocol TBox into the domain ontology, so
every domain engineer would restate the MQTT contract on every parameter; and it would materialise the
broker address into the served datamodel, which is precisely the bypass the IT-OT boundary exists to
prevent.

Upstream declares only `inf:hasValue` + `tu:hasUnit`, comments the property "MQTT-accessible speed
attribute of a conveyor belt (value and unit)", and puts `inf:hasMQTTTopic` in no restriction anywhere.

*Partially superseded on 2026-07-28:* the conclusion that connection metadata must live outside every
restriction is replaced by merge depth. What survives is the requirement it protected — a broker
address must never reach a northbound consumer — and the rule that a domain ontology never restates the
protocol contract.

Also corrected then: an earlier warning claimed a range on an `inf:` superproperty would give every
subproperty two ranges. It would not, for the RDFS reason recorded above.

### 2026-07-27 (#25) — rewritten against ADR 0015–0024

The 2026-07-23 draft modelled states as `svc:StateProperty` subclasses with their own Capability classes
and dropped the parameter blank nodes entirely. Every one of those decisions was reversed:

- **Removed** `tu:ConveyorSpeedProperty` / `tu:LightBarrierProperty`. ADR 0015 retires StateProperty
  into `inf:InterfaceAccessibleParameter`: a state **is** a protocol-interface parameter, one node
  carrying value + unit + the metadata a connector needs to reach the device. Settability is a facet
  (`inf:accessMode`), not a subclass.
- **Removed** `tu:ConveyorSpeedCapability` / `tu:LightBarrierCapability`. States have no capabilities.
  The authoritative sfb1574 ontology has none either, and the draft carried two "this is wrong
  modeling, delete" markers saying so. A light barrier does not have a "light-barrier capability".
- **Restored** the parameter blank nodes the draft dropped, but without values (locator pattern).
- **Added** the `inf:` interface-property hierarchy and the MQTT connection metadata (ADR 0023), which
  is what lets a connector register itself from the graph (ADR 0020/0023).
- **Added** `tu:hasConveyorSpeed` / `tu:isOccupied` as parameter properties.

### 2026-07-23 (#25) — initial draft

Superseded by the rewrite above.

## Resolved: why this file could not be loaded (#55)

For a while every write of a `tu:` predicate returned `500 - Unexpected exception`, through the import
endpoint and `INSERT DATA` alike, and scenario 3 could not be seeded at all. The cause had nothing to do
with the ontology: **two stale Kafka connector instances** in the `Tests` repository. GraphDB
connector plugin re-indexes an entity whenever a watched predicate changes, and with no broker to reach
it threw `com.ontotext.trree.plugin.externalsync.api.ConnectorServerException`, which RDF4J wrapped as a
`SailException` and the REST layer flattened to an opaque 500.

Dropping both connectors fixed it. Worth remembering, because the symptom is thoroughly misleading: it
looks like malformed data, it survives every simplification of the file, and it does not reproduce on a
different namespace. If a write starts failing with a bare 500, list the connectors first:

```sparql
SELECT ?c ?s WHERE { ?c <http://www.ontotext.com/connectors/kafka#listConnectors> ?s }
```

Two incidental corrections from the same investigation: the ~70 triples that survive `clear_graph()` are
GraphDB ruleset axioms, not residue; and a bisect that lands on a blank-node triple does not mean
blank nodes are the problem — here it was a coincidence of statement ordering.
