# Scenario 3 — The TransferUnit Factory

This demo builds a very small factory on your computer and lets you watch it from a web browser.

**The demo observes.** In KAPPS a machine changes only through a workflow of its service. This
demo has no machine workflows yet, so the control station reads the machines and changes none.
You can still move a belt at the machine itself, on its own panel page.

Nothing here is real hardware. Every machine is a Python program that pretends to be one. But the
parts talk to each other the same way real ones would, so what you see is the real behaviour.

Where scenarios 1 and 2 each show one interaction between two peers, this one shows the whole
picture running at once: six processes, discovery through the graph, and a screen that was never
told where anything is.

## What you will see

The demo starts **6 programs** at once. Each one is a separate process, like a separate machine on
a factory floor.

| program | what it pretends to be |
| --- | --- |
| **PLC 1** and **PLC 2** | Two conveyor machines. Each has 2 belts and 2 light barriers. Each has its own small web page. |
| **Middleware 1** and **Middleware 2** | One "translator" per machine. It reads the machine over MQTT and offers it to the network over HTTP. |
| **Control station** | An operator screen. It finds the machines by asking a database, then shows what they report. |
| **Launcher** | Starts all of the above and shows you a picture of them. |

A **TransferUnit** is one conveyor machine: 2 belts and 2 light barriers.

**The point of the demo:** the control station is never told where the machines are. It asks the
knowledge graph, finds whatever is there, and reads it. You can stop a machine and it disappears
from the screen. Nothing is hardcoded.

## Before you start

A productive circular factory already holds the shared ontologies and shapes in its knowledge graph;
the files shipped with the middleware seed the demonstrations and tests, and document the
vocabulary. The launcher seeds the shipped ontologies and shapes for this demonstration.

You need two things.

### 1. The package, with the `examples` extra

A plain install brings the library alone. The demo needs the extra:

```bash
pip install "kapps-semantic-middleware[examples]"
```

You do **not** need to install an MQTT broker. Each machine starts its own, inside its own process.

### 2. A triple store

The triple store holds the knowledge graph, and the demo reads and writes it. See
[Install the stack](../index.md) for how to start one. Put these three values in your environment:

```bash
export TRIPLESTORE_URL=https://your-triplestore-server
export TRIPLESTORE_USERNAME=your-username
export TRIPLESTORE_PASSWORD=your-password
```

There is no fourth value. The demo always uses the dataset named **`kapps-demo`**, which is the
one `docker compose` creates for you, and it names that in code rather than reading it from your
environment. A `TRIPLESTORE_DATASET` you happen to have set is ignored.

:::{warning}
The demo **writes into** `kapps-demo`, and `--force` **deletes and rewrites** the demo's data in it.
Point `TRIPLESTORE_URL` at a store you are allowed to overwrite — the dataset is pinned, but the
server is not, and a `kapps-demo` on a shared server is still somebody's.
:::

## Start it

```bash
kapps-transferunit-factory --units 2
```

Then open **<http://127.0.0.1:8080/>** in your browser.

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

## What to try, in order

### 1. Look at the launcher page — <http://127.0.0.1:8080/>

A picture of all 6 programs and how they connect. Hover over any box to read what it is and which
source file it comes from.

% SCREENSHOT SLOT 1 of 4: the launcher page, showing the six boxes and their connections.

### 2. Open a machine's own page

Click a **PLC** box. This is the machine's own control panel, the kind of screen that sits on the
machine itself. Set a belt speed and watch it move.

**Belts have momentum.** A belt does not jump to a new speed. It ramps up at 1 m/s per second, so
asking for 3 m/s takes about 3 seconds. This is on purpose — a real belt has mass.

% SCREENSHOT SLOT 2 of 4: a PLC panel with a belt mid-ramp.

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

% SCREENSHOT SLOT 3 of 4: the station board with show IRIs turned on.

### 4. Prove the discovery is real

With the demo still running, press **stop** on one unit in the launcher page. Go back to the station
board. Within a few seconds that machine is gone from the screen. Nobody edited any configuration —
the machine removed itself from the graph, and the station board simply stopped finding it.

% SCREENSHOT SLOT 4 of 4: the station board before and after one unit is stopped.

## When something goes wrong

| what you see | what it usually means |
| --- | --- |
| `ModuleNotFoundError`, or strange errors on start | The `examples` extra is not installed. Install `"kapps-semantic-middleware[examples]"`. |
| An error mentioning `TRIPLESTORE_` | The three environment variables are not set, or the store is unreachable. |
| "a live factory is already running" | A previous run did not shut down. Start again with `--force`. |
| A box on the launcher page turns red | That program crashed. **Click the red box** — it opens and shows that program's last output. |
| The page loads but no values change | Give it a few seconds. Machines publish their state on a timer. |

Every program's output also appears in the terminal you started from, with a name in front of each
line (`plc-1`, `middleware-2`, `control`), so you can see which program said what.

## The path this demo proves

```text
PLC → MQTT → middleware → knowledge graph → control station finds it → HTTP read → station board
```

Every arrow in that line is a mechanism the earlier scenarios showed one at a time. The difference
here is only that there are six processes instead of one notebook. The way back to the PLC returns
with workflows: a machine changes only through a workflow of its service, and this demo has none
yet.
