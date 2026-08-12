# Connector Binding

This page explains how the middleware wires itself to external systems — devices, peer middleware instances, and the knowledge graph itself. It covers the recognition chain that turns ontology declarations into live connections, how to add support for a protocol this library does not ship, and the failure modes that produce no exception.

## The Recognition Chain

A semantic connector registers itself from the knowledge graph. The chain runs once, at middleware construction:

1. **Domain property** — a complex property in your ClassScope, e.g. `tu:hasConveyorSpeed`.
2. **Interface property** — the property's `rdfs:subPropertyOf*` ancestry is queried against the connector registry. If it descends from `inf:isInterfaceAccessibleMQTTParameter`, the MQTT binding matches.
3. **Binding descriptor** — the registry returns the descriptor keyed on that interface property.
4. **Metadata fetch** — the binding reads the connection-metadata properties it declared (e.g. `inf:hasMQTTTopic`, `inf:hasMQTTBrokerIP`) from the graph.
5. **Connector build** — the binding yields one or more `Registration` objects, each naming a `connector_cls`, a `ConnectionInfo`, a `SyncDirection`, and a formatter.

Recognition runs over the **ClassSpec and the graph**, not over materialized instance data. Everything it needs — which properties are COMPLEX, which match an interface property — is available before any instance data is fetched.

```python
# The SPARQL pattern that decides a match
ASK { tu:hasConveyorSpeed rdfs:subPropertyOf* inf:isInterfaceAccessibleMQTTParameter }
```

## Registration Happens at Construction

**Critical:** Bindings are built and registered in the middleware **constructor**. Registering a connector later — during `on_start_up` or datamodel materialization — means the framework never calls `connect()` on it. Inbound traffic dies **silently**: the listener task never starts, the queue is never fed, and `receive()` blocks forever. Outbound may limp along because `consume()` reconnects on failure, making the fault one-directional and quiet.

```python
# Correct: bindings register during SemanticMiddleware.__init__
mw = SemanticMiddleware(
    mode="resource",
    resource_iri=tui.TransferUnit1,
    autoregister_connectors=True,
    connector_sync_direction=SyncDirection.BIDIRECTIONAL,
)
# By this point, all connectors are already in the framework's connection registry.
```

The framework calls `connect()` on everything in the connection registry **before** it runs `on_start_up` callbacks. Registration in the constructor avoids the silent failure entirely.

## Adding a Protocol

To support a protocol this library does not ship, build a binding descriptor and inject it into the connector registry. A descriptor **references** its connector class rather than subclassing it, so a connector nobody here owns can still be made semantic.

```python
from kapps_semantic_middleware.connectors.semantic import semantic_connector, Registration
from kapps_semantic_middleware.vocabulary import INF
from transitional_sync_middleware.middleware.sync.synced_connector import SyncDirection

@semantic_connector
class VendorBinding:
    connector_cls = SomeVendorConnector
    interface_property = INF.isInterfaceAccessibleVendorParameter
    connection_metadata = (INF.hasVendorAddress, INF.hasVendorChannel)

    @staticmethod
    def build(metadata, conn_info, direction):
        address = metadata[INF.hasVendorAddress]
        channel = metadata[INF.hasVendorChannel]
        # Formatter reassembles static facets (unit, access mode) into the payload
        formatter = ... 
        yield Registration(
            SomeVendorConnector(address, channel),
            conn_info,
            SyncDirection.TO_PERSISTENCE,
            formatter,
            float
        )
        if direction is SyncDirection.BIDIRECTIONAL:
            yield Registration(
                SomeVendorConnector(address, channel + "_set"),
                conn_info,
                SyncDirection.FROM_PERSISTENCE,
                formatter,
                float
            )

# Inject after init
mw.connector_registry[INF.isInterfaceAccessibleVendorParameter] = VendorBinding
```

One binding may yield **two** connectors against one binding target — a read topic and a write topic — differing only in `sync_direction`. The framework's `ConnectionRegistry.connections` is `Dict[ConnectionInfo, List[str]]`, so both bind to one `ConnectionInfo`.

## Connector Wiring

