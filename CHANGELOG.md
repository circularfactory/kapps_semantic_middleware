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

## 0.2.5 — 2026-10-24

### Fixed

- A clean stop no longer leaves a resource-mode instance live in the graph after one store error.
  Before, a deregistration that failed was tried once, and its error skipped the rest of the stop:
  the address stayed, and the Service counted as live for up to the liveness limit. Now the
  heartbeat stops first, the deregistration is tried again on a store error for about 3 s, and a
  final failure is logged as one ERROR that names the Service; the notifications' stop still runs.
  A store call that hangs at the stop no longer holds the process's exit. Count about 4 s for the
  library's stop: 3 s of deregistration and 1 s of notifications.

### Documentation

- The TransferUnit factory demo's README names its parts as the rest of the documentation does.
  Each middleware is a middleware instance that builds its MQTT connectors from the broker and
  topics in the knowledge graph, not a "translator"; the control station queries the knowledge
  graph, not "a database". The path diagram now shows that live values stay in each middleware's
  data model and never enter the graph. The packaging comment names `examples` as the demo's one
  extra; `demonstrations` was a stale name.

## 0.2.4 — 2026-10-23

### Changed

- The dashboard's title and heading read "<instance> Dashboard" (was "<instance> — dashboard").
- The dashboard's subtitle reads "Visualization of the middleware state".
- The activity page no longer has a subtitle; its heading stays.
- The library's own boxes no longer show the shortening note: an IRI cell shows its local name and
  keeps the full IRI on hover. `SHORTENING_NOTE` stays importable with the same value for one
  release; a later release removes it.
- The provisioning-and-seeding page, the README, the tests README and the docstrings of the seeding
  helpers and the vocabulary say that the shipped ontologies and shapes seed demonstrations and
  tests: a productive circular factory already holds them in its knowledge graph, and the
  middleware never calls the seeding helpers. Documentation only; no behaviour changes.
