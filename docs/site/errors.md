# When a call fails: the error map

This page tells a consumer of the KAPPS stack what to do when it catches an error. It covers the
three layers: `kapps-triplestore-interface` (the interface), `kapps-ogm` (the OGM) and
`kapps-semantic-middleware` (the middleware). It describes the error types of interface 2.3, OGM
0.4 and middleware 0.3, and the changes that the next releases plan.

## The rule

> Currently the user/consumer interprets errors, not KAPPS. Therefore KAPPS passes all errors along
> transparently. Interpretation, and therefore modification, of errors is fog in the far future.

The map does not change an error. It tells a consumer what it can do with one. Two rules follow
from the first:

- No layer raises a lower layer's error again as its own type. A library raises its own type only
  where it detects the condition.
- A message stays as the layer that detected the condition wrote it, and a store report stays raw.

## Five actions

Each error asks for one of five actions.

| Action | What happened | Who acts | Send the same request again? |
|---|---|---|---|
| **Repair** | The tool or its deployment is broken: configuration, credentials, rights, an ontology that the OGM cannot map, a missing optional extra, an internal fault. | The operator. The KAPPS maintainers, for a bug. | No, not before the repair. |
| **Retry** | This attempt failed for a temporary reason: the store is unreachable or slow, or a peer is not live. | Nobody. | Yes, later. |
| **Refresh** | The caller's information is old: another writer changed a value, the instance is gone, or another worker took the Operation. | The consumer, at run time. | After it reads the state again. |
| **Refused** | The tool and the request are correct, but a rule of the domain does not permit it: a SHACL shape, the OGM's check against the ontology, a handover precondition. | The consumer, with another plan. A domain expert, if the rule is wrong. | No, not before the state changes. |
| **Fix the call** | The caller used the API incorrectly: an argument type, the IRI syntax, a mode, a malformed query. | The consumer's developer. | No. |

## The map

A solid arrow means that the type tells the action. A dashed arrow means that the type alone does not
tell it: the consumer must read the message, and that is interpretation. Each dashed arrow is a gap,
and {ref}`error-map-gaps` lists them. A type with a dashed border has at least one gap.

### kapps-triplestore-interface

```{mermaid}
flowchart LR
  T1["ShaclValidationError"]
  T2["PreconditionFailedError"]
  T3["TripleStoreResponseError<br/>(any other error answer)"]
  T4["AuthenticationError (401)<br/>TripleStoreAccessError (403)<br/>InvalidDatasetError<br/>ValueError: variable not set"]
  T5["InvalidIRIError · InvalidInputError<br/>InvalidQueryError · TypeError"]
  T6["TripleStoreUnavailableError<br/>(unreachable, timeout,<br/>dropped connection)"]
  T7["TripleStoreNotSupportedError"]
  RE(["Repair<br/>operator"])
  RT(["Retry<br/>nobody"])
  RF(["Refresh<br/>consumer at run time"])
  NO(["Refused<br/>consumer · domain expert"])
  FX(["Fix the call<br/>consumer's developer"])

  T1 --> NO
  T2 --> RF
  T3 --> RE
  T4 --> RE
  T5 --> FX
  T6 --> RT
  T7 --> FX
```

### kapps-ogm

```{mermaid}
flowchart LR
  O1["OGMDataError"]
  O2["OGMOntologyError"]
  O3["OGMNotFoundError<br/>AnonymousNodeFetchError<br/>UnresolvableNodeAddressError<br/>AmbiguousNodeAlignmentError"]
  O4["OGMUsageError<br/>OGMUsageTypeError"]
  O5["pydantic.ValidationError<br/>(raw)"]
  O6["RuntimeError<br/>ValueError: an unknown value kind,<br/>an invalid Skolem namespace"]
  RE(["Repair<br/>operator"])
  RF(["Refresh<br/>consumer at run time"])
  NO(["Refused<br/>consumer · domain expert"])
  FX(["Fix the call<br/>consumer's developer"])

  O1 --> NO
  O2 --> RE
  O3 --> RF
  O4 --> FX
  O5 --> NO
  O6 --> RE
```

