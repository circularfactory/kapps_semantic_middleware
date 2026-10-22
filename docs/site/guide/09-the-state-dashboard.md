# The State Dashboard

The state dashboard is an HTML page that shows a middleware instance's in-memory state. Each instance serves one page at `/dashboard`. The library's own boxes change nothing, with one exception: the form of the Logs tab sets a logger's level in the process.

:::{dropdown} Terms this page assumes
{term}`ClassScope` · {term}`Parameter` · {term}`Service`
:::

## Turning It On

```python
from kapps_semantic_middleware.dashboard import enable_dashboard

registry = enable_dashboard(middleware)
```

The call mounts the routes on `middleware.app` and returns the `DashboardRegistry`. It also stores that registry as `middleware.dashboard`. A second call on the same instance returns the same registry and mounts nothing twice.

The page, its boxes and its static files are not part of the instance's API. They are left out of its API description, `/openapi.json`, so `/docs` shows the instance's services and not the dashboard's own routes.

Pass `register_default_boxes=False` to start with an empty page:

```python
registry = enable_dashboard(middleware, register_default_boxes=False)
```

## The Page's Name

The page's title and its heading name the instance's resource, as in "Module 1 — dashboard". With several dashboards open, each browser tab shows which instance it belongs to.

The name is the resource's `rdfs:label`. A resource without one is named by the local name of its IRI, the part after the last `#` or `/`: `…#Station1` gives "Station1 — dashboard". An instance that represents no resource keeps the title "Middleware Dashboard".

A resource may carry several labels. The page uses the same one every time. A label without a language tag comes first, then an English one (`@en`, `@en-GB`, …), then the others. Within each group the first language tag in code-point order wins, and within one tag the first label in code-point order. A value that is an IRI, or that holds only white space, is not a name.

`enable_dashboard` reads the name from the graph once. No render of the page reads it again, so a label that changes later shows after the process restarts. If the store cannot answer at that moment, the page uses the local name, and the log says why.

## The Tab That Opens

The page opens on the first tab of the bar, which is System when System has a box. Choose another tab by the title that the tab bar shows:

```python
registry = enable_dashboard(middleware, first_tab="Overview")
```

`first_tab` sets the tab that the page opens on. A link overrides it with the query `tab`: `dashboard?tab=System` opens System, whatever `first_tab` says. Encode a name with a space as any query value: `dashboard?tab=Line%20monitor`.

A name that no tab has is passed over. An unknown `?tab=` opens the `first_tab`, and an unknown `first_tab` opens the first tab of the bar, so a stale link or a typo still shows the page. `enable_dashboard` does not check `first_tab`, because your boxes, and so your tabs, are registered after the call. The first render that cannot open it logs a warning, once.

The server chooses the tab: the page marks the radio of that tab as checked, so no script is necessary. The tab bar keeps its order, and every box stays on the page. The query reaches the page under a path prefix too, and through the redirect from `/dashboard/`.

## The Default Boxes

The library registers six boxes, all of which read only what the instance already holds: five in the System tab, and the Logs box in a tab of its own. The id in brackets is what `hide` takes.

1. **Instance** (`header`) — identity, mode, health, and the graph binding, with its last read, last write and last error. Only a watchdog-mode instance runs a sweep, so only its box shows the sweep's interval and whether the sweep's task is alive.
2. **Held data** (`held-data`) — the materialized datamodel, with each parameter's protocol metadata, its connection state and the age of its last sync.
3. **Services** (`services`) — the workflows and state properties this instance serves, each with what its function's docstring says. See The Services Box below.
4. **Connectors** (`connectors`) — what each wired connector is, as its descriptor declares it, and its state, last sync and last error. See Connection State and the Graph Binding below.
5. **Refused writes** (`refused-writes`) — the writes of this instance that the store's shapes refused, newest first.
6. **Logs** (`logs`) — the newest records of every logger in the process, and a form that sets a logger's level. See The Logs Tab below.

## The Services Box

