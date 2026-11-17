# Provisioning and Seeding

Every other page in this set takes `ogm=ogm` as given. This page shows how to construct that
`OGM`, the store under it, and the ontology and instance data the middleware needs before its
first registration. It is the bootstrap order the other pages assume you are already past.

This page is the bootstrap of a demonstration or test store. A productive circular factory already
holds these ontologies and shapes in its knowledge graph; the files shipped with the middleware seed the
demonstrations and tests, and document the vocabulary. A service in a productive factory starts at
constructing the middleware.

:::{dropdown} Terms this page assumes
{term}`Parameter` · {term}`ClassScope` · {term}`Capability` · {term}`Workflow` · {term}`Service` ·
{term}`Resource` · {term}`Projection`
:::

## Bootstrap Order

The order is fixed: construct the store, wrap it in an OGM, load the shared ontologies,
author and seed the domain data, then construct the middleware. In a productive store the
ontologies and the domain data are already there, so the order starts at the last step.

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
    resource_iri=IRI("http://example.org/kapps-demo#my_resource"),
    service_class=IRI("http://example.org/kapps-demo#MyService"),
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
Registration fails if a referenced class is missing, so the ontologies must be in the store first: a
demonstration or test loads them, and a productive store already holds them.

## Loading the Shared Ontologies

Every scenario needs the published Core, service, and MES modules in the store. A demonstration or
test loads them with `load_shared_ontologies(db, *, reload=False)`; a productive store already holds
them and never calls it. The function loads all three; each lands in its own named graph and is
skipped if already present unless you pass `reload=True`.

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

## After a KSM Upgrade

After a KSM upgrade, reload the shared graphs once with `load_shared_ontologies(db, reload=True)`.
On a shared store, set `TRIPLESTORE_ALLOW_DESTRUCTIVE=1` for that one call. An older shared graph is
skipped, not replaced, and `clear_dataset(db)` leaves it standing. So until the reload the store
keeps the old terms and refuses the new ones.

(interface-terms-a-deployment-must-hold)=
## The Interface Terms a Deployment Must Hold

The middleware reads and writes nine terms of the interface vocabulary, the `inf:` prefix, whose
namespace IRI in full is `http://www.sfb1574.kit.edu/ontologies/CrcInterfaces#`. They belong to the
deployed ontology of the factory. The middleware neither ships nor deploys them at runtime: the
ontology that the deployment receives through its ontology pipeline must define each of them.

| Term | Kind | Expected range | Direction | What the middleware does with it |
|---|---|---|---|---|
| `inf:isInterfaceAccessibleParameter` | object property | a class that restricts `inf:accessMode` | northbound | recognizes a parameter: a domain property that is `rdfs:subPropertyOf` it is bound by the REST connector |
| `inf:isInterfaceAccessibleMQTTParameter` | object property | a class that restricts the five MQTT terms; a subproperty of the generic marker | southbound | recognizes a parameter that is wired to MQTT; a provision declares it, and the parameter property then resolves as if it were a subproperty of it |
| `inf:hasValue` | datatype property | none globally; each parameter's restriction gives its datatype | northbound | holds the live value: the MQTT connector fills it from the device's readings and reads it to send a write |
| `inf:accessMode` | datatype property | `xsd:string`, `"read"` or `"readwrite"` | northbound | decides whether a peer may write, and is written during provisioning; absent or unrecognized means read-only |
| `inf:hasMQTTTopic` | datatype property | `xsd:string` | southbound | reads the topic that the device publishes readings on; written during provisioning |
| `inf:hasMQTTSetTopic` | datatype property | `xsd:string` | southbound | reads the topic that a setpoint is published to; readwrite parameters only; written during provisioning |
| `inf:hasMQTTBrokerIP` | datatype property | `xsd:string` | southbound | reads the address of the broker that carries the topics; written during provisioning |
| `inf:hasMQTTBrokerPort` | datatype property | `xsd:integer` | southbound | reads the broker port; absent means 1883; written during provisioning |
| `inf:hasMQTTValuePath` | datatype property | `xsd:string` | southbound | reads the dotted path to the value in a JSON envelope; absent means a raw scalar; written during provisioning |

Northbound terms may appear in what the middleware serves. Southbound terms are connection
metadata and are never served: a peer that learned them could reach the device without the
middleware. The kinds and ranges follow the code. The ranges are the datatypes that the restrictions of the two markers give; no term needs a global range.

If the deployed ontology lacks one of these terms, the fix is in the ontology, not around it:

- A missing marker means the parameter is not recognized, so no connector is wired to it.
- A missing metadata term means the OGM refuses the write when provisioning writes the term, and a
  connector that needs it cannot read its connection.

