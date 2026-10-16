# Tests

Run the suite with `uv run pytest`. Tests marked `live` (any test that asks for the `store`
fixture) need the `TRIPLESTORE_*` variables and run against the dataset `Tests`; `-m "not live"`
selects the rest. A live test that needs a SHACL shape sets it with the store's `shapes_set`,
which replaces every shape of the dataset, and removes every shape again at its end. So a live
run also removes the shapes that other code loaded into `Tests`.

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
