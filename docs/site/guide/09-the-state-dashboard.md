# The State Dashboard

The state dashboard is a read-only HTML page that shows a middleware instance's in-memory state. Each instance serves one page at `/dashboard`. The page has no write path.

:::{dropdown} Terms this page assumes
{term}`ClassScope` · {term}`Parameter` · {term}`Service`
:::

## Turning It On

```python
from kapps_semantic_middleware.dashboard import enable_dashboard

registry = enable_dashboard(middleware)
```

The call mounts the routes on `middleware.app` and returns the `DashboardRegistry`. It also stores that registry as `middleware.dashboard`. A second call on the same instance returns the same registry and mounts nothing twice.

Pass `register_default_boxes=False` to start with an empty page:

```python
registry = enable_dashboard(middleware, register_default_boxes=False)
```

## The Page's Name

The page's title and its heading name the instance's resource, as in "Module 1 — dashboard". With several dashboards open, each browser tab shows which instance it belongs to.

The name is the resource's `rdfs:label`. A resource without one is named by the local name of its IRI, the part after the last `#` or `/`: `…#WarehouseManagementSystem1` gives "WarehouseManagementSystem1 — dashboard". An instance that represents no resource keeps the title "Middleware Dashboard".

A resource may carry several labels. The page uses the same one every time. A label without a language tag comes first, then an English one (`@en`, `@en-GB`, …), then the others. Within each group the first language tag in code-point order wins, and within one tag the first label in code-point order. A value that is an IRI, or that holds only white space, is not a name.

`enable_dashboard` reads the name from the graph once. No render of the page reads it again, so a label that changes later shows after the process restarts. If the store cannot answer at that moment, the page uses the local name, and the log says why.

## The Tab That Opens

The page opens on the first tab of the bar, which is System when System has a box. Choose another tab by the title that the tab bar shows:

```python
registry = enable_dashboard(middleware, first_tab="WMS")
```

`first_tab` sets the tab that the page opens on. A link overrides it with the query `tab`: `dashboard?tab=System` opens System, whatever `first_tab` says. Encode a name with a space as any query value: `dashboard?tab=UC2%20monitor`.

A name that no tab has is passed over. An unknown `?tab=` opens the `first_tab`, and an unknown `first_tab` opens the first tab of the bar, so a stale link or a typo still shows the page. `enable_dashboard` does not check `first_tab`, because your boxes, and so your tabs, are registered after the call. The first render that cannot open it logs a warning, once.

The server chooses the tab: the page marks the radio of that tab as checked, so no script is necessary. The tab bar keeps its order, and every box stays on the page. The query reaches the page under a path prefix too, and through the redirect from `/dashboard/`.

## The Four Default Boxes

The library registers four boxes, all of which read only what the instance already holds. The id in brackets is what `hide` takes.

1. **Instance** (`header`) — identity, mode, health, and the graph binding.
2. **Held data** (`held-data`) — the materialized datamodel, with each parameter's protocol metadata.
3. **Services** (`services`) — the workflows and state properties this instance serves.
4. **Connectors** (`connectors`) — what each wired connector is, as its descriptor declares it.

## Registering Your Own Box

The registry is the extension point. A box is a function that takes the middleware and returns an HTML fragment:

```python
@registry.box("spawn", title="Spawn a box", tab="Actions", refresh_seconds=10)
def spawn_box(middleware: Any) -> str:
    return "<p>The state I want to show.</p>"
```

`tab` defaults to the System tab. `refresh_seconds` is optional: `None` means the box never polls, and the page fetches it once on load.

Registration order is display order. A duplicate id replaces the earlier box, which is how a subproject overrides one of the library's own.

An id must be URL-safe — letters, digits, hyphen and underscore. The id becomes an HTML id and a path segment of `/dashboard/box/<id>`, so an unsafe id would render a slot the browser can never fill. Registration refuses it with a `ValueError` instead.

## Hiding a Box

A subproject that wants most of the default boxes, but not all of them, hides the rest by id.

:::{note}
**Which file this goes in: your own.** The dashboard is opt-in, so nothing in this library calls `enable_dashboard`, and no file here is the place to look. The call belongs in the module of your project that constructs the middleware instance and serves it — the module that holds your `SemanticMiddleware(...)` call, beside your workflow registrations. For a monitoring instance whose whole purpose is the page, that module is usually the process entry point of the monitor itself.
:::

Both lines sit there, in that order:

```python
middleware = SemanticMiddleware(
    mode=Mode.RESOURCE,
    resource_iri=MY_RESOURCE,
    service_class=MY_SERVICE_CLASS,
    ogm=OGM(db=db),
    host="127.0.0.1",
    port=8001,
)

registry = enable_dashboard(middleware)
registry.hide("held-data", "connectors")
# ... your own boxes are registered here, then the instance is served
```

