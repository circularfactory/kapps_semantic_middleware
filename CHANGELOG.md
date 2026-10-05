# Changelog

Notable changes to `kapps-semantic-middleware`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

`release-mechanism prepare` turns the Unreleased heading below into the dated entry for the
version being released, and stops the release if there is no such heading. Write the entry as the
work lands, not at release time.

(That sentence deliberately does not spell the heading out. The release mechanism insists on
finding exactly one of it, and prose naming it is a second occurrence — which stopped a release
the first time this file was written.)

## 0.2.0 — 2026-10-05

### Added

- A dashboard box may fetch. A render declared with `async def` is awaited, and any other render
  runs in a worker thread, so neither form runs inline on the instance's event loop: a slow box
  delays its own fragment and nothing else, not the heartbeat and not another box. No existing box
  changes; the render type is widened, not replaced.
- `DashboardRegistry.hide(*box_ids)` drops single boxes from the state dashboard, the library's
  own four among them, for a subproject that wants most of the defaults and not all of them. A
  hidden box leaves the page and its fragment route answers 404; a tab left with no visible box
  leaves the tab bar; an id that was never registered raises `ValueError`; and registering a box
  again under a hidden id shows it again.
- The dashboard serves htmx's `json-enc` extension beside htmx itself, pinned to
  `htmx-ext-json-enc` 2.0.1, so a box's form can post a JSON body to a workflow of its own
  instance with `hx-ext="json-enc"`. The file carries no version string of its own, which is why
  the pinned release is named here.
- [`docs/mechanics/09-the-state-dashboard.md`](docs/mechanics/09-the-state-dashboard.md)
  documents the page, the registry, hiding a box, and that worked example end to end.
- The dashboard opens on a tab of your choice. `enable_dashboard(mw, first_tab="WMS")` sets the
  tab that the page opens on, and a link overrides it with the query `?tab=<tab name>`, such as
  `dashboard?tab=WMS`. A name that no tab has opens the tab that would open without it, and an
  unknown `first_tab` is logged once. The server marks the tab, so the page still needs no script
  of its own; the tab bar keeps its order, and every box stays on the page.
- `mw.commit()` (`SemanticMiddleware.commit`), the general write: a state transition that the
  store admits before the work it describes begins. Its entry transition is committed first, and
  a refusal raises before the body runs. A clean exit commits the body's changes
  (`transition.update(...)`) and the exit transition as one commit; an error, or a cancellation
  under `async with`, commits the error transition instead and raises again. It works with
  `with` and with `async with`, and the body holds no connection of the triple store.
- `mw.fetch()` (`SemanticMiddleware.fetch`), the general read: `OGM.fetch` as a plain call.
- `kapps_semantic_middleware.errors`, the public home of the errors a caller catches:
  `HandoverPreconditionError`, `PossessionSwitchConflict`, `OperationQueueEmpty`,
  `OperationResolutionError` and `OntologyGroundTruthError`. Importing them from
  `kapps_semantic_middleware.registration`, as before, keeps working and gives the same classes.
- `mw.notify(operation=..., resource=...)` sends a notification: a message that names one
  Operation in the graph and carries nothing else. Nothing is written to the graph for it.
  `notify` finds the resource's one live Service, posts the IRI to that Service's event trigger,
  and returns when the receiver confirms receipt. It is a blocking call; async code runs it with
  `anyio.to_thread.run_sync`.
- `mw.on_notification(handler, class_scope=None)` registers a notification handler, and the
  instance then takes the Operations it is notified of through its intake:
  - the event trigger reads the Operation once, under a scope of the Capability it implements and
    the Workflow that realizes it, united with `class_scope`, and answers at once
  - the Operation goes into the instance's FIFO, and one worker runs one Operation at a time, in
    arrival order; an Operation that waited is read again before it runs
  - an Operation whose Capability is one of the instance's own runs the exposed workflow that
    realizes it; any other goes to the handler, such as "this transport ended"

  Both get the Operation object: a kapps-ogm `Node`, materialized under the scope. A workflow
  typed `def box_transport(transport: Node)` registers as before. The middleware writes no
  status and no provenance; the workflow or the handler commits its own transitions with
  `mw.commit()`. A function may be a plain function, which runs in a worker thread, or a
  coroutine function. An exception is logged, and the next Operation runs.
