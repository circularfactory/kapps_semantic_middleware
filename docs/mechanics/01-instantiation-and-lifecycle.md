# Instantiation and Lifecycle

This page covers how to construct a middleware instance, what appears in the graph when it starts, how it maintains liveness, and what happens on shutdown. It assumes familiarity with the core vocabulary — Mode, Service, Resource, Heartbeat, Transport — defined in `CONTEXT.md`.

## Choosing a Mode

The `mode` constructor parameter determines what the instance is for. Only two modes are implemented:

- **`"resource"`** — Wraps one `Resource` (passed as `resource_iri`). The REST surface exposes user-registered Workflows and StateProperties, the built-in `execute()` event trigger, and a read-only API generated from the resource's datamodel: GET routes only, because a device changes only through a workflow of its service. Transactional operations (dispatch, pull-and-run, and the handover of the possession extension `kapps_semantic_middleware.possession`) remain Python-only.
- **`"watchdog"`** — Wraps no Resource. Exposes little or no REST surface. Runs a background sweep that removes `svc:hasAddress` and `svc:hasEndpoint` triples from Services whose heartbeat has gone stale.

A third mode value, `"server"`, is documented but **not implemented**. It remains reserved for future graph-serving participants. Do not use it.

## Construction Requirements

Resource mode requires a `resource_iri` at construction. The instance's network address must be absolute — a default cannot be derived, and a non-absolute address raises `ValueError`. This address becomes part of the Service node's identity.

An optional `ensure_transport` hook may be supplied:

```python
def my_starter(host: str, port: int) -> None:
    # Ensure something is listening at host:port before connectors dial it
    ...

mw = SemanticMiddleware(
    mode="resource",
    resource_iri=IRI("http://example.org/kapps-demo#my_resource"),
    ensure_transport=my_starter,
)
```

The hook is called once per distinct `(host, port)` address declared in the graph during construction. The library never starts, probes, or stops a transport itself — it states the need, and the deployment decides how to meet it. No transport implementation ships in this package.

## One Service Per Instance, Not Per Resource

A common mistake is assuming one Service per Resource. **There is one Service per middleware instance.** Multiple instances may wrap the same Resource — for example, a controller and a monitor — each owning its own Service node, address, and heartbeat. All link to the Resource via `svc:isServiceOf`.

The Service IRI includes a discriminator derived from the instance's normalized address. This ensures stability across restarts of the same deployment while keeping concurrent instances distinct. Discovery queries may return multiple Services for one Resource; consumers select by address, liveness, or advertised capabilities.

## Registration on Startup

When a resource-mode instance starts, it writes:

- `svc:hasAddress` on its Service individual (the base URL)
- `svc:hasEndpoint` on each registered Workflow and StateProperty individual (the full callable URL)

Both properties are removed on deregistration. Storing the endpoint directly on each Workflow/StateProperty allows any caller with just that IRI to invoke it without walking the graph to find the Service.

Deregistration keeps everything else: the individuals, their types, and their structural links. An instance that restarts at the same address finds its earlier individuals in place and adopts them, so it writes only the address and the endpoints again. A Capability individual is named after the Resource and the workflow or state name, not after the address. An instance that restarts at another address therefore gets a new Service and new Workflows, and each Capability it registers is re-pointed to the new Workflow or StateProperty. For the same reason, when two instances on one Resource register a Workflow with the same name, the Capability points to the instance that registered last.

## Heartbeat and Staleness

Every resource-mode instance runs an internal periodic Workflow that refreshes `svc:hasLastHeartbeat` on its Service individual. Registration writes the Service's address and a first heartbeat in one commit, so a sweep that starts after the registration finds a starting Service, or one restarting after its old heartbeat went stale, live. A sweep already in flight when the registration commits can still remove the address that the restart just wrote; the instance keeps running without an address until it restarts. The loop beats once at startup. It then waits a random time, drawn uniformly from zero up to `heartbeat_interval`, and only then beats every interval. Without that phase, instances started together would beat together for life, and a grid's writes would land in one burst that fills the store's write queue. While the store answers, no two heartbeats of an instance are further apart than one interval plus one write; a beat that fails leaves a gap of one more interval for each failure. Watchdog-mode instances query for Services whose heartbeat has exceeded a staleness threshold and remove their `svc:hasAddress` and `svc:hasEndpoint` triples. A heartbeat or a sweep that fails on a store error (the store unreachable, a dropped connection, an error answer) is logged as one warning and retried at the next interval; the loop goes on. Inside a sweep, a store error costs only the Service or Operation it hit: the sweep logs it and goes on with the next stale Service. A Service whose stranded Operations were not all failed keeps its address, and a deregistration removes the endpoints first and the address last, so the next sweep finds a half-done Service again and finishes it. Known limits: the heartbeat survives a store outage only while the outage is shorter than the staleness threshold minus one heartbeat interval; a longer one lets a watchdog sweep the instance while it runs. A swept instance that is still running keeps beating, without an address, so it is not discoverable until it restarts. An ambiguous write when the sweep marks an Operation `failed` (the store applied it, the answer was lost) is logged as a failure; the next sweep no longer finds the Operation stranded, so its issuer is not notified.

A deployment needs at least one running watchdog instance to guarantee no dangling registrations after crashes or power loss. The factory demonstrator (`demo/transferunits`) starts one as its own process, `watchdog.py`, with the default staleness threshold and a 5-second sweep interval; `--no-watchdog` leaves it out. Set the three liveness parameters as keyword arguments to `SemanticMiddleware`:

