# Views and Projection

This page explains how consumers control what they see of the knowledge graph, and what peers see of them through the REST surface. A **ClassScope** defines a reusable view rooted at the node a consumer cares about. There is no single "the datamodel" for a resource — each consumer projects what it needs from where it needs it. The middleware enforces a northbound projection that removes protocol metadata before any data is fetched, ensuring connection details never reach the REST surface.

## Consumer-Defined Views

A view belongs to its consumer and is configured in the code that embeds the middleware library, not in the ontology. The `SemanticMiddleware` constructor accepts a `class_scope` parameter carrying the **user view** — the northbound projection that is materialized and REST-exposed.

```python
mw = SemanticMiddleware(
    mode="resource",
    resource_iri=tui.TransferUnit1,
    service_class=tu.TransferUnitService,
    class_scope=ClassScope.from_property_chains([
        [TU.hasConveyorBelt, TU.hasConveyorSpeed],
        [TU.hasConveyorBelt, TU.hasConveyorPosition],
        [TU.hasLightBarrier, TU.isOccupied],
    ]),
    ogm=ogm,
)
```

Each consumer defines its own ClassScope rooted at the node it cares about. A connector for the same resource does not see the TransferUnit at all — its scope is rooted at the component it serves. Neither is "the" datamodel.

## Rootedness Makes Top-Level Resources

Being a **Root** is what makes a node a top-level resource rather than a component. TransferUnit1 is a unit and ConveyorBelt1_left is a part of it **because a view is rooted at the unit and reaches the belt**, not because of any part-of relation in the graph. There is no composition property in the vocabulary, and none is needed. Rootedness is a property of the view, not an intrinsic property of the resource.

## A Scope Terminates at a Parameter

A ClassScope selects *which* parameters a consumer sees. It **cannot select within one**. Below a complex property, the chain is **silently discarded** — no exception is raised. The following third element is dropped without warning:

```python
ClassScope.from_property_chains([
    [TU.hasConveyorBelt, TU.hasConveyorSpeed, INF.hasValue],  # <- silently discarded
])
```

A view names properties down to the parameter and no further. The parameter blanknode's contents are fixed by the TBox restriction, identical for every consumer. A scope chooses which parameters, never which parts of one.

## Empty Projections Are Legitimate

Omitting `class_scope` falls back to an unscoped fetch that materializes the `id` alone. This is not an error condition. A resource whose view is a bare individual serves a one-field datamodel and starts normally. The middleware acts on a projection; the knowledge graph holds the information.

Fetch and commit are two different mechanisms, so their handling of a property outside the scope differs — do not conflate them. A **fetch** silently discards a property the scope does not name: it simply does not appear in the materialized model. A **commit** is strict the other way: writing an attribute the scope did not declare raises `ValueError: object has no field ...` during scope hydration. Silent discard is a read-path rule; the `ValueError` is a write-path rule.

## The Northbound Projection Prunes Before Fetching

The middleware builds its northbound view by **removing protocol metadata from the ClassSpec before fetching**. It passes the pruned spec to `OGM.fetch(class_spec=…)`. What counts as protocol metadata is **read from the ontology at every startup**, per parameter. The pruning runs unconditionally for every connector wiring, including one that wires nothing.

Walking upward from one parameter property:

| Level | Contributes | Verdict |
|---|---|---|
| The parameter property's own range | value, unit | **keep** — domain content |
| Protocol markers between it and `inf:isInterfaceAccessibleParameter` | broker, topics, endpoints | **delete** |
| `inf:isInterfaceAccessibleParameter`'s own range | `inf:accessMode` | **keep** — northbound-safe |

The projection therefore happens **before** any connection metadata is read out of the store. The northbound model has no field able to carry a broker address.

## The Projection Runs for Every Connector Wiring

Registry construction, recognition and pruning all run for every connector wiring. Only `connect()` and the sync registration are gated. If pruning were gated on `autoregister_connectors`, an **Inspecting** wiring would serve broker addresses. This is the least privileged wiring — it connects nothing. The least-privileged instance would leak the most. All three wirings (Driving, Observing, Inspecting) serve byte-identical northbound payloads.

## Connection Metadata Is Structurally Absent from REST

A consumer **cannot read connection metadata over the REST surface**. This is structural, not a permission check. The broker address is physically absent from the served model. Hiding it stops a peer *learning* how to bypass the middleware from this surface. It does not stop someone who already knows the broker from talking to it. Real access control involves role-based named graphs and a governed environment — this is future work.

## Routes Reach the Complex Property

The REST route generator recurses through nested `Identifiable` models and **terminates at a `PropertyValueKind.COMPLEX` property** whose blanknode-backed dict is the atomic addressable unit:

```
GET|PUT /{TransferUnit}/{tui:TransferUnit1}
                       /{tu:hasConveyorBelt}/{tui:ConveyorBelt1_left}
                       /{tu:hasConveyorSpeed}
     body: [{ inf:hasValue: 3.0, tu:hasUnit: "m/s", inf:accessMode: "readwrite" }]
```

Every value in the materialized tree is a **list**. RDF multiplicity applies, scalars included. **GET and PUT are symmetric** — PUT accepts exactly what GET returns. The generator emits **no PUT route at all** for a read-only parameter. A write to a light barrier is a 405 from the server, not a runtime rejection. Path segments are literal, not FastAPI path parameters — one concrete route exists per individual.

## One Closed-World Moment

The architecture has **exactly one closed-world moment: SHACL at admission**. Everywhere else, the absence of a triple means **unknown**, never **false**. Nothing the OGM derives from OWL may produce a required field. It may not reject an unknown property. It may not treat its own view of a node as that node's full extent. Requiredness, closedness and cardinality enforcement belong to SHACL shapes the triple store evaluates when a write is admitted. A consumer extending the library does not get to introduce a second closed-world moment.

OWL restrictions supply **type**, never requiredness. `allValuesFrom` constrains all values instead of asserting one exists. `someValuesFrom` documents intent but must not be read as a gate. An existential axiom says a value exists *in the world*; it does not say the triple is in your graph.