- **Breaking: the service ontology's status property is renamed.** `svc:operationStatus` is now
  `svc:hasOperationStatus`, so that a triple reads as a sentence ("op hasOperationStatus
  queued"). There is no alias and no equivalence: stored data that carries the old name is not
  read. Reload the shared graphs (`load_shared_ontologies(db, reload=True)`) before you run on
  the new version; the seeds and the demonstrations do it now. The `OperationStatus` enum keeps
  its name.
- **Breaking: eight more service-ontology properties are named as predicates.**
  `svc:precondition`, `svc:outcome`, `svc:address`, `svc:endpoint`, `svc:lastHeartbeat`,
  `svc:executionTimestamp`, `svc:executionResult` and `svc:failureState` are now
  `svc:hasPrecondition`, `svc:hasOutcome`, `svc:hasAddress`, `svc:hasEndpoint`,
  `svc:hasLastHeartbeat`, `svc:hasExecutionTimestamp`, `svc:hasExecutionResult` and
  `svc:hasFailureState`. The vocabulary terms (`SVC.hasAddress` and so on) follow. There is no
  alias and no equivalence: stored data that carries an old name is not read. Reload the shared
  graphs (`load_shared_ontologies(db, reload=True)`) before you run on the new version; the
  seeds and the demonstrations do it now. Names that already read as predicates stay.
- **Breaking: every ontology namespace uses `http://`.** The copied Core ontology, the service
  and MES ontologies, the transfer-unit demo's CRC namespaces and the example namespaces change
  their scheme from `https` to `http`, in step with the Core ontology. The shared named graphs
  are named by these IRIs, so a reload writes new graphs and leaves the old `https` Core,
  Service and MES graphs on the dataset: drop them once after you upgrade. Stored data that
  carries the old namespaces is not read.

### Added

- **Every Operation has exactly one status, and a shipped shape states it.** The service ontology
  now ships `service-shapes.ttl` (an interim name) beside `service.ttl`: one SHACL node shape that targets Core's
  Operation class, so every subclass, with a property shape on `svc:hasOperationStatus`: at least
  one, at most one, and one of `queued`, `running`, `done` and `failed`. A store that validates it
  refuses at commit an Operation without a status, with two, or with another value; a refused
  middleware commit shows in `mw.refused_writes` and in the log. A productive store already holds
  its ontologies and shapes; the file is for seeding a demonstration or test store, and as
  documentation. `seeding.read_service_shapes()` returns it and `seeding.set_service_shapes(db)`
  sets it (both interim names); on a dataset without SHACL validation the second logs one line and
  returns `False`. The examples' seeds and the transfer-unit demo's seed set it now, after they
  clear the dataset. `shapes_set` replaces every shape: a caller with shapes of its own sends them
  joined with these. The live tests run with it set for the session.
- **The factory demo's station board has a prewritten-query dropdown.** It fills the view
  heuristic with All, Even-indexed or Odd-indexed TransferUnits, or a Custom starter. The default
  view, and what "reset" restores, is now all live TransferUnits rather than the even-indexed
  ones. `build_view_query` takes a `parity` argument ("all", "even", "odd").

### Changed

- **The shipped text and tests use the middleware's own vocabulary only.** Examples, test data and
  recorded store reports that named another project's resources and operations now name neutral
  ones (a drill, a planner, a transport line); the check that bans those names scans every
  shipped file, and the changelog's unreleased section.

## 0.2.2 — 2026-10-21

- **kapps_semantic_middleware requires kapps-ogm 0.3.2 and kapps-triplestore-interface 2.2.2 or
  later within their minor versions** (`kapps-ogm~=0.3.2`, `kapps-triplestore-interface~=2.2.2`).
  They are the releases this version is tested against; the registration's `Append` needs
  kapps-ogm 0.3.2.

### Added

- **The handover is a function of the possession extension: `possession.handover(mw, *, mode,
  workpiece, counterpart, workpiece_update=None, operation=None, entry=None)`.** It is the context
  manager that `SemanticMiddleware.handover()` was, with the same behaviour: the two preconditions
  are read first, the change of possession and `entry` are committed before the body, a refusal
  writes nothing and skips the body, a `PreconditionFailedError` of a commit that created the
  counterpart's state is read, checked and committed once more, and an error in the body undoes
  nothing. It uses only `mw.commit()`, `mw.ogm`, `mw.resource` and `mw.mode`, so the generic core
  names no possession term. `create_possession`, `switch_possession`, `find_possession_state` and
  `counterpart_has_complementary_ability` are in `kapps_semantic_middleware.possession` too.

- **`mw.commit()` takes the keyword-only `verb`, `"commit"` (the default) or `"handover"`.** It
  labels a refused write in `mw.refused_writes` and in the dashboard's "Refused writes" box, so a
  refused handover is still shown as `handover`. Any other value raises `ValueError` before
  anything is written.

- **`op.update(instance_iri, data)` changes an existing instance in the commit that creates the
  Operation.** In the block of `mw.issue_operation()`, the Issued Operation takes the `update` of a
  `mw.commit()` transition: each property of `data` replaces the stored values, `[]` removes one,
  and an IRI value becomes a reference. The store admits or refuses the Operation and the updates
  together, and the receiver is notified only after that commit, so a planner writes a Screw's
  back-link to its Operation without a second commit that can leave a one-sided pair. The updates
  are staged after `op.data` and after every creation nested in the block: an update of an
  instance created there folds into its `Create`, the update's value winning; two updates of one
  instance merge. An error in the block drops them; inside another `mw.commit()` they land in the
  Operation's own commit. An update that names the Operation raises `ValueError`, one after the
  block `RuntimeError`.

- **The instance records what it has seen of its connections and of its graph binding, and the
  dashboard shows it.** Each connector that the instance wires reaches the framework through an
  observing wrapper, and the store behind the OGM through an observing store, so nothing is
  added to the traffic. `mw.connections` gives one `ConnectionRecord` per wired connector, with its
  state, last provide, consume, receive and sync, and last error; `mw.graph_binding` gives the
  `GraphBindingRecord`, with the last read, last write and last error of the store. Both are read
  from memory and carry the instance's resource and service IRI. The types, `CallError` and the
  predicate `is_connection_metadata` are in `kapps_semantic_middleware.observation`. The boxes
  "Held data" and "Connectors" show each value's state and the age of its last sync, and the box
  "Instance" the graph binding's last read, last write and last error.

- **`enable_dashboard(mw, dev=False)` makes a page without connection metadata.** It leaves out
  the MQTT topic column, the list of the properties that the projection stripped, and the message
  of a last error; `registry.dev` says which page it is. The default, `dev=True`, shows them as
  before.

- **The factory demo starts a watchdog.** `python -m demo.transferunits` now runs seven
  processes with two units: a new `watchdog` child (`demo/transferunits/watchdog.py`), one
  watchdog-mode middleware on the demo dataset with the default staleness threshold and a
  5-second sweep interval, listed on the launcher page with its state and no address.
  `--no-watchdog` turns it off. The demo README gains "Watch the watchdog", and the smoke test
  kills a unit's middleware and observes the sweep.

- **The factory launcher takes its dataset on the command line.** `python -m demo.transferunits`
  (and `kapps-transferunit-factory`) accepts `--dataset NAME`, default `kapps-demo`, and passes it
  to every graph-side child: the middlewares, the watchdog and, new, the control station, which
  has its own `--dataset` option. The PLCs do not touch the graph and take none. The environment
  still cannot name the dataset.

### Deprecated

- **`SemanticMiddleware.handover()`** is the deprecated alias of
  `kapps_semantic_middleware.possession.handover(mw, ...)`. Each call emits one
  `DeprecationWarning` that names the successor, then returns the successor's context manager
  with the same arguments, so it behaves as before. The contract step after the next release
  removes it.

- **The possession names of `registration`**: `create_possession`, `switch_possession`,
  `find_possession_state`, `counterpart_has_complementary_ability`, and the re-exports of
  `mint_possession_state_iri`, `HandoverPreconditionError` and `PossessionSwitchConflict`. They
  moved to `kapps_semantic_middleware.possession`, and the errors live in
  `kapps_semantic_middleware.errors`. Each old name still resolves: each use emits one
  `DeprecationWarning` that names the new home and returns the same object. Importing
  `registration` does not import the possession module; the first use of a moved helper does.
  Outside these aliases, `registration` names no possession term. The release after this one
  removes them.

### Removed

- **The write routes of a resource's served datamodel.** Resource mode serves its datamodel
  read-only: GET routes on the model, its contained models and each interface-accessible
  parameter. The framework's POST on the model, PUT and DELETE on an item, PUT, DELETE and POST
  on a contained model, and the PUT on each read-write parameter route are no longer mounted, and
  a write verb on these paths answers 405. They go without a deprecated alias: a write through
  them set the device, or the graph, with no commit around it. Drive a resource through its
  service's workflows instead: an operation, or a direct workflow call.

- **`KnowledgeGraphConnector` and its module `connectors.knowledge_graph_connector`.** They go
  without a deprecated alias: nothing used the class, and its docstring claimed that all graph
  access went through it and that registration never called the OGM directly, which was never
  true. Framework constructs reach the graph through the middleware's `commit`, `create` and
  `fetch`, which call the OGM. The connector-binding page is now "Graph Access from Framework
  Constructs".

- **The TransferUnit factory demo writes nothing to a device; it observes.** In KAPPS a device
  changes only through a workflow of its service, and the demo has no unit workflows yet, so its
  write paths go: the station board's set controls and its `/api/set` and `/api/algorithm/mode`
  routes, the commanded value and write status on each row, the controller's `push` and
  `push_parameter` and its write tracker, the controller's REST write legs (it now wires read legs
  only), the algorithm's timed mode, its write step and the control station's `--tick` option.
  The algorithm still reacts to a light barrier's change, and only reads. The board shows what
  the units report; a belt still moves at its own PLC panel. The demo's control returns with
  workflows.

