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
| [`docs/mechanics/05-operation-coordination.md`](docs/mechanics/05-operation-coordination.md) | Dispatch, the event trigger, pull-and-run, the status lifecycle, handover, recovery |
| [`docs/mechanics/06-views-and-projection.md`](docs/mechanics/06-views-and-projection.md) | Defining a ClassScope, what a view cannot select, what the northbound projection removes |
| [`docs/mechanics/07-writing-to-the-graph-and-to-devices.md`](docs/mechanics/07-writing-to-the-graph-and-to-devices.md) | Every path that causes a write, naming the field that moved, northbound vs. southbound, IRI handling |
| [`docs/mechanics/08-provisioning-and-seeding.md`](docs/mechanics/08-provisioning-and-seeding.md) | **Read first if `ogm=` is a mystery.** The bootstrap order, loading the shared ontologies, authoring the domain TBox, and seeding the instance data a Parameter needs |

### A note on the citations you will find in the glossaries

The `CONTEXT.md` files cite architecture decision records as **ADR 00nn** and **root ADR
000n**. Those records are development-repository material and are **not part of this
distribution** — no copy of them ships, and the release rewrites every path that pointed
at one, so a citation here is never a link you can follow.

**Do not go looking for them.** The citations are provenance markers, not links. Every mechanism a
glossary cites a record for is explained in full by the mechanics page covering it — ADR 0004's
subject is the "Address and endpoint" section of
[`02-workflow-registration.md`](docs/mechanics/02-workflow-registration.md), ADR 0006's is "The
graph-facing connector" in [`04-connector-binding.md`](docs/mechanics/04-connector-binding.md), and
so on. If a citation seems to point at something the mechanics pages do not cover, that is a
documentation bug worth reporting, not a missing file worth hunting.

## Where the names come from

This library re-exports much of its surface from its three dependencies, so the import you need is
often *not* from `kapps_semantic_middleware`. Use this table rather than guessing.

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
| `DataModel`, `Reference`, `Identifier` | `kapps_semantic_middleware` (re-exported from `transitional_sync_middleware`) |

Binding internals, if you are adding a protocol, live under
`kapps_semantic_middleware.connectors` — `semantic`, `mqtt_binding`, `rest_binding`,
`knowledge_graph_connector`, `wiring`.

> `kapps_semantic_middleware/ontology/` is **not** a Python module. It is the directory holding the
> three vocabulary files (`core.ttl`, `mes.ttl`, `service.ttl`). Importing from it fails.

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

### These fail loudly

**Every class you register against must already exist in the ontology.** The middleware creates
instances automatically but never mints classes. A domain-specific subclass of `svc:Service`,
`svc:Workflow` or `svc:StateProperty`, and the `cfc:Capability` subclass a Workflow realizes, must
all pre-exist. Startup fails immediately, naming the missing class.

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

**There is one Service per middleware *instance*, not per Resource.** Several instances may wrap
one Resource — a controller and a monitor, say — each owning its own Service node, address and
heartbeat, all linked by `svc:isServiceOf`. Discovery may therefore return several Services for one
Resource; select by address or by advertised capability rather than assuming exactly one.

**Only `resource` and `watchdog` modes are implemented.** `server` is reserved and raises
`NotImplementedError`. A graph-*consuming* participant — a planner, a controller, a mobile robot —
is a resource-mode instance with its own Resource, not a server-mode one.

**Docker is a prerequisite for running the examples, never for using the library.** The bundled
`docker compose` file provides the GraphDB the scenarios and the factory need.