- `handover(..., entry=[...])` takes more kapps-ogm changes, built by the caller, and commits them
  in the handover's entry commit together with the change of possession: one commit, refused
  together, before the body. A transport that carries a box from module to module is reassigned
  to the next module this way.
- `CFC.executedOn` in `kapps_semantic_middleware.vocabulary`: Core's `cfc:executedOn`, the
  Resource that executes an Operation. A transport's hop reassigns it in a handover's `entry`.
- `NotificationUndelivered`, imported from `kapps_semantic_middleware.errors`. `notify`
  raises it when the resource has no live Service, when it has more than one, and when the post
  fails, is refused or times out. `request()` raises it when its notification fails.
- `svc:issuedBy` (`SVC.issuedBy`), an object property from an Operation to its issuer: the
  Resource of the middleware whose `request()` created it. `request()` writes it in the commit
  that creates the Operation. The issuer is responsible for the Operation while it is `queued`;
  the Resource that claims it is responsible from `running` on.
- The issuer hears of its Operation. After a commit that the store admitted and that moves an
  Operation with an issuer to `running`, `done` or `failed`, the middleware that made the commit
  notifies the issuer, as `notify()` does, in the background: one worker per instance sends
  the notifications in the order of their commits, and the commit goes on at once, so a slow
  issuer never delays the claimer. This holds for `claim_next()`, `mw.commit()`, `mw.create()`,
  `handover(operation=...)`, the startup's crash recovery and the watchdog's sweep. The middleware
  sees the move in the status that the commit sets, so every such commit notifies, also one that
  sets the status that the Operation already has, such as a second claimant's `running`. The
  issuer ignores a repeat itself. An issuer that cannot be notified is logged at WARNING, and the
  work goes on. `claim_next()` reads the issuer in the read that it makes before the claim; any
  other such commit reads it once, after the write. On the issuer's side, the notification wakes
  the Operation's handle, the notification handler gets it, and it enters no queue and runs no
  workflow.
- `IssuedOperation` in `kapps_semantic_middleware.middleware`, what `request()` yields: in the
  block, the draft (`op.data`, `op.iri`); after it, the handle, with `op.issuer`.
  `await op.claimed(timeout=30)` is `True` once the Operation is claimed and `False` when the
  timeout passes first; `await op.ended(timeout=None)` is `"done"` or `"failed"` and raises
  `TimeoutError` when the timeout passes first. `op.wait_claimed(timeout)` and
  `op.wait_ended(timeout)` are the blocking twins. A wait reads the status from the graph first,
  then waits for the notification that the claimer sends, and reads it again.
- `kapps_semantic_middleware.possession.possession_state()`: a Resource's one possession state,
  found by its `cfc:hasPossessor` link, and the changes that create `<resource>_possession` when it
  has none. `possession_change()` in the same module gives a workpiece's change of possession for
  one commit.
- `SemanticMiddleware.handover()` takes the keyword `operation`: the Operation that the handover
  completes becomes `done` in the same commit as the change of possession.
- `mw.create(cls, scope, instance_iri=None)` (`SemanticMiddleware.create`) creates one new
  instance, built on `mw.commit()`. It mints the IRI `<class>_<12 hex>` when it is entered, unless
  `instance_iri` is given, and the body reads it as the creation's `iri`, so a nested creation can
  be linked before anything is written. The creation's `update({property: [values]})` sets the
  new instance's properties; a value that is an IRI is a reference. It works with `with` and with
  `async with`:
  - alone, it commits at its clean exit, and an error in its body writes nothing
  - inside the body of a `mw.commit()` or of another `mw.create()` of the same middleware (the
    same OGM and named graph) whose write is still to come, it joins that write, and the store
    admits or refuses them together
  - inside the body of `handover()`, whose commit has already happened, and inside a write of
    another middleware, it commits on its own

  After the clean exit, the creation's `changes` are the changes that it committed or joined.
- Two predefined scopes for `mw.create()`, which bring a rule of the circular factory to a new
  instance. A missing input raises `ValueError` when `mw.create()` is entered, before any write.
  - `workpiece(scope, possessor=...)` in `kapps_semantic_middleware.possession`: the Workpiece
    points at its possessor's one possession state. When the possessor has none, the same commit
    creates `<possessor>_possession` and links the possessor to it.
  - `operation(scope, capability=...)` in the new module `kapps_semantic_middleware.operation`:
    the Operation's `cfc:implementsCapability`.

  `PredefinedScope` in `kapps_semantic_middleware.commit` is the hook for a domain's own scope,
  and `ScopeChanges` is what its `changes()` returns. These names are provisional.
