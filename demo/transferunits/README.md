# The TransferUnit Factory demo

This demo builds a very small factory on your computer and lets you drive it from a web browser.

Nothing here is real hardware. Every machine is a Python program that pretends to be one. But the
parts talk to each other the same way real ones would, so what you see is the real behaviour.

## What you will see

The demo starts **6 programs** at once. Each one is a separate process, like a separate machine on
a factory floor.

| program | what it pretends to be |
| --- | --- |
| **PLC 1** and **PLC 2** | Two conveyor machines. Each has 2 belts and 2 light barriers. Each has its own small web page. |
| **Middleware 1** and **Middleware 2** | One "translator" per machine. It reads the machine over MQTT and offers it to the network over HTTP. |
| **Control station** | An operator screen. It finds the machines by asking a database, then drives them. |
| **Launcher** | Starts all of the above and shows you a picture of them. |

A **TransferUnit** is one conveyor machine: 2 belts and 2 light barriers.

**The point of the demo:** the control station is never told where the machines are. It asks a
knowledge graph (a database of facts), finds whatever is there, and drives it. You can stop a
machine and it disappears from the screen. Nothing is hardcoded.

---

## Before you start

You need three things.

### 1. Python and `uv`

`uv` installs the project and its libraries. If you do not have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. The library and its dependencies

Nothing needs to sit beside this project any more. The three libraries the middleware builds on
— `kapps-ogm`, `kapps-triplestore-interface` and `transitional-sync-middleware` — are published,
so installing this package brings them with it:

```bash
pip install "kapps-semantic-middleware[examples]"
```

The `[examples]` extra is what the demo needs: without it the factory exits on a missing
`uvicorn` or `amqtt`.

> This step used to be the most common reason the demo did not start. It asked you to clone
> three sibling repositories into the same parent folder and put each on a particular branch,
> because they were wired in as editable path checkouts. They are ordinary dependencies now.

### 3. A GraphDB database

GraphDB stores the knowledge graph. The demo reads and writes it. Put these three values in your
environment (for example in a `.env` file, or with `export`):

```bash
export GRAPHDB_URL=https://your-graphdb-server
export GRAPHDB_USERNAME=your-username
export GRAPHDB_PASSWORD=your-password
```

