# Core Middleware

One of five contexts in this repo — see `/CONTEXT-MAP.md` at the repo root for the others
(SHACL Interop, Example Scenarios, Module Requirements).

> This file defines the **vocabulary**. For how to *use* the mechanisms it names — and for the
> constraints that do not surface in a type signature — read `AGENTS.md` and the `docs/mechanics/`
> set beside it. The decisions behind these definitions are recorded in the development
> repository; those records are not distributed, and `docs/mechanics/` is their shipped
> replacement.

The reference implementation of the KAPPS architecture's Semantic Middleware Runtime: the
Interface-Layer component that lets a piece of Python code running on or next to a shopfloor
resource (or as a standalone service) expose functionality through the knowledge graph, and
lets other middleware instances discover and invoke that functionality via the graph rather
than via hardcoded network references. This context owns the Service/Workflow/Capability/
Operation/Mode registration and execution machinery. It delegates the actual generation and
parsing of workflow precondition/outcome SHACL shapes to the SHACL Interop context.

Built on `transitional_sync_middleware` (forked by inheritance, being incrementally reimplemented locally),
`kapps_ogm` (all knowledge graph reads/writes), and `kapps_triplestore_interface` (raw triple store
access, used directly where `kapps_ogm` has no equivalent yet, e.g. instance discovery).

## Language

**Service**:
A distributed runtime entity wrapped by a single middleware instance (e.g. a door
controller, a screwing-resource controller, a planning service). Typed via a
domain-specific subclass of `svc:Service` that must pre-exist in the ontology.
There is **one Service per middleware instance, not per Resource**: several instances may be bound to
one Resource, each owning its own Service node, address and heartbeat, all linked by
`svc:isServiceOf`. Discovery may therefore return several Services for one Resource.
_Avoid_: Middleware instance (that is the Python object; Service is its graph representation), Resource (Service *wraps* a Resource, it is not one); "the service of a resource" (there may be several).

**Connector wiring** (of a resource-mode instance):
A configuration of the one library, not a distinct class. Resource mode is a **library woven into a
domain expert's Python package**, never a monolithic server. A wiring is two facts: a **protocol**
and a **direction**. This is the only axis that describes an instance.

*Direction.* **Driving**: connectors wired bidirectionally, writes as well as reads. **Observing**:
connectors wired `TO_PERSISTENCE`, reads live values, structurally unable to write. **Inspecting**:
`autoregister_connectors=False`, nothing connected, structure and graph content only.

*Protocol.* **MQTT** reaches a device, recognised on the parameter (`inf:hasMQTTTopic`). **REST**
reaches another middleware instance over its recursive REST routes, recognised through the resource's
Service (`svc:address`). A peer middleware is a device as far as the seam is concerned.

Recognition and the **Projection** run identically in every combination, so connection metadata
never reaches a **served datamodel**.

**The log path is held to the same claim.** `activity.py` serves
this package's INFO records over HTTP, which for a while made `/activity` a way around the
projection: `connectors/mqtt_binding.py` logged each value with its topic at INFO. The topic now
sits at DEBUG on both legs, and the feed's handler filters at its own level rather than the
logger's — so widening the package logger to DEBUG for a debugging session still cannot put a
topic on the page. The INFO line names the parameter and the value, which is what the feed is
for.
_Avoid_: Flavour, retired. **Role**, retired — an instance has no property
beyond its wiring. Consumer and Resource middleware as *defined* terms; they survive only as informal
shorthand. Read-only mode, because a **Mode** is `resource`/`server`/`watchdog`, and a connector
wiring is a configuration *within* resource mode. Monitor mode.

**Transport**:
The thing a connector dials, as opposed to the connector that dials it — an MQTT broker, and
nothing else so far. A Parameter's connection metadata *names* a transport by address; it never
provides one. A middleware may be asked to **ensure** a transport exists at a declared address
before it registers the first connector aimed there, but it never holds a transport
implementation: it states the need and the deployment meets it. So a transport is the
one part of the southbound path that is neither in the graph nor in this library.
_Avoid_: Connector (the client this middleware constructs, one per topic — it *uses* a transport);
Binding / Binding descriptor (the recognition rule that builds connectors); Broker as a
general term, since it is one transport and the seam is not MQTT-shaped; Endpoint and Address,
which are northbound (`svc:address`, the recursive REST routes).