- `mw.resource` (`SemanticMiddleware.resource`) is the middleware's own Resource, as in
  `workpiece(scope, possessor=mw.resource)`.
- `AGENTS.md` lists which modules are generic and which are specific to the circular factory. A
  test fails when a generic module names a `cfc:` IRI or a module is missing from the list.

### Removed

- `kapps_semantic_middleware` no longer re-exports thirteen names from
  `transitional_sync_middleware`. They were `Middleware`, `AasMiddleware`, `Reference`,
  `Identifier`, `DataModel`, `DataModelRebuilder`, `AAS`, `Submodel`,
  `SubmodelElementCollection`, `Blob`, `File`, `formatting` and `connectors`. Seven of them were
  the AAS data model and its persistence layer, which this library states it does not carry.
  Import any name you still need from `transitional_sync_middleware` by its own path. The
  package exports `SemanticMiddleware` and `Mode`.
- `kapps_semantic_middleware.connectors` is this package's own subpackage, as before. The
  re-export of the same name never reached a consumer: importing the subpackage rebound the
  attribute, so `__all__` named one module and the namespace held another.
- `kapps_semantic_middleware.registration.revert_operation`. `request()` no longer reverts an
  Operation whose notification failed (see *Changed*), and the function had no other caller in
  this repository.

### Fixed

- A middleware instance that gets a stop signal while it starts now runs its shutdown. So it
  removes its address and its endpoints from the graph. Before, the address stayed in the graph,
  and nothing was left to remove it. uvicorn releases before 0.41.0 skip the shutdown in that
  case, so the `[examples]` extra now requires `uvicorn` 0.41.0 or later.
  `transitional-sync-middleware` 0.1.1 allows it.
- A middleware instance now deregisters when it is stopped while a client holds its activity
  feed open. Before, the open stream held the stop open until the supervising process killed the
  instance, and its address stayed in the graph.
- The factory demonstration bounds each server's wait for open connections at 3 s, below the
  launcher's stop timeout of 5 s.
- `KnowledgeGraphConnector.provide` and `KnowledgeGraphConnector.consume` now raise the error of
  `OGM.fetch` or `OGM.commit` unchanged, with its type and its message. Before, both raised a
  `ConnectionError` in its place. So a refusal by the store, bad data and an unknown class all
  read as a network failure. A caller that catches `ConnectionError` or `OSError` for these
  errors no longer catches them. The REST routes still answer such an error with status 500, but
  the message is now the error's own.
- `seeding.clear_repository(db)` now raises when the store refuses to clear the default graph. So
  a seed stops there. Before, it ignored the result of `clear_graph()`, and the seed went on over
  the old data. It then failed later with a misleading error, or not at all. An error that
  `clear_graph()` raises passes on unchanged. When `clear_graph()` returns `False`, as
  `kapps-triplestore-interface` 2.1.1 does on an error answer, `clear_repository` raises a
  `GraphDbException` that names the repository.
- A middleware instance that restarts against a graph that holds its earlier registration now
  adopts the individuals it finds there: its Service, Workflows, StateProperties and
  Capabilities. It writes nothing to an individual that did not change. An instance that
  restarts at another address gets a new Service and new Workflows, and each of its Capabilities
  now points to the new Workflow or StateProperty. Before, the creation of an individual that
  existed wrote nothing and reported nothing. So a Capability kept the link to the Workflow of the
  earlier run, whose Service no longer had an address.
- The state dashboard and the activity page now work behind a proxy that serves the process
  under a path prefix, such as `/p/<process>/dashboard`. Each page names its own resources by a
  path relative to the page: `dashboard/static/...`, `dashboard/box/<id>` and `activity/stream`.
  Before, the paths were absolute, so under a prefix the browser asked the proxy's own root for
  them, and the page showed no box and no stream. `/dashboard/` and `/activity/` now redirect to
  the page by a relative path, which keeps the prefix; before, the redirect named an absolute URL
  and dropped it. The worked example of a box's form in the dashboard's documentation now posts
  to `workflows/<name>/execute`, without the leading slash, for the same reason.
