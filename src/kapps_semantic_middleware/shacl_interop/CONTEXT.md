# SHACL Interop

One of five contexts in this repo — see `/CONTEXT-MAP.md` at the repo root for the others.

> This file defines the **vocabulary**. For how to *use* the mechanisms it names, read `AGENTS.md`
> and the `docs/mechanics/` set beside it — workflow signatures and their shapes
> are covered by `docs/mechanics/02-workflow-registration.md`.

Temporary scaffolding that holds an authoring aid for ontology
authors: a helper that drafts the SHACL shape of a Workflow class's precondition/outcome from
a Python function's type hints. Nothing in the middleware calls it. The shapes themselves
belong to the domain's ontology, which its authors write; the middleware does not write them
at registration. The helper properly belongs in the native `kapps_ogm` support.
Deliberately kept separate from Core
Middleware so it can be deleted wholesale, without untangling it from Core Middleware's own
vocabulary, once `kapps_ogm` absorbs this capability.

## Language

**Precondition shape**:
The `sh:property` group reachable via `svc:hasPrecondition` on a Workflow class's `sh:NodeShape`
(`sh:targetClass`), describing the arguments a Workflow's underlying Python function requires.
Written by the ontology's authors; the helper can draft one from the function's type hints.
_Avoid_: Argument schema, input schema (those describe the Python-level shape; Precondition
shape is specifically its SHACL-RDF encoding).

**Outcome shape**:
The `sh:property` group reachable via `svc:hasOutcome` on the same `sh:NodeShape`, describing the
Workflow's return value. Same authorship and encoding pattern as Precondition shape.

**Drafted shape**:
The authoring aid this context holds: turning a Python function's type hints into a draft
SHACL `NodeShape` for an ontology author to review (empty for zero-argument functions). Targets the Workflow/StateProperty *class*, never a specific
instance, for the same fleet-scale reason Core Middleware requires pre-existing types at all.
