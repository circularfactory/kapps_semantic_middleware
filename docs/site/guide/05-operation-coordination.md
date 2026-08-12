# Operation Coordination

Services coordinate by passing Operations through the knowledge graph and firing an event trigger.
This page describes how a caller dispatches work, how a receiver pulls and executes it, and how
the system ensures durability and consistency without synchronous RPC. The graph is the source of
truth for operation state; the per-resource queue is an in-memory cache reconstructed at startup.

:::{dropdown} Terms this page assumes
{term}`Operation` · {term}`Capability` · {term}`Workflow` · {term}`Service` · {term}`Resource` ·
{term}`ClassScope`
:::

## Capability Resolution

An {term}`Operation` never references a Workflow directly. Resolution always follows a two-hop chain through
a {term}`Capability`. The Operation individual links to a Capability via `cfc:implementsCapability`. The
Capability links to exactly one {term}`Workflow` via `svc:realizedByWorkflow`. This indirection ensures
that callers address a *capability* rather than a specific function implementation. Planning services
discover work against Capabilities; the middleware resolves the concrete Workflow at execution time.

## Dispatching Work

Caller-side dispatch is a transactional context manager. The caller uses the `request` context
manager to create the Operation in the graph and notify the receiver atomically. The body of the
context manager populates the Operation properties. The atomic exit performs the graph write and
fires the receiver's event trigger. This guarantees that an Operation is never created without
notification, and notification is never sent for an uncommitted Operation.

```python
with mw.request(target_resource) as op:
    op.status = "queued"
    op.implementsCapability = capability_iri
    # Additional domain properties set here
# Exit commits the Operation and triggers the receiver
```

## The Event Trigger

Every resource-mode middleware exposes a built-in `execute()` Workflow on its REST API. The dispatch
process fires this trigger after committing the Operation. The trigger carries only the Operation
IRI; the payload lives entirely in the graph. The trigger returns immediately and does not block
on the work. It does not return a business result. Callers must not expect a synchronous response.
The trigger simply enqueues the Operation for later processing by the receiver's domain code.

## Operation Queue

Each resource maintains an in-memory queue of Operations addressed to it. This queue is durable
because the Operation and its status are written to the graph before the trigger fires. On startup,
a middleware reconstructs its queue by querying the graph for its own `queued` Operations. It also
reclaims any orphaned `running` Operations left from a previous session. A missed trigger loses no
work; the baseline is self-healing. Resources do not poll the graph continuously for new work.

## Status Lifecycle

An Operation moves through three states: `queued`, `running`, and terminal (`done` or `failed`).
Execution provenance is written as part of the terminal transition. The provenance records which
Workflow ran the Operation, when it ran, and the result. The status field itself serves as the
provenance record; there is no separate success flag. A `done` status implies success; `failed`
implies an exception or validation error during execution. Transitioning to a terminal state is
atomic and irreversible without dispatching a fresh Operation.

## Pull-and-Run

Receiver-side execution uses the `claim_next` transactional context manager. The domain code takes
the next `queued` Operation, sets it `running`, and runs the work in the body. The middleware
re-fetches the Operation under a domain-supplied {term}`ClassScope` to ensure the correct data view.
On atomic exit, the context manager records the terminal `done` or `failed` state. If the body
raises an exception, the transaction reverts and the status updates to `failed` with provenance.

```python
with mw.claim_next(scope=view_scope) as claimed:
    # Read the Operation's domain fields off `claimed.operation`
    # (re-fetched under the ClassScope you passed to claim_next).
    op = claimed.operation

    result = perform_work(op)          # physical actuation, calculations, etc.

    # Assign a str to `claimed.result` to attach a result. On clean exit the
    # middleware records it as `executionResult`, alongside `executedByWorkflow`
    # and `executionTimestamp`.
    claimed.result = result
# Exit records done/failed and dumps state on failure
```

Inside the block, `claimed.operation` is the read side — the Operation materialized under your `ClassScope` — and `claimed.result` is the write side. Setting `claimed.result` is optional; a body that only actuates need not set it.

## Handover Primitive

Bilateral coordination between participants uses the `mw.handover` primitive rather than a plain
dispatch. Handover models a timeless administrative possession switch preceded by a timed physical
preparation phase. The primitive is a context manager that checks preconditions in `__enter__` and
commits the switch in `__exit__`. It operates on Core's reified `cfc:PossessionState`. The switch
is a single atomic transaction that updates the Workpiece's `cfc:hasPossessedWorkpiece` link.

```python
with mw.handover(mode="Put", workpiece=wp, counterpart=other):
    # Physical transport and preparation
    # Coordinate with counterpart if needed
# Exit atomically switches possession state
```

The handover primitive ensures that the "possessed by exactly one" cardinality constraint is never
transiently violated. The old PossessionState is kept as implicit history. The middleware does not
model the physical transport itself; that logic belongs in the context manager body.

## Failure Recovery

If a resource dies mid-Operation, its stranded `running` Operations are handled centrally. A
watchdog-mode instance sweeps stale resources and transitions their orphaned `running` Operations
to `failed`. The failed status carries any dumped resource state available at the time of failure.
Recovery is never a silent physical replay. The planner or domain decides whether to dispatch a
fresh Operation. Physical operations are frequently not safely repeatable, so automatic re-actuation
is avoided. The watchdog ensures the graph does not retain stale `running` states indefinitely.
