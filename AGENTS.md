# AGENTS.md

Orientation for an agent building against `kapps_semantic_middleware`.

This library lets a piece of Python code running on or next to a shopfloor resource expose its
functionality through an RDF knowledge graph, and lets other middleware instances discover and
invoke that functionality through the graph rather than through hardcoded network references.

If you are prototyping against this middleware, read the consumption rules below first. They are
constraints that **do not surface in type signatures** — most of them fail silently, producing
structurally wrong behavior rather than an exception. Then read the mechanics pages for whichever
part you are touching.

## Where to read what

**Vocabulary — what the words mean.** Read these before the mechanics pages; the mechanics pages
assume their terms and do not re-define them.

| File | Covers |
|---|---|
| [`CONTEXT-MAP.md`](CONTEXT-MAP.md) | The five contexts, how they depend on each other, and the three-module ontology layering |
| [`src/kapps_semantic_middleware/CONTEXT.md`](src/kapps_semantic_middleware/CONTEXT.md) | The core vocabulary: Service, Workflow, Parameter, ClassScope, Projection, Binding descriptor, Operation, Mode |
| [`src/kapps_semantic_middleware/shacl_interop/CONTEXT.md`](src/kapps_semantic_middleware/shacl_interop/CONTEXT.md) | Precondition and outcome shapes derived from a function's type hints |
| [`examples/CONTEXT.md`](examples/CONTEXT.md) | The self-contained scenario notebooks |
| [`demo/transferunits/CONTEXT.md`](demo/transferunits/CONTEXT.md) | The runnable multi-process factory demonstration |

**Mechanics — how to use it.** Written in construction order; a reader can go start to finish
without forward references.

| Page | Covers |
|---|---|
| [`docs/mechanics/01-instantiation-and-lifecycle.md`](docs/mechanics/01-instantiation-and-lifecycle.md) | Constructing an instance, choosing a mode, what appears in the graph at startup, heartbeat, deregistration |
| [`docs/mechanics/02-workflow-registration.md`](docs/mechanics/02-workflow-registration.md) | Declaring what a Service exposes, the ontology prerequisites, signature-derived shapes, address vs. endpoint |
| [`docs/mechanics/03-state-and-parameters.md`](docs/mechanics/03-state-and-parameters.md) | Modelling resource state as Parameters, committed value vs. locator, where a parameter's shape comes from |
| [`docs/mechanics/04-connector-binding.md`](docs/mechanics/04-connector-binding.md) | The recognition chain, adding a protocol the library does not ship, connector wiring, transports |
| [`docs/mechanics/05-operation-coordination.md`](docs/mechanics/05-operation-coordination.md) | Dispatch, the issuer and the Issued Operation, the event trigger, notifications and the intake, pull-and-run, the status lifecycle, handover, recovery |
| [`docs/mechanics/06-views-and-projection.md`](docs/mechanics/06-views-and-projection.md) | Defining a ClassScope, what a view cannot select, what the northbound projection removes |
| [`docs/mechanics/07-writing-to-the-graph-and-to-devices.md`](docs/mechanics/07-writing-to-the-graph-and-to-devices.md) | Every path that causes a write, creating an instance and the predefined scopes, naming the field that moved, northbound vs. southbound, IRI handling |
| [`docs/mechanics/08-provisioning-and-seeding.md`](docs/mechanics/08-provisioning-and-seeding.md) | **Read first if `ogm=` is a mystery.** The bootstrap order, loading the shared ontologies, authoring the domain TBox, and seeding the instance data a Parameter needs |
| [`docs/mechanics/09-the-state-dashboard.md`](docs/mechanics/09-the-state-dashboard.md) | The page an instance serves about itself, the box registry a subproject extends, hiding a box, posting to its own workflow, and the Logs tab with a logger's level set at run time |

### Where the reasoning lives

The architecture decision records behind this library are development-repository material and are
**not part of this distribution**. The glossaries state what was decided; the mechanics pages above
carry the reasoning, with the deliberation removed. If a glossary claim seems to have no mechanics
page behind it, that is a documentation bug worth reporting, not a missing file worth hunting.

