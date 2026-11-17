# kapps_semantic_middleware

Let a piece of Python running on or beside a shopfloor resource expose what it can do through an
RDF knowledge graph — and let other instances discover and invoke that capability **through the
graph**, rather than through hardcoded network addresses.

A resource's state is described in the graph as a protocol-interface **parameter**. The middleware
wires that description to the real device: it discovers peers, serves them over REST, and keeps the
graph and the device in step.

Python 3.12 or newer. Nothing else is required to use the library. Install it into a virtual
environment, and call the environment's own interpreter, so that no step needs an activation
script and nothing lands in your global Python:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install kapps-semantic-middleware
```

On Windows, in PowerShell or `cmd`:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install kapps-semantic-middleware
```

> **Windows: "running scripts is disabled on this system".** That message comes from
> `Activate.ps1`. The steps here never call it: each command names the environment's interpreter
> (`.venv\Scripts\python.exe`), so they work without activation, and the install stays inside
> `.venv`. Without the environment, a bare `pip` is the global one.

## When to fetch and when to query

**Guideline for reads (should, not must).** A participant should fetch a data object with
`mw.fetch()` when it works with that object through the middleware: when it changes it, appends
to it, connects connectors to it, or commits it. A participant should ask the graph a question
(a list, a filter, a check, a label) with a query through the access module (`ogm.db`), or with
the triple store interface's functions that wrap one. The usual flow is a query for the
candidates, then a fetch of the one it picks. Where this does not fit a case, a direct read is
allowed: reads are not constrained. Writes are: every write goes through the OGM, in
`mw.create()` or `mw.commit()`.

For example, a participant that needs an idle robot queries for the robots whose state is idle,
picks one IRI, fetches that robot with `mw.fetch()`, and changes it in `mw.commit()`.

## Run the examples

The scenarios and the factory demo need a few more packages and a running triple store. One extra
covers the packages:

```bash
.venv/bin/python -m pip install "kapps-semantic-middleware[examples]"
```

On Windows:

```powershell
.venv\Scripts\python.exe -m pip install "kapps-semantic-middleware[examples]"
```

Run `kapps-examples` to copy the runnable files out of the installed package into a directory you
own — a notebook inside `site-packages` cannot be opened, edited or re-run. The path is relative to
your current working directory:

```bash
.venv/bin/kapps-examples ./kapps-examples
cd kapps-examples
```

On Windows:

```powershell
.venv\Scripts\kapps-examples.exe .\kapps-examples
cd kapps-examples
```

You now have scenario 1 and scenario 2 (each as a `.py` and a `.ipynb`), the seed data they load,
and a `docker/` directory. Add the `[notebooks]` extra if you want to open the `.ipynb` files in
Jupyter; the `.py` versions run with `[examples]` alone.

A productive circular factory already holds the shared ontologies and shapes in its knowledge graph;
the files shipped with the middleware seed the demonstrations and tests, and document the vocabulary. The examples
seed them into the throwaway store below.

<!-- vendor-check: off -- the local Docker store -->
### Start a GraphDB

Docker is needed for the examples and the demo, **never for the library itself**.

```bash
cd docker
docker compose up -d
cd ..
```

(From the `kapps-examples` directory. The commands below also run from there, with the environment
at `..\.venv`, one level up.)

GraphDB comes up on <http://localhost:7200> and the `kapps-demo` repository is created for you.
This works the same on Linux, macOS and Windows.
<!-- vendor-check: on -->

Then point the library at it:

Linux and macOS:
```bash
export TRIPLESTORE_URL=http://localhost:7200
export TRIPLESTORE_USERNAME=admin
export TRIPLESTORE_PASSWORD=root
```

PowerShell (current session only):
```powershell
$env:TRIPLESTORE_URL = "http://localhost:7200"
$env:TRIPLESTORE_USERNAME = "admin"
$env:TRIPLESTORE_PASSWORD = "root"
```

cmd.exe (current session only):
```batch
set TRIPLESTORE_URL=http://localhost:7200
set TRIPLESTORE_USERNAME=admin
set TRIPLESTORE_PASSWORD=root
```

