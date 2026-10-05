# Guide

Orientation for a developer or agent building against `kapps-semantic-middleware`.

This library lets a piece of Python code running on or next to a shopfloor resource expose its
functionality through an RDF knowledge graph, and lets other middleware instances discover and
invoke that functionality through the graph rather than through hardcoded network references.

If you are prototyping against this middleware, read the consumption rules below first. They are
constraints that **do not surface in type signatures** — most of them fail silently, producing
structurally wrong behavior rather than an exception. Then read the mechanics pages for whichever
part you are touching.

:::{dropdown} Terms this page assumes
{term}`Service` · {term}`Resource` · {term}`Mode` · {term}`Workflow` · {term}`Capability` ·
{term}`Operation` · {term}`Parameter` · {term}`ClassScope` · {term}`Projection` · {term}`Heartbeat` ·
{term}`Address vs. Endpoint`
:::

## The mechanics pages

Written in construction order; a reader can go start to finish without forward references.

| Page | Covers |
|---|---|
| {doc}`01-instantiation-and-lifecycle` | Constructing an instance, choosing a mode, what appears in the graph at startup, heartbeat, deregistration |
| {doc}`02-workflow-registration` | Declaring what a Service exposes, the ontology prerequisites, signature-derived shapes, address vs. endpoint |
| {doc}`03-state-and-parameters` | Modelling resource state as Parameters, committed value vs. locator, where a parameter's shape comes from |
| {doc}`04-connector-binding` | The recognition chain, adding a protocol the library does not ship, connector wiring, transports |
| {doc}`05-operation-coordination` | Dispatch, the issuer and the Issued Operation, the event trigger, notifications and the intake, pull-and-run, the status lifecycle, handover, recovery |
| {doc}`06-views-and-projection` | Defining a ClassScope, what a view cannot select, what the northbound projection removes |
| {doc}`07-writing-to-the-graph-and-to-devices` | Every path that causes a write, creating an instance and the predefined scopes, naming the field that moved, northbound vs. southbound, IRI handling |
| {doc}`08-provisioning-and-seeding` | **Read first if `ogm=` is a mystery.** The bootstrap order, loading the shared ontologies, authoring the domain TBox, and seeding the instance data a Parameter needs |
| {doc}`09-the-state-dashboard` | The read-only page an instance serves about itself, the box registry a subproject extends, hiding a box, and posting to its own workflow |

```{toctree}
:hidden:
:maxdepth: 1

01-instantiation-and-lifecycle
02-workflow-registration
03-state-and-parameters
04-connector-binding
05-operation-coordination
06-views-and-projection
07-writing-to-the-graph-and-to-devices
08-provisioning-and-seeding
09-the-state-dashboard
```

## Where the names come from

`kapps_semantic_middleware` exports two names, `SemanticMiddleware` and `Mode`, and re-exports
nothing. Every other name you need comes from one of this package's own modules or from one of
its three dependencies, by that dependency's own path. Use this table rather than guessing.