There is no fourth value. The demo always uses the repository named **`kapps-demo`**, which is the
one `docker compose` creates for you, and it names that in code rather than reading it from your
environment. A `GRAPHDB_REPOSITORY` you happen to have set is ignored (issue #146).

> **Warning.** The demo **writes into** `kapps-demo`, and `--force` **deletes and rewrites** the
> demo's data in it. Point `GRAPHDB_URL` at a GraphDB you are allowed to overwrite — the repository
> is pinned, but the server is not, and a `kapps-demo` on a shared server is still somebody's.

You do **not** need to install an MQTT broker. Each machine starts its own, inside its own process.

---

## Start it

From the `kapps_semantic_middleware` folder:

```bash
uv sync                                   # install everything (only needed once)
uv run python -m demo.transferunits --units 2
```

Then open **<http://127.0.0.1:8080/>** in your browser.

### If you installed the package instead of cloning it

The demo also ships inside the installed library, under a longer name. You need the
`examples` extra, because a plain install brings the library alone:

```bash
uv add "kapps-semantic-middleware[examples]"
kapps-transferunit-factory --units 2
```

That command and the `python -m demo.transferunits` above start the same factory. The two
names differ because a package called `demo` on PyPI would collide with half the index.

That address is fixed and always the same. Every other program picks a free port at random, so the
launcher page is your way in — it lists every program and links to it.

Two options:

- `--units 2` — how many machines to build. Try `--units 3`.
- `--force` — wipe the demo's old data and start fresh. Use it if a previous run did not shut down
  cleanly.

## Stop it

Press **Ctrl+C** in the terminal. Or press **stop the factory** on the launcher page.

Both shut the programs down in the right order, so each machine removes itself from the knowledge
graph on the way out. If you just close the terminal window instead, the graph keeps stale entries
and the next run may complain — that is what `--force` clears.

---

## What to try, in order

### 1. Look at the launcher page — <http://127.0.0.1:8080/>

A picture of all 6 programs and how they connect. Hover over any box to read what it is and which
source file it comes from.

### 2. Open a machine's own page

Click a **PLC** box. This is the machine's own control panel, the kind of screen that sits on the
machine itself. Set a belt speed and watch it move.

**Belts have momentum.** A belt does not jump to a new speed. It ramps up at 1 m/s per second, so
asking for 3 m/s takes about 3 seconds. This is on purpose — a real belt has mass.

### 3. Open the control station — the station board

Click the **control station** box. This screen never talked to the machines directly. It ran a
database query, found them, and connected.

Try this:

1. Press **pause**. The station board runs an automatic program on a timer; you must pause it before
   you can drive a machine by hand.
2. Expand a machine's card and type a new belt speed, then press **set**.
3. Watch the value. It says `sending`, then climbs, then reaches your number and says `settled`.
4. Turn on **show IRIs**. Every row now also shows its full name in the knowledge graph, the exact
   Python line that sets it, and which private details were hidden from the network.

### 4. Prove the discovery is real

With the demo still running, press **stop** on one unit in the launcher page. Go back to the station
board. Within a few seconds that machine is gone from the screen. Nobody edited any configuration —
the machine removed itself from the graph, and the station board simply stopped finding it.

---

## When something goes wrong

| what you see | what it usually means |
| --- | --- |
| `ModuleNotFoundError`, or strange errors on start | Something the demo needs is not installed. Re-run `pip install "kapps-semantic-middleware[examples]"` — the `[examples]` extra is what brings `uvicorn` and `amqtt`. |
| An error mentioning `GRAPHDB_` | The three environment variables are not set, or the database is unreachable. |
| "a live factory is already running" | A previous run did not shut down. Start again with `--force`. |
| A box on the launcher page turns red | That program crashed. **Click the red box** — it opens and shows that program's last output. |
| The page loads but no values change | Give it a few seconds. Machines publish their state on a timer. |

Every program's output also appears in the terminal you started from, with a name in front of each
line (`plc-1`, `middleware-2`, `control`), so you can see which program said what.

---

## Running it on a remote server over SSH

Only port **8080** is fixed. Every other port is chosen at random when the program starts, so your
editor cannot know them in advance.

- Forward port `8080`.
- In VS Code or Cursor, set `remote.autoForwardPortsSource` to `output` or `hybrid`. Each program
  prints its address when it starts, and with that setting the editor reads those lines and forwards
  each port automatically.
- Open the launcher page through the forwarded port. Its links are built from the address you are
  browsing with, so they follow the same tunnel.

**Known limitation.** If one of those random ports is already busy on your own machine, your editor
forwards it to a *different* local port, but the launcher's link still names the remote one. That
one link will be wrong. Forward that port by hand, or free the port and restart.

---

## For developers: what each file does

The demo is deliberately split so that each file has one job. Guard tests fail if the split is
broken.

| file | job |
| --- | --- |
| `__main__.py` | The entry point. Builds the factory and serves the launcher page. |
| `launcher.py` | Starts, tracks and stops the child programs. **No web routes here.** |
| `index.py` | The launcher page's web routes. **No process handling here.** |
| `templates/index.html` | The topology picture and its polling code. |
| `middleware.py` | Runs one middleware per machine, and starts that machine's MQTT broker. |
| `control_station.py` | Runs the control station process and its timed algorithm. |
| `station_board.py` | The station board's web routes and template. |
| `controller.py` | Finding machines by query, and driving them over HTTP. |
| `algorithm.py` | The example automatic program. It reads a barrier on one machine and sets a belt speed on another — only to show that it can reach them. It is not a real controller. |
| `seed.py` | Writes the starting facts for N machines into the knowledge graph. |
| `plc/` | The pretend machine and its own panel page. |
| `factory.ttl`, `transferunit.ttl` | The vocabulary: what a TransferUnit is made of. |

**The full loop this demo proves:**

```
PLC → MQTT → middleware → knowledge graph → control station → HTTP → back to the PLC
```

`CONTEXT.md` in this folder explains the words used here (Factory, Unit index, Launcher, Runner,
Control station, Panel, Live factory) and points at the decision records behind each choice.