**Workflow**:
An invokable function exposed by a Service, registered with `@mw.workflow(...)`. Realizes
exactly one Capability. Typed via a domain-specific subclass of `svc:Workflow` that must
pre-exist in the ontology, carrying a SHACL shape describing its arguments (precondition)
and return value (outcome) — see the SHACL Interop context for how that shape is read/
written.
_Avoid_: Skill, Action (AAS-tradition terms for the same invocation-interface idea; KAPPS
reserves "Service" for the deployable middleware-wrapped entity and "Workflow" for what it
exposes — see the paper's explicit divergence from the AAS capability-skill-service model).

**Parameter** (interface-accessible parameter) — _supersedes StateProperty_:
A readable and/or settable state of a Resource, modelled as **one graph node** carrying its
value/unit **and** the metadata a protocol connector needs to reach the device (e.g. an MQTT
topic + broker). It carries **no named `rdf:type`** — its only types are anonymous restriction nodes,
which exist by inference and never survive an explicit-graph fetch — so it is recognised by the
**Interface property** its domain property specializes, never by its class.
The middleware's former "readable state" and the shop-floor "parameter" are one thing seen from
two directions — southbound (how the middleware reaches the device) and northbound (how peers
reach the middleware). Whether it is externally settable is a **facet** (an access mode), not a
subclass. Northbound it is **atomic**: value, unit
and access mode are read and written together as one dict, because they share one blanknode — the
locked circular-factory pattern for metadata about a property, RDF having no properties-about-
properties. Its **shape is the TBox restriction** on its property's `rdfs:range`, not the
instance data: anything the restriction does not declare is dropped at materialization with only a
warning, so metadata a connector needs must be declared there or it never arrives.
A complex property that matches no registered connector is **not** a Parameter — it is ordinary
data the consumer asked for, displayed and readable, with nothing wired.
Whether its value lives in the graph is the domain's choice — see **Committed value** / **Locator**. It is
also the deepest thing a binding can address: `ConnectionInfo` bottoms out at the Parameter, never at the
value inside it.
_Avoid_: StateProperty (retired term); Sensor value / Observation (those describe the
data, not the graph node); Capability (states have none — there is no "light-barrier capability").

**Interface class** — _retired term, do not use_:
A protocol-specific parameter *class* (`inf:MQTTParameter`, `inf:OPCUAParameter`) that a connector was
paired with one-to-one, resolved by the parameter's `rdf:type`. **The parameter node has no named
type**, so nothing could ever match on it (measured). The concept it reached for — the
protocol-extensibility seam — survives intact as the **Interface property**, and the ontology terms it
named are gone from the scenario-3 TBox. Use **Interface property**.
_Avoid_: the term itself. Also Adapter, Driver.

**ClassScope**:
A **projection — a view** — over the graph, expressed (in the OGM) as a tree of property-chains
rooted at a class. A view **belongs to its consumer** and is rooted at the node that consumer cares
about. There is no single "the datamodel" for a resource. This is how one central ontology serves
both the IT-OT boundary and the control/factory layer without duplicating concepts.
A view **terminates at a Parameter and cannot select within one**: below a complex property the
chain is silently discarded, and the blanknode's contents are fixed by the TBox restriction, the
same for every consumer. A scope chooses *which* parameters, never *which parts* of one.
An **empty** projection is legitimate — the graph holds the information, and a resource whose view
is a bare individual serves a one-field datamodel and starts normally.
_Avoid_: Filter, Query (a ClassScope is a reusable named view of which metadata to materialise).

**Root** (of a view):
The node a ClassScope is rooted at. Being a root is what makes a Resource a *top-level* thing rather
than a component — TransferUnit1 is a unit and ConveyorBelt1_left is a part of it **because a view is
rooted at the unit and reaches the belt**, not because of any part-of relation in the graph. There is
no composition property in Core, and none is needed.
_Avoid_: Top-level resource, Aggregate (both suggest an intrinsic property of the resource; rootedness
is a property of the view).

**User view**:
The ClassScope a resource-mode middleware is constructed with, and the one it materializes into the
datamodel it REST-exposes: the **northbound** projection. Stated by the domain code that embeds the
library, since only that code knows what the instance is for. Omitting it falls back to an unscoped
fetch, which materializes the `id` alone — a legitimate, if minimal, projection.
Connection metadata is absent from it not because the view declines to fetch it (it cannot — see
**ClassScope**) but because the **Projection** removes it, so a peer cannot learn the
broker address and bypass the middleware.
_Avoid_: The datamodel, Schema (it is one view among many; a connector's view of the same resource is
a different one).

**Projection** (northbound):
What keeps connection metadata out of the served datamodel: the middleware **removes the protocol
properties from the ClassSpec before fetching**, and materializes the pruned spec. What
counts as protocol metadata is **read from the ontology**, per Parameter, at every startup: everything
contributed by an **Interface property** strictly between the Parameter's own property and
`inf:isInterfaceAccessibleParameter`. The Parameter's own range (value, unit) and the root's own range
(`inf:accessMode`) stay. It runs for **every connector wiring**, including one that wires nothing — gating it
would make the least-privileged instance the one that leaks.

Deriving the set from the **registry** instead was tried and **fails open**: it knows only the
protocols this middleware has code for, so a Parameter reachable over an unregistered protocol had its
endpoint served (measured). A **Binding descriptor**'s connection metadata is now a *cross-check*
against the ontology, not the source. A keep-list — naming what is safe — was rejected: it is a second
closed-world moment (the design allows exactly one) and it hides new domain content by default.
_Avoid_: Deny-list *of field names* (the point is that the list is derived from the authoritative
ontology, not enumerated); Access control (the Projection stops a peer *learning* the broker address
from this REST surface, not someone who already knows it — that is future work).

Earlier this was recorded as *not* a middleware step at all: a Parameter materializes to exactly what its
property's restriction declares, so on the merge-depth reading a broker address physically could not
reach a peer. That premise died when the `inf:` interface properties gained their own ranges —
necessary so provisioning can write connection metadata through the OGM — because `PropertySpec` merges
the entire `rdfs:subPropertyOf*` chain with no depth parameter. Measured: the unpruned belt materializes
carrying topic, set topic and broker. Merge depth remains the right *description* of the two views; the
middleware has to realize the shallow one itself (the earlier claim that merge depth alone
projected the broker away is superseded).
_Avoid_: Filter, Stripping *of data* (the prune is on the shape, before any data is read — the northbound
model has no field to carry a broker address in); View (the view is the ClassScope, which selects *which*
Parameters, not which parts of one).

**Interface property**:
The property a **Semantic connector** binds to — `inf:isInterfaceAccessibleMQTTParameter` and its
siblings, under `inf:isInterfaceAccessibleParameter`. A resource's parameter declares its protocol by
being a **subproperty** of one, which is how the authoritative upstream ontology already models it.
Recognition is `rdfs:subPropertyOf*` against the registry. The parameter blanknode itself carries no
named class to match on.
_Avoid_: Interface class (retained for the ontology concept, but the *match* is on the property).

**Known primitives**:
The bounded form of the system's flexibility: novel *combinations* are handled, novel *vocabulary* is
not. A task assembled from grip/move/place may never have been seen in that combination, and a product
may combine a screw and a gear never before combined — but grip, move, place, screw and gear are all
known. Views and discovery listings are configured over known classes for this reason. It is a
deliberate boundary, not a limitation to be engineered away.
_Avoid_: Zero-configuration, Fully generic (both overclaim — the system does not discover concepts it
has never been taught).

**Semantic connector**:
Any connector able to **register itself from the knowledge graph**. Every connector transitional_sync_middleware ships
(MQTT, OPC-UA, HTTP, websocket, webhook, AAS client, model) is a candidate. Only the bare `Connector`
protocol is not, being the interface specification itself. Realized as a **Binding descriptor**, not as a
connector subclass.
_Avoid_: Connector (the bare transitional_sync_middleware protocol, without the metadata ontology); Adapter, Driver;
MQTT connector as the archetype (MQTT is the first instance, not the shape of the concept).

**Binding descriptor**:
The object that makes a connector semantic: it names the **connector class** it builds, the **Interface
property** it binds to, the connection-metadata properties its protocol needs — which are also exactly
what the **Projection** hides northbound — and how to turn one Parameter's metadata into one or more
framework registrations. It references its connector class rather than subclassing it, so a connector
nobody here owns can still be made semantic. One binding may yield **two** connectors (a read topic and a
write topic) against **one** binding target, differing only in direction. Built and registered **at
construction**, from the ClassSpec and the graph — registering later means the framework never connects
them and inbound traffic dies silently.
_Avoid_: Connector factory, Plugin (the descriptor is declarative — it states what a protocol needs, and
building is one method on it).

**Static facet**:
A part of a Parameter that does not change with a reading — unit, access mode. Captured by the **Binding
descriptor** at wiring time and reassembled into the payload on every inbound message, because
`setattr` replaces the whole Parameter node and `Formatter.deserialize` sees only the payload, with no
access to the current value. Free to carry, since `OGM.commit` filters unchanged triples.

Skolemization retired the *graph* reason for this — a skolemised Parameter node is addressable, so a commit
diffs per triple and an unchanged facet cannot be wiped. The **in-memory** reason stands and is why the
reassembly remains: without it, a bare inbound scalar blanks the unit in the very model that is served
over REST.
_Avoid_: Constant, Config (a facet belongs to the Parameter and is authored in the ontology, not to the
deployment).

**Connector registry**:
The universal map from **Interface property** to **Binding descriptor**. Built at middleware
initialization from the known, tested descriptors shipped in the middleware, and extensible after init by
injecting a domain-built one. Resolution is
`parameter property rdfs:subPropertyOf* → interface property → binding descriptor`. Supporting a new
protocol is registering a new entry, never a core change. Recognition runs over the **ClassSpec and the
graph**, not over materialized instance data, which is what allows registration to happen early enough
for the framework to connect them.
_Avoid_: Connector factory, Plugin loader (the registry keys specifically on the interface property);
keying on `rdf:type` (superseded — the parameter blanknode has no named type).

**Committed value** / **Locator**:
The two legitimate ways a domain may treat a Parameter's value, chosen per subproject — the middleware is
agnostic and enforces neither. **Committed value**: the data point changes slowly, so the domain code
commits it and the graph holds the value. `@state` is not involved. **Locator**: the data point changes
fast, so the graph holds only *where the value lives* — unit, access mode, topic, broker — and never the
value itself, which exists only in the datamodel and over REST. Scenario 3 is a locator, which is why its
instance data carries no `inf:hasValue` literals. The restriction still declares the field, so an
unobserved Parameter reads as `[]`.
_Avoid_: Cached value, Stale value (a committed value is authoritative for its update rate, not a stale
copy); "the live value is never persisted" as a middleware rule (it is the locator pattern's property).

**Capability**:
Defined in Core (`cfc:Capability`, subclasses `EquippedCapability`/`FlexibilityCapability`/
`ChangeabilityCapability`). An ability a Resource currently has. In this project's usage,
every Capability instance is created automatically by the middleware from a pre-existing
Capability *type* the moment a matching Workflow or StateProperty is registered — it is
never instantiated by hand.
_Avoid_: Skill (AAS term for a related but not identical concept).

**Operation**:
Defined in Core (`cfc:Operation`, subclass of `Task`). The executable, resource-assigned
form of a task; links to a Capability via `cfc:implementsCapability`. A caller creates an
Operation in the graph and dispatches it to the resource that will carry it out. That
resource queues it, pulls it, runs it, and the outcome is recorded back onto it. The
graph-level unit of work exchanged between middleware instances.
_Avoid_: Workflow invocation, Job (Operation is the graph-level unit of work; a single
Operation resolves to exactly one Workflow via its Capability).

**Event trigger** (`execute()`):
The receiver-side built-in Workflow that every resource-mode middleware exposes on its REST
API. A caller "triggers" it to signal that an Operation addressed to that resource now exists in
the graph. The receiver enqueues the Operation, `ogm.fetch`es it, and hands it to an optional
domain callback (else leaves it `queued`). The trigger carries only the Operation IRI — the
payload lives in the graph — and does not block on the work or return a business result.
_Avoid_: RPC call, Invoke (the event trigger notifies; it does not run the work synchronously).

**Dispatch**:
The caller-side act of handing an Operation to another resource: create the Operation
individual in the graph (through the OGM-routed write path) and then fire the receiver's
event trigger. Accessed from domain Python as a transaction context manager — the body populates
the Operation, the atomic exit performs the create-and-notify.
_Avoid_: Send, Publish (there is no message bus; dispatch is a graph write plus an event trigger).

**Operation queue**:
A resource-mode middleware's pending-work list — the Operations addressed to its Resource
that await or are in progress. Held in memory as a cache and reconstructed at startup by
querying the graph (own `queued` operations, plus own orphaned `running` operations to
reclaim); filled live by event triggers. A dead resource's stranded operations are swept
centrally by a watchdog, not by per-resource polling.
_Avoid_: Message queue, Broker (there is no broker; the queue is a view over graph state).

**Operation status**:
The lifecycle state of an Operation: `queued` (created and addressed, awaiting pull) →
`running` (pulled, in progress) → `done` or `failed` (terminal). Drives coordination and
recovery. Execution provenance (which Workflow ran it, when, the result) is written as part
of the terminal transition, so the status is itself the provenance record — there is no
separate success flag.

**Pull-and-run**:
The receiver-side transaction context manager by which domain code takes the next `queued`
Operation, sets it `running` (re-fetching it under a domain-supplied `ClassScope`), runs the
work in the body, and on atomic exit records the terminal `done`/`failed` state and
provenance — dumping the Resource's datamodel to the graph on failure.
_Avoid_: Poll, Consume (pull-and-run is the guarded unit of work, not the delivery mechanism).

**Resource**:
Defined in Core (`cfc:Resource`). The physical or logical thing a Service wraps (a door, a
transformer cell, a screwing tool). Required at construction time in resource mode.

**Mode**:
A `SemanticMiddleware` construction-time choice governing what the instance is *for*:
- `"resource"` — wraps one Resource; the REST surface is the user-registered Workflows/
  StateProperties, the built-in `execute()` event trigger, and a CRUD REST API generated from
  the resource's own datamodel (`generate_rest_api_for_data_model`). The
  transactional context-manager surface (dispatch/`request`, pull-and-run, handover) and the
  graph-write helpers stay Python-only, not REST-exposed.
- `"server"` — wraps no Resource. CRUD/`execute` themselves are the REST surface (e.g. a
  future data-serving "product server"). Not yet implemented, and deliberately still reserved: a
  graph-*consuming* participant (a planner, a mobile robot, a controller) is a resource-mode
  planner with its own Resource, not a server.
- `"watchdog"` — wraps no Resource, exposes little to no REST surface; runs a sweep loop
  that removes stale `svc:address`/`svc:endpoint` triples left by resource-mode instances
  that stopped heartbeating.
_Avoid_: Deployment type, Role (Mode is specifically the constructor discriminator).

**Heartbeat**:
A resource-mode Service's periodic re-assertion of its own liveness — refreshing
`svc:lastHeartbeat` on its Service individual via an internal interval-based Workflow. Read
by watchdog-mode instances to decide staleness.

**Address vs. Endpoint**:
`svc:address` is a Service's base URL, set on startup and removed on
deregistration/staleness. `svc:endpoint` is the full, directly callable URL for one specific
Workflow or StateProperty, also set on startup and removed on deregistration/staleness. Both
being present is a deliberate divergence from the paper's literal "address on Service only"
wording — see the "Address and endpoint" section of
`docs/mechanics/02-workflow-registration.md`.
_Avoid_: URL, endpoint URL used interchangeably for both — they are distinct properties on
distinct entity types.

**KnowledgeGraphConnector**:
An `transitional_sync_middleware`-protocol `Connector` (`connect`/`disconnect`/`provide`/`consume`) whose
`provide`/`consume` wrap `kapps_ogm.OGM.fetch`/`commit`. The mechanism by which any
`transitional_sync_middleware` construct (workflows, synced connectors) can read/write the knowledge graph
without going around the OGM's validated write path. See "The graph-facing connector"
in `docs/mechanics/04-connector-binding.md`.

**Deregistration**:
The reverse of registration: on shutdown (or, for watchdog-mode-detected staleness), removing
a Service's `svc:address` and its Workflows'/StateProperties' `svc:endpoint` triples while
preserving the individuals themselves, for provenance.
