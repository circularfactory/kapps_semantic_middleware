# Concepts

## Why a knowledge graph sits in the middle

A conventional middleware moves values between a machine and an application. This one first asks the graph *what the machine is* and *what it can do*, then moves the values. Registration, discovery and execution are three views of the same triples. The graph holds the authoritative description of each resource; peers query it to learn what is available and how to reach it.

## Why the reasoner is not optional

The middleware relies on the triple store's reasoner: `include_implicit` defaults to querying `FROM onto:implicit`, and both ontologies carry `owl:imports`. A store without a reasoner returns fewer triples and fails quietly, which is why GraphDB — not an in-memory graph — is a prerequisite.

## Glossary

```{glossary}

Service
    A distributed runtime entity wrapped by a single middleware instance, such as a door controller or a screwing-resource controller. Typed via a domain-specific subclass that must pre-exist in the ontology. One Service per middleware instance, not per Resource; several instances may be bound to one Resource.

Resource
    The physical or logical thing a Service wraps, such as a door, a transformer cell, or a screwing tool. Required at construction time in resource mode.

Mode
    A construction-time choice governing what the middleware instance is for: `"resource"` wraps one Resource and exposes its Workflows and StateProperties; `"server"` wraps no Resource and serves CRUD operations; `"watchdog"` wraps no Resource and sweeps stale addresses from the graph.

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