## Where the names come from

`kapps_semantic_middleware` exports two names, `SemanticMiddleware` and `Mode`, and re-exports
nothing. Every other name you need comes from one of this package's own modules or from one of
its three dependencies, by that dependency's own path. Use this table rather than guessing.

| Name | Import from |
|---|---|
| `SemanticMiddleware`, `Mode` | `kapps_semantic_middleware` |
| `IRI`, `TripleStore` | `kapps_triplestore_interface` |
| `OGM` | `kapps_ogm` |
| `ClassScope` | `kapps_ogm.utils.class_scope` |
| `SyncDirection` | `transitional_sync_middleware.middleware.sync.synced_connector` |
| `ConnectionInfo` | `transitional_sync_middleware.middleware.registries` |
| `INF` (the interface vocabulary) | `kapps_semantic_middleware.vocabulary` |
| `triplestore_for`, `endpoint_for` | `kapps_semantic_middleware.credentials` |
| `enable_dashboard`, `panel`, `kv_table`, `hint`, `escape` | `kapps_semantic_middleware.dashboard` |
| `mw.commit()`, `mw.create()`, `mw.fetch()`, `mw.notify()`, `mw.on_notification()`, `mw.resource` (members of `SemanticMiddleware`, nothing more to import; the keyword-only `verb` of `mw.commit()`, `"commit"` or `"handover"`, labels a refused write in `mw.refused_writes`; `mw.create(cls, scope)` creates one new instance, `mw.resource` is the middleware's own Resource, `mw.notify(operation=op, resource=r)` sends a notification, an Operation's IRI with no payload and no write, and `mw.on_notification(handler, class_scope=...)` takes the notified Operations through the intake, one at a time, each as the Operation object, to the exposed workflow that realizes its Capability or else to the handler, with no lifecycle; the feedback of an Operation that the instance's Resource issued and that is no longer `queued` goes to the handler, also when a workflow of the instance realizes its Capability) | `kapps_semantic_middleware` |
| `mw.refused_writes` (the writes of this instance that the store's shapes refused, newest first: each a `RefusedWrite` with its time, its verb, `commit`, `create` or `handover` (the possession extension's), its rules and the report; the newest 50, in memory only, and each logged once at INFO) | `kapps_semantic_middleware` |
| `mw.connections`, `mw.graph_binding` (what the instance has seen since it started, in memory only: one `ConnectionRecord` per wired connector, in the order of the wiring, with its state, last provide, consume, receive and sync, and last error; and the `GraphBindingRecord` of the store behind its OGM, with its last read, last write and last error. Each record carries the instance's resource and service IRI.) | `kapps_semantic_middleware` |
| `ConnectionRecord`, `GraphBindingRecord`, `CallError`, `is_connection_metadata` (the record types, and the one predicate that names the fields carrying connection metadata, such as an error's message.) | `kapps_semantic_middleware.observation` |
| `readable_results`, `validation_results`, `rule_names` (the store's validation report, the `report` of `ShaclValidationError`, read as RDF: each result for a person, as `ReadableResult`s of `Term`s with `short` and `full`; each result for comparing, as `ValidationResult`s with full IRIs; the rules for a log line), `RefusedWrite`, `render_refused_write` (one refused write as HTML: its first line, and an expander with the results and the whole report) | `kapps_semantic_middleware.validation_report` |
| `IssuedOperation` (what `mw.issue_operation()` yields: the draft of the new Operation in its block, with `op.data` and `op.update(instance_iri, data)`, which changes an existing instance in the Operation's commit, and after it the handle with `op.iri` and `op.issuer`, which waits with `await op.claimed(timeout=30)` and `await op.ended(timeout=None)`, or with their blocking twins `op.wait_claimed(timeout)` and `op.wait_ended(timeout)`) | `kapps_semantic_middleware.middleware` |
| `Create`, `Update` (the changes that a transition of `mw.commit()` is made of, and the `entry` of `possession.handover()`) | `kapps_ogm` |
| `Node` (the Operation object that the intake passes to a workflow or a notification handler; type a workflow's parameter with it) | `kapps_ogm` |
| `workpiece` (the scope of a new Workpiece for `mw.create()`: `workpiece(scope, possessor=mw.resource)`) | `kapps_semantic_middleware.possession` |
| `handover` (the change of possession, then the move: `with handover(mw, mode=MES.Pass, workpiece=workpiece, counterpart=peer, operation=op, entry=[...]):`, a context manager built on `mw.commit()`. `SemanticMiddleware.handover()` is its deprecated alias for one release.) | `kapps_semantic_middleware.possession` |
| `create_possession`, `switch_possession` (a workpiece's first possession, and a change of possession with no move, each in one `OGM.commit`, for a seed or a caller that holds no middleware), `find_possession_state`, `counterpart_has_complementary_ability` (the reads of a handover's preconditions), `mint_possession_state_iri` | `kapps_semantic_middleware.possession` |
| `operation` (the scope of a new Operation for `mw.create()`: `operation(scope, capability=cap)`) | `kapps_semantic_middleware.operation` |
| `PredefinedScope`, `ScopeChanges` (the hook through which a domain module brings its rules to `mw.create()`, and what its `changes()` returns) | `kapps_semantic_middleware.commit` |
| `SVC`, `OperationStatus` (the service vocabulary, and an Operation's status values) | `kapps_semantic_middleware.vocabulary` |
| `MES` (the handover abilities, such as `MES.Pass`, that `possession.handover()` takes as its `mode`) | `kapps_semantic_middleware.vocabulary` |
| `HandoverPreconditionError`, `PossessionSwitchConflict`, `OperationQueueEmpty`, `OperationResolutionError`, `OntologyGroundTruthError`, `NotificationUndelivered`; the types that tell the action: `OperationReceiverUnavailableError` and `NotificationReceiverUnavailableError` (Retry), `DeploymentError` with `DeploymentValueError` and `DeploymentRuntimeError`, `CapabilityNotRealizedError` and `NotificationReceiverAmbiguousError` (Repair) | `kapps_semantic_middleware.errors` |
| `ShaclValidationError` (the store refused a write by SHACL validation; `.report` is its validation report, verbatim), `TripleStoreResponseError` (any other error answer of the store; `ShaclValidationError` subclasses it), `PreconditionFailedError` (the store found the precondition of a write false and wrote nothing: a `Create` of an instance that it holds, or a value that a change replaces and that another write changed after the read), `TripleStoreInterfaceError` (any error of the interface) | `kapps_triplestore_interface.exceptions` |
| `AnonymousNodeFetchError`, `AmbiguousNodeAlignmentError`, `UnresolvableNodeAddressError` (each a `ValueError`) | `kapps_ogm.utils.errors` |
| `RequestException` (the store cannot be reached; `requests` is the store's HTTP client, reached through `kapps_triplestore_interface`, and not one of the three dependencies) | `requests.exceptions` |

Binding internals, if you are adding a protocol, live under
`kapps_semantic_middleware.connectors` — `semantic`, `mqtt_binding`, `rest_binding`,
`wiring`.

> `kapps_semantic_middleware/ontology/` is **not** a Python module. It is the directory holding the
> three vocabulary files (`core.ttl`, `mes.ttl`, `service.ttl`). Importing from it fails.

## Generic and circular-factory-specific modules

The generic core knows only `svc:` and standard RDF (`rdf:`, `rdfs:`, `owl:`, `xsd:`). Everything
specific to the circular factory, which is Core's `cfc:` vocabulary, lives in small topic modules.
Each topic module is built on the generic core: `mw.commit()`, `mw.create()`, `mw.fetch()` and
kapps_ogm's changes. It never writes past them. A domain that is not modelled on Core uses the
generic modules and none of the cfc-specific ones.

Possession and the handover are such an extension, `kapps_semantic_middleware.possession`. The
core still knows Core's Capability, Resource and Operation (registration writes
`cfc:hasCapability`), but `middleware` and `registration` name no possession term and no `mes:`
handover ability, except two deprecated aliases that only delegate to the extension: the method
`SemanticMiddleware.handover()` and the `__getattr__` of `registration`, which resolves the old
names of the moved helpers and errors. `tests/test_core_names_no_possession.py` checks it.

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
| `kapps_semantic_middleware._deprecation` | generic | |
| `kapps_semantic_middleware.activity` | generic | |
| `kapps_semantic_middleware.commit` | generic | |
| `kapps_semantic_middleware.connectors` | generic | |
| `kapps_semantic_middleware.connectors.mqtt_binding` | generic | |
| `kapps_semantic_middleware.connectors.rest_binding` | generic | |
| `kapps_semantic_middleware.connectors.semantic` | generic | |
| `kapps_semantic_middleware.connectors.wiring` | generic | |
| `kapps_semantic_middleware.credentials` | generic | |
| `kapps_semantic_middleware.dashboard` | generic | |
| `kapps_semantic_middleware.dashboard_boxes` | generic | |
| `kapps_semantic_middleware.dashboard_logs` | generic | |
| `kapps_semantic_middleware.errors` | generic | |
| `kapps_semantic_middleware.examples_cli` | generic | |
| `kapps_semantic_middleware.modes` | generic | |
| `kapps_semantic_middleware.observation` | generic | |
| `kapps_semantic_middleware.projection` | generic | |
| `kapps_semantic_middleware.rest_router` | generic | |
| `kapps_semantic_middleware.shacl_interop` | generic | |
| `kapps_semantic_middleware.shacl_interop.shape_from_typehints` | generic | |
| `kapps_semantic_middleware.validation_report` | generic | |
| `kapps_semantic_middleware.possession` | cfc-specific | Possession, an extension built on the generic core: one possession state per Resource, the `handover()` with its preconditions and its change of possession, the `workpiece` scope, and the possession helpers that a seed uses |
| `kapps_semantic_middleware.operation` | cfc-specific | The Operation: its link to the Capability that it implements, the `operation` scope, and the scope and the Capabilities of a received Operation |
| `kapps_semantic_middleware.middleware` | generic, with cfc-specific parts | `SemanticMiddleware.handover`, `SemanticMiddleware.issue_operation`, `SemanticMiddleware._read_received`: the deprecated alias of the possession extension's handover, which calls it, the Operation of a request, created under the operation scope, and the intake's read of a received Operation with the Capabilities it implements |
| `kapps_semantic_middleware.registration` | generic, with cfc-specific parts | `register_workflow`, `register_state_property`, `_register_capability`, `create_operation`, `_resolve_claim`, `find_resource_operations`, `find_stranded_operations`: the Capability links of registration and dispatch (the resource's link is one OGM append, in the commit that writes the Capability) |
| `kapps_semantic_middleware.seeding` | generic, with cfc-specific parts | `CORE_GRAPH`: the graph into which the shared ontologies load Core |
| `kapps_semantic_middleware.vocabulary` | generic, with cfc-specific parts | `CORE_NS`, `CORE_ONTOLOGY`, `CFC`: the one home of every IRI holds Core's terms too |

The demonstration (`kapps_semantic_middleware.demonstrations`) is an illustration, not part of
the package's API, and is not classified.

## Consumption rules

### These fail silently

**`TripleStoreEndpoint.from_env()` connects to whatever `TRIPLESTORE_DATASET` names, and seeding
destroys what it connects to.** `seeding.clear_dataset` clears the default graph of the store's
dataset, so a value left over in a shell — from another project, another checkout, a `.bashrc`
written months ago — silently redirects a wipe. Nothing validates that the dataset was the one you
meant; it only has to exist. Use `credentials.triplestore_for("name")`, which names the dataset in
code and ignores the variable, for anything that seeds, clears, or re-seeds. Note this hazard belongs
to the *variable*, not the server: pinning the dataset does not stop `TRIPLESTORE_URL` pointing at a
shared instance. `seeding` guards the server too: `clear_dataset` and `load_shared_ontologies(reload=True)`
raise `DeploymentValueError` for a host that is neither loopback nor a bare container alias, unless
`TRIPLESTORE_ALLOW_DESTRUCTIVE=1` is set.

**A `ClassScope` terminates at a Parameter and cannot select within one.** Any chain element below
a complex property is silently discarded during fetch. A view that tries to reach inside a
parameter blanknode materializes only as far as the parameter itself, with no error raised. A scope
chooses *which* parameters, never *which parts* of one.

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

**A connection that stays open holds a stop open, and the instance then never deregisters.**
uvicorn runs the lifespan shutdown, where the instance removes its address, only after every open
response has finished, and by default it waits without a limit. A supervisor that kills the
process past its stop timeout skips the deregistration, and the address stays in the graph. The
library's `/activity/stream` ends after 2 s for this reason; a client other than a browser must
reconnect and send `Last-Event-ID`. A streaming route of your own must end by itself too, and
where you run uvicorn, set `timeout_graceful_shutdown` below your supervisor's stop timeout.

**A dashboard box that names a URL of its own instance with a leading slash breaks behind a path
prefix.** A box is placed into the page at `/dashboard`, and a proxy may serve the process under a
prefix, such as `/p/<process>/dashboard`. An absolute `hx-post="/workflows/<name>/execute"` then
goes to the proxy's own root, and the form posts to nothing, with no error on the page. Write the
path relative to the page, `workflows/<name>/execute`, as the library's own pages do. See
[`docs/mechanics/09-the-state-dashboard.md`](docs/mechanics/09-the-state-dashboard.md).

**The dashboard shows connection metadata unless it is told not to.** `enable_dashboard(mw)` makes
a development page: it shows each parameter's MQTT topic, the protocol properties the projection
stripped, and the message of a connection's or the graph binding's last error, which may name a
host, a port or a URL. That is on purpose: the page is the instance's own diagnostic surface for
laboratory deployments, not the northbound payload that other machines read. A deployment that is
not a laboratory one passes `dev=False`, or it prints topics and addresses on an HTTP port. The log
keeps an MQTT topic at DEBUG either way; that the page and the log differ is deliberate. See
[`docs/mechanics/09-the-state-dashboard.md`](docs/mechanics/09-the-state-dashboard.md).

### These fail loudly

**Every class you register against must already exist in the ontology.** The middleware creates
instances automatically but never mints classes. A domain-specific subclass of `svc:Service`,
`svc:Workflow` or `svc:StateProperty`, and the `cfc:Capability` subclass a Workflow realizes, must
all pre-exist. Startup fails immediately, naming the missing class.

**All knowledge-graph writes go through the OGM.** Raw SPARQL `UPDATE` or direct
`kapps_triplestore_interface` mutation calls bypass the validated write path: they skip the OGM's checks and its atomicity, so a property
replacement is not one update. The store still validates every update against its shapes. Reads may use the access module directly; only writes are constrained. The resource's
link to each Capability it provides is one `Append` in an OGM commit, which keeps the other links,
so instances that register on one resource at the same moment keep every link. Only seeding, which
provisions a dataset before any service runs, writes to the store directly.

**`@mw.workflow` and `@mw.state` are resource-mode only.** Each raises `RuntimeError` when called
on an instance in any other mode, and `ValueError` when a required class IRI is missing.

### Structural facts

**Build against `src/kapps_semantic_middleware/`. `examples/` and `demo/` are illustrations, not
API.** They are teaching vehicles seeded against throwaway datasets, not production patterns to
copy.

**The demo imports as `kapps_semantic_middleware.demonstrations`, not `demo`.** The wheel remaps
the `demo/` directory into the library namespace. `import demo.transferunits` works only in a
development checkout.

**There is one Service per middleware *instance*, not per Resource.** Several instances may wrap
one Resource — a controller and a monitor, say — each owning its own Service node, address and
heartbeat, all linked by `svc:isServiceOf`. Discovery may therefore return several Services for one
Resource; select by address or by advertised capability rather than assuming exactly one.

**Only `resource` and `watchdog` modes are implemented.** `server` is reserved and raises
`NotImplementedError`. A graph-*consuming* participant — a planner, a controller, a mobile robot —
is a resource-mode instance with its own Resource, not a server-mode one.

**Docker is a prerequisite for running the examples, never for using the library.** The bundled
`docker compose` file provides the triple store the scenarios and the factory need.