The box "Services", on the System tab, lists each workflow and each state property that the instance serves, with a summary and a description from the docstring of the function you registered: the docstring's first line is the summary, and the whole docstring the description. For a workflow, the routes that run it, `workflows/<name>/execute` and `workflows/<name>/execute_background`, carry the text. Its routes `description` and `interrupt` keep the summaries that the framework gives them. A state property's route, `state/<name>`, carries its getter's text. The event trigger, `workflows/event_trigger/...`, which every resource-mode instance serves, says what it does in the same way.

```python
@mw.workflow(capability_class=SPAWN_CAPABILITY, workflow_class=SPAWN_WORKFLOW)
def spawn(count: int) -> dict:
    """Spawn new boxes at the module's entry.

    The count says how many, and each box gets a fresh id.
    """
    ...
```

The box reads the instance's API description, which the process generates in memory, so it causes no traffic. `/docs` shows the same text. A function without a docstring shows the default summary, such as "Execute", and no description.

## Connection State and the Graph Binding

The instance records what it has seen of each connector it wired and of its graph binding, the store behind its OGM. It records this in memory, from the moment it is constructed, and adds no traffic to do so. A restart empties the records.

**A connection.** The framework gets an observing wrapper in place of each connector that the instance wires, REST, MQTT and the knowledge graph alike, and the wrapper notes each call. It records:

- the state: *never connected* until the framework asks the connector to connect, *connected* once its `connect()` has returned, and *not connected* after its `disconnect()` or a `connect()` that failed. The state is what the connector's own `connect()` reports;
- the last sync: the last value the connector received, or the last value written to it. The last `provide()` is kept apart and is no sync, because a pushing connector, such as the MQTT one, answers `provide()` from a value it holds and reads nothing;
- the last error, with its type, its time and its message.

The box "Connectors" shows the state, the last sync and the last error of each connector. The box "Held data" shows the state and the last sync of each value, over the connectors that serve it: a value with one connector for each direction shows the newer of their syncs. A value that no connector serves shows *not wired*.

**The graph binding.** Every read and write of the instance reaches the store through the `db` of its OGM: the OGM's own calls do, and so do the queries of the library. The instance puts an observing store in that place when it is constructed, once for each OGM. The header box "Instance" shows, under *Graph binding*, the last read and the last write that the store answered, and the last error of the binding. A write that the store refused, by a shape or by a false precondition, is an answer: the binding works, and the refusal shows in "Refused writes". The last error is a call that got no answer, whose credentials or access the store refused, or that the store failed itself.

Several instances in one process may share one OGM, and so one store. Each then shows the same reads, writes and error, because they are the store's. An `ogm.db = ...` after construction ends the observation.

**Reading the records in code.** Two members of `SemanticMiddleware` give the records, read from memory:

- `mw.connections` — one `ConnectionRecord` per wired connector, in the order of the wiring, and empty for an instance that wires none. Each has `connected`, `last_provide`, `last_consume`, `last_receive`, `last_sync` and `last_error`, and names the connector by `connector_id`, `holder`, `field`, `connector` (its class) and `direction`.
- `mw.graph_binding` — the `GraphBindingRecord`, with `dataset`, `named_graph`, `last_read`, `last_write` and `last_error`.

Each record also carries this instance's `resource` and `service` IRI, and is frozen. A time is aware, in UTC; the box computes an age from it when it renders. A `last_error` is a `CallError` with `time`, `type` and `message`. The types are in `kapps_semantic_middleware.observation`.

```python
for record in mw.connections:
    print(record.field, record.connected, record.last_sync)
print(mw.graph_binding.last_error)
```

A box of your own, and an exporter, read the records the same way.

## A Page Without Connection Metadata

The dashboard is a development surface. By default it shows the connection metadata of the instance: the MQTT topic of each parameter in "Held data", the protocol properties that the northbound projection stripped from it, and the message of a last error, which may name a host, a port, a topic or a URL. That is what a laboratory deployment needs to see whether a value was fetched properly.

A deployment that is not a laboratory one turns it off:

```python
registry = enable_dashboard(middleware, dev=False)
```

