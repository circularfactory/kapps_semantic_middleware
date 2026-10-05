# Writing to the Graph and to Devices

This page describes every path by which a consumer causes data to change — what gets written, where it lands, and what else moves as a consequence. The middleware distinguishes two directions: **northbound** writes into the knowledge graph (the served datamodel), and **southbound** writes out to devices through connectors. Understanding which path your code takes determines whether a change persists, whether a device receives a command, and whether unrelated resources get notified.

:::{dropdown} Terms this page assumes
{term}`Parameter` · {term}`Projection` · {term}`Resource` · {term}`Operation` · {term}`ClassScope` ·
{term}`Heartbeat`
:::

## All Graph Writes Go Through the OGM

Every knowledge-graph write must go through `kapps_ogm.OGM`. Use `OGM.create` for new typed individuals. Use `OGM.commit` for adding, removing, or replacing properties. No write issues raw SPARQL UPDATE. No write calls `kapps_triplestore_interface` mutation methods directly. Reads may use the access module (`ogm.db`) directly; only writes are constrained.

Writes that bypass the OGM skip validation and break atomicity guarantees. A replacement of a cardinality-constrained property requires the removal and insertion to apply in one transaction. The intermediate state — zero possessors during a handover, for example — fails validation without atomicity.

## State Transitions: `mw.commit()` and `mw.fetch()`

A participant writes through `mw.commit()`, a context manager that wraps `OGM.commit`. It commits a state transition, and the store admits or refuses it, before the work that carries the transition out begins. A read is `mw.fetch()`, a plain call that wraps `OGM.fetch`: a read has nothing to guard.

```python
from kapps_ogm import Update
from kapps_semantic_middleware.vocabulary import SVC


def status(value):
    return [Update(instance_iri=op, data={str(SVC.operationStatus): [value]})]


async with mw.commit(
    op, class_scope=scope, entry=status("running"), exit=status("done"), error=status("failed")
) as transition:
    torque = await robot.screw(transition.node.instance)  # the work; no transaction is held
    transition.update(op, {str(SVC.executionResult): [torque]})  # written at the clean exit
```

`entry`, `exit` and `error` are lists of kapps_ogm `Create` and `Update` changes. Each is written as one commit, so each may span several instances. `mw.commit()` knows no vocabulary of its own: any datamodel and any state transition fit. The object it yields is the transition.

| Step | What happens |
|---|---|
| Entry | The entry transition is committed. If the store refuses it, its error is raised, nothing is written, and the body does not run. Then the instances named first are fetched for the body: materialized under a {term}`ClassScope` given as `class_scope`, or as references without one. They are `transition.nodes` by IRI, and `transition.node` when there is one. |
| Body | Domain code, such as a robot's move. It holds no transaction and no connection of the triple store, so {term}`heartbeats <Heartbeat>` and other writes go on while it awaits. `transition.update(instance, {property: [values]})` changes the datamodel; nothing of it is written yet. |
| Clean exit | The body's changes and the exit transition are committed as one commit. Changes to an instance that a change names merge into one change, because kapps_ogm refuses two changes that write one instance. |
| Error in the body | The body's changes are dropped, the error transition is committed, and the error is raised again. |
| Refused exit | The error transition is committed, and the store's error is raised. |

**Only `transition.update()` changes the graph.** A change made to `transition.node` or to its `.instance` is not committed. An assignment to an attribute that the transition does not have, such as `transition.hasTimeSeriesReference = ref`, raises `AttributeError` instead of being lost.

**A body that sets a property that the exit transition also sets, on the same instance, wins.** Its value is committed, and the exit transition is skipped as a whole. So a body that sets an {term}`Operation`'s status itself keeps it. An error still commits the error transition.

**Inside a coroutine, use `async with`.** It runs every graph call in a worker thread, so graph I/O never blocks the event loop. The synchronous `with` makes the same calls in the calling thread and blocks it. Nothing stops it from being used on the event loop, where it stalls every other task, heartbeats included. **A body that blocks its thread** (`time.sleep`, a blocking driver call) must run in a worker thread, with the synchronous `with` inside that thread, as callbacks and notification handlers run. The heartbeats go on in the event loop meanwhile.

