"""Guard tests enforcing separation of concerns for the station board.

- control_station.py names no FastAPI route decorator or response class -- it is the
  runner, not the routes file.
- station_board.py names no subprocess/signal -- it is the routes file, not the runner.
- The demo package sends no PUT, POST, PATCH or DELETE to a unit's served data-model
  route. The demo observes: in KAPPS a device changes only through a workflow of its
  service, and this demo has none yet.
  The monitor design states the underlying invariant ("a consumer cannot drive, because
  its own code holds no method that sends a PUT request") and notes "the guard test
  follows the wiring rather than the source text". The wiring half is the controller's
  read-only connector wiring, asserted on a real wiring in tests/test_controller_view.py
  (every registration a read leg). This file is the source half, shaped after
  tests/test_plc_guard.py and tests/test_launcher_index_guard.py's own AST-based checks:
  no demo module imports an HTTP client or calls a connector's send, the controller has
  no write method, and every request a demo page makes goes to its own ``/api/`` route,
  never to a served data-model route (``/{Model}/{root}/...``).
- Every backend file named in station_board.TEACH exists on disk, so a tooltip never
  points at a file that moved (the launcher page's pattern).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


def get_imports_and_names(filepath: Path) -> set[str]:
    """Extract all imported module names and top-level names from a Python file."""
    content = filepath.read_text(encoding="utf-8")
    tree = ast.parse(content)

    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                names.add(base.id)

    return names


def test_control_station_no_fastapi_route_names():
    """control_station.py must not import or reference FastAPI route-building
    concepts -- it constructs the Controller and grafts station_board onto it, but
    defines no route of its own."""
    path = REPO_ROOT / "demo" / "transferunits" / "control_station.py"
    names = get_imports_and_names(path)

    forbidden = {"FastAPI", "HTMLResponse", "JSONResponse", "APIRouter"}
    violations = names & forbidden

    assert not violations, f"control_station.py must not reference: {forbidden}. Found: {violations}"


def test_station_board_no_subprocess_or_signal():
    """station_board.py must not import or reference subprocess/signal concepts --
    process control stays in control_station.py."""
    path = REPO_ROOT / "demo" / "transferunits" / "station_board.py"
    names = get_imports_and_names(path)

    forbidden = {"subprocess", "Popen", "signal", "SIGTERM"}
    violations = names & forbidden

    assert not violations, f"station_board.py must not reference: {forbidden}. Found: {violations}"


def test_station_board_never_imports_from_control_station():
    """The split is one-directional: control_station.py imports station_board (to graft
    its routes on), and station_board.py must never import back from control_station.py
    -- the same shape index.py/launcher.py already hold (index.py imports launcher.py
    for the Factory type; launcher.py never imports index.py)."""
    path = REPO_ROOT / "demo" / "transferunits" / "station_board.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert "control_station" not in imported_modules, (
        "station_board.py must not import from control_station.py"
    )


DEMO_ROOT = REPO_ROOT / "demo"

HTTP_CLIENTS = {"httpx", "requests", "urllib", "urllib3", "aiohttp", "http"}
"""Every way a Python module here could open an HTTP request of its own."""

SENDING_CALLS = {
    "put", "post", "patch", "delete", "consume", "push", "push_parameter", "request", "send",
}
"""Method names that send a value somewhere: an HTTP client's write verbs, and a connector's
``consume`` (the REST parameter connector's ``consume`` refuses, but a call is still an attempt
to send). A FastAPI route decorator (``@app.post``) declares a route of the demo's own; the
check skips decorators."""


def _demo_modules() -> list[Path]:
    modules = sorted(DEMO_ROOT.rglob("*.py"))
    assert modules, f"no Python module under {DEMO_ROOT}"
    return modules


def test_no_demo_module_imports_an_http_client():
    """The whole package, not a list of files: a new module cannot open a side door."""
    violations = {}
    for path in _demo_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        found = imported & HTTP_CLIENTS
        if found:
            violations[str(path.relative_to(REPO_ROOT))] = sorted(found)

    assert not violations, f"demo modules import an HTTP client: {violations}"


def test_no_demo_module_calls_a_sending_method():
    """No ``.put(``, ``.post(``, ``.delete(``, ``.consume(``, ``.push(`` and the like anywhere in the
    package's code, so a write cannot come back through a client or a connector the
    module already holds without a new import the check above would catch."""
    violations = []
    for path in _demo_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        decorators = {
            id(decorator)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            for decorator in node.decorator_list
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and id(node) not in decorators
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in SENDING_CALLS
            ):
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} .{node.func.attr}("
                )

    assert not violations, f"demo code sends a value: {violations}"


def test_the_controller_has_no_write_method():
    """``push`` and ``push_parameter`` drove an assignment out to a unit. They are gone,
    and must not come back under the same names."""
    from demo.transferunits.controller import Controller

    present = [name for name in ("push", "push_parameter") if hasattr(Controller, name)]
    assert not present, f"Controller has write methods again: {present}"


_FETCH_CALL = re.compile(r"\bfetch\(\s*([^,)]+)")
_POST_HELPER_CALL = re.compile(r"\bpost\(\s*([^,)]+)")


def _page_targets(text: str) -> list[str]:
    """The first argument of every ``fetch(`` and of the pages' own ``post(`` helper."""
    return [m.group(1).strip() for m in _FETCH_CALL.finditer(text)] + [
        m.group(1).strip() for m in _POST_HELPER_CALL.finditer(text)
    ]


def test_every_page_request_goes_to_its_own_api_route():
    """A page reaches its own server's ``/api/`` routes only. The ``post(url, body)``
    helper's own ``fetch(url, ...)`` is the one indirection, and every call of the helper
    is checked in its place."""
    templates = sorted(DEMO_ROOT.rglob("*.html"))
    assert templates, f"no page template under {DEMO_ROOT}"

    violations = []
    for path in templates:
        for target in _page_targets(path.read_text(encoding="utf-8")):
            literal = target.strip("'\"`")
            if target == "url" or literal.startswith("/api/"):
                continue
            violations.append(f"{path.relative_to(REPO_ROOT)}: {target}")

    assert not violations, f"a page requests something other than its own /api/: {violations}"


def test_teach_files_exist_on_disk():
    """Every backend file named in station_board.TEACH must exist, so a tooltip never
    lies about it (the launcher page's pattern, mirrored from
    test_launcher_index_guard.py::test_teach_files_exist_on_disk)."""
    from demo.transferunits import station_board

    missing = [
        entry["file"] for entry in station_board.TEACH.values() if not (REPO_ROOT / entry["file"]).is_file()
    ]

    assert not missing, f"TEACH names files that don't exist: {missing}"


# The domain terms of scenario 3. `algorithm.py` is allowed to name these; nothing else in
# the demo is (see station_board.py's own module docstring).
SCENARIO_3_DOMAIN_TERMS = (
    "tu:",
    "TransferUnit",
    "ConveyorBelt",
    "LightBarrier",
    "hasConveyorSpeed",
    "hasConveyorBelt",
    "hasLightBarrier",
    "isOccupied",
)


def test_station_board_names_no_domain_term():
    """station_board.py's module docstring claims no domain term appears in it. Hold it down.

    The claim was true and unasserted until the milestone-1 release review, which found the
    teaching copy naming ``tu:hasConveyorSpeed`` while the docstring above it said nothing
    did. A claim a file makes about itself should fail a test, not age quietly.

    The whole source text is checked, not just the code: a tooltip that names a domain term
    teaches the reader that this screen is TransferUnit-specific, which is the opposite of
    what the board demonstrates. It renders whatever the view selects.
    """
    path = REPO_ROOT / "demo" / "transferunits" / "station_board.py"
    source = path.read_text(encoding="utf-8")

    # The docstring lists the terms in order to forbid them, so exempt the sentence that
    # does the forbidding -- and this test's own name for them, imported at module scope.
    lines = [
        line
        for line in source.splitlines()
        if "domain term (tu:" not in line and "this file, in code or in the teaching copy" not in line
    ]
    body = "\n".join(lines)

    found = sorted({term for term in SCENARIO_3_DOMAIN_TERMS if term in body})
    assert not found, (
        f"station_board.py names domain terms {found}, contradicting its own module "
        f"docstring. Either phrase it generically, or change the docstring -- but the two "
        f"must agree. algorithm.py is the only file in this demo allowed to name one."
    )