The page then shows no topic column and no list of stripped properties; "Held data" keeps the count of what was stripped. A last error shows its type and its time, and not its message. The states and the ages stay.

This page is not the northbound payload. The projection keeps connection metadata out of what other machines read from the instance; this page is the instance's own diagnostic surface, and the switch is the one control over what it shows. The log keeps an MQTT topic at DEBUG either way.

Which field of a record is connection metadata is marked in the record's type, and one predicate reads the mark: `kapps_semantic_middleware.observation.is_connection_metadata(record, name)`. The library's boxes leave out what it names, and a box of your own does the same:

```python
from kapps_semantic_middleware.observation import is_connection_metadata

@registry.box("last-error", title="Last error")
def last_error(middleware: Any) -> str:
    error = middleware.graph_binding.last_error
    if error is None:
        return hint("No error since the instance started.")
    show_message = registry.dev or not is_connection_metadata(error, "message")
    return kv_table([("Type", error.type), ("Message", error.message if show_message else None)])
```

`registry.dev` says whether the page is a development page. Pass `dev` on the first call: a second `enable_dashboard` returns the registry of the first and ignores its options, `dev` among them.

## The Box of Refused Writes

When the store's SHACL shapes refuse a write of `mw.commit()`, `mw.create()` or the possession extension's `handover(mw, ...)`, which commits through `mw.commit()` under the verb `handover`, the caller gets `ShaclValidationError`, unchanged, and the instance keeps the refusal in memory: the time, the verb, the rules that the store's validation report names, and the report. It keeps the newest 50, and writes nothing of them to the graph. `mw.refused_writes` gives them, newest first. The write of a `commit`'s error transition, which the shapes may refuse when the body failed, is kept too: its verb is that of the commit, `commit` unless the call named another such as `handover`, and its first line reads `<verb> (error transition) refused by ...`; the caller still gets the error of the body, with a note that the error transition was not committed. A write that is refused for another reason, such as a false precondition, is not kept.

The box "Refused writes", on the System tab, shows them and refreshes every 5 seconds. Each refused write is one line that is always visible, for example:

```text
14:03:12 handover refused by StationPossessionShape at Station3
```

That is the time, the verb, and each rule as its source shape at its focus node. Click the line to open its expander. It holds a table with one block of rows for each result of the report: the message, the severity, the focus node, the path, the value, the source shape and the constraint component. Each name shows its local name; hold the pointer over it to read the full IRI. A path is in SPARQL property path syntax. Below the table is the whole report, as the store sent it, with its line breaks. Every value is escaped, so a message that holds markup shows as text. An expander that you opened stays open when the box refreshes.

A report that is not RDF names no result. The box then says so, and shows the text as it is.

Each refusal is also logged, as one record at INFO on the logger `kapps_semantic_middleware.validation_report`: its first line says what was refused and by which rules, and the following lines hold the readable results and the whole report. The activity page and the Logs tab show it as its first line, with the rest behind an expander (see The Logs Tab); the Logs tab cuts a long message, and the box keeps the whole report.

To show a refused write on a page of your own, call `kapps_semantic_middleware.validation_report.render_refused_write(refused)`. The same module reads a report for code: `readable_results(report)` for a person, `validation_results(report)` for comparing, and `rule_names(report)` for a log line.

## The Logs Tab

The Logs tab shows the newest records of the whole process: every logger, the library's own, your subproject's, such as `myplant.line.station`, and those of the libraries it calls, such as `httpx`. The activity page at `/activity` stays as it was, a live stream of the library's own logger.

**Where the records come from.** The tab is a default box, so `enable_dashboard(middleware)` attaches one handler to the root logger, once per process, however many instances in the process enable the dashboard. `register_default_boxes=False` leaves out the tab, its handler and its route. The handler keeps the newest 500 records in memory, and cuts a message at 4000 characters; the oldest record goes when a new one arrives. A render reads only that memory and the loggers' levels, so the box causes no traffic. The handler changes no logger's level, so the terminal shows what it showed before.

