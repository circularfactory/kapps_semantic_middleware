# Workflow Registration

This page covers how a resource-mode middleware instance declares the Workflows it exposes through the knowledge graph. It describes the registration decorators, the ontology prerequisites they depend on, and how the middleware materializes Capability instances and SHACL shapes from your code. For calling Workflows — dispatch, event triggers, and operation status — see {doc}`05-operation-coordination`.

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

It takes `capability_class`, `state_property_class`, and an optional `name`. Like Workflows, the Capability instance is created automatically at registration. The live value is never written to the graph — only the stable `svc:endpoint` triple is written at registration time. Peers read the current value by invoking the endpoint, not by querying the graph.

## Signature-Derived SHACL Shapes

The middleware generates SHACL shapes describing the Workflow's precondition (arguments) and outcome (return value) from the function's type hints. These shapes are attached to the Workflow *class* (`sh:targetClass`), not to individual Workflow instances. All resource instances sharing the same Workflow class share the same shape.

```python
@mw.workflow(
    capability_class=EX.DoorOpenCapability,
    workflow_class=EX.DoorOpenWorkflow,
)
def open_door(direction: str) -> bool:  # Type hints become SHACL shape
    ...
```

The precondition shape describes the arguments the underlying Python function requires. The outcome shape describes the return value. Both are derived automatically — you do not author SHACL by hand for workflow signatures.

Arguments are supported: `build_workflow_shape` introspects the signature and mints an argument property per parameter (`{workflow_class_iri}#param_{name}`) plus a `#return` property. A function with no arguments produces an empty precondition shape, which is valid. Zero-argument functions are what the test suite exercises most heavily, but that is a statement about coverage rather than a capability limit; the `open_door(direction: str)` example above works. Complex nested types may not yet be fully exercised.

## Address and Endpoint

When a resource-mode middleware starts, it writes two kinds of location metadata to the graph:

- **`svc:address`** on the {term}`Service` individual — the base URL of this middleware instance
- **`svc:endpoint`** on each Workflow and StateProperty individual — the full, directly callable URL for that specific entry point

Both are written at registration time. Both are removed when the middleware deregisters (on clean shutdown) or when a watchdog marks the Service stale (on heartbeat failure). Confusing these two is a common mistake: callers resolve a Workflow IRI and read its `svc:endpoint` directly — they do not walk through the Service's `svc:address` and append a route convention.

Storing the endpoint on the Workflow itself means any caller with just a Workflow IRI can invoke it with one property read. The trade-off is duplication: the same host and port appear in both `svc:address` and every `svc:endpoint` under that Service.

## Vocabulary Layering

Your domain ontology must fit into a three-module stack:

- **`cfc:` (Core)** — Published, external, superior. Includes `cfc:Operation`, `cfc:Capability`, `cfc:Resource`, `cfc:Task`. Import and specialize Core; never modify it. Possession uses Core's `cfc:hasPossessor` / `cfc:hasPossessedWorkpiece` directly.
- **`mes:` (MES)** — Domain-facing. This is where your manufacturing-execution vocabulary lives. Domain experts author terms here. Currently carries handover-ability predicates (`mes:hasHandoverAbility` and its enumerated individuals).
- **`svc:` (Service)** — Middleware-facing. Includes `svc:Service`, `svc:Workflow`, `svc:StateProperty`, `svc:address`, `svc:endpoint`, `svc:lastHeartbeat`, and Operation status/provenance vocabulary. Domain code does not touch this layer.

When you introduce new domain terms (a new device type, a new capability kind), author them in `mes:`. Reference Core types from `cfc:`. Never add domain vocabulary to `svc:` — that module is reserved for reachability and coordination state only.

Your Workflow and Capability classes will typically be `mes:` subclasses that specialize Core patterns, while their runtime instances and endpoints live in `svc:`.