### Fixed

- **The documentation says what the code does.** A PUT on a parameter route is southbound only and
  writes nothing to the graph. Server mode is reserved and not built. The concepts page names the
  ruleset `rdfsplus-optimized` and says which reads are inferred and which asserted. Operation
  dispatch is described as it works: the dispatcher posts the Operation's IRI to the Service's
  event trigger, and the Service claims it and runs the Workflow in-process. The contact setting of
  the served API is a role and a URL, not a person. The closed-vocabulary rule of the OGM (a write
  of a property that the class does not declare is refused) is stated on the views-and-projection
  page.

- **Shipped text carries no decision provenance.** Dates, persons, ticket numbers and a server
  name are gone from the validation-report, credentials and repository-configuration comments;
  each rule and its reason stays.

- **The watchdog fails only the Operations that no surviving Service will finish.** The sweep
  used to fail every `queued` and `running` Operation of a resource with a stale Service, so a
  dead monitor took its live controller sibling's queue down with it. It now fails, and notifies
  the issuer of, only the stranded ones, live meaning a Service with an `svc:address` and a
  heartbeat within the staleness threshold: a `queued` Operation is stranded when its Capability
  is realized by no live Service of its resource; a `running` Operation when the Service of its
  executor is not live, or it has no recorded executor. The stale Service is still deregistered.
  `claim_next()` now records the executor: its claim commits `svc:executedByWorkflow`, the
  claiming instance's own Workflow of the Operation's Capability, with `running`, in one commit,
  when the instance exposes a workflow for that Capability; the end writes the same Workflow
  (before, the end wrote the Workflow that the graph names as the Capability's realizer, which
  is the same one unless a sibling on the resource registered the same workflow later). Claims
  that use `mw.commit()` with an entry of their own record no executor, and their running
  Operations are failed by the sweep as before. The new
  `registration.find_stranded_operations(ogm, resource_iri, statuses, max_age_seconds, now,
  named_graph, *, excluding=())` is the predicate, in one query. The startup
  reconstruction of a queue uses it for its orphaned `running` Operations, with its own Service
  excluded; its `queued` Operations are taken up as before, without the predicate. An instance
  that restarts at a new address within the threshold leaves its orphan `running` until the
  watchdog sweeps its old Service. The mechanics pages on instantiation and lifecycle and on
  operation coordination state these limits. Behaviour change: a deployment that relied on a dead
  resource's whole queue being failed now keeps the queued Operations a live sibling realizes, and
  the running Operations a live sibling claimed.

