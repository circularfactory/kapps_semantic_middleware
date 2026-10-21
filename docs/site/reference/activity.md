# Activity

Tracking what the middleware is doing while it does it.

## A refused write

When the store's SHACL shapes refuse a write of `mw.commit()`, `mw.create()` or
`possession.handover(mw, ...)`, the instance logs the refusal as one record at INFO, on its own
logger, `kapps_semantic_middleware.validation_report`. The feed shows it like any record. Its
message has several lines:

```text
handover refused by StationPossessionShape at Station3
Result 1 of 1
  Message: A Station may possess at most one Workpiece at a time.
  Severity: Violation
  Focus node: Station3
  Path: (hasPossessor/^hasPossessedWorkpiece)
  Value: —
  Source shape: StationPossessionShape
  Constraint: MaxCountConstraintComponent
The store's validation report, verbatim:
_:b0 <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/ns/shacl#ValidationReport> .
...
```

The first line says what was refused, the verb, and by which rules: each source shape at its focus
node, by local name. When the store refuses the write of a `commit`'s error transition, after the
body failed, the first line reads `commit (error transition) refused by ...`, and the caller still
gets the error of the body. The following lines hold each result of the report, readable, with local
names, and then the whole report, as the store sent it. A report that is not RDF names no result;
the record says so, and holds the text as it is. The record of a stream event carries the whole
message, line breaks included, in its `message` field.

The activity page shows a record of several lines as its first line, the text before the first
line break, with the rest behind an expander that is closed at first. Opened, the expander shows
the rest as text, with its line breaks and its indentation kept. A record of one line has no
expander.

The same refusal is in the instance's memory, `mw.refused_writes`, and in the dashboard's box
"Refused writes", which shows the full IRIs as well. The caller of the write gets the store's
`ShaclValidationError`, unchanged.

```{eval-rst}
.. automodule:: kapps_semantic_middleware.activity
   :members:
   :show-inheritance:
```