`OGMError` is the base of the four categories, and the OGM raises only the categories. Each
category also subclasses the built-in that it replaces: the four are each a `ValueError`, and
`OGMUsageTypeError` is also a `TypeError`.

### kapps-semantic-middleware

```{mermaid}
flowchart LR
  K1["HandoverPreconditionError"]
  K2["PossessionSwitchConflict"]
  K3["OperationQueueEmpty"]
  K4["OperationReceiverUnavailableError<br/>(an OperationResolutionError)<br/>NotificationReceiverUnavailableError<br/>(a NotificationUndelivered)"]
  K5["DeploymentError:<br/>DeploymentValueError · DeploymentRuntimeError<br/>CapabilityNotRealizedError<br/>NotificationReceiverAmbiguousError"]
  K6["TimeoutError"]
  K7["OntologyGroundTruthError · ProjectionError<br/>ImportError · NotImplementedError"]
  K8["ValueError · RuntimeError<br/>AttributeError"]
  K9["REST routes:<br/>HTTP 500 for every error"]
  RE(["Repair<br/>operator"])
  RT(["Retry<br/>nobody"])
  RF(["Refresh<br/>consumer at run time"])
  NO(["Refused<br/>consumer · domain expert"])
  FX(["Fix the call<br/>consumer's developer"])

  K1 --> NO
  K2 --> RF
  K3 --> RF
  K4 --> RT
  K5 --> RE
  K6 --> RT
  K7 --> RE
  K8 -.-> FX & RF & RT
  K9 -.-> RE & RT & RF & NO & FX

  classDef gap stroke-dasharray: 5 5
  class K8,K9 gap
```

The same map as a table:

| Layer | Error type | Action |
|---|---|---|
| interface | `ShaclValidationError`, a subtype of `TripleStoreResponseError`: the store's shapes refused the write. `.report` is the store's report, verbatim. | Refused |
| interface | `PreconditionFailedError`: the precondition of a checked write was false, and nothing was written. It is a sibling of `TripleStoreResponseError`, not a subtype: the store answered the request successfully. | Refresh |
| interface | `TripleStoreResponseError`: the store answered with an error status that no subtype covers. `.status` is the HTTP status, and the message holds the store's answer unchanged. | Repair |
| interface | `AuthenticationError` (401) and `TripleStoreAccessError` (403), both subtypes of `TripleStoreResponseError`; `InvalidDatasetError`; a `ValueError` for a store variable that is not set | Repair |
| interface | `TripleStoreUnavailableError`: the store is unreachable, did not answer in time, or dropped the connection. It is chained to the transport error. | Retry |
| interface | `InvalidIRIError`, `InvalidInputError`, `InvalidQueryError`, `TypeError` | Fix the call |
| interface | `TripleStoreNotSupportedError`: the store, or the dataset, cannot do the operation. | Fix the call |
| OGM | `OGMError`: the base of the four categories below. The OGM raises only the categories. | the action of the category |
| OGM | `OGMDataError`, a `ValueError`: the data breaks the class. A required property is missing, a property is unknown, a count is wrong, or a value has the wrong type. So does a class scope that names a property the class does not have, or that continues a property chain past a literal. `instance_iri`, `property_iri`, `class_iri` and `value` give the context. | Refused |
| OGM | `OGMOntologyError`, a `ValueError`: the ontology has a shape that the OGM cannot map. A class or a property has no type, a property has no range, its ranges do not merge, or the OGM does not support its range. `class_iri` and `property_iri` give the context. | Repair |
| OGM | `OGMNotFoundError`, a `ValueError`: the instance or the node does not exist, or the OGM cannot resolve it. `iri` names it. `AnonymousNodeFetchError`, `UnresolvableNodeAddressError` and `AmbiguousNodeAlignmentError` are its subclasses. | Refresh |
| OGM | `OGMUsageError`, a `ValueError`, and its subclass `OGMUsageTypeError`, also a `TypeError`: the API was called incorrectly. | Fix the call |
| OGM | `RuntimeError`: an internal invariant failed. A bare `ValueError`: a `PropertySpec` has an unknown value kind, or the Skolem namespace given to the OGM is invalid. | Repair |
| OGM | `pydantic.ValidationError`, raw: the data does not fit the generated model. | Refused |
| middleware | `HandoverPreconditionError`, raised by the possession extension's `handover(mw, ...)` | Refused |
| middleware | `PossessionSwitchConflict`, raised by the possession extension's `handover(mw, ...)` and `switch_possession`; `OperationQueueEmpty` | Refresh |
| middleware | `OperationReceiverUnavailableError`, a subtype of `OperationResolutionError`: a Workflow realizes the Capability, but no Service of it is live now. `NotificationReceiverUnavailableError`, a subtype of `NotificationUndelivered`: the resource has no live Service, or it did not take the notification. | Retry |
| middleware | `DeploymentError`, the base of the middleware's own Repair types: `DeploymentValueError`, also a `ValueError` (a store variable that is not set, an unknown mode or a mode without the arguments that it requires, a Resource with more than one possession state, an instance with no asserted domain type); `DeploymentRuntimeError`, also a `RuntimeError` (the bundled examples cannot be found, an Operation reached the intake of an instance without a notification handler); `CapabilityNotRealizedError`, also an `OperationResolutionError` (no Workflow realizes the Capability); `NotificationReceiverAmbiguousError`, also a `NotificationUndelivered` (the resource has more than one live Service) | Repair |
| middleware | `TimeoutError` of a wait for an Operation | Retry |
| middleware | `OntologyGroundTruthError`, `ProjectionError`, `ImportError` of an optional extra, `NotImplementedError` of a mode | Repair |
| middleware | `ValueError` "REST wiring is read-only: drive a peer through its workflows": at construction, a settable parameter binds over REST under driving wiring (`BIDIRECTIONAL`); also when a write reaches the REST parameter connector. Wire it `TO_PERSISTENCE`, and drive the peer through its workflows. | Fix the call |
| middleware | `ValueError`, `RuntimeError`, `AttributeError` | mostly Fix the call (gap 4) |
| middleware | an error answer of its REST routes | always 500 (gap 7) |

(error-map-gaps)=
## Gaps: where the type does not tell the action

1. **The interface's error answers** (closed in interface 2.3). Every error answer of the store is
   a `TripleStoreResponseError` with the store's HTTP status in `status`, and a 401 and a 403 have
   a type of their own. A caller does not read the message to tell them. Fetching a graph and
   listing the datasets raise these types too, and no longer return `None`. The type's action is
   Repair, also for a 400 or a 5xx, and a 401 can also come from a token that expired during a long run, because the
   interface does not log in again.
2. **Transport errors** (closed in interface 2.3). An unreachable store, a timeout and a dropped
   connection raise `TripleStoreUnavailableError`, chained to the HTTP client's error. A consumer
   no longer imports the HTTP client to catch them. An error of a request that the client could
   not build, such as an invalid URL, still crosses the border raw.
3. **The OGM's `ValueError`** (closed in OGM 0.4). The OGM's own errors have the base `OGMError`
   and one category per action: `OGMDataError` (Refused, for example a required property is
   missing), `OGMOntologyError` (Repair, a property has no `rdfs:range`), `OGMUsageError` (Fix the
   call, two changes name the same instance) and `OGMNotFoundError` (Refresh, the class of an
   instance cannot be found). Each category is also a `ValueError`, so a handler of the built-in
   keeps working. A `ValueError` that is none of the categories is Repair.
4. **The middleware's `ValueError` and `RuntimeError`** (Repair closed in middleware 0.3) cover Fix
   the call, mostly, and also Refresh and Retry. The middleware's Repair conditions, such as a
   missing store variable or an unknown mode, raise `DeploymentValueError` or
   `DeploymentRuntimeError`. Each is a subtype of `DeploymentError` and of the built-in that the
   condition raised before, so an `except ValueError` or `except RuntimeError` still catches it.