`exit` may be a function that returns the changes, called at the clean exit, so the exit transition can depend on what the body did. `error` may be a function that takes the exception. A cancellation of the body under `async with` (a deadline of `anyio.fail_after` or `move_on_after` that runs out, a cancelled task) is an error of the work: the error transition is committed in a shielded cancel scope, then the cancellation is raised again. An interrupt or an exit (`KeyboardInterrupt`, `SystemExit`, `GeneratorExit`) writes no transition: the entry transition stays, as after a crash.

**The write goes to the graph only.** Every change goes to the middleware's named graph. The datamodel that the instance serves over REST does not change, and no synced connector hears of the write, so no device is written. In this library only a PUT on a parameter route notifies the synced connectors (see the next section). One notification may follow a commit: when it moves an Operation that has an issuer to `running`, `done` or `failed`, the middleware notifies the issuer (see {doc}`05-operation-coordination`).

**What a caller catches.** A refusal by the store, a SHACL refusal included, is `GraphDbException`, a `TripleStoreInterfaceError` (both from `kapps_triplestore_interface.exceptions`). kapps_ogm raises `ValueError` when a change does not fit its class, and when two changes of one transition write one instance. The store refuses a write whose precondition is false with `PreconditionFailedError`, a `TripleStoreInterfaceError` from the same module, and nothing of that commit is written. That is a `Create` of an instance that the store already holds, and a change that removes a value that is no longer there. `mw.commit()` does not catch it: for a `Create`, it signals a reused IRI. The merge reaches only the instance that a change names: when two changes both write a named instance that they nest with properties, kapps_ogm raises `ValueError` at the exit, after the error transition is committed. `mw.commit()` raises `ValueError` itself for a `Create` that carries data but no `class_scope` (a field of `Create`), before it is written: without one, kapps_ogm would drop a data property that the class does not know, with only a warning. An unreachable store raises `requests.exceptions.RequestException`. The middleware's own errors live in `kapps_semantic_middleware.errors`.

**A write whose precondition no longer holds is refused.** The OGM reads each instance that a commit changes, and the store removes only the values that the read found. When a value that a change replaces is already gone when the write arrives, the store writes nothing of that commit, and the call raises `PreconditionFailedError`. A write of another writer that lands before the read is not refused: the commit replaces its value.

## Creating an Instance: `mw.create()`

Something new is `mw.create()`; something that exists is `mw.commit()`. A new Operation for another resource is `mw.request()`, which creates it with `mw.create()` and then notifies the receiver (see {doc}`05-operation-coordination`). `mw.create(cls, scope, instance_iri=None)` is a context manager built on `mw.commit()`. It creates one new instance of the class `cls`, and `scope` is that class's {term}`ClassScope` or a predefined scope (below).

```python
with mw.create(EX.DecisionSnapshot, snapshot_scope) as snapshot:
    snapshot.update({str(EX.decision): ["unscrew"]})  # written at the clean exit
```

| Step | What happens |
|---|---|
| Entry | The middleware mints the IRI `<class>_<12 hex>`, unless `instance_iri` is given. The body reads it as the creation's `iri` (`snapshot.iri` above), so a nested creation can be linked to it before anything is written. A predefined scope checks its inputs. Nothing is read or written. |
| Body | The creation's `update({property: [values]})` sets the new instance's properties. A later call for the same property replaces the earlier value. A value that is an IRI, such as the `iri` of a nested creation, is a reference to that instance. |
| Clean exit, alone | One commit writes the instance's `Create`, the changes of its predefined scope, and the changes of every creation nested in it. If the store refuses the commit, its error is raised, and nothing is written. |
| Clean exit, nested | Inside the body of a `mw.commit()` or of another `mw.create()` of the same middleware whose write is still to come, the same changes join that write instead. The store admits or refuses them together with it, and an error in that body drops them. |
| Error in the body | Nothing is written or joined, and the error is raised again. Nothing exists yet that could carry an error transition. |

