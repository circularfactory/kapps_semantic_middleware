# Context Map

## Contexts

- [Core Middleware](./src/kapps_semantic_middleware/CONTEXT.md) — the middleware library itself.
  It holds the Service/Workflow/Capability/Operation/Mode registration machinery and the
  execution machinery.
- [SHACL Interop](./src/kapps_semantic_middleware/shacl_interop/CONTEXT.md) — the temporary SHACL
  seam for workflow precondition shapes and outcome shapes. It generates and parses them. It is
  explicit scaffolding, will move to `kapps_ogm` (see its ADR 0001).
- [Example Scenarios](./examples/CONTEXT.md) — self-contained demonstration notebooks, plus the
  seed-data logic and the ontology-provisioning logic under them. That logic makes each notebook
  reproducible against a dummy repository, and never against the production state.
- [TransferUnit Factory](./demo/transferunits/CONTEXT.md) — the runnable multi-process demo. A
  launcher seeds N units and starts one process per participant (ADR 0029). Its decisions live
  with the Core Middleware ADRs, next to ADR 0029.
- **Module Requirements** — requirements this project places on sibling KAPPS-family
  modules (`kapps_ogm`, the not-yet-created visual-toolbox GUI). Not a code context, and its
  documents are development-repository material that does not ship.

## Relationships

- **Core Middleware → SHACL Interop**: Core Middleware calls into SHACL Interop to read or write
  a shape. The shape belongs to a Workflow class or a StateProperty class. The call happens when
  `@mw.workflow` or `@mw.state` registers or resolves one. SHACL Interop has no dependency back
  on Core Middleware. It operates on class IRIs and shapes, and not on types of the Core Middleware.
- **Example Scenarios → Core Middleware**: Example Scenarios instantiate and exercise Core
  Middleware end-to-end, against the ontology and the instance data they seed themselves. Core Middleware
  has no dependency on Example Scenarios.
- **TransferUnit Factory → Core Middleware**: the factory runs several instances of the library
  in separate processes. That is one instance per unit, plus a controller. A monitor is
  milestone 2 (ADR 0032) and is not built. Core Middleware has no dependency on the factory.
- **Module Requirements** records obligations that Core Middleware and SHACL Interop place on
  `kapps_ogm` and the visual-toolbox repo. It does not depend on the code contexts, and no code
  context depends on it. It is a record for work that belongs elsewhere.

## Ontology-module layering

The vocabulary of the project is layered across three modules (Core Middleware ADR 0012):

- **`cfc:` — Core** (`.../Core#`): published, external, superior — `Operation`/`Capability`/
  `Resource`/`Task`. The system imports and specializes it. The system does not modify it.
- **`mes:` — MES** (`.../MES#`, `src/kapps_semantic_middleware/ontology/mes.ttl`): a
  **domain** ontology that imports Core and details it out — possession + handover ability.
  What domain experts touch.
- **`svc:` — Service** (`.../Service#`, `ontology/service.ttl`): middleware-to-middleware
  **reachability and coordination only**, domain code does not touch it. It holds Service, Workflow and
  StateProperty, the address, the endpoint and the heartbeat. It also holds the resolution chain,
  and an Operation's status and provenance.

`mes:` and `svc:` are siblings that both import Core. `mes:` is domain-facing, `svc:` is
middleware-facing.

## Where the decisions are written down

The architecture decision records are development-repository material and are not part of
this distribution. What they decided is described, without the deliberation, in
[`docs/mechanics/`](docs/mechanics/); [`AGENTS.md`](AGENTS.md) indexes those pages.