- **The heartbeat and the watchdog's sweep survive a store error.** One failed call to the store
  (the store unreachable, a dropped connection, an error answer, a value changed by another
  write) used to end the loop silently: a watchdog stopped sweeping, so a killed instance kept
  its address, and an instance stopped beating, so a watchdog swept it while it ran. Each failure
  is now logged as one warning that names the error, and the loop tries again at its next
  interval. Cancellation still ends both loops; any other error ends them, logged at ERROR, and
  is raised. Known limits: a running instance is safe from the sweep only while a store outage
  is shorter than the staleness threshold minus one heartbeat interval; a swept instance that
  still runs keeps beating without an address until it restarts.

- **One stale Service no longer blocks the sweep of the others.** A store error inside a sweep is
  logged with the Service or Operation it hit, and the sweep goes on with the next stale Service.
  A Service whose stranded Operations were not all failed keeps its address for the next sweep,
  and `sweep()` / `sweep_stale_services` return only the Services actually deregistered. An
  ambiguous write that marks an Operation `failed` (applied by the store, answer lost) can skip
  the notification of its issuer.

- **A deregistration removes the address last.** `deregister_service` clears the Workflow and
  StateProperty endpoints first and `svc:address` last, so a deregistration that a store error
  stops halfway leaves a Service that the watchdog finds again and finishes, not endpoints that
  nothing finds.

- **Registration writes the first heartbeat with the address.** `register_service` writes
  `svc:address` and `svc:lastHeartbeat` in one commit. Before, a sweep that ran between the
  registration and the heartbeat loop's first beat found an address without a fresh heartbeat
  and deregistered the starting instance, failing its stranded Operations (a sweep already in
  flight when the registration commits can still remove the new address). A Service registered
  with no heartbeat loop (`heartbeat_interval=None`) is now stale after the threshold instead of
  at the first sweep.

- **An update folded into a `Create` keeps its properties.** When a `transition.update()` of
  `mw.commit()` changed an instance that a nested `mw.create()` made, the update folded into the
  `Create` but the `Create`'s class scope did not name the update's properties, so kapps_ogm could
  drop them with only a warning. The folded `Create` now names every property it carries.

### Changed

- **The REST binding is read-only.** It polls a peer's served datamodel by GET and builds no write
  leg. A settable parameter that binds over REST under bidirectional wiring raises `ValueError`
  ("REST wiring is read-only: drive a peer through its workflows") at construction, before
  anything is sent; wire it `TO_PERSISTENCE`. `RESTParameterConnector.consume` refuses with the
  same error, and its `poll` argument is gone.

