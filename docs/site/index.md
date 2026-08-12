# kapps-semantic-middleware

Semantic middleware for industrial data integration. It registers equipment and its capabilities in a knowledge graph, discovers what a machine can do by querying that graph, and executes operations over MQTT and HTTP.

## Hello

This middleware sits between shopfloor resources and higher-level applications. Instead of hardcoding network addresses or data formats, it uses a knowledge graph to describe what each resource is, what it can do, and how to reach it. Peers discover and invoke functionality through the graph rather than through direct configuration.

## What is KAPPS

KAPPS lets you register equipment and its capabilities in a knowledge graph, discover what a machine can do by querying that graph, and execute operations over MQTT and HTTP. Registration, discovery, and execution are three views of the same triples. A resource publishes what it offers; a consumer queries the graph to find it; coordination happens through graph-state, not message queues.

## The components

Several packages work together:

- **kapps-ogm** — handles all knowledge graph reads and writes through a validated object-graph mapping layer.
- **kapps-triplestore-interface** — provides raw triple-store access where the OGM has no equivalent yet, such as instance discovery.
- **transitional-sync-middleware** — the synchronization layer, a transitional fork of `aas-middleware` carrying four synchronization fixes, incrementally reimplemented locally. It is retired within a few releases; do not build on it directly.
- **GraphDB** — the triple store, brought up with `docker compose` from the `docker/` directory that `kapps-examples` copies out. Reasoning is required.
- **Per-unit in-process broker** — each middleware unit runs its own MQTT broker for southbound device communication.

## Install the stack

Install the library:

```bash
pip install kapps-semantic-middleware
```

To run the example scenarios and factory demos:

```bash
pip install "kapps-semantic-middleware[examples]"
```

To also open the notebooks in Jupyter:

```bash
pip install "kapps-semantic-middleware[examples,notebooks]"
```

Copy the example notebooks into your working directory. This is also how you get the compose
files that start GraphDB, so it comes first:

```bash
kapps-examples ./kapps-examples
cd kapps-examples
```

Now start GraphDB (Docker is required for the examples, not for using the library itself):

```bash
cd docker
docker compose up -d
```

:::{important}
GraphDB must be reachable before the first registration. Set the `GRAPHDB_*` environment variables to tell the middleware where it is.
:::

## Bootstrap

The middleware depends on a specific order of operations. Follow this sequence to reach a running middleware:

```python
from kapps_triplestore_interface import GraphDB
from kapps_ogm import OGM
from kapps_semantic_middleware import SemanticMiddleware, Mode
from kapps_semantic_middleware.seeding import load_shared_ontologies

# 1. Construct the GraphDB connection
db = GraphDB.from_env()

# 2. Construct the OGM wrapper
ogm = OGM(db=db)

# 3. Load the shared ontologies (Core + Interfaces)
load_shared_ontologies(db)

# 4. Seed the individuals your scenario assumes
# (e.g. create resources, parameters, capabilities via ogm.create())

# 5. Construct and start the middleware
middleware = SemanticMiddleware(
    mode=Mode.RESOURCE,
    resource_iri=YOUR_RESOURCE_IRI,
    service_class=YOUR_SERVICE_CLASS,
    ogm=ogm,
    host="127.0.0.1",
    port=8993,
)
```

A reader who follows this sequence reaches a running middleware without leaving the page.

## Where next

::::{grid} 1 1 2 2
:gutter: 3

:::{grid-item-card} {octicon}`book;1.5em;sd-mr-1` Concepts
:link: concepts
:link-type: doc

Why a knowledge graph sits in the middle, what the three modes mean, and why the reasoner is not optional.
:::

:::{grid-item-card} {octicon}`book;1.5em;sd-mr-1` Guide
:link: guide/index
:link-type: doc

Nine pages in construction order: instantiation, registration, parameters, connectors, coordination, views, writes, and the bootstrap that precedes them.
:::

:::{grid-item-card} {octicon}`beaker;1.5em;sd-mr-1` Scenarios
:link: scenarios/index
:link-type: doc

Three worked examples in increasing size: Hello World, the door and robot, and the six-process TransferUnit factory.
:::

:::{grid-item-card} {octicon}`code;1.5em;sd-mr-1` API reference
:link: reference/index
:link-type: doc

Fourteen modules, generated from the docstrings in `src/`.
:::

::::

```{toctree}
:hidden:
:maxdepth: 2

concepts
guide/index
scenarios/index
reference/index
```
