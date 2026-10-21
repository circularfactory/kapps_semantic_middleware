# Provisioning and Seeding

Every other page in this set takes `ogm=ogm` as given. This page shows how to construct that
`OGM`, the store under it, and the ontology and instance data the middleware needs before its
first registration. It is the bootstrap order the other pages assume you are already past.

## Bootstrap Order

The order is fixed: construct the store, wrap it in an OGM, load the shared ontologies,
author and seed the domain data, then construct the middleware.

The examples keep to this order and add one rule: each notebook is self-contained. It runs against its
own dummy user and dataset, clears it first, and seeds the ontology modules and instance data its
scenario starts from, so a re-run never writes to production state and a newcomer needs no local
setup of their own. See *Self-contained* in `examples/CONTEXT.md`.

```python
from kapps_triplestore_interface import IRI
from kapps_ogm import OGM
from kapps_semantic_middleware import SemanticMiddleware, Mode
from kapps_semantic_middleware.credentials import triplestore_for
from kapps_semantic_middleware.seeding import load_shared_ontologies, clear_dataset

db = triplestore_for("kapps-demo")  # reads TRIPLESTORE_URL, TRIPLESTORE_USERNAME, TRIPLESTORE_PASSWORD
ogm = OGM(db=db)

clear_dataset(db)         # empties the default graph (see below)
load_shared_ontologies(db)
# author the domain TBox and seed the instance data here (see below)

mw = SemanticMiddleware(
    mode=Mode.RESOURCE,
    resource_iri=IRI("https://example.org/kapps-demo#my_resource"),
    service_class=IRI("https://example.org/kapps-demo#MyService"),
    ogm=ogm,
)
```

`triplestore_for` builds the store with the interface's `connect()`, which logs in and checks the
dataset at once. The dataset is named in code, because the seed clears it. To build a store
yourself, call `connect(TripleStoreEndpoint.from_env(), GDBTokenAuth.from_env())`, which also
reads the dataset from `TRIPLESTORE_DATASET`. Type a store as `TripleStore`; all four names come
from `kapps_triplestore_interface`.

Constructing the middleware wires its connectors and prepares its registration; serving `mw.app`
(an ASGI application) with `uvicorn` is what brings it live and fires the on-startup registration.
Registration fails if a referenced class is missing, which is why the ontologies load first.

## Loading the Shared Ontologies

Every scenario needs the published Core, service, and MES modules. `load_shared_ontologies(db, *,
reload=False)` loads all three; each lands in its own named graph and is skipped if already present
unless you pass `reload=True`.

```python
from kapps_semantic_middleware.seeding import load_shared_ontologies, clear_dataset

clear_dataset(db)           # wipes scenario data
load_shared_ontologies(db)  # loads the shared modules, left standing by clear_dataset
```

`clear_dataset(db)` empties the default graph of the dataset so a re-run starts clean. Because the
store reasons across graphs, a domain class in the default graph still resolves its shared
superclass.

`clear_dataset(db)` and `load_shared_ontologies(db, reload=True)` also refuse a store whose host is
neither loopback nor a bare container alias, and raise `DeploymentValueError` before they clear
anything. Set `TRIPLESTORE_ALLOW_DESTRUCTIVE=1` to state that the target is disposable.

If the store refuses to clear the default graph, `clear_dataset(db)` raises. So the seed stops
there and does not write over the old data. The error is a `TripleStoreResponseError` from
`kapps_triplestore_interface.exceptions`, or a subclass of it. One cause is a loaded SHACL shape
that makes the store fail on deletes.

## Getting the Domain Classes into the Ontology

The middleware never creates ontology classes. Every Capability, Workflow, StateProperty and Service
class **must pre-exist** (see `02-workflow-registration.md`). You author the domain TBox as Turtle
and load it with the store's `graph_add`, as `examples/seed.py` does:

```python
db.graph_add(
    turtle_text,                      # the domain TBox, classes only
    content_type="application/x-turtle",
)
```

The Turtle declares classes, not individuals. Keeping the TBox stable across many identical resource
deployments is why the class is authored once here rather than minted per instance.

The **interface ontology** must be among the classes the store holds before the serving path runs:
the northbound projection reads it at every startup to decide which connection properties to prune
(see `06-views-and-projection.md`). If it is absent when a payload is projected, the projection
raises `ProjectionError`, naming the property it could not resolve — a loud failure, not a silent
one. There is no tool that fetches or seeds it for you today; load it yourself as shown above.

## Seeding the Instance Data a Parameter Needs

A Parameter is recognized only if its individual already carries its connection metadata in the
graph — for an MQTT parameter, properties such as `inf:hasMQTTTopic` and `inf:hasMQTTBrokerIP`.
Nothing promotes a Parameter without them. You write that metadata as instance data through the OGM
before the middleware starts. The pattern, from `examples/seed.py`:

```python
from kapps_ogm.utils.class_scope import ClassScope

ogm.create(
    class_iri=CONVEYOR_BELT_CLASS,
    instance_iri=CONVEYOR_BELT_LEFT,
    class_scope=ClassScope.from_property_chains([[TU_HAS_CONVEYOR_SPEED]]),
    data={
        TU_HAS_CONVEYOR_SPEED: [
            {
                TU_HAS_UNIT: ["m/s"],
                INF_ACCESS_MODE: ["readwrite"],
                INF_HAS_MQTT_TOPIC: ["TransferUnit1/ConveyorBelt/left/speed"],
                INF_HAS_MQTT_SET_TOPIC: ["TransferUnit1/ConveyorBelt/left/speed_set"],
                INF_HAS_MQTT_BROKER_IP: ["127.0.0.1"],
            }
        ]
    },
)
```

The `data` keys are property IRIs; each value is a list of dicts, matching RDF multiplicity. The
`ClassScope` names the property chain that gets written. The middleware reads this metadata at
startup and wires the connector automatically — no `@mw.state` decorator is needed where the
metadata is already in the graph.

See `examples/seed.py` and `demo/transferunits/seed.py` for complete, worked scenario seeds. Each is
a self-contained specification of its own prerequisites and never depends on production state.
