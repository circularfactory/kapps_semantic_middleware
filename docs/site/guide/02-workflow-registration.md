# Workflow Registration

This page covers how a resource-mode middleware instance declares the Workflows it exposes through the knowledge graph. It describes the registration decorators, the ontology prerequisites they depend on, and how the middleware materializes Capability instances from your code. For calling Workflows — dispatch, event triggers, and operation status — see {doc}`05-operation-coordination`.

:::{dropdown} Terms this page assumes
{term}`Workflow` · {term}`Capability` · {term}`Service` · {term}`Operation` · {term}`Resource` ·
{term}`Address vs. Endpoint`
:::

## Ontology Types Must Pre-Exist

The middleware never creates ontology classes. Every type referenced at registration time **must already exist** in the ontology as an OWL class with the correct IRI. This includes:

- A domain-specific subclass of `svc:Service` for the middleware instance itself
- A domain-specific subclass of `svc:Workflow` for each {term}`Workflow` you register
- A subclass of `cfc:Capability` that the Workflow realizes

Registration decorators accept IRIs to these classes, not Python types. If the middleware cannot find a referenced class in the graph at startup, it fails immediately with an error indicating which class was missing. You cannot register a Workflow against a {term}`Capability` type that does not exist, and you cannot omit the `workflow_class` parameter expecting the middleware to derive one.

This requirement ensures that hundreds of identical resource instances (many doors, many controllers) all share the same Workflow and Capability classes rather than minting duplicates at runtime. Author these classes once, before any middleware instance starts.

## Registration Decorators

A Workflow is registered by decorating a Python function with `@mw.workflow()`. The decorator requires the Capability class IRI and the Workflow class IRI as keyword-only arguments:

```python
from kapps_semantic_middleware import SemanticMiddleware

mw = SemanticMiddleware(
    mode="resource",
    resource_iri=EX.DoorController1,  # IRI, not an object
    service_class=EX.DoorService,     # Must pre-exist in ontology
)

@mw.workflow(
    capability_class=EX.DoorOpenCapability,   # Must pre-exist in ontology
    workflow_class=EX.DoorOpenWorkflow,       # Must pre-exist in ontology
)
def open_door(direction: str) -> bool:
    """Open the door in the specified direction."""
    ...
```

Both `capability_class` and `workflow_class` are required in resource mode. Omitting `capability_class` raises a `ValueError`. The decorators `@mw.workflow` and `@mw.state` are **resource-mode only** — calling either on a middleware instance in any other mode raises a `RuntimeError`.

The middleware performs two actions when this decorator runs:

1. It creates a **Capability instance** automatically from the pre-existing Capability *type*. You never instantiate Capabilities by hand — one instance is created per running process the moment the Workflow is registered.
2. It registers the Workflow instance, linking it to the Capability via `svc:realizedByWorkflow`.

Every Workflow realizes exactly one Capability. An {term}`Operation` addressed to that Capability resolves to this Workflow through the chain `Operation → implementsCapability → realizedByWorkflow`. See {doc}`05-operation-coordination` for how Operations are dispatched and executed.

### State Property Registration

The sibling decorator `@mw.state` registers a readable and/or settable state as a GET-only REST endpoint:

```python
@mw.state(
    capability_class=EX.DoorStatusCapability,
    state_property_class=EX.DoorOpenState,
    name="door_open_status",  # Optional
)
def get_door_status() -> bool:
    """Return whether the door is open."""
    ...
```

It takes `capability_class`, `state_property_class`, and an optional `name`. Like Workflows, the Capability instance is created automatically at registration. The live value is never written to the graph — only the stable `svc:hasEndpoint` triple is written at registration time. Peers read the current value by invoking the endpoint, not by querying the graph.

## Signatures and SHACL Shapes

The shapes that describe a Workflow's arguments (precondition) and return value (outcome) belong to the domain's ontology. Its authors write them, together with the Workflow class, and the knowledge graph already holds them when the middleware starts. The middleware does not write, change or attach them: registration writes no shape. A shape sits on the Workflow *class* (`sh:targetClass`), so all resource instances of the same Workflow class share it.

```python
@mw.workflow(
    capability_class=EX.DoorOpenCapability,
    workflow_class=EX.DoorOpenWorkflow,
)
def open_door(direction: str) -> bool:  # The signature is not turned into a shape
    ...
```