The vocabulary class that holds these terms is documented on the
[vocabulary reference page](../reference/vocabulary.md).

## Getting the Domain Classes into the Ontology

The middleware never creates ontology classes. Every {term}`Capability`, {term}`Workflow`,
StateProperty and {term}`Service` class **must pre-exist** (see {doc}`02-workflow-registration`).
You author the domain TBox as Turtle and load it with the store's `graph_add`, as
`examples/seed.py` does:

```python
db.graph_add(
    turtle_text,                      # the domain TBox, classes only
    content_type="application/x-turtle",
)
```

A demonstration or test authors and loads this TBox; a productive store already holds its domain
classes. The Turtle declares classes, not individuals. Keeping the TBox stable across many
identical resource deployments is why the class is authored once here rather than minted per instance.

The **interface ontology** must be among the classes the store holds before the serving path runs:
the northbound {term}`Projection` reads it at every startup to decide which connection properties to
prune (see {doc}`06-views-and-projection`). If it is absent when a payload is projected, the
projection raises `ProjectionError`, naming the property it could not resolve — a loud failure, not a
silent one. The ontology must be in the store. No tool fetches or seeds it today: a demonstration or
test loads it as shown above, and a productive store already holds it.

## Seeding the Instance Data a Parameter Needs

A {term}`Parameter` is recognized only if its individual carries its connection metadata in the graph —
for an MQTT parameter, properties such as `inf:hasMQTTTopic` and `inf:hasMQTTBrokerIP`. Nothing
promotes a Parameter without them. There are two ways to get the metadata there: let the middleware
provision it at its first start, or write it yourself before the middleware starts.

### Let the Middleware Provision It

Declare each parameter's interface and metadata in the embedding code, and pass the declarations to
the constructor. At construction the middleware writes what the stored parameter nodes lack, through
one OGM commit, and then recognizes and wires the parameters from the graph. From `examples/seed.py`:

```python
from kapps_semantic_middleware.connectors.provisioning import Provision

provisions = [
    Provision(
        holder_iri=CONVEYOR_BELT_LEFT,
        property_iri=TU_HAS_CONVEYOR_SPEED,
        interface_iri=INF_IS_INTERFACE_ACCESSIBLE_MQTT_PARAMETER,
        metadata={
            INF_ACCESS_MODE: ["readwrite"],
            INF_HAS_MQTT_TOPIC: ["TransferUnit1/ConveyorBelt/left/speed"],
            INF_HAS_MQTT_SET_TOPIC: ["TransferUnit1/ConveyorBelt/left/speed_set"],
            INF_HAS_MQTT_BROKER_IP: ["127.0.0.1"],
        },
    ),
    # ... one per parameter
]

mw = SemanticMiddleware(..., class_scope=unit_scope, provisions=provisions)
```

- **A restart writes nothing.** A node that already carries the declared values is left as it is,
  and a write updates the stored node, never a second one beside it.
- **The declaration gives the interface.** The parameter property resolves as if it were
  `rdfs:subPropertyOf` the declared interface, also where the ontology does not say so.
- **A contradiction is loud.** A stored value of a declared property that differs from the
  declared one fails construction with a `DeploymentValueError`. Mark the declaration
  `exclusive=True` to replace the stored values instead.
- **Another interface merges.** The metadata of an interface the declaration does not select is
  no contradiction: the node may be reached over two protocols, and a plain declaration leaves
  that metadata as it is. An exclusive declaration removes it in the same commit. A property
  that no interface restricts, such as an `rdfs:comment`, always stays.
- **A monitor writes nothing.** With `autoregister_connectors=False` the middleware provisions
  nothing, and still hides the declared interface's metadata northbound.
- **It needs a `class_scope` that reaches each declared parameter.** Provisions without one fail
  construction, and so does a declared parameter that the scope does not reach from the
  resource: it would be written but never wired.
- **Each value is a list, and each parameter is declared once.** A bare string such as
  `"readwrite"`, an empty list, or a second declaration of one parameter of a holder fails
  construction. So does a `kapps-ogm` before 0.4.0, which has no `Interface`.

`seed_scenario3(db, ogm, metadata=False)` seeds a TransferUnit without any parameter node, and
`scenario3_provisions()` is its declaration.

### Write It Yourself

Write the metadata as instance data through the OGM before the middleware starts. The pattern, from
`examples/seed.py`:

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
{term}`ClassScope` names the property chain that gets written. The middleware reads this metadata at
startup and wires the connector automatically — no `@mw.state` decorator is needed where the
metadata is already in the graph.

See `examples/seed.py` and `demo/transferunits/seed.py` for complete, worked scenario seeds. Each is
a self-contained specification of its own prerequisites and never depends on production state.