- The README's "Status" section no longer names a version. It announced `0.1.0` as the first
  public release for the whole of 0.1.1, because it was the only hardcoded version string in any
  shipped document and no release step owned it. It now describes the `0.x` guarantee instead,
  which does not go stale.
- The README answers three questions it used to leave open, in a "Questions" section below the
  reading table: how to install with uv, what to do about the `externally-managed-environment`
  refusal that Debian and Ubuntu give `pip`, and why `[examples]` is not optional for the factory.
  They sit below the install instruction rather than beside it, so copying the code blocks in
  order still runs one installer and not two.
- The README names the factory demonstration's address, <http://127.0.0.1:8080/>, and its
  `--units` and `--force` options. Before, it said only that you watch it from a browser.
- Installing without `[examples]` is documented as what it is. The factory does not fail on the
  missing `amqtt`: the launcher seeds the graph, the first middleware starts, and the mock PLC
  then retries a refused broker connection indefinitely, with no traceback and no `Factory
  running`. The manifest comment that claimed a clean exit now says this.

### Changed

- Each dashboard page names its instance's resource: the title and the heading are
  "<name> — dashboard", where the name is the resource's `rdfs:label`, else the local name of its
  IRI. Several open dashboards are no longer browser tabs that all read "Middleware Dashboard". Of
  several labels, one without a language tag wins, then an English one. `enable_dashboard` reads
  the name once, so no render reads the graph; an instance with no resource keeps "Middleware
  Dashboard".
- `/activity/stream` now ends after 2 s. A browser's `EventSource` reconnects by itself and
  loses nothing, so the `/activity` page works as before. Any other client must reconnect when
  a stream ends, and send the `id` of the last event it read as the `Last-Event-ID` header. The
  next stream then starts after that event. Every event now carries an `id`, and the 15 s
  keepalive comment is gone.
- An instance with a notification handler takes every notification through its intake, a
  dispatched Operation included, with no lifecycle. The middleware writes no status and no
  provenance for it, the Operation does not enter the in-memory queue, and the event trigger's
  answer names the Operation and carries no `status`. At startup, such an instance's own `queued`
  Operations go into its intake, after its own orphaned `running` ones are marked `failed`. An
  instance without a handler queues every notification as before, and its startup warns when it
  has no `register_callback` callback either. One instance cannot register both a notification
  handler and a `register_callback` callback: the second registration raises `RuntimeError`.
- `request()` creates its Operation in one commit, and only then notifies the receiver. The
  Operation's type, its Capability link, the status `queued`, its issuer and the draft's data are
  admitted together or not at all. A refused Operation notifies nobody. An `operation_iri` that
  the store already holds raises `PreconditionFailedError`, and that Operation stays as it was.
  Before, the new Operation merged into it, and a failed notification then removed its status.
- `request()` is built on `mw.create()` and on the delivery of `notify()`. A failed notification
  leaves the Operation `queued`, as the commit wrote it, and raises `NotificationUndelivered`,
  with the HTTP client's error as its cause. Before, `request()` removed the Operation and raised
  the HTTP client's error itself. The receiver takes up its `queued` Operations when it starts,
  and the issuer is responsible for the Operation while it is `queued`.
- An instance without a notification handler reads a notified Operation with one query, of its
  type, its status and its issuer, before it queues it. A notification of an Operation that its
  resource issued and that is no longer `queued` is not queued: the answer names the Operation
  and claims no status.
- An instance with a notification handler reads the status and the issuer of an Operation that
  matches one of its workflows, in one query, before the workflow runs. A notification of an
  Operation that the instance's resource issued and that is no longer `queued` goes to the handler
  and never runs the workflow.
- `claim_next()` is built on `mw.commit()`. It commits the claim, `queued` to `running`, before
  the body runs, and a refused claim raises before the body runs. `done` or `failed`, with its
  provenance, is one commit, as before. Two cases that left the Operation `running` now record
  `failed`, with its provenance and the dumped datamodel: a `done` that the store refuses, and a
  fetch of the Operation that fails after the claim. `register_callback()` keeps this lifecycle.
- `SemanticMiddleware.handover()` commits the change of possession when it is entered, through
  `mw.commit()`, and only then runs its body, the move. If the store refuses the change, the body
  does not run and nothing is written. A clean exit writes nothing more, and an error in the body
  undoes nothing. Before, the body ran first and the change was written when it ended.