- **Registration writes each Capability and the resource's link to it in one commit, through the
  OGM.** `register_workflow` and `register_state_property` write the Capability, its link to its
  Workflow or StateProperty, and the resource's link to it (`cfc:hasCapability`) in one
  `OGM.commit`, to the registration's named graph: a `Create` of the Capability and one `Append`
  of the link. A Capability that an earlier run left in the graph is adopted in the same way, an
  `Update` and the `Append` in one commit. Before, the link was a write of its own, the access
  module's `triple_add`, after the Capability's commit, so the store held a Capability without
  its resource in between, and a shape that requires a Capability's resource refused every
  registration. Nothing is read of the resource's stored capabilities. An `Append` adds the link
  and removes none, so two instances that register on one resource at the same moment both keep
  their capability. The link now passes the OGM's checks, the property and its range, as every
  other registration fact. One difference: a resource that the store does not hold is refused
  with the OGM's `OGMNotFoundError`, and nothing is written; the direct insert wrote the link
  anyway. It needs the kapps_ogm release that brings `Append`.

- A static test, `tests/test_no_direct_writes.py`, fails when any module of the package other than
  `seeding` calls a mutation operation of the store's contract (`triple_add`, `triples_update`,
  `graph_clear`, a `query` that may send an update, and the others), and when the contract has a
  public member that it does not classify as a read or a write.

- **The library's examples name no consumer.** The docstring examples of `handover`, `notify`,
  `commit`, `create` and `issue_operation`, the dashboard's explanation of the tab-name rule, a
  comment in `registration`, the glossary and the mechanics pages now use generic terms, such as a
  workpiece, a peer, a tool and a tab named "Line monitor", in place of another project's
  vocabulary. No behaviour changes.

- **The smoke tier boots the factory on the suite's dataset**, the one the `store` fixture
  names, not on `kapps-demo`, so it can run on a disposable dataset. The factory deregistration test does the same.

## 0.2.1 — 2026-10-16

### Added

- **A wipe refuses a store that is not disposable.** `seeding.clear_dataset(store)` and
  `seeding.load_shared_ontologies(store, reload=True)` raise `DeploymentValueError` before they
  clear anything when the store's host is neither loopback (`localhost`, `127.0.0.1`, `::1`) nor a
  bare container alias with no dot, unless `TRIPLESTORE_ALLOW_DESTRUCTIVE=1` is set. The message
  names the dataset and the URL. `seeding.is_disposable_target(url)` is the predicate, with the
  same name and rule as in `kapps_ogm` and `kapps_triplestore_interface`. Reading is never refused. A store that names no URL is refused too, and the old
  `GRAPHDB_ALLOW_DESTRUCTIVE` is not read.
  This guards the example scenarios and the factory demo as well as the tests, since every one of
  them wipes through `seeding`.
- **The middleware gets its store from the triple store interface's factory, and names no vendor.**
  In `kapps_semantic_middleware.credentials`: `triplestore_for(dataset)` returns a connected
  store, built with the interface's `connect()` from a `TripleStoreEndpoint` and a
  `GDBTokenAuth`, so the identity logs in and the dataset is probed at once; `endpoint_for(dataset)`
  returns the `TripleStoreEndpoint` of the dataset; `triplestore_env_present()` says whether the
  environment can reach a store; `REQUIRED_TRIPLESTORE_ENV` names the three variables a store
  needs, `TRIPLESTORE_URL`, `TRIPLESTORE_USERNAME` and `TRIPLESTORE_PASSWORD`; and `DEMO_DATASET`
  is the dataset of the examples and the demo, `kapps-demo`. The caller still names the dataset,
  and `TRIPLESTORE_DATASET` is not read. A missing variable raises `DeploymentValueError`, which
  names it. In `kapps_semantic_middleware.seeding`: `clear_dataset(store)` clears the default
  graph of the store's dataset. Each replaces an old name, which stays as a deprecated alias (see
  *Deprecated*).
- The middleware's own errors tell the action, by type. In `kapps_semantic_middleware.errors`:
  `OperationReceiverUnavailableError` (Retry: a Workflow realizes the Capability, but no Service
  of it is live now) and `CapabilityNotRealizedError` (Repair: no Workflow realizes the
  Capability), both subtypes of `OperationResolutionError`; `NotificationReceiverUnavailableError`
  (Retry: the resource has no live Service, or did not take the notification) and
  `NotificationReceiverAmbiguousError` (Repair: the resource has more than one live Service), both
  subtypes of `NotificationUndelivered`; and `DeploymentError`, the base of the middleware's own
  Repair types, with `DeploymentValueError` (also a `ValueError`) and `DeploymentRuntimeError`
  (also a `RuntimeError`). These two replace the bare built-in of a broken deployment: a store
  variable that is not set, an unknown mode or a mode without the arguments that it requires, a
  Resource with more than one possession state, an instance with no asserted domain type, the
  bundled examples that cannot be found, and an Operation in the intake of an instance without a
  notification handler. `CapabilityNotRealizedError` and `NotificationReceiverAmbiguousError` are
  `DeploymentError`s too. Each type's docstring names its action. Every `except` written against
  the older type or the built-in still catches the new one, and each message is unchanged; only
  the class name in a traceback or a `repr` changes. Wrong use of the API keeps `ValueError`,
  `TypeError` and `RuntimeError`. To tell Retry from Repair, `issue_operation()` reads the graph
  once more when it finds no live receiver; a request that finds one reads as before. The error
  page shows the middleware's errors as they are now.