- `heartbeat_interval` (default `30.0`) — seconds between heartbeat refreshes, resource mode.
- `staleness_threshold` (default `90.0`) — seconds before a Service is considered stale: the sweep's threshold in watchdog mode, and in resource mode the threshold of the startup reconstruction's test of a live sibling.
- `sweep_interval` (default `30.0`) — seconds between staleness sweeps, watchdog mode.

```python
mw = SemanticMiddleware(
    mode=Mode.RESOURCE,
    resource_iri=resource_iri,
    service_class=service_class,
    ogm=ogm,
    heartbeat_interval=30.0,
)
```

The defaults suit most deployments; tune them against a real fleet's network latency and crash-recovery requirements.

Give every instance of a deployment, watchdog and resource mode, the same `staleness_threshold`: a restarting instance judges a sibling's liveness with its own value, so a resource instance left at the default fails a running Operation of a sibling whose heartbeat lags more than 90 s, even where a watchdog with a longer threshold still counts that sibling live. A watchdog threshold shorter than the resource instances' is harmless: the watchdog sweeps later what the reconstruction left.

## Deregistration

On clean shutdown, a resource-mode instance removes its `svc:hasAddress` and its Workflows'/StateProperties' `svc:hasEndpoint` triples while preserving the individuals themselves for provenance.

A clean shutdown runs only once the server has closed every open connection. uvicorn closes its listeners, then waits for each open response to finish, and only then runs the lifespan shutdown where the deregistration happens. By default that wait has no limit. So a response that stays open holds the stop open, and a supervisor that kills the process past its stop timeout skips the deregistration: the address stays in the graph. Two things keep a stop clean:

- **A streaming route must end by itself.** The library's own `/activity/stream` ends after 2 s. A browser's `EventSource` reconnects at once and resumes with `Last-Event-ID`. Any other client must reconnect when a stream ends and send that header itself. A streaming route of your own needs the same treatment.
- **Bound the wait where you run uvicorn.** Set `timeout_graceful_shutdown` below your supervisor's stop timeout, and leave the library's stop about 4 s: up to 3 s for the deregistration and up to 1 s for the notifications, below. This covers every connection the library does not own.

The stop runs three steps in this order, and none of them raises, so a failure in one does not skip the next:

1. **The heartbeat stops.** A beat still waiting on the store is abandoned, not awaited. A late beat writes only the heartbeat, never the address, so it cannot make a deregistered Service live.
2. **The deregistration.** The endpoints go first and the address last. A store error (the store unreachable, a dropped connection, an error answer) is logged as a warning, and the whole deregistration is tried again after a short pause. No new attempt starts after about 3 s, and an attempt still waiting on the store then is abandoned. A final failure, or any other error, is logged as one ERROR that names the Service. The address then stays until a watchdog sweeps it.
3. **The notifications of issuers.** The stop sends those that still wait, for up to one second, and then drops the rest with one WARNING line.

A store call that the stop abandons runs in a daemon thread, so it does not hold the process's exit.

If an instance stops heartbeating without shutting down cleanly, a watchdog-mode instance removes the same triples. It also marks `failed` the `queued` and `running` Operations of that Service's Resource that are stranded, and notifies the issuer of each one that has an issuer.

The decision is per Operation (`find_stranded_operations` in `registration`), live meaning a Service with an `svc:hasAddress` and an `svc:hasLastHeartbeat` within the staleness threshold. A `queued` Operation is stranded when its Capability is realized by no live Service of its Resource (Operation → Capability → `svc:realizedByWorkflow` → Workflow → `svc:isWorkflowOf` → Service on the same Resource). A `running` Operation is stranded when the Service that claimed it is not live: `claim_next()` records the claiming instance's own Workflow as the executor (`svc:executedByWorkflow`) in the commit of the claim, and the query follows that Workflow to its Service. A running Operation with no recorded executor (claimed by a commit of your own, or before claims recorded one) is stranded. No other Service takes up a running Operation, so a live sibling does not keep it. So when a monitor and a controller run on one Resource, the monitor's death does not fail the controller's queue, and a live monitor that realizes no Workflow does not shield a dead controller's queue. A queued Operation that a live sibling realizes stays queued, and the sibling takes it up at its next startup; nothing notifies it before. An issuer that waits on such an Operation waits until then; the sibling's startup takes it up. The stale Service loses its address either way.

A restarting instance decides its own orphaned `running` Operations with the same predicate and its own `staleness_threshold`: it fails one unless its executor's Service is another live Service. Its own Service never counts, since its heartbeat may still be fresh from before the crash. An instance that restarts at a new address within the threshold has a new Service; its old Service still has an address and a fresh heartbeat, so the orphan it claimed before the crash stays `running` until the watchdog sweeps the old Service, and is then failed; without a watchdog it stays `running`, as the old Service keeps its address. A deployment that restarts instances at new addresses needs a watchdog for both. The reconstruction takes up its `queued` Operations without the predicate; the re-registration writes a fresh heartbeat with the address, so a sweep after it counts the instance live and does not fail a queued Operation that the reconstruction queues. One limit for queued Operations: a Capability is minted per Resource, and the last instance to register a Workflow of that name becomes its only realizer in the graph, so two instances that expose the same Workflow count as one realizer, the one that registered last.
