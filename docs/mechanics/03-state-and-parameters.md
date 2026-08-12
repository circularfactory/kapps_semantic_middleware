# State and Parameters

This page describes how resource state is modeled as **Parameters** in the knowledge graph, how they are wired from domain Python, and the two patterns for persisting their values. A Parameter is a single graph node carrying both its current value and the protocol metadata needed to reach the device — recognized by the **Interface property** its domain property specializes, never by a class.

## What a Parameter Is

A Parameter is one blank node attached to a resource via a domain property. That node holds the value, unit, access mode, and connection metadata (topic, broker) together. The node has **no named `rdf:type`**. Recognition works by matching the domain property against the **Interface property** hierarchy (`rdfs:subPropertyOf*`), not by inspecting the node's class.

```turtle
tui:ConveyorBelt1 tu:hasConveyorSpeed [
    tu:hasUnit "m/s" ;
    inf:accessMode "readwrite" ;
    inf:hasMQTTTopic "TransferUnit1/ConveyorBelt/left/speed" ;
    inf:hasMQTTBrokerIP "127.0.0.1"
] .
```

If you query for instances of a Parameter class, you get nothing. Match on the property instead.

## Registration from Domain Python

The common case requires no Python at all. The middleware reads the ontology at construction, resolves each parameter's interface from the instance data, and wires connectors automatically.

Use `@mw.state` only when retrieval or actuation is more complex than a direct connector mapping. Where the decorator is used, only marked fields become discoverable — auto-promoting every field would flood the graph with plumbing.

`@mw.state` takes `capability_class` and `state_property_class` as **keyword-only** IRIs, both required, plus an optional `name`. Both classes must pre-exist in the ontology. See `02-workflow-registration.md` for the registration rules the decorator shares with `@mw.workflow`.

```python
class TransferUnit:
    @mw.state(
        capability_class=TU.ConveyorSpeedCapability,
        state_property_class=TU.ConveyorSpeedState,
    )
    def conveyor_speed(self) -> float:
        ...
```

The surrounding class is the consumer's own domain model — a plain Python class, a `@dataclass` in the examples. **There is no base class to subclass and no decorator the class itself needs.** `DataModel` and `DataModelRebuilder` are internal wrappers the middleware builds around your class; the consumer neither imports nor extends them. What decides which properties materialize is the `ClassScope` passed to `SemanticMiddleware`, not the class's own type. If an instance already carries connection metadata in the graph **and** a `@mw.state` decorator is bound to the same field, the middleware warns about the conflict.

## Committed Value vs. Locator

Whether a Parameter's value lives in the graph is the domain's choice. The middleware enforces neither pattern.

**Committed value** — the data point changes slowly. Domain code commits it on change; the graph holds the value. No `@mw.state` is involved.

**Locator** — the data point changes fast. The graph holds only metadata (unit, access mode, topic, broker). The live value exists only in the datamodel and over REST. An unobserved Parameter reads as `hasValue: []` — meaning "not yet observed", not zero. Consumers must handle empty values.

```python
# Locator pattern: value never committed
datamodel.conveyor_speed = 12.5  # lives in memory/REST only
ogm.commit(datamodel)            # writes metadata, not the value
```

## The Shape Comes from the TBox Restriction

A Parameter's materialized shape is determined by the **TBox restriction** on its property's `rdfs:range`, not by the instance data. Anything the restriction does not declare is dropped at materialization **with only a warning logged**.

This is a silent-failure trap. If a connector needs a metadata field (topic, broker) and the TBox restriction omits it, the field never arrives at the connector. Declare all required metadata in the ontology restriction.

```python
# If restriction omits inf:hasMQTTBrokerIP, the topic arrives but broker is missing
# Connector fails silently; only a warning is logged
```

## Access Mode Is a Facet

Whether a Parameter is externally settable is expressed as `inf:accessMode`, valued `"read"` or `"readwrite"`. This is a facet on the one node, not a subclass distinction.

Value, unit and access mode are read and written together as one atomic dict. A `readwrite` parameter exposes GET and PUT; a `read` parameter exposes GET only, with no PUT route and no outbound connector. A sensor is structurally unwritable through the advertised surface.

## Static Facets

Parts of a Parameter that do not change with a reading — unit, access mode — are **static facets**. They are captured at wiring time and reassembled into the payload on every inbound message.

Without this reassembly, a bare inbound scalar would blank the unit and access mode in the model served over REST.

## When It Is Not a Parameter

A complex property that matches no registered connector is **not** a Parameter. It is ordinary data the consumer asked for — displayed and readable, with nothing wired. Only properties specializing an **Interface property** become Parameters with active connectors.