- The documentation has a page, *When a call fails*, with the error map of the stack. It names the
  five actions that an error asks for (Repair, Retry, Refresh, Refused, Fix the call) and who acts
  on each. It maps each error type of `kapps-triplestore-interface`, `kapps-ogm` and this package
  to its action, lists where a type does not tell the action, and names the planned changes. The
  navigation bar and the API reference link to it. The docs toolchain adds `sphinxcontrib-mermaid`
  for the page's diagrams, which the browser draws with mermaid.js from a CDN.
- A record whose message holds several lines shows its first line in its row and the rest behind
  an expander, closed at first, on the activity page (`/activity`) and in the dashboard's Logs
  tab. Opened, the expander shows the rest as text, with its line breaks and indentation kept; a
  record of one line has no expander. In the Logs tab each expander has a stable id and is kept
  across the box's refresh, as the box of refused writes keeps its own. A store's refusal report
  and a traceback line now take one line until a reader opens them. The Logs tab's cut at 4000
  characters is unchanged. No public name changes; the tests gain a browser tier
  (`pytest-playwright`, marker `browser`), which skips where no browser is installed.
- The "Refused writes" box, `mw.refused_writes` and the activity feed also record a refused error
  transition: when the store's shapes refuse the write that ends a `commit` in error, the record's
  verb is `commit` and its first line reads `commit (error transition) refused by ...`. The
  caller still gets the body's error, with the note that the error transition was not committed.
  No new public name: the record is still a `RefusedWrite`.
- Every instance shows its refused writes readably. When the store's SHACL shapes refuse a write
  of `mw.commit()`, `mw.create()` or `mw.handover()`, the caller still gets `ShaclValidationError`,
  unchanged, and the instance keeps the refusal in memory: `mw.refused_writes`, the newest 50,
  newest first, each a `RefusedWrite` with its time, its verb, the rules its report names, and the
  report. Nothing of it is written to the graph, and a false precondition is not kept. The
  dashboard has a new default box, "Refused writes" (`refused-writes`, tab System), which shows
  each refused write as one visible line, what was refused and by which rules, with an expander
  that holds a table of the report's results and the whole report, verbatim; every value is
  escaped, and an opened expander stays open when the box refreshes. Each refusal is also logged as
  one record at INFO on the logger `kapps_semantic_middleware.validation_report`: its first line
  names the verb and the rules, and the following lines hold the readable results and the whole
  report, so the activity page and the Logs tab show it too. The new module
  `kapps_semantic_middleware.validation_report` reads a validation report as RDF:
  `readable_results` for a person (message, severity, focus node, path, value, source shape and
  source constraint component, each a `Term` with `short` and `full`), `validation_results` for
  comparing (full IRIs, the path in SPARQL property path syntax), and `rule_names` for a log line;
  `render_refused_write` renders one refused write as HTML for a page of your own.
- The state dashboard has a tab "Logs", a default box with the id `logs`. It shows the newest 500
  records of the whole process, every logger, newest first, with a level filter for the view.
  `enable_dashboard` attaches one handler to the root logger for that, once per process; the
  handler keeps the records in memory and changes no logger's level, and a render reads only that
  memory. The tab's form sets a logger's level while the process runs, through
  `POST dashboard/logs/level` with `{"logger": "<name>", "level": "<LEVEL>"}`, and the answer is
  the level in force. The level is one of `NOTSET`, `DEBUG`, `INFO`, `WARNING`, `ERROR` and
  `CRITICAL`; any other value is refused with 422. The tab also lists the loggers seen so far
  with their levels. `/activity` is unchanged, and `register_default_boxes=False` still starts an
  empty page.

### Deprecated

