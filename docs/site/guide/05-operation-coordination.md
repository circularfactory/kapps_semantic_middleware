# Operation Coordination

Services coordinate by passing Operations through the knowledge graph and firing an event trigger.
This page describes how a caller dispatches work, how a receiver takes it in and runs it, and how
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

Caller-side dispatch is a transactional context manager, `request`. It is built on two other parts
of the middleware and repeats neither: `mw.create()` writes the Operation, and the delivery of
`mw.notify()` notifies the receiver. Its own part is discovery: before the body runs, it finds a
live Service that realizes the capability class (with `target_resource`, one of that resource). The
body populates the Operation properties. The exit then creates the Operation in one commit: its
type, its Capability link, the status `queued`, its issuer (`svc:issuedBy`, the Resource of the
calling middleware) and the populated properties. Only after the store has admitted that commit
does the exit notify the Service that discovery found. If the store refuses the Operation, nothing
is written and the receiver is not notified. If the notification fails, nothing is undone: the
Operation stays `queued`, and the caller gets `NotificationUndelivered`. The receiver takes up its
`queued` Operations when it starts, and the issuer is responsible for the Operation while it is
`queued`. So a notification is never sent for an uncommitted Operation. Use `request` for a new
Operation; `mw.create()` creates a new instance of any other class, and `mw.notify()` tells a
resource about an Operation that exists.

`request` is a `mw.create()` of the Operation under the `operation` scope, which writes the
Capability link (see {doc}`07-writing-to-the-graph-and-to-devices`). It commits on its own, also
inside the body of another `mw.commit()` or `mw.create()`, because the receiver reads the Operation
after the notification. A `mw.create()` in its body joins the Operation's commit. The body may not
set `svc:issuedBy`: `request` sets it, and raises `ValueError` before anything is written when the
body sets it too.

```python
with mw.request(capability_class=cap, operation_class=op_cls) as op:
    # The Operation's domain properties, if any, as {property IRI: [values]}.
    op.data[str(EX.hasScrew)] = [{"id": str(screw)}]
# The exit creates the Operation in one commit, then notifies the receiver
```

## The Issuer and the Issued Operation

After the block, `op` is an `IssuedOperation` (from `kapps_semantic_middleware.middleware`): the
Operation's IRI is `op.iri`, and its issuer is `op.issuer`. The issuer is responsible for the
Operation while it is `queued`; the Resource that claims it is responsible from `running` on. The
handle waits for both changes:

```python
if await op.claimed(timeout=30):  # True once claimed; False when the timeout passes first
    status = await op.ended()  # "done" or "failed"; TimeoutError when a timeout passes first
```

`op.wait_claimed(timeout)` and `op.wait_ended(timeout)` are the blocking twins, with the same
results, for code that runs in a worker thread. Never call them on the event loop of the issuer's
middleware: its event trigger must take the notification that ends the wait.

The middleware that claims the Operation notifies the issuer after its claim commit (`queued` to
`running`), and after its terminal commit (`done` or `failed`). It does so after every commit that
the store admitted and that moves an Operation with an issuer to one of those statuses, whichever
function made it: `claim_next`, `mw.commit()`, `mw.create()`, `mw.handover(operation=...)`, the
crash recovery at startup, or a watchdog's sweep. It sees such a move in the commit's own changes,
so every status commit notifies, also one that sets the status that the Operation already has; the
issuer ignores a repeat itself. If the issuer has no live Service, or more than one, or the post
fails, the claimer logs one line at WARNING and goes on: the status is in the graph. It is one
notification per such commit, and no extra write: two for an Operation that is claimed and then
ends, and one more for each repeat.

The issuer's middleware wakes the handle when such a notification arrives at its event trigger, and
the handle reads the status from the graph. A wait reads the status first, so a claim that came
before the wait is seen, and it reads the status once more when its timeout passes. Nothing else
wakes it, so a wait ends early only when the issuer's middleware is served and has exactly one live
Service. The issuer's notification handler sees each of these notifications. They are no work for
the issuer: an instance without a handler does not queue them, and the intake never gives them to a
workflow, also when an instance issued an Operation to a Capability of its own. To know this, the
intake reads the status and the issuer of an Operation that matches one of its workflows, in one
query, before the workflow runs. The read is in the graph, so the rule holds after a restart too.

The notification runs in the background. After the commit, the middleware hands it to one worker of
its own and goes on at once, so a slow or hanging issuer never delays the claimer. The worker sends
the notifications in the order of their commits, so the claim's notification of an Operation
leaves before the end's when one instance commits both. When another instance commits the end, for
example with `mw.handover(operation=...)`, each instance sends its own, and the two notifications
have no order. The handle reads the status from the graph, so the order does not change what a
wait returns. A stop of the instance, and the end of the process, send what still waits for up to
one second, then drop the rest and log one WARNING line with their number. The status is in the
graph either way, but a wait of the issuer's handle then ends only at its timeout.

## The Event Trigger