A record reaches the box only when its logger sends it: at or above the level in force for that logger, and on to the root logger, which every logger does unless its `propagate` is off. A level that holds a record back from the terminal holds it back from the box too. A process that configures logging again after `enable_dashboard`, with `logging.basicConfig(force=True)` or `logging.config.dictConfig`, removes the handler from the root logger, and the box then says that no new record reaches it. Configure logging first.

Each record shows its time, level, logger and message, newest first. A record with an exception shows the exception's type and message after its own; the traceback stays in the terminal.

**A record of several lines** shows its first line, the text before the first line break, in its row, and the rest behind an expander that is closed at first. A store's refusal report, or a message with an exception after it, takes one line until a reader opens it. Opened, the expander shows the rest as text, with its line breaks and its indentation kept; markup in a message is shown as text. A record of one line has no expander. The expander stays open when the box refreshes, and a new record neither opens nor closes another record's expander. The cut at 4000 characters applies to the whole message first, so the expander ends with the note of how many characters were cut. The activity page at `/activity` folds a record of several lines the same way.

**The level filter** chooses which records the box shows: those at or above DEBUG, INFO, WARNING, ERROR or CRITICAL. It filters the view in the browser, changes no level, and keeps its choice when the box refreshes. A record of a level of your own, such as 25, shows with the standard level below it.

**Setting a logger's level.** The form sets the level of one logger while the process runs. Type the logger's name, or pick one the box has seen, choose the level, and press *Set level*. The level holds until the process ends or the level is set again. The table beside the form lists the loggers seen so far, root first, each with its own level and the level in force: a logger whose own level is NOTSET takes its parent's.

The form posts to `POST dashboard/logs/level`, and so can any other client:

```http
POST /dashboard/logs/level HTTP/1.1
Content-Type: application/json

{"logger": "myplant.line", "level": "DEBUG"}
```

The answer is the level now in force, `{"logger": "myplant.line", "level": "DEBUG"}`.

- The level is one of `NOTSET`, `DEBUG`, `INFO`, `WARNING`, `ERROR` and `CRITICAL`, spelled so. Any other value, `debug` or `10` among them, is refused with 422 and the list of the names. `NOTSET` clears the logger's own level, and the answer is then the level it takes from its parent.
- The root logger is named `root`. An empty name is refused, rather than taken for the root logger.
- A name the process has no logger for yet gets one, so a level can be set before the logger first writes. A misspelt name gets one too: check the name in the answer.
- The page's own form gets its answer as HTML, and a refusal as a readable error in the box.
- When the box `logs` is hidden, the route answers 404, as the box's fragment does.

The level belongs to the logger, for the whole process: `root` at DEBUG sends DEBUG records to every handler of the process, the terminal included. Anyone who reaches the instance's address can set a level, as anyone who reaches it can run its workflows.

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

Hide takes the box ids, which are not the titles shown on the page. The library's own six are `header`, `held-data`, `services`, `connectors`, `refused-writes` and `logs`.

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

Write a box that reads what the instance already holds as a plain function. This is what the library's own boxes do:

```python
@registry.box("mine", title="Mine")
def mine(middleware: Any) -> str:
    return kv_table([("Dataset", middleware.ogm.db.dataset)])
```

Write a box that queries a graph, or calls a peer, with `async def`, and await the I/O inside it:

```python
@registry.box("topology", title="Topology", tab="Line", refresh_seconds=10)
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

Write the path without a leading slash: `workflows/spawn_box/execute`, not `/workflows/spawn_box/execute`. A box is placed into the page at `/dashboard`, so the browser resolves a relative path against the page's own address and reaches the instance that served the page. That holds at the process's root and behind a proxy that serves the process under a path prefix, such as `/p/<process>/dashboard`. An absolute path goes to the proxy's own root there, and the form posts to nothing. Any URL a box names on its own instance follows the same rule, and so does the page itself: its scripts, its boxes, the form of the Logs tab and the activity page's stream are named `dashboard/static/...`, `dashboard/box/<id>`, `dashboard/logs/level` and `activity/stream`. The page answers at `/dashboard`; `/dashboard/` redirects there.
