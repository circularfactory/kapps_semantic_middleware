# Instantiation and Lifecycle

This page covers how to construct a middleware instance, what appears in the graph when it starts, how it maintains liveness, and what happens on shutdown.

:::{dropdown} Terms this page assumes
{term}`Mode` · {term}`Service` · {term}`Resource` · {term}`Workflow` · {term}`Heartbeat` ·
{term}`Address vs. Endpoint`
:::

## Choosing a Mode

The `mode` constructor parameter determines what the instance is for. Only two modes are implemented:

- **`"resource"`** — Wraps one {term}`Resource` (passed as `resource_iri`). The REST surface exposes user-registered Workflows and StateProperties, the built-in `execute()` event trigger, and a CRUD API generated from the resource's datamodel. Transactional operations (dispatch, pull-and-run, handover) remain Python-only.
- **`"watchdog"`** — Wraps no Resource. Exposes little or no REST surface. Runs a background sweep that removes `svc:address` and `svc:endpoint` triples from Services whose heartbeat has gone stale.

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
    resource_iri=IRI("https://example.org/kapps-demo#my_resource"),
    ensure_transport=my_starter,
)
```

The hook is called once per distinct `(host, port)` address declared in the graph during construction. The library never starts, probes, or stops a transport itself — it states the need, and the deployment decides how to meet it. No transport implementation ships in this package.

## One Service Per Instance, Not Per Resource

A common mistake is assuming one Service per Resource. **There is one {term}`Service` per middleware instance.** Multiple instances may wrap the same Resource — for example, a controller and a monitor — each owning its own Service node, address, and heartbeat. All link to the Resource via `svc:isServiceOf`.

The Service IRI includes a discriminator derived from the instance's normalized address. This ensures stability across restarts of the same deployment while keeping concurrent instances distinct. Discovery queries may return multiple Services for one Resource; consumers select by address, liveness, or advertised capabilities.

## Registration on Startup

When a resource-mode instance starts, it writes:

- `svc:address` on its Service individual (the base URL)
- `svc:endpoint` on each registered Workflow and StateProperty individual (the full callable URL)

Both properties are removed on deregistration. Storing the endpoint directly on each Workflow/StateProperty allows any caller with just that IRI to invoke it without walking the graph to find the Service.

Deregistration keeps everything else: the individuals, their types, and their structural links. An instance that restarts at the same address finds its earlier individuals in place and adopts them, so it writes only the address and the endpoints again. A Capability individual is named after the Resource and the workflow or state name, not after the address. An instance that restarts at another address therefore gets a new Service and new Workflows, and each Capability it registers is re-pointed to the new Workflow or StateProperty. For the same reason, when two instances on one Resource register a Workflow with the same name, the Capability points to the instance that registered last.

## Heartbeat and Staleness

Every resource-mode instance runs an internal periodic {term}`Workflow` that refreshes `svc:lastHeartbeat` on its Service individual. Watchdog-mode instances query for Services whose {term}`heartbeat <Heartbeat>` has exceeded a staleness threshold and remove their `svc:address` and `svc:endpoint` triples.

A deployment needs at least one running watchdog instance to guarantee no dangling registrations after crashes or power loss. Set the three liveness parameters as keyword arguments to `SemanticMiddleware`:

- `heartbeat_interval` (default `30.0`) — seconds between heartbeat refreshes, resource mode.
- `staleness_threshold` (default `90.0`) — seconds before a Service is considered stale, watchdog mode.
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

## Deregistration

On clean shutdown, a resource-mode instance removes its `svc:address` and its Workflows'/StateProperties' `svc:endpoint` triples while preserving the individuals themselves for provenance.

A clean shutdown runs only once the server has closed every open connection. uvicorn closes its listeners, then waits for each open response to finish, and only then runs the lifespan shutdown where the deregistration happens. By default that wait has no limit. So a response that stays open holds the stop open, and a supervisor that kills the process past its stop timeout skips the deregistration: the address stays in the graph. Two things keep a stop clean:

- **A streaming route must end by itself.** The library's own `/activity/stream` ends after 2 s. A browser's `EventSource` reconnects at once and resumes with `Last-Event-ID`. Any other client must reconnect when a stream ends and send that header itself. A streaming route of your own needs the same treatment.
- **Bound the wait where you run uvicorn.** Set `timeout_graceful_shutdown` below your supervisor's stop timeout, and leave the deregistration a second or two. This covers every connection the library does not own.

After the deregistration, the stop sends the notifications of issuers that still wait, for up to one second, and then drops the rest with one WARNING line. So count one second more for the stop.

If an instance stops heartbeating without shutting down cleanly, a watchdog-mode instance removes the same triples. It also marks the `queued` and `running` Operations of that Service's Resource `failed`, and notifies the issuer of each one that has an issuer.