Every resource-mode middleware exposes a built-in event trigger on its REST API,
`POST /workflows/event_trigger/execute`. It is framework plumbing, not a `svc:Workflow` in the
graph. The dispatch process fires this trigger after committing the Operation. The trigger carries
only the Operation IRI; the payload lives entirely in the graph. The trigger returns immediately and
does not block on the work. It does not return a business result. Callers must not expect a
synchronous response. What the trigger does next depends on the receiving instance, never on the
Operation. With a notification handler, it puts the Operation into the receiver's intake (see
Notifications and the Intake below). Without one, it enqueues the Operation for later processing by
the receiver's domain code, unless the Operation is one that the receiver's Resource issued and
that is no longer `queued`: that notification is feedback (see The Issuer and the Issued
Operation above).

## Notifications and the Intake

A notification is a one-way message that names one Operation in the graph and carries nothing
else: no payload, and no result. It goes in both directions: down, "an Operation for you exists",
and up, "this Operation ended or changed". Nothing is written to the graph for it. It travels over
the same event trigger. Notifications are plumbing: they stand in for a triple store that cannot
trigger events itself, so they are not in the graph, and the graph has no kind of notified
Operation or notified Workflow.

The sender calls `mw.notify(operation=op, resource=r)`. The call finds the one live Service of `r`
(a Service with an `svc:address`), posts the IRI to its event trigger, and returns when the
receiver confirms receipt. It raises `NotificationUndelivered` (from
`kapps_semantic_middleware.errors`) when `r` has no live Service, when it has more than one,
and when the post fails, is refused or times out. It is a blocking call; async code runs it with
`anyio.to_thread.run_sync`.

The receiver registers a notification handler with `mw.on_notification(handler, class_scope=...)`
before it starts. Then it takes every Operation it is notified of through its **intake**:

1. **Read once.** The event trigger reads the Operation under a scope: the Capability that the
   Operation implements and the Workflow that realizes it, united with `class_scope`, the
   receiver's own {term}`ClassScope` of the Operation. A read that fails is answered with an error, so the
   sender's `notify()` raises. Then the trigger answers at once, with the Operation's IRI and no
   status.
2. **Buffer.** The Operation goes into the instance's FIFO. The FIFO keeps Operations that arrive
   faster than the resource carries them out, and it tells the resource that more work waits.
3. **One at a time.** One worker per instance runs one Operation at a time, in arrival order. When
   the Operation's Capability is one of this instance's own, matched in memory by its IRI, the
   worker passes the Operation object to the exposed workflow that realizes it. Otherwise it
   passes the object to the notification handler, the fallback, such as for "this transport
   ended". The exception is feedback: an Operation that this instance's Resource issued and that
   is no longer `queued` goes to the notification handler, also when its Capability is one of
   this instance's own (see The Issuer and the Issued Operation above). An Operation that waited
   behind another one is read again before it runs.

The Operation object is a kapps_ogm `Node`, materialized under the scope (`operation.instance`). A
workflow takes it as its one argument, typed `kapps_ogm.Node`, and registers as any other does. Its
REST route is mounted too, but the framework takes a one-parameter workflow's argument as an
optional query parameter `arg`, not as a body: a call passes `None`, or a string from the query, and
the framework's type check (typeguard) refuses both, so a call over REST is answered with an
HTTP 500 and such a workflow is run by the intake.

```python
from kapps_ogm import Node


@mw.workflow(capability_class=BOX_TRANSPORT_CAPABILITY, workflow_class=BOX_TRANSPORT_WORKFLOW)
def box_transport(transport: Node) -> None:
    ...  # move the box; commit the transport's own transitions with mw.commit()


def on_transport_ended(transport: Node) -> None:
    ...  # an Operation of no own Capability, such as the WMS's "this transport ended"


mw.on_notification(on_transport_ended, class_scope=transport_scope)
```

The intake has no lifecycle. The middleware writes no status and no provenance: the workflow or
the handler commits its own transitions with `mw.commit()`. A plain function runs in a worker
thread and may block; a coroutine function runs on the event loop. An exception is logged,
nothing is written for it, and the next Operation runs. At startup, the instance's own `queued`
Operations go into the FIFO again, each read before it runs, after its orphaned `running` ones
have become `failed`.

An instance without a handler keeps the queue below and the dispatch lifecycle (`register_callback`
or a manual `claim_next`), and startup warns about a resource-mode instance that has neither a
handler nor a callback. An instance cannot register both a handler and a `register_callback`
callback.

## Operation Queue

Each resource maintains an in-memory queue of Operations addressed to it. This queue is durable
because the Operation and its status are written to the graph before the trigger fires. On startup,
a middleware reconstructs its queue by querying the graph for its own `queued` Operations. It also
reclaims any orphaned `running` Operations left from a previous session. A missed trigger loses no
work; the baseline is self-healing. Resources do not poll the graph continuously for new work.
A resource with a notification handler has no queue: its notifications and, at startup, its own
`queued` Operations go into its intake instead, and it still fails its own orphaned `running`
Operations.