**A creation inside a body whose commit has already happened commits on its own.** The body of `mw.handover()` is such a body: its commit is its entry, and its clean exit writes nothing more. A creation inside a `mw.commit()` inside that body joins that `mw.commit()`. **A creation joins only a write of its own middleware**: one that goes through the same OGM into the same named graph. Inside a write of another middleware, such as a planner's creation inside a resource's commit in one process, it commits on its own. **`request()` commits its Operation on its own**, also inside another body, because its receiver reads the Operation after the notification. A creation in the body of `request()` joins the Operation's commit.

**A failed notification of `request()` undoes nothing.** The Operation, `queued`, and every instance created with it stay as the request's commit wrote them, and `request()` raises `NotificationUndelivered`. The receiver takes up its `queued` Operations when it starts.

After the clean exit, the creation's `changes` are the changes that it committed or joined: its own `Create` first, then those of its predefined scope and of the creations nested in it.

**The `Create` names every property that the scope names and every property that the body sets**, so kapps_ogm drops none of them. An assignment to an attribute of the creation, such as `transport.hasBox = box`, raises `AttributeError` instead of being lost. A `Create` of an IRI that the store already holds raises `PreconditionFailedError` at the commit, and nothing is written. **Inside a coroutine, use `async with`**, as for `mw.commit()`.

### Predefined scopes

The generic core knows only `svc:` and standard RDF. A rule of the circular factory enters `mw.create()` through a predefined scope: a ClassScope, plus a check of its inputs when `mw.create()` is entered, plus the changes that it adds to the same commit. Two come with the library, each in the topic module of its rule:

| Scope | Import from | What it requires, and what it writes |
|---|---|---|
| `workpiece(scope, possessor=...)` | `kapps_semantic_middleware.possession` | A possessor, any {term}`Resource`; `mw.resource` is the middleware's own. The Workpiece's `cfc:hasPossessedWorkpiece` points at the possessor's one possession state. When the possessor has no state yet, the same commit creates `<possessor>_possession` and links the possessor to it. |
| `operation(scope, capability=...)` | `kapps_semantic_middleware.operation` | The {term}`Capability` that the Operation implements. It writes `cfc:implementsCapability`. |

Without its required input, a predefined scope raises `ValueError` when `mw.create()` is entered, before the body runs and before any write. The body must not set a property that its scope sets: that raises `ValueError` at the exit, and nothing is written. A domain module gives a scope of its own as a `PredefinedScope` (from `kapps_semantic_middleware.commit`), whose `changes()` returns a `ScopeChanges`: the data that the scope adds to the `Create`, and the further changes. `AGENTS.md` lists which modules are specific to the circular factory.

### The detector's decision snapshot

The detector claims an Operation, decides, and records its decision in a snapshot, an instance with an identity of its own. The snapshot joins the commit that makes the {term}`Operation` `done`:

```python
with mw.claim_next(operation_scope) as claimed:
    decision = detector.decide(claimed.operation)
    with mw.create(EX.DecisionSnapshot, snapshot_scope) as snapshot:
        snapshot.update({str(EX.decision): [decision], str(EX.aboutOperation): [claimed.iri]})
    claimed.result = decision
# One commit: the Operation done, and the snapshot. If the store refuses it,
# the Operation is failed, and no snapshot exists.
```

### Opening a transport

The WMS opens a transport in one commit: the transport Operation, the box, and the box's first possession, by the WMS itself:

```python
from kapps_semantic_middleware.operation import operation
from kapps_semantic_middleware.possession import workpiece

transport_rule = operation(transport_scope, capability=transport_capability)
with mw.create(EX.BoxTransport, transport_rule) as transport:
    transport.update({str(SVC.operationStatus): ["queued"]})
    with mw.create(EX.Box, workpiece(box_scope, possessor=mw.resource)) as box:
        pass
    transport.update({str(EX.hasBox): [box.iri]})
# One commit: the transport, the box, and the WMS's possession state if it had none.
# Then the WMS hands the box to the origin with mw.handover().
```

**Known limit.** A Workpiece's first possession can race another write that creates the same possession state. The store then refuses the commit with `PreconditionFailedError`, and nothing of it is written; unlike `mw.handover()`, `mw.create()` does not try again.

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