An instance is described by its connector's **protocol** and **direction**, and by nothing else. These are constructor parameters of resource mode:

| Wiring | `autoregister_connectors` | `connector_sync_direction` | Behavior |
|---|---|---|---|
| **Driving** | `True` (default) | `BIDIRECTIONAL` (default) | Reads live values and drives the device |
| **Observing** | `True` | `TO_PERSISTENCE` | Reads live values, structurally unable to write |
| **Inspecting** | `False` | (ignored) | Nothing connected; structure and graph content only |

Direction is the most restrictive of two constraints: a parameter's `inf:accessMode` and the instance's wiring. A read registration is always emitted. A write registration is emitted only when the parameter is `readwrite` **and** the wiring permits a write. A monitor can never drive a writable belt, and a controller can never write a sensor — structurally, not by convention. An absent or unrecognized `accessMode` yields read-only.

## Shipped Protocols

Two bindings ship in the library:

**MQTT** (`connectors/mqtt_binding.py`) — reaches a device. Recognized by `inf:hasMQTTTopic` on the parameter blanknode. Metadata: `inf:hasMQTTBrokerIP`, `inf:hasMQTTTopic`, `inf:hasMQTTSetTopic` (present iff `accessMode` is `readwrite`), optional `inf:hasMQTTValuePath`, optional `inf:hasMQTTBrokerPort` (absent means 1883). Topic scheme: `TransferUnit<n>/<component>/<position>/<param>`, and a setpoint appends `_set`. Payload is raw scalar by default; the parameter's ontology datatype parses it. With `inf:hasMQTTValuePath`, a JSON envelope is read and written at that path.

**REST** (`connectors/rest_binding.py`) — reaches another middleware instance over its generated REST routes. Recognized by the resource's Service carrying an `svc:address`; no parameter-local marker is needed. A peer middleware is a device as far as the seam is concerned. The route is structural: address plus structural path derived from the datamodel tree is a complete binding. See `06-views-and-projection.md` for the route shape.

## Connection Metadata and Projection

A binding descriptor declares the connection-metadata properties its protocol needs. **The same set is what gets hidden from the northbound surface.** The projection removes protocol properties from the ClassSpec before fetching, so a peer cannot learn the broker address and bypass the middleware. What counts as protocol metadata is read from the ontology at every startup: everything contributed by an Interface property strictly between the Parameter's own property and `inf:isInterfaceAccessibleParameter`.

See `06-views-and-projection.md` for the hiding mechanism.

## Transport

A **transport** is what a connector dials — an MQTT broker, and nothing else so far. A Parameter's connection metadata **names** a transport by address; it never provides one. This library ships no transport implementation. The middleware may be asked to **ensure** a transport exists at a declared address before it registers the first connector aimed there:

```python
def my_starter(host: str, port: int) -> None:
    # Start amqtt broker, or verify one is reachable, or ...
    ...

mw = SemanticMiddleware(..., ensure_transport=my_starter)
```

The hook is called once per distinct `(host, port)` during construction. The library never starts a broker, never probes one, never stops one, and never reads a return value. It states a need; the deployment meets it.

## The Graph-Facing Connector

Framework constructs (workflows, synced connectors) read and write the knowledge graph through `KnowledgeGraphConnector`, not by calling `ogm.fetch`/`ogm.commit` directly. This connector implements `transitional_sync_middleware`'s `Connector` protocol (`connect`/`disconnect`/`provide`/`consume`), wrapping OGM operations. Any future MES-style synchronization pattern (`SyncedConnector`, `SyncRole`/`SyncDirection`) that `transitional_sync_middleware` provides for other connectors becomes available for knowledge-graph synchronization without new plumbing.

```python
# Internal: all graph access goes through this connector
from kapps_semantic_middleware.connectors.knowledge_graph_connector import KnowledgeGraphConnector
```

This also becomes the extension point for `@state`'s value source. Any `transitional_sync_middleware` connector can back a StateProperty's getter — an `OpcUaConnector` reads a PLC register, an `MqttClientConnector` subscribes to a topic. Reuse the existing IT/OT bridge directly.
