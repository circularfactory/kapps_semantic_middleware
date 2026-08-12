# Writing to the Graph and to Devices

This page describes every path by which a consumer causes data to change — what gets written, where it lands, and what else moves as a consequence. The middleware distinguishes two directions: **northbound** writes into the knowledge graph (the served datamodel), and **southbound** writes out to devices through connectors. Understanding which path your code takes determines whether a change persists, whether a device receives a command, and whether unrelated resources get notified.

:::{dropdown} Terms this page assumes
{term}`Parameter` · {term}`Projection` · {term}`Resource`
:::

## All Graph Writes Go Through the OGM

Every knowledge-graph write must go through `kapps_ogm.OGM`. Use `OGM.create` for new typed individuals. Use `OGM.commit` for adding, removing, or replacing properties. No write issues raw SPARQL UPDATE. No write calls `kapps_triplestore_interface` mutation methods directly. Reads may use the access module (`ogm.db`) directly; only writes are constrained.

Writes that bypass the OGM skip validation and break atomicity guarantees. A replacement of a cardinality-constrained property requires the removal and insertion to apply in one transaction. The intermediate state — zero possessors during a handover, for example — fails validation without atomicity.

## Persistence Writes Name the Changed Field

A write into persistence carries a second piece of information beside the value: which region of the model actually changed. `PersistedConnector.consume` and `_notify_synced_connectors` take it as `changed: ConnectionInfo`, and the fan-out notifies only the connectors that region covers.

```python
# Correct: names the specific field that changed
consume(value, changed=ConnectionInfo(model_id, contained_model_id, field_id))

# Incorrect: None means "the whole model changed"
consume(value, changed=None)  # notifies everything
```

Matching is by prefix, not equality. An unspecified level in a `ConnectionInfo` means every value at that level. A whole-model write reaches every connector. A field-level write reaches only that field's connectors.

**Silent failure:** If a write does not name its field, an unscoped notification fans out to every synced connector. Devices get written that nobody touched. For a settable parameter, an unnecessary notification is a fabricated command — a write leg re-deriving at the wrong moment publishes the last value the device reported onto the topic the device treats as a setpoint. The device then reads its own actual state back as a fresh command.

## Northbound vs Southbound

**Northbound** writes go into the knowledge graph — the served datamodel that peers read over REST. These are `OGM.commit` operations on {term}`Parameter` nodes. The {term}`Projection` removes connection metadata before materializing the northbound view, so peers cannot learn broker addresses or bypass the middleware.

**Southbound** writes go out to devices through connectors. These happen automatically when a persistence write names a field that has a synced connector. The binding serializes the value and publishes it to the device's topic or endpoint.

A consumer action determines which direction fires:
- Calling `OGM.commit` on a Parameter → northbound only
- Calling `consume` on a synced connector → southbound only  
- A PUT to the REST API → both (writes graph, then notifies connectors)

## Inbound Messages Replace Whole Parameter Nodes

An inbound message from a device replaces the entire Parameter node. The binding reassembles static facets — unit, access mode — from cached metadata so a bare scalar does not blank the parts that did not change. Without this reassembly, an inbound scalar would wipe `hasUnit` and every other facet, because `update_persistence_with_value` does `setattr(contained_model, field_id, value)` — it replaces the whole list.

The formatter reassembles the blanknode from cached facets plus the live value. That is a pure function per message. It reads no current state and it needs no framework change.

## IRIs and Display

Any prettified or shortened form of an IRI is display only. Production code carries fully back-resolvable IRIs in their mangled form. The generated API documentation renders a readable form where it can, but nothing parses that form back.

**Silent failure:** A consumer that parses a displayed IRI, or round-trips one back into a query, gets a wrong answer with no exception raised. Use the mangled form (`IRI.lined`) for all production operations — REST path segments, datamodel field names, `svc:endpoint` triples. The mangled form is mechanically reversible to the original IRI. Pretty-printing is a rendering concern with no correctness weight.
