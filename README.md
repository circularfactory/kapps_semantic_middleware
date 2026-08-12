# kapps_semantic_middleware

Let a piece of Python running on or beside a shopfloor resource expose what it can do through an
RDF knowledge graph — and let other instances discover and invoke that capability **through the
graph**, rather than through hardcoded network addresses.

A resource's state is described in the graph as a protocol-interface **parameter**. The middleware
wires that description to the real device: it discovers peers, serves them over REST, and keeps the
graph and the device in step.

```bash
pip install kapps-semantic-middleware
```

Python 3.12 or newer. Nothing else is required to use the library.

## Run the examples

The scenarios and the factory demo need a few more packages and a running GraphDB. One extra
covers the packages:

```bash
pip install "kapps-semantic-middleware[examples]"
```

Copy the runnable files out of the installed package into a directory you own — a notebook inside
`site-packages` cannot be opened, edited or re-run:

```bash
kapps-examples ./kapps-examples
cd kapps-examples
```

You now have scenario 1 and scenario 2 (each as a `.py` and a `.ipynb`), the seed data they load,
and a `docker/` directory. Add the `[notebooks]` extra if you want to open the `.ipynb` files in
Jupyter; the `.py` versions run with `[examples]` alone.

### Start a GraphDB

Docker is needed for the examples and the demo, **never for the library itself**.

```bash
cd docker
docker compose up -d
```

GraphDB comes up on <http://localhost:7200> and the `kapps-demo` repository is created for you.
This works the same on Linux, macOS and Windows.

Then point the library at it:

```bash
export GRAPHDB_URL=http://localhost:7200
export GRAPHDB_USERNAME=admin
export GRAPHDB_PASSWORD=root
```

Three variables, not four. `GraphDBCredentials.from_env()` in `kapps_triplestore_interface` also
reads `GRAPHDB_REPOSITORY`, and your own code may well use it — but nothing here does. The examples
and the demo name `kapps-demo` in code, and the test suite names its own. **A `GRAPHDB_REPOSITORY`
you already have set is ignored rather than obeyed**, because these are the parts that wipe and
re-seed whatever they connect to.

> **Never point `GRAPHDB_URL` at a GraphDB you care about.** The examples and the demo clear the
> repository they use on every run. `docker compose down -v` wipes the throwaway one; the next run
> re-seeds it.

### Run a scenario

```bash
python scenario1_hello_world.py     # operation coordination through the graph
python scenario2_door.py            # direct state discovery and control
```

### Run the factory demo

A small factory as six real processes — one per mock PLC, one per middleware instance, and a
controller that discovers every unit *in the graph* and drives it over REST. You watch it from a
browser.

```bash
kapps-transferunit-factory
```

## Where to read what

| | |
|---|---|
| [`AGENTS.md`](AGENTS.md) | **Start here to build against this.** The consumption rules — including the ones that fail *silently* — and which name to import from where. |
| [`docs/mechanics/`](docs/mechanics/) | How each mechanism actually works, one page each, in construction order. |
| [`CONTEXT-MAP.md`](CONTEXT-MAP.md) | The five contexts and how they relate. |
| `examples/` | Scenario 1 and scenario 2, self-contained. |
| `demo/transferunits/` | The factory demo. |

If you are writing code against this library, read `AGENTS.md` first. Several of its rules are
constraints that **do not appear in any type signature** and fail by producing quietly wrong
behaviour rather than an exception.

## Status

`0.1.0` is the first public release. It is usable — the scenarios and the factory demo run end to
end — but the API may still move. See [`CHANGELOG.md`](CHANGELOG.md).

## Contributing

Development happens in a private repository and this one is the published half: it carries one
commit per release and no ancestry, so there is nothing here to branch from.

Bug reports and questions are welcome as issues on this repository. A patch is welcome too --
say what it changes and why, and it will be applied on the development side and credited in
`CHANGELOG.md` for the release that carries it.

## Acknowledgements

This package is developed as part of the INF subproject of the CRC 1574: Circular Factory for the
Perpetual Product. This work is therefore supported by the Deutsche Forschungsgemeinschaft (DFG,
German Research Foundation) [grant-number: SFB-1574-471687386].