- **The old names of the middleware's store helpers.** Each still works, runs its successor and
  emits one `DeprecationWarning` per use that names the successor. A later release removes them.

  | Old | New |
  |---|---|
  | `credentials.graphdb_for(repository)` | `credentials.triplestore_for(dataset)` |
  | `credentials.graphdb_env_present()` | `credentials.triplestore_env_present()` |
  | `credentials.REQUIRED_GRAPHDB_ENV` | `credentials.REQUIRED_TRIPLESTORE_ENV` |
  | `credentials.credentials_for(repository)` | `credentials.endpoint_for(dataset)` |
  | `credentials.DEMO_REPOSITORY` | `credentials.DEMO_DATASET` |
  | `seeding.clear_repository(db)` | `seeding.clear_dataset(store)` |

  `graphdb_for` returns the store of `triplestore_for`, typed as before. `credentials_for` still
  returns the interface's deprecated credentials, with the URL, the identity and the dataset, and
  not an endpoint. `REQUIRED_GRAPHDB_ENV` and `DEMO_REPOSITORY` are their successors' values, so
  `REQUIRED_GRAPHDB_ENV` now names the `TRIPLESTORE_*` variables.
- **`GRAPHDB_URL`, `GRAPHDB_USERNAME` and `GRAPHDB_PASSWORD` are the deprecated fallback of the
  `TRIPLESTORE_*` variables.** A store builds from them when its `TRIPLESTORE_*` variable is not
  set, and each value read from one emits a `DeprecationWarning` that names the variable which
  replaces it. A `TRIPLESTORE_*` variable wins over its fallback. A later release removes the
  fallback.

### Fixed

- The heartbeat loop no longer beats in phase across participants. A resource-mode instance
  refreshed its Service's heartbeat every interval from the moment it started, so participants
  that started together beat together for life, and a grid's heartbeat writes landed in one burst
  that filled the store's write queue. The loop still writes its first heartbeat at startup, so a
  reader sees a fresh one right after registration; it then waits a random time, drawn uniformly
  from zero up to the interval, before it enters its period. No two heartbeats of an instance are
  further apart than one interval plus one write.
- The dashboard marks the selected tab. The rule that was to give the selected tab's label the
  accent (`.tab-radio:checked + .tab-label`) could never match, because the radios sit beside the
  tab strip and not before each label, so the tabs switched and the highlight never showed, on
  every module's tabs and on the WMS's. The shell now carries one rule per tab, generated with
  the panel rule from the same slug, that reaches the label by its `for` from the checked radio.
- The dashboard's Services box shows what each workflow and each state property does, from the
  docstring of the function that was registered: its first line as the summary, and the whole
  docstring as the description. Before, every workflow read "Execute" with no description, and
  every state property "Get State" with the same text about the library's own handler. The
  middleware sets the text on the routes that it mounts, so the instance's API description
  (`/openapi.json`, and `/docs`) carries it too: on the routes that run a workflow,
  `workflows/<name>/execute` and `workflows/<name>/execute_background`, the event trigger's among
  them, and on a state property's route, `state/<name>`. A workflow's `description` and
  `interrupt` routes keep their summaries, and a function without a docstring keeps the default
  summary and gets no description. No route changes its behaviour or its status codes, and no
  public name changes.
- The dashboard's own routes, the page, its boxes and its static files, are no longer in the
  instance's API description (`/openapi.json`), so `/docs` shows the instance's services and not
  the dashboard. They answer at their addresses as before.
- The dashboard's Instance box shows the sweep's interval and whether the sweep's task is alive
  only on a watchdog-mode instance, the one mode that runs a sweep. A resource-mode instance no
  longer shows "Sweep task alive: no", which read like a failure.
- The HTTP example of the dashboard guide, which sets a logger's level, has the HTTP version in
  its request line. Without it, the docs build with warnings as errors stopped at that example.
- The factory demonstration fails, and says why, when a unit's broker does not come up. Before,
  it hung. Without `[examples]` the broker package `amqtt` is missing, and a broker that took
  longer than 5 s to listen left the same state: the first middleware came up as if healthy, its
  PLC retried a refused connection forever, `Factory running` never printed, and the one
  traceback stayed off the console. Now the unit's middleware exits with an error that names the
  broker's address and either the 5 s timeout or the cause of the failed start, such as the
  missing package. The launcher's wait for a PLC's panel ends when that unit's middleware ends,
  or after 60 s at the latest. It fails with the PLC's last lines of output, and with the
  middleware's too when the middleware still runs at the timeout. A child process that exits
  with an error code while the launcher is not stopping it has its last lines echoed to the
  console. A healthy run echoes the same address lines as before and waits no longer.