- A Resource has one possession state, `<resource>_possession`, from its first possession on. A
  handover points the workpiece at the counterpart's state, and writes the counterpart's
  `cfc:hasPossessor` only when it creates that state. Before, each handover made a new state, added
  a link to it to the counterpart, and left the giver's link to the old state. `create_possession`
  and `switch_possession` follow the same model, each as one OGM commit.
- `mint_possession_state_iri` takes a Resource now, and returns its fixed state IRI
  `<resource>_possession`. Before, it took a workpiece and returned a new IRI at each call. It is
  still importable from `kapps_semantic_middleware.registration`.
- A `workpiece_update` value is checked by the OGM now, so its property must be declared in the
  ontology. An IRI (an `IRI` or an rdflib `URIRef`) and `{"id": iri}` are references to that
  object, as before. Any other value goes to the OGM as it is: a plain `str` for an object property
  raises `ValueError` before anything is written, where it was written as a literal before.
- `handover()` raises `PossessionSwitchConflict` when the store refuses its commit because the
  precondition of the commit is false. An example is a second handover of the same workpiece that
  lands between the commit's read of the workpiece and its write. The store's
  `PreconditionFailedError` is its cause, nothing of the commit is written, and the body does not
  run. `switch_possession` raises it in the same case, as in 0.1.0.
- `request()` creates its Operation with `mw.create()` under the `operation` scope. Its callers
  do not change. Five details are new:
  - the Operation's IRI is `<class>_<12 hex>`, without the `_op_` that `mint_operation_iri` writes
  - it commits on its own also inside the body of another commit, and a `mw.create()` in its body
    joins the Operation's commit
  - a value of the draft that is an IRI is a reference, as in `workpiece_update`
  - a draft that sets `cfc:implementsCapability` raises `ValueError` before anything is written,
    because the scope sets it
  - a draft that sets `svc:issuedBy` raises `ValueError` before anything is written, because
    `request()` sets it
- A commit of `mw.commit()` merges two equal `Create`s of one instance into one, as when two new
  Workpieces create their possessor's state in one commit. kapps-ogm still refuses two different
  `Create`s of one instance.
- A `Create` of an instance that the store already holds raises `PreconditionFailedError` from
  `kapps_triplestore_interface.exceptions`, not `ValueError`, in `mw.commit()`, `mw.create()`,
  `request()` and `create_operation`. So does a commit that removes a value that another write
  changed after the commit read it; before, such a commit wrote nothing and returned. The store
  checks both in the same request as the write, and the error passes on unchanged from kapps-ogm.

### Known limitations

- An issuer that has more than one live Service, such as a controller and a monitor of one
  Resource, is not notified, as `notify()` notifies no such resource; the warning says so.
- The notifications of issuers that are still to send when an instance stops, or when the
  process ends, are sent for up to one second, and the rest are dropped with one WARNING line. A
  dropped notification leaves its issuer's wait to its timeout: the status is in the graph.
- `await op.ended(timeout=None)` and its blocking twin `op.wait_ended(timeout=None)` can wait
  forever although the graph already says `done` or `failed`. The wait ends only when a
  notification of the Operation arrives, and a notification can be lost: a stop of the claiming
  instance drops the notifications that are still queued, and an issuer that does not answer
  delays the notifications of the others. A caller passes a timeout: when it passes, the wait
  reads the graph once more and returns `done` or `failed`.
- Two claimants of one Operation are not told apart yet. The second claim changes nothing, so
  the store admits it, and the bodies of both claimants run.
- Two handovers of one workpiece at the same moment are refused only in part. Both pass the
  preconditions before either commits. Inside its commit, the OGM compares the workpiece with a
  fresh read of it. If the other commit landed before that read, the later commit overwrites it,
  both handovers succeed, and both bodies run. Only a commit that lands between that read and the
  write is refused (see *Changed*).
- A Workpiece's first possession under the `workpiece` scope can race another write that creates
  the same possession state. The store then refuses the commit with `PreconditionFailedError`,
  and nothing of it is written. Unlike `handover()`, `mw.create()` does not try again.

## 0.1.1 — 2026-09-20

### Fixed

- The factory demonstration (`kapps-transferunit-factory`) no longer deadlocks on Windows while
  it starts. The launcher now drains every child's output from the moment it spawns the child:
  - a middleware's output no longer waits unread while the launcher waits for the next PLC
  - a PLC's output drains on a thread, not on the launcher's own thread