5. **The middleware's `OperationResolutionError`** (closed in middleware 0.3). It raises
   `OperationReceiverUnavailableError` (Retry) when a Workflow realizes the Capability but no
   Service of it is live now, and `CapabilityNotRealizedError` (Repair) when no Workflow realizes
   the Capability. Both are subtypes of `OperationResolutionError`.
6. **The middleware's `NotificationUndelivered`** (closed in middleware 0.3). It raises
   `NotificationReceiverUnavailableError` (Retry) when the resource is not live or did not take
   the notification, and `NotificationReceiverAmbiguousError` (Repair) when the resource has more
   than one live Service. Both are subtypes of `NotificationUndelivered`.
7. **The middleware's REST routes** answer 500 for every error, with the message as the detail. A
   remote consumer cannot tell the action.
8. **The middleware's `PossessionSwitchConflict`** replaces the interface's
   `PreconditionFailedError`, which is its cause, and it is not a subclass of it. A consumer that
   catches `PreconditionFailedError` misses a handover conflict.

### Paths that give no error

These calls report a failure with no error. A consumer that does not check the return value does
not see the failure.

| Layer | Call | What it returns on a failure |
|---|---|---|
| interface | `triple_add` | `False`, when the insert gives no result |
| middleware | a read of a parameter route | `[]` |
| middleware | a value read of the MQTT binding | `None` |

### Errors that are logged, not raised

Some handlers log an error and do not raise it. They are not paths to a consumer:

- **Background tasks with no caller**, such as a domain callback or a notification handler. The
  graph records the outcome, for example the status `failed`, and the log holds the trace.
- **Observer hooks of a commit.** A hook that raises does not change the result of the write. A
  refusal of the write itself is raised again.
- **An error transition that fails.** The original error is raised, and the failure is added to it
  as a note.
- **Rendering and diagnostics:** dashboard boxes, the log handler, pretty printing, the cancel of a
  background loop, and optional imports.

## What the next releases change

The next releases close the open gaps. Interface 2.3 already closes gaps 1 and 2 and the import
path of "Paths that give no error", OGM 0.4 closes gap 3, and middleware 0.3 closes gaps 5 and 6
and the Repair part of gap 4. Each change below is planned for the release that the table names.

| Gap | The change | Planned for |
|---|---|---|
| 1 | The auth provider logs in again before the token expires, so a 401 after an expiry becomes rare. | interface 2.4.0 |
| 4 | The built-ins that are Refresh or Retry: not decided. | — |
| 7 | One HTTP status per action, and a raw error body (see below). | middleware 0.3.0 |
| 8 | Not decided. | — |
| no error: `triple_add`, the middleware's two reads | Not decided. | — |

A write whose connection drops after it was sent, or that times out, raises
`TripleStoreUnavailableError`. The interface never sends a write again. A retry by the caller is
safe for two reasons:

- A checked write refuses a second application with `PreconditionFailedError`.
- An insert or an import repeats as a no-op, because a graph is a set. The exceptions are blank
  nodes, which a second insert creates again, and a raw update that the caller writes itself.

### The middleware's HTTP statuses

The middleware's own routes, the parameter routes and the dashboard routes, will answer a typed error
with the status of its action:

| Action | Status |
|---|---|
| Repair | 500 |
| Retry | 503 |
| Refresh | 409, or 404 when the addressed instance does not exist |
| Refused | 422 |
| Fix the call | 400, also for a request that fails validation |

The body of every error answer will have three fields: `error`, the type name; `detail`, the
original message; and `report`, the raw store report or `null`. A bare built-in, such as a
`ValueError`, will answer 500, because its type does not tell the action. No answer will carry a
`Retry-After` header.

The routes that the framework generates do not follow this map. Its read routes on the datamodel
answer 400 with a message of their own, and its workflow routes answer 409 for any `RuntimeError`.
The served datamodel mounts no write route, so a write verb on it answers 405.
