# Concepts

## Why a knowledge graph sits in the middle

A conventional middleware moves values between a machine and an application. This one first asks the graph *what the machine is* and *what it can do*, then moves the values. Registration, discovery and execution are three views of the same triples. The graph holds the authoritative description of each resource; peers query it to learn what is available and how to reach it.

## The components

Several packages work together:

- **kapps-ogm** — handles all knowledge graph reads and writes through a validated object-graph mapping layer.
- **kapps-triplestore-interface** — provides raw triple-store access where the OGM has no equivalent yet, such as instance discovery.
- **transitional-sync-middleware** — the synchronization layer, a transitional fork of `aas-middleware` carrying four synchronization fixes, incrementally reimplemented locally. It is retired within a few releases; do not build on it directly.
- **The triple store** — the examples bring one up with `docker compose` from the `docker/` directory that `kapps-examples` copies out. It runs the ruleset `rdfsplus-optimized`.
- **Per-unit in-process broker** — each middleware unit runs its own MQTT broker for southbound device communication.

## Which reads are inferred and which are asserted

The triple store runs the ruleset `rdfsplus-optimized`, and the examples' store configuration sets it. A query reads the inferred statements as well as the asserted ones unless it asks otherwise, and both ontologies carry `owl:imports`. The reads that go through the OGM, such as `fetch`, see the inferred statements too, except that the OGM reads the data of an anonymous node as asserted only. The recognition of a parameter's wiring reads the asserted statements only: a parameter node's only types that recognition could find by inference are anonymous restriction nodes, and counting them would inflate every result. So the wiring does not depend on inference.

A store without that ruleset returns fewer statements to the reads that are inferred. It does not make the projection leak: the projection fails closed. When the ontology cannot be read, or a protocol marker's range is not understood, it raises instead of serving.

## Glossary

```{glossary}

Service
    A distributed runtime entity wrapped by a single middleware instance, such as a door controller or a drilling-resource controller. Typed via a domain-specific subclass that must pre-exist in the ontology. One Service per middleware instance, not per Resource; several instances may be bound to one Resource.

Resource
    The physical or logical thing a Service wraps, such as a door, a transformer cell, or a drill. Required at construction time in resource mode.

Mode
    A construction-time choice governing what the middleware instance is for: `"resource"` wraps one Resource and exposes its Workflows and StateProperties; `"server"` is reserved and not built: constructing one raises; `"watchdog"` wraps no Resource and sweeps stale addresses from the graph.

Workflow
    An invokable function exposed by a Service, registered with a decorator. Realizes exactly one Capability. Typed via a domain-specific subclass that must pre-exist in the ontology, carrying a SHACL shape describing its arguments and return value.

Capability
    An ability a Resource currently has. Every Capability instance is created automatically by the middleware from a pre-existing Capability type the moment a matching Workflow or StateProperty is registered.

Operation
    The executable, resource-assigned form of a task. Links to a Capability. A caller creates an Operation in the graph and dispatches it to the resource that will carry it out. The graph-level unit of work exchanged between middleware instances.

Parameter
    A readable and/or settable state of a Resource, modelled as one graph node carrying its value, unit, and the metadata a protocol connector needs to reach the device. Recognised by the Interface property its domain property specializes, never by its class.

ClassScope
    A projection — a view — over the graph, expressed as a tree of property-chains rooted at a class. A view belongs to its consumer and is rooted at the node that consumer cares about. There is no single datamodel for a resource.

Root
    The node a ClassScope is rooted at. Being a root is what makes a Resource a top-level thing rather than a component. Rootedness is a property of the view, not an intrinsic property of the resource.

Projection
    The mechanism that keeps connection metadata out of the served datamodel. The middleware removes the protocol properties from the ClassSpec before fetching, and materializes the pruned spec. What counts as protocol metadata is read from the ontology per Parameter at every startup.

Binding descriptor
    The object that makes a connector semantic. It names the connector class it builds, the Interface property it binds to, the connection-metadata properties its protocol needs, and how to turn one Parameter's metadata into one or more framework registrations.

Heartbeat
    A resource-mode Service's periodic re-assertion of its own liveness. Refreshes a timestamp on its Service individual via an internal interval-based Workflow. Read by watchdog-mode instances to decide staleness.

Address vs. Endpoint
    `svc:address` is a Service's base URL, set on startup and removed on deregistration. `svc:endpoint` is the full, directly callable URL for one specific Workflow or StateProperty, also set on startup and removed on deregistration. They are distinct properties on distinct entity types.

```