- Ctrl+C stops the factory demonstration cleanly. Before, a `KeyboardInterrupt` traceback and
  exit code 130 followed "Stopping factory..." after every child had already stopped.
- Scenario 1 labels the `executionTimestamp` it prints as UTC. The graph still stores UTC, as
  before.
- The hint that `kapps-examples` prints names the README section that exists, "Start a GraphDB".
- The README now states three things about the setup:
  - `kapps-examples` does the copying
  - its destination is relative to the current working directory
  - the three `GRAPHDB_*` variables have PowerShell, `cmd.exe` and `setx` forms beside the POSIX
    ones
- The `docker compose` file that `kapps-examples` copies names its project `kapps-demo` and its
  containers `kapps-demo-graphdb` and `kapps-demo-graphdb-init`. Before, compose took the
  project name from the containing directory, which was literally "docker".
- The `docker compose` configuration that `kapps-examples` copies now creates the `kapps-demo`
  repository with the `owl-max` ruleset. `rdfsplus-optimized` did not infer the `rdfs:Class` of
  an `rdfs:range`, which `kapps-ogm` needs. A `kapps-demo` repository that already exists keeps
  its ruleset.
- An install no longer prints a `SyntaxWarning` from `kapps-triplestore-interface`. This package
  now requires at least version 2.1.1 of it, which carries the fix.

### Changed

- The three sibling dependencies are compatible-release ranges now:
  - `kapps-ogm~=0.2.0`
  - `kapps-triplestore-interface~=2.1.1`
  - `transitional-sync-middleware~=0.1.0`

  A patch release of a sibling installs as before. A new minor or major release installs only
  after this package declares it.
- No file in the distribution cites a decision record any more. The wheel excludes those records,
  so each citation pointed at text the user could not read. Every statement that carried a
  citation keeps its content. The sweep covers:
  - the output that scenario 1 prints
  - the `docker compose` file
  - docstrings and source comments
  - `AGENTS.md`, `CONTEXT-MAP.md` and the glossary
  - the examples' documentation
- `AGENTS.md` now says where the reasoning lives. Before, it told a consuming agent not to follow
  the citations.
- No shipped file cites an issue-tracker ticket any more. Each rule that a citation stood for
  now appears in place. The sweep covers:
  - docstrings and notebook code cells
  - the command-line help texts
  - the station board's hover text and the factory demonstration's page templates
  - the factory ontology's comment
  - the source comments that the documentation site renders
  - every other source comment, the tests, the manifest and this changelog
  - the TransferUnit ontology's design notes, folded into the ontology's own comments and
    retired

### Known limitations

- The SHACL validation that the `kapps-demo` repository configures is unverified. No test shows
  yet that the repository rejects a write that violates a shape.

## 0.1.0 — 2026-08-12

### Added

- **First public release.** A resource's state is described in an RDF knowledge graph as a
  protocol-interface parameter, and the middleware wires that description to a real device:
  peer discovery through the graph, a northbound REST surface, and southbound connectors that
  keep graph and device in step.
- Two runnable scenarios and a six-process factory demonstration, copied out of the installed
  package with `kapps-examples` — including the `docker compose` files that stand up the
  GraphDB they need, which previously reached only a reader of the source repository.
- `kapps-transferunit-factory`, a console script that boots the factory demonstration.
- Agent-facing documentation inside the distribution: `AGENTS.md`, `CONTEXT-MAP.md` and the
  eight `docs/mechanics/` pages, so a consuming agent reading `site-packages` finds the rules
  that explain what it is reading.

### Changed

- The synchronization layer is now `transitional-sync-middleware`, imported as
  `transitional_sync_middleware`. It is the same code this project has always built on — a
  transitional fork of `aas-middleware` carrying four synchronization fixes — published under
  its own name. **It is retired within a few releases; do not build on it directly.**

### Known limitations

- Only `resource` and `watchdog` modes are implemented. `server` is reserved and raises
  `NotImplementedError`.
- A `ClassScope` selects *which* parameters, never *which parts* of one. Any chain element
  below a complex property is discarded during fetch, with nothing raised.
- Several consumption rules fail silently rather than raising. They are listed in `AGENTS.md`
  under "These fail silently", and reading that section is not optional if you are writing code
  against this library.