The signature of a registered Workflow is visible on the API page (`/docs`) of the running instance. There a reader sees the arguments and the return type of every Workflow the instance serves.

To draft a shape for a new Workflow class, an ontology author can run `build_workflow_shape` from `kapps_semantic_middleware.shacl_interop` on the function. It introspects the signature and returns a SHACL graph: an argument property per parameter (`{workflow_class_iri}#param_{name}`) plus a `#return` property. A function with no arguments gives an empty precondition shape. The result is a starting point that the author reviews and puts into the ontology. Nothing in the middleware calls the helper.

## Address and Endpoint

When a resource-mode middleware starts, it writes two kinds of location metadata to the graph:

- **`svc:hasAddress`** on the {term}`Service` individual — the base URL of this middleware instance
- **`svc:hasEndpoint`** on each Workflow and StateProperty individual — the full, directly callable URL for that specific entry point

Both are written at registration time. Both are removed when the middleware deregisters (on clean shutdown) or when a watchdog marks the Service stale (on heartbeat failure). Confusing these two is a common mistake: callers resolve a Workflow IRI and read its `svc:hasEndpoint` directly — they do not walk through the Service's `svc:hasAddress` and append a route convention.

Storing the endpoint on the Workflow itself means any caller with just a Workflow IRI can invoke it with one property read. The trade-off is duplication: the same host and port appear in both `svc:hasAddress` and every `svc:hasEndpoint` under that Service.

## Host and address

Three settings decide where a service can be reached, and they are easy to mix up:

- **The server runner's listen host** is where the server listens. You set it where you run the application, for example `uvicorn.run(app, host="0.0.0.0", port=8000)`. `127.0.0.1` accepts connections from this machine only; `0.0.0.0` accepts them on every interface.
- **The constructor's `host`** is used only to build the default address, `http://<host>:<port>`. It does not make the server listen anywhere.
- **The constructor's `address`** is what other participants read from the graph. It is the one that has to be reachable from where they run.

The default is local on purpose: `http://127.0.0.1:<port>` is safe to start without thinking about the network, and it works whenever caller and service share a machine. A participant on another machine reads the same address and calls itself. To be reachable from other machines, listen on an interface they can reach and advertise an address on it:

```python
# Where you run the server: listen on every interface.
uvicorn.run(app, host="0.0.0.0", port=8000)

# In the middleware: advertise an address of this machine, not a loopback one.
middleware = SemanticMiddleware(..., address="http://192.168.1.20:8000")
```

Exposing the service makes its endpoints reachable from the network without authentication, and a firewall, WSL or Docker may need a port forwarded. An instance that advertises a loopback or an unspecified address (`0.0.0.0`) says so on its dashboard, in a box on the System tab that lists the machine's addresses and these settings.

## Vocabulary Layering

Your domain ontology must fit into a three-module stack:

- **`cfc:` (Core)** — Published, external, superior. Includes `cfc:Operation`, `cfc:Capability`, `cfc:Resource`, `cfc:Task`. Import and specialize Core; never modify it. Possession uses Core's `cfc:hasPossessor` / `cfc:hasPossessedWorkpiece` directly, in the possession extension `kapps_semantic_middleware.possession`; registration itself writes only the Capability link `cfc:hasCapability`.
- **`mes:` (MES)** — Domain-facing. This is where your manufacturing-execution vocabulary lives. Domain experts author terms here. Currently carries handover-ability predicates (`mes:hasHandoverAbility` and its enumerated individuals), which the possession extension's `handover(mw, mode=...)` reads; the generic core names none of them.
- **`svc:` (Service)** — Middleware-facing. Includes `svc:Service`, `svc:Workflow`, `svc:StateProperty`, `svc:hasAddress`, `svc:hasEndpoint`, `svc:hasLastHeartbeat`, and Operation status/provenance vocabulary. Domain code does not touch this layer.

When you introduce new domain terms (a new device type, a new capability kind), author them in `mes:`. Reference Core types from `cfc:`. Never add domain vocabulary to `svc:` — that module is reserved for reachability and coordination state only.

Your Workflow and Capability classes will typically be `mes:` subclasses that specialize Core patterns, while their runtime instances and endpoints live in `svc:`.
