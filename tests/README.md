# Tests

Run the suite with `uv run pytest`. Tests marked `live` (any test that asks for the `store`
fixture) need the `TRIPLESTORE_*` variables and run against the dataset `Tests`; `-m "not live"`
selects the rest.

The live tests seed the ontologies and shapes into a disposable dataset: a productive store already
holds them.

A live run starts by clearing the default graph of `Tests` and setting the middleware's shipped
shapes (`service-shapes.ttl`, beside the service ontology) as the dataset's shapes, so every live
test runs with the operation-status rule active: each Operation has exactly one status. A live
test that needs a SHACL shape of its own sends it joined with the shipped shapes
(`_shapes.with_shipped_shapes`), because the store's `shapes_set` replaces every shape of the
dataset, and restores the shipped shapes at its end (`_shapes.restore_shipped_shapes`), never an
empty document. So a live run replaces the shapes that other code loaded into `Tests`.

## The shipped stack

`test_shipped_stack_validates_shapes.py` is opt-in: it runs only when `KAPPS_COMPOSE_COMMAND`
names a Compose v2 command, such as `docker compose` or the path of a standalone Compose v2 binary.
Without the variable it skips, so a plain `uv run pytest` never starts Docker. It is also marked
`live`, but it reads no store variable. It starts the compose stack of `examples/docker/` as a
project of its own, posts a shape into the SHACL shapes graph of the dataset `kapps-demo`, and
checks that a conforming write is admitted and a violating one is refused with a validation report.
Then it removes the project with its volume. It needs Docker and skips where Docker or the compose
command does not run:

```bash
KAPPS_COMPOSE_COMMAND="docker compose" \
    uv run pytest -m live tests/test_shipped_stack_validates_shapes.py
```

## Browser tests

Tests marked `browser` open a page of an instance, served in this process, in a headless browser
through pytest-playwright and read what the browser shows. They skip where no browser is
installed, which is the case in the CI. Install Playwright's Chromium with
`uv run playwright install chromium`; where it is missing, an installed Google Chrome runs the
tests instead (`_browser.py`).

The browser tests run last (`conftest.py`): Playwright's sync API keeps an event loop running
in the main thread, and an asyncio test that starts after it fails.