## Status Lifecycle

An Operation moves through three states: `queued`, `running`, and terminal (`done` or `failed`).
Execution provenance is written as part of the terminal transition. The provenance records which
Workflow ran the Operation, when it ran, and the result. The status field itself serves as the
provenance record; there is no separate success flag. A `done` status implies success; `failed`
implies an exception or validation error during execution. Transitioning to a terminal state is
atomic and irreversible without dispatching a fresh Operation.

## Pull-and-Run

Receiver-side execution uses the `claim_next` transactional context manager. The domain code takes
the next `queued` Operation, and the entry commits the claim, `queued` to `running`, before the body
runs. If the store refuses the claim, its error is raised and the body does not run. The middleware
then re-fetches the Operation under a domain-supplied {term}`ClassScope` to ensure the correct data view,
and the body runs the work. On a clean exit, the context manager records `done` with its provenance
in one commit. If the body raises an exception, the context manager records `failed` with its
provenance and a dump of the resource's datamodel in one commit, and raises the exception again. A
`done` that the store refuses is recorded as `failed` in the same way.

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
dispatch. A handover has a timeless administrative part, the change of possession, and a timed
physical part, the move. The primitive is a context manager built on `mw.commit()`, and the
possession changes first: the entry checks two preconditions (the caller possesses the Workpiece,
and the counterpart has the complementary handover ability), then commits the possession change.
The store admits or refuses it before the move begins. If the store refuses it, the error is
raised, nothing is written, and the body does not run.

```python
from kapps_semantic_middleware.vocabulary import MES

with mw.handover(mode=MES.Put, workpiece=wp, counterpart=other, operation=op):
    # The store has admitted the possession change.
    # Physical transport and preparation
    # Coordinate with counterpart if needed
# A clean exit writes nothing more
```

Possession is Core's reified `cfc:PossessionState`, with one state per Resource: a Resource gets
its state with its first possession and keeps it, and a Workpiece points at the state of its
Possessor with `cfc:hasPossessedWorkpiece`. So the possession change is one commit that changes
only the Workpiece's link, together with `workpiece_update` (more properties of the Workpiece that
change with its possessor, such as its transport state) and `operation`, the Operation that the
handover completes, which becomes `done`. When the counterpart has no state yet, the same commit
creates it and links the counterpart to it. Apart from that first link, a handover writes no
Resource's `cfc:hasPossessor`: neither the giver's nor that of a counterpart that has a state.

More changes that must be admitted together with the possession change go into `entry`, a list of
kapps_ogm `Create` and `Update` changes that the caller builds. They are written in the same commit,
refused together with it, and before the body. A transport that carries a box along a line of
conveyor modules, for example, is reassigned to the next module at each hop, and that module is
then notified:

```python
from kapps_ogm import Update

moved_on = Update(
    instance_iri=transport,
    data={
        str(CFC.executedOn): [{"id": str(next_module)}],
        str(CFC.implementsCapability): [{"id": str(next_transport_capability)}],
    },
)
with mw.handover(mode=MES.Pass, workpiece=box, counterpart=next_module, entry=[moved_on]):
    conveyor.pass_box()
mw.notify(operation=transport, resource=next_module)
```

The next module's intake then reads a transport that implements its own transport Capability, and
runs the workflow that realizes it. A change of the Workpiece belongs in `workpiece_update`, and
the completed Operation in `operation`: the OGM refuses two changes of one instance in one commit.

An error in the body is raised and undoes nothing: the possession has already changed, so the caller
records what really happened. For the same reason, a `mw.create()` in the body commits on its own:
the handover's commit has already happened. Possession history is not in the graph: a handover
overwrites the Workpiece's link, and the store's history mechanism, such as GraphDB's history plugin
when it is turned on, keeps the previous value. The middleware does not model the physical transport
itself; that logic belongs in the context manager body.

Core says that a Resource and a Workpiece each have exactly one possession state. That is OWL, which
refuses no write. What refuses a handover at commit is the domain's SHACL shape, such as one that
allows one box on a conveyor module.

Two handovers of the same Workpiece at the same moment are not always refused. Both pass the
preconditions before either commits. Inside its commit, the OGM compares the Workpiece with a fresh
read of it. If the other commit landed before that read, the later commit overwrites it, both
handovers succeed, and both bodies run. If it landed between that read and the write, the store
refuses the later commit and writes nothing of it, and the later handover raises
`PossessionSwitchConflict` before its body runs.

## Failure Recovery

If a resource dies, its stranded `queued` and `running` Operations are handled centrally. A
watchdog-mode instance sweeps stale resources, transitions their stranded Operations to `failed`,
and notifies the issuer of each one that has an issuer. The failed status
carries any dumped resource state available at the time of failure. Recovery is never a silent
physical replay. The planner or domain decides whether to dispatch a fresh Operation. Physical
operations are frequently not safely repeatable, so automatic re-actuation is avoided. The watchdog
ensures the graph does not retain stale `queued` or `running` states indefinitely.