There is no configuration file, no environment variable and nothing in the graph that controls this, and nothing a viewer of the page can change. A box is hidden for every reader of that instance's page, for as long as the process runs.

Hide takes the box ids, which are not the titles shown on the page. The library's own four are `header`, `held-data`, `services` and `connectors`.

Hide any number of them in one call, or call it again later as long as the process has not yet served the page. `enable_dashboard(middleware, register_default_boxes=False)` stays the all-or-nothing switch; `hide` is for keeping most of the defaults and dropping a few.

A hidden box leaves the page, and its fragment route answers 404. A tab whose every box is hidden leaves the tab bar, because the alternative is a tab that opens an empty panel.

An id that was never registered raises `ValueError`, and the call hides nothing. A hidden id renders nothing by definition, so a typo in one would otherwise remove nothing and report nothing.

Hiding marks a box rather than deleting it. Registering a box again under a hidden id shows it again: registering is a request to put a box on the page.

## The Helpers a Box Should Use

These come from `kapps_semantic_middleware.dashboard`:

- `panel(title, body_html, subtitle="")` — wraps a fragment in a titled panel. The dashboard already wraps each box, so a box needs this only for a panel inside a larger fragment.
- `kv_table(rows)` — a two-column table from a sequence of `(label, value)` pairs.
- `hint(text)` — one short explanatory line, in the muted style.
- `escape(value)` — `html.escape(str(value))`, with `None` rendered as an em dash.
- `SHORTENING_NOTE` — the one sentence every box uses above a table that shortens an identifier.

`kv_table` escapes every cell it renders. Pass raw values to it and never pre-escape them, or the reader sees the escape rather than the value.

## What a Box May Read

The library's own boxes read only what the instance already holds in memory. They cause no database and no peer traffic that would not happen with the page closed.

A subproject's box may fetch in its own render. That traffic is the subproject's own business and its own cost.

## A Box That Fetches

A render may take either form, and the page waits on neither.

Write a box that reads what the instance already holds as a plain function. This is what the library's own four boxes do:

```python
@registry.box("mine", title="Mine")
def mine(middleware: Any) -> str:
    return kv_table([("Repository", middleware.ogm.db.repository)])
```

Write a box that queries a graph, or calls a peer, with `async def`, and await the I/O inside it:

```python
@registry.box("topology", title="Topology", tab="UC2", refresh_seconds=10)
async def topology(middleware: Any) -> str:
    rows = await read_the_topology(middleware)       # your own query, your own cost
    return kv_table(rows)
```

Either way the render does not run on the instance's event loop: an `async def` render is awaited, and a plain one runs in a worker thread. So a slow box delays its own fragment and nothing else — not the instance's heartbeat, and not another box.

:::{note}
Two limits are worth knowing. The number of plain renders that may block at the same moment is the worker-thread limit, 40 by default, which a page of a few boxes does not approach. And a render that never returns occupies its thread for as long as the process runs: give your own query a timeout, because the dashboard does not impose one.
:::

## Posting to a Workflow

A box may reach a workflow of the instance that serves the page, and never a peer's. A generated workflow route is `POST /workflows/<name>/execute`, and it takes a JSON body. An HTML form sends form-encoded fields instead, which that route refuses. The library therefore vendors htmx's `json-enc` extension, served from `/dashboard/static/json-enc.js` and loaded after htmx itself.

A box's form asks for it with `hx-ext="json-enc"`, and asks htmx to leave the page alone with `hx-swap="none"`:

```python
@registry.box("spawn", title="Spawn a box", tab="Actions")
def spawn_box(middleware: Any) -> str:
    return (
        '<form hx-post="workflows/spawn_box/execute" hx-ext="json-enc" hx-swap="none">'
        '<label>How many <input type="number" name="count" value="1"></label>'
        '<button type="submit">Spawn</button>'
        "</form>"
    )
```

The operator presses Spawn. htmx collects the fields, encodes them as `{"count": 1}`, and posts that to the instance's own workflow. The page does not move, and the next refresh of any box shows whatever the workflow changed.

Write the path without a leading slash: `workflows/spawn_box/execute`, not `/workflows/spawn_box/execute`. A box is placed into the page at `/dashboard`, so the browser resolves a relative path against the page's own address and reaches the instance that served the page. That holds at the process's root and behind a proxy that serves the process under a path prefix, such as `/p/<process>/dashboard`. An absolute path goes to the proxy's own root there, and the form posts to nothing. Any URL a box names on its own instance follows the same rule, and so does the page itself: its scripts, its boxes and the activity page's stream are named `dashboard/static/...`, `dashboard/box/<id>` and `activity/stream`. The page answers at `/dashboard`; `/dashboard/` redirects there.