To persist across sessions on Windows, use `setx` instead of `set`. **`setx` does not affect the
shell that runs it** — open a new shell after running it.

Three variables, not four. `TripleStoreEndpoint.from_env()` in `kapps_triplestore_interface` also
reads `TRIPLESTORE_DATASET`, and your own code may well use it — but nothing here does. The examples
and the demo name the dataset `kapps-demo` in code, and the test suite names its own. **A
`TRIPLESTORE_DATASET` you already have set is ignored rather than obeyed**, because these are the
parts that wipe and re-seed whatever they connect to.

A deployment can also name the reasoning ruleset that it requires, in one more, optional variable,
`TRIPLESTORE_REQUIRED_RULESET` (for KAPPS: `rdfsplus-optimized`). When it is set, a resource-mode
instance asks the store for the dataset's ruleset as it starts. If the store names another ruleset,
the start refuses with one line that names the dataset, its ruleset and the required one. If the
store does not say, the start goes on and logs that the ruleset is unknown. Unset, the ruleset is
not checked.

> **Never point `TRIPLESTORE_URL` at a store you care about.** The examples and the demo clear the
> dataset they use on every run. `docker compose down -v` wipes the throwaway one; the next run
> re-seeds it.

### Run a scenario

From inside `kapps-examples` (the `Next steps` that `kapps-examples` prints name the interpreter
that ran it, whatever its path):

```bash
../.venv/bin/python scenario1_hello_world.py       # operation coordination through the graph
../.venv/bin/python scenario2_door.py              # direct state discovery and control
```

```powershell
..\.venv\Scripts\python.exe scenario1_hello_world.py
..\.venv\Scripts\python.exe scenario2_door.py
```

### Run the factory demo

A small factory as six real processes — one per mock PLC, one per middleware instance, and a
controller that discovers every unit *in the graph* and drives it over REST. You watch it from a
browser.

```bash
../.venv/bin/kapps-transferunit-factory
```

On Windows:

```powershell
..\.venv\Scripts\kapps-transferunit-factory.exe
```

It serves a live status page on <http://127.0.0.1:8080/>. `--units N` sets how many TransferUnits
run (two by default); `--force` clears the graph even when a live factory still holds a fresh
heartbeat. Ctrl+C stops every process.

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

## Questions

**Can I install it with uv?** Yes. Run this *instead of* the install at the top, not after
it:

```bash
uv add "kapps-semantic-middleware[examples]"
```

**`pip install` answers `externally-managed-environment`.** Debian and Ubuntu mark their system
Python as externally managed, so `pip` refuses to write into it. Use a virtual environment and
call its interpreter directly, with no activation:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install "kapps-semantic-middleware[examples]"
```

On Windows: `.venv\Scripts\python.exe -m pip install "kapps-semantic-middleware[examples]"`.

`uv` needs no such step — it creates and manages the environment itself.

**Is `[examples]` really needed for the factory?** Yes, and leaving it out does not look like an
error. `uvicorn` and `rdflib` reach you anyway, as dependencies of something else; `amqtt` — the
in-process MQTT broker each unit brings up — does not. Without it the launcher seeds the graph, the
first middleware starts and prints its address, and then the mock PLC repeats a line like

```
broker at 127.0.0.1:18831 not ready yet ([Errno 111] Connection refused), retrying in 5.0s
```

for as long as you let it run. No traceback appears, and neither does `Factory running`. If you see
that line, the extra is what is missing.

## Status

Usable, and still on a `0.x` line: the scenarios and the factory demo run end to end, but the API
may still move, and a minor bump may break you. [`CHANGELOG.md`](CHANGELOG.md) records what each
release changed.

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

This library imports [`transitional-sync-middleware`](https://pypi.org/project/transitional-sync-middleware/), a fork of Sebastian Behrendt's aas-middleware ([Behrendt et al. 2025](https://doi.org/10.1016/j.ifacol.2025.12.315)) with four synchronization fixes.
