# API reference

Generated from the docstrings in `src/` by `sphinx.ext.autodoc`. 117 of the
package's 127 public classes and functions carry one.

:::{note}
The reference documents **this project's modules**. The top-level
`kapps_semantic_middleware` namespace holds two names, `SemanticMiddleware` and
`Mode`, and both are documented under Core. It re-exports nothing from
`transitional_sync_middleware`: presenting another project's API as ours is the
conflation the fork's naming decision was made to avoid.
:::

```{toctree}
:caption: Core
:maxdepth: 1

middleware
modes
activity
```

```{toctree}
:caption: The knowledge graph
:maxdepth: 1

registration
seeding
projection
vocabulary
```

```{toctree}
:caption: Connectors
:maxdepth: 1

connectors/semantic
connectors/wiring
connectors/mqtt_binding
connectors/rest_binding
connectors/knowledge_graph_connector
```

```{toctree}
:caption: Interop
:maxdepth: 1

rest_router
shacl_interop/shape_from_typehints
```