- `mw.emit_heartbeat()` outside resource mode raises `RuntimeError` with the mode, as the other
  resource-only methods do: "emit_heartbeat() is only valid in resource mode; current mode is
  ...". A watchdog has no Service whose heartbeat it could refresh. Before, the call reached the
  OGM's commit without an instance and failed there, with a `TypeError` that named no mode.

### Changed

- **The store comes from `connect()`, and the middleware speaks the store's contract.** The
  middleware, its demo and its examples build their store with `triplestore_for(dataset)`, which
  calls the interface's `connect()`. It logs in and probes the dataset when it is built, and a
  dataset that the store does not have raises `InvalidDatasetError` there. They read the store's
  URL and identity from `TRIPLESTORE_*`. Seeding clears and loads with the Graph Store verbs
  `graph_clear` and `graph_add`, and a clear that returns `False` raises
  `TripleStoreResponseError`, the same class as before under its new name. The middleware calls
  no name that the interface deprecates, so it needs the interface release that brings
  `connect()`, `TripleStore`, `graph_add`, `graph_clear`, `dataset` and `shapes_set`.
- The dashboard's Instance box shows the store's dataset in a row named "Dataset", read from the
  store's `dataset`. The row was "Repository".
- The factory demo's middleware process takes `--dataset` in place of `--repository`. A PLC
  process receives none of the store's variables, `TRIPLESTORE_*` or their deprecated fallback.
- The examples, the docs and the copied examples' README say `TRIPLESTORE_*` and "dataset". Only
  the texts that start the local Docker store name its product.
- The live tests load and remove their SHACL shapes with the store's `shapes_set`, which replaces
  every shape of the dataset. A live run therefore also removes the shapes that other code loaded
  into the same dataset. The tests' dataset is still `Tests`, and the `store` fixture gives it.
- The test suite turns each `DeprecationWarning` of the form `<old> is deprecated; use`, `set` or
  `pass` `<successor>` into an error, so a use of a name that the interface or the middleware
  deprecates fails its test.
- The documentation site's landing page is the README, so the two can no longer say different
  things. A link that the README writes relative to the repository points at the same file in
  the public repository, at the release's tag. The old page's list of the components moves to
  the Concepts page; the rest of it was already in the README, on a guide page, on the Concepts
  page or in the navigation bar.
- The example Docker repository (`examples/docker/graphdb-repo-config.ttl`) now runs the
  `rdfsplus-optimized` ruleset, the one ruleset of the project's repositories, where it ran
  `owl-max`.
- The connector recognition queries of `plan_wiring()` and `class_of()` read asserted statements
  only through the interface's `SPARQLQuery(..., include_implicit=False)`, not through a graph IRI
  of GraphDB written into the query text. GraphDB gives the same results as before; another store
  no longer reads the scope as an empty named graph and returns nothing. The module constant
  `connectors.wiring.EXPLICIT_GRAPH` is gone.
- `mw.request()` is renamed `mw.issue_operation()`: it issues an Operation, and the name says so.
  Its signature, its return value, what it raises and what it does are unchanged. `request()` stays
  for one release as a deprecated alias: it warns with a `DeprecationWarning` that names
  `issue_operation()`, then calls it with the same arguments. The alias goes in the release after
  next, so move callers to `issue_operation()`. The docs, the examples and the glossary use the new
  name.
- The constructor converts `named_graph` to an `IRI`, so `mw.named_graph` is an `IRI` or None.
  A string is still accepted. A `named_graph` that is no IRI now raises `InvalidIRIError` at
  construction, after the checks of the mode's arguments, so a missing argument is still the
  first error. Before, the triple store interface raised the same error only at the first write
  to the graph, and an instance that did not write did not raise it at all.
- The library type-checks against the published types of `kapps-ogm` and
  `kapps-triplestore-interface`. For a type checker, `mw.resource_iri`, `mw.service_class` and
  `mw.service_iri` are now `IRI | None`, which is what they are: None outside resource mode.
  Before, they read as `IRI`. A caller that passes one of them on where an `IRI` is required
  checks for None first.

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