| Name | Import from |
|---|---|
| `SemanticMiddleware`, `Mode` | `kapps_semantic_middleware` |
| `IRI`, `GraphDB` | `kapps_triplestore_interface` |
| `OGM` | `kapps_ogm` |
| `ClassScope` | `kapps_ogm.utils.class_scope` |
| `SyncDirection` | `transitional_sync_middleware.middleware.sync.synced_connector` |
| `ConnectionInfo` | `transitional_sync_middleware.middleware.registries` |
| `INF` (the interface vocabulary) | `kapps_semantic_middleware.vocabulary` |
| `graphdb_for`, `credentials_for` | `kapps_semantic_middleware.credentials` |
| `mw.commit()`, `mw.create()`, `mw.fetch()`, `mw.notify()`, `mw.on_notification()`, `mw.resource` (members of `SemanticMiddleware`, nothing more to import; `mw.create(cls, scope)` creates one new instance, `mw.resource` is the middleware's own Resource, `mw.notify(operation=op, resource=r)` sends a notification, an Operation's IRI with no payload and no write, and `mw.on_notification(handler, class_scope=...)` takes the notified Operations through the intake, one at a time, each as the Operation object, to the exposed workflow that realizes its Capability or else to the handler, with no lifecycle; the feedback of an Operation that the instance's Resource issued and that is no longer `queued` goes to the handler, also when a workflow of the instance realizes its Capability) | `kapps_semantic_middleware` |
| `Create`, `Update` (the changes that a transition of `mw.commit()` is made of, and the `entry` of `mw.handover()`) | `kapps_ogm` |
| `Node` (the Operation object that the intake passes to a workflow or a notification handler; type a workflow's parameter with it) | `kapps_ogm` |
| `workpiece` (the scope of a new Workpiece for `mw.create()`: `workpiece(scope, possessor=mw.resource)`) | `kapps_semantic_middleware.possession` |
| `operation` (the scope of a new Operation for `mw.create()`: `operation(scope, capability=cap)`) | `kapps_semantic_middleware.operation` |
| `PredefinedScope`, `ScopeChanges` (the hook through which a domain module brings its rules to `mw.create()`, and what its `changes()` returns) | `kapps_semantic_middleware.commit` |
| `SVC`, `OperationStatus` (the service vocabulary, and an Operation's status values) | `kapps_semantic_middleware.vocabulary` |
| `MES` (the handover abilities, such as `MES.Pass`, that `mw.handover()` takes as its `mode`) | `kapps_semantic_middleware.vocabulary` |
| `HandoverPreconditionError`, `PossessionSwitchConflict`, `OperationQueueEmpty`, `OperationResolutionError`, `OntologyGroundTruthError`, `NotificationUndelivered` | `kapps_semantic_middleware.errors` |
| `TripleStoreInterfaceError`, `GraphDbException` (a refusal by the store, SHACL included) | `kapps_triplestore_interface.exceptions` |
| `AnonymousNodeFetchError`, `AmbiguousNodeAlignmentError`, `UnresolvableNodeAddressError` (each a `ValueError`) | `kapps_ogm.utils.errors` |
| `RequestException` (the store cannot be reached; `requests` is the store's HTTP client, reached through `kapps_triplestore_interface`, and not one of the three dependencies) | `requests.exceptions` |

Binding internals, if you are adding a protocol, live under
`kapps_semantic_middleware.connectors` — `semantic`, `mqtt_binding`, `rest_binding`,
`knowledge_graph_connector`, `wiring`.

> `kapps_semantic_middleware/ontology/` is **not** a Python module. It is the directory holding the
> three vocabulary files (`core.ttl`, `mes.ttl`, `service.ttl`). Importing from it fails.

## Generic and circular-factory-specific modules

The generic core knows only `svc:` and standard RDF (`rdf:`, `rdfs:`, `owl:`, `xsd:`). Everything
specific to the circular factory, which is Core's `cfc:` vocabulary, lives in small topic modules.
Each topic module is built on the generic core: `mw.commit()`, `mw.create()`, `mw.fetch()` and
kapps_ogm's changes. It never writes past them. A domain that is not modelled on Core uses the
generic modules and none of the cfc-specific ones.

Some generic modules still have cfc-specific parts inside. The table names those parts: the
functions, methods, classes and module-level names where a `cfc:` term may appear. Nowhere else in
such a module may one appear.

`tests/test_generic_modules.py` reads this table. It fails when a generic module names a `cfc:`
IRI or imports a cfc-specific module, when a module with cfc-specific parts speaks `cfc:` outside
the parts named here, when a module of the package is not in the table, and when a module or a
part listed with `cfc:` no longer has any.

| Module | Kind | What is cfc-specific in it |
|---|---|---|
| `kapps_semantic_middleware` | generic | |
| `kapps_semantic_middleware.activity` | generic | |
| `kapps_semantic_middleware.commit` | generic | |
| `kapps_semantic_middleware.connectors` | generic | |
| `kapps_semantic_middleware.connectors.knowledge_graph_connector` | generic | |
| `kapps_semantic_middleware.connectors.mqtt_binding` | generic | |
| `kapps_semantic_middleware.connectors.rest_binding` | generic | |
| `kapps_semantic_middleware.connectors.semantic` | generic | |
| `kapps_semantic_middleware.connectors.wiring` | generic | |
| `kapps_semantic_middleware.credentials` | generic | |
| `kapps_semantic_middleware.errors` | generic | |
| `kapps_semantic_middleware.examples_cli` | generic | |
| `kapps_semantic_middleware.modes` | generic | |
| `kapps_semantic_middleware.projection` | generic | |
| `kapps_semantic_middleware.rest_router` | generic | |
| `kapps_semantic_middleware.shacl_interop` | generic | |
| `kapps_semantic_middleware.shacl_interop.shape_from_typehints` | generic | |
| `kapps_semantic_middleware.possession` | cfc-specific | Possession: one possession state per Resource, the change of possession that `handover()` commits, and the `workpiece` scope |
| `kapps_semantic_middleware.operation` | cfc-specific | The Operation: its link to the Capability that it implements, the `operation` scope, and the scope and the Capabilities of a received Operation |
| `kapps_semantic_middleware.middleware` | generic, with cfc-specific parts | `SemanticMiddleware.handover`, `SemanticMiddleware.request`, `SemanticMiddleware._read_received`: the change of possession of a handover, the Operation of a request, created under the operation scope, and the intake's read of a received Operation with the Capabilities it implements |
| `kapps_semantic_middleware.registration` | generic, with cfc-specific parts | `register_workflow`, `register_state_property`, `create_operation`, `resolve_operation_workflow`, `find_resource_operations`, `find_possession_state`, `switch_possession`: the Capability links of registration and dispatch, the possession read of a handover's precondition, and the possession helpers that a seed uses |
| `kapps_semantic_middleware.seeding` | generic, with cfc-specific parts | `CORE_GRAPH`: the graph into which the shared ontologies load Core |
| `kapps_semantic_middleware.vocabulary` | generic, with cfc-specific parts | `CORE_NS`, `CORE_ONTOLOGY`, `CFC`: the one home of every IRI holds Core's terms too |

The demonstration (`kapps_semantic_middleware.demonstrations`) is an illustration, not part of
the package's API, and is not classified.

## Consumption rules

### These fail silently

**`GraphDB.from_env()` connects to whatever `GRAPHDB_REPOSITORY` names, and seeding destroys what it
connects to.** `seeding.clear_repository` clears the default graph of the client's current
repository, so a value left over in a shell — from another project, another checkout, a `.bashrc`
written months ago — silently redirects a wipe. Nothing validates that the repository was the one you
meant; it only has to exist. Use `credentials.graphdb_for("name")`, which names the repository in
code and ignores the variable, for anything that seeds, clears, or re-seeds. Note this hazard belongs
to the *variable*, not the server: pinning the repository does not stop `GRAPHDB_URL` pointing at a
shared instance.

**A {term}`ClassScope` terminates at a {term}`Parameter` and cannot select within one.** Any chain
element below a complex property is silently discarded during fetch. A view that tries to reach
inside a parameter blanknode materializes only as far as the parameter itself, with no error raised.
A scope chooses *which* parameters, never *which parts* of one.

**Connector bindings must be registered at construction, not later.** The framework calls
`connect()` on everything in the connection registry before it runs `on_start_up` callbacks. A
connector registered after construction never has `connect()` called, so inbound traffic dies
silently — the listener task never starts and the queue is never fed — while outbound may limp
along, making the fault one-directional and quiet.

**A Parameter's shape comes from the TBox restriction on its property's range, not from the
instance data.** Anything the restriction does not declare is dropped at materialization with only
a warning logged. Metadata a connector needs must be declared in the restriction or it never
arrives, and the connector fails with nothing raised.

**Under the locator pattern a Parameter's value is not in the graph at all.** Fast-changing
parameters keep only metadata in the graph; the live value exists only in the datamodel and over
REST. An unobserved parameter materializes as an empty list, which means *not yet read* — not zero,
and not null. Handle the empty case.

**A persistence write must name the region of the model that changed.** The fan-out notifies only
the connectors that region covers. A write that does not name its field notifies every synced
connector, so devices are written that nobody touched — and for a settable parameter that
fabricates a command rather than merely wasting traffic.

**The northbound REST payload never carries protocol connection metadata.** Broker addresses,
topics and endpoints are pruned from the served datamodel *before* any data is fetched. This is
structural, not a permission check: the northbound model has no field able to carry a broker
address. It stops a peer *learning* an address from this surface; it does not stop someone who
already knows it.

**OWL existential restrictions do not make a field required.** Only SHACL shapes enforce
requiredness, and only at admission. A parameter with no observed value does not raise a validation
error on materialization merely because its restriction declares `owl:someValuesFrom`. Absence of a
triple means *unknown*, never *false*.

**Any prettified or shortened IRI is display only.** Production code carries fully back-resolvable
IRIs in their mangled form — REST path segments, datamodel field names, `svc:endpoint` triples. A
consumer that parses a displayed IRI, or round-trips one back into a query, gets a wrong answer
with no exception raised.

### These fail loudly

**Every class you register against must already exist in the ontology.** The middleware creates
instances automatically but never mints classes. A domain-specific subclass of `svc:Service`,
`svc:Workflow` or `svc:StateProperty`, and the `cfc:Capability` subclass a {term}`Workflow`
realizes, must all pre-exist. Startup fails immediately, naming the missing class.

**All knowledge-graph writes go through the OGM.** Raw SPARQL `UPDATE` or direct
`kapps_triplestore_interface` mutation calls bypass the validated write path, so the written node passes no
shape check and a property replacement loses its atomicity — the intermediate state fails
validation. Reads may use the access module directly; only writes are constrained.

**`@mw.workflow` and `@mw.state` are resource-mode only.** Each raises `RuntimeError` when called
on an instance in any other mode, and `ValueError` when a required class IRI is missing.

### Structural facts

**Build against `src/kapps_semantic_middleware/`. `examples/` and `demo/` are illustrations, not
API.** They are teaching vehicles seeded against throwaway repositories, not production patterns to
copy.

**The demo imports as `kapps_semantic_middleware.demonstrations`, not `demo`.** The wheel remaps
the `demo/` directory into the library namespace. `import demo.transferunits` works only in a
development checkout.

**There is one {term}`Service` per middleware *instance*, not per {term}`Resource`.** Several
instances may wrap one Resource — a controller and a monitor, say — each owning its own Service
node, address and {term}`heartbeat <Heartbeat>`, all linked by `svc:isServiceOf`. Discovery may
therefore return several Services for one Resource; select by address or by advertised capability
rather than assuming exactly one.

**Only `resource` and `watchdog` modes are implemented.** `server` is reserved and raises
`NotImplementedError`. A graph-*consuming* participant — a planner, a controller, a mobile robot —
is a resource-mode instance with its own Resource, not a server-mode one.

**Docker is a prerequisite for running the examples, never for using the library.** The bundled
`docker compose` file provides the GraphDB the scenarios and the factory need.
