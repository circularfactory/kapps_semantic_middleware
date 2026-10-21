# The TransferUnit Factory demo

This demo builds a very small factory on your computer and lets you watch it from a web browser.

**The demo observes.** In KAPPS a machine changes only through a workflow of its service. This
demo has no machine workflows yet, so the control station reads the machines and changes none.
Its control returns with workflows after the paper. You can still move a belt at the machine
itself, on its own panel page.

Nothing here is real hardware. Every machine is a Python program that pretends to be one. But the
parts talk to each other the same way real ones would, so what you see is the real behaviour.

## What you will see

The demo starts **7 programs** at once (6 with `--no-watchdog`). Each one is a separate process, like a separate machine on
a factory floor.

| program | what it pretends to be |
| --- | --- |
| **PLC 1** and **PLC 2** | Two conveyor machines. Each has 2 belts and 2 light barriers. Each has its own small web page. |
| **Middleware 1** and **Middleware 2** | One "translator" per machine. It reads the machine over MQTT and offers it to the network over HTTP. |
| **Control station** | An operator screen. It finds the machines by asking a database, then shows what they report. |
| **Watchdog** | A middleware in watchdog mode. It sweeps the knowledge graph every 5 seconds and removes the address of any program that stopped reporting. It registers nothing itself, so the launcher page shows its state and no address. |
| **Launcher** | Starts all of the above and shows you a picture of them. |

A **TransferUnit** is one conveyor machine: 2 belts and 2 light barriers.

**The point of the demo:** the control station is never told where the machines are. It asks a
knowledge graph (a database of facts), finds whatever is there, and reads it. You can stop a
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

### 3. A triple store

The triple store holds the knowledge graph. The demo reads and writes it. Put these three values in your
environment (for example in a `.env` file, or with `export`):

```bash
export TRIPLESTORE_URL=https://your-triplestore-server
export TRIPLESTORE_USERNAME=your-username
export TRIPLESTORE_PASSWORD=your-password
```

There is no fourth value. The demo uses the dataset named **`kapps-demo`**, which is the one
`docker compose` creates for you, unless `--dataset NAME` names another (for a test, or for a
second factory on the same server). The environment still cannot name it: a `TRIPLESTORE_DATASET`
you happen to have set is ignored.

> **Warning.** The demo **writes into** the dataset the demo uses, and `--force` **deletes and
> rewrites** the demo's data in it. Point `TRIPLESTORE_URL` at a store you are allowed to
> overwrite — the dataset is named on the command line, not by the environment, and a
> `kapps-demo` on a shared server is still somebody's.

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

Options:

- `--units 2` — how many machines to build. Try `--units 3`.
- `--force` — wipe the demo's old data and start fresh. Use it if a previous run did not shut down
  cleanly.
- `--no-watchdog` — do not start the watchdog (6 programs instead of 7).
- `--dataset NAME` — the dataset the demo seeds and every program that reads the graph joins
  (default: `kapps-demo`). The environment cannot name it.

## Stop it

Press **Ctrl+C** in the terminal. Or press **stop the factory** on the launcher page.

Both shut the programs down in the right order, so each machine removes itself from the knowledge
graph on the way out. If you just close the terminal window instead, the graph keeps stale entries
and the next run may complain — that is what `--force` clears.

---

## What to try, in order

### 1. Look at the launcher page — <http://127.0.0.1:8080/>

A picture of all the programs and how they connect. Hover over any box to read what it is and which
source file it comes from.

### 2. Open a machine's own page

Click a **PLC** box. This is the machine's own control panel, the kind of screen that sits on the
machine itself. Set a belt speed and watch it move.

**Belts have momentum.** A belt does not jump to a new speed. It ramps up at 1 m/s per second, so
asking for 3 m/s takes about 3 seconds. This is on purpose — a real belt has mass.

### 3. Open the control station — the station board

Click the **control station** box. This screen never talked to the machines directly. It ran a
database query, found them, and connected. It only reads: it has no set control.

Try this:

1. Expand a machine's card. Every belt speed and light barrier shows the value the machine reports.
2. On that machine's own panel page (step 2), set a belt speed. Within a few seconds the station
   board shows the belt climbing to your number.
3. Turn on **show IRIs**. Every row now also shows its full name in the knowledge graph, the exact
   Python expression that reaches its value, and which private details were hidden from the network.

The bar above the cards shows the automatic program. It waits for a light barrier to change, then
reads a barrier and a belt speed and writes both to the control station's log. **pause** stops it.

### 4. Prove the discovery is real

With the demo still running, press **stop** on one unit in the launcher page. Go back to the station
board. Within a few seconds that machine is gone from the screen. Nobody edited any configuration —
the machine removed itself from the graph, and the station board simply stopped finding it.

### 5. Watch the watchdog

The **stop** button is a clean shutdown: the machine removes its own address on the way out, so the
watchdog has nothing left to do. To see the watchdog work, kill a machine's middleware so it cannot
say goodbye. Take the pid from its box on the launcher page and run `kill -9 <pid>`.

A running middleware refreshes a heartbeat in the knowledge graph. After the kill the heartbeat stops
being renewed. When it is older than the staleness threshold (90 seconds, the middleware's default;
the demo does not change it), the watchdog's next sweep removes that machine's address from the
graph, so the station board stops finding it. Sweeps run every 5 seconds, so allow about 95 seconds
after the kill at most.

The sweep also settles that machine's operations. Each machine has exactly one service, its own
middleware, so when it is swept every operation of that machine that is still **queued** or
**running** is marked failed: nothing is left to run it. The control station is not affected: it
registers a service but no workflow, so it never has operations queued to it. The failed operations
are visible in the graph, as `svc:operationStatus "failed"`, and the issuer of each is notified. The launcher page's
**watchdog** box shows only that the process is live; its output (click it if it turns red) and the
terminal show what it ran.

---

## When something goes wrong

| what you see | what it usually means |
| --- | --- |
| `ModuleNotFoundError`, or strange errors on start | Something the demo needs is not installed. Re-run `pip install "kapps-semantic-middleware[examples]"` — the `[examples]` extra is what brings `uvicorn` and `amqtt`. |
| An error mentioning `TRIPLESTORE_` | The three environment variables are not set, or the store is unreachable. |
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
| `watchdog.py` | Runs the one watchdog-mode middleware that sweeps stale services. |
| `control_station.py` | Runs the control station process and its observing algorithm. |
| `station_board.py` | The station board's web routes and template. It shows; it sets nothing. |
| `controller.py` | Finding machines by query, and reading them over HTTP. |
| `algorithm.py` | The example automatic program. When a barrier changes, it reads a barrier on one machine and a belt speed on another — only to show that it can reach them. It writes nothing and is not a real controller. |
| `seed.py` | Writes the starting facts for N machines into the knowledge graph. |
| `plc/` | The pretend machine and its own panel page. |
| `factory.ttl`, `transferunit.ttl` | The vocabulary: what a TransferUnit is made of. |

**The path this demo proves:**

```
PLC → MQTT → middleware → knowledge graph → control station finds it → HTTP read → station board
```

The way back to the PLC returns with workflows: a machine changes only through a workflow of its
service, and this demo has none yet.

`CONTEXT.md` in this folder explains the words used here (Factory, Unit index, Launcher, Runner,
Control station, Panel, Live factory) and points at the decision records behind each choice.
