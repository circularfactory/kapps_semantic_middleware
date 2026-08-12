"""Guard tests enforcing separation of concerns for the station board (#82, ADR 0029).

- control_station.py names no FastAPI route decorator or response class -- it is the
  runner, not the routes file.
- station_board.py names no subprocess/signal -- it is the routes file, not the runner.
- Neither file (nor algorithm.py, which also drives a write) names an HTTP client
  directly -- every write reaches a peer through Controller.push(), never a raw PUT
  issued from demo code. This is the "PUT-grep guard" #82's acceptance criteria cite:
  ADR 0032 already states the underlying invariant ("a consumer cannot drive, because
  its own code holds no method that sends a PUT request") and notes "the guard test
  follows the wiring rather than the source text" -- the wiring-level half of that is
  _VIEW_REGISTRY's REST-only recognition (tests/test_controller_view.py), and this is
  the source-text half, shaped after tests/test_plc_guard.py and
  tests/test_launcher_index_guard.py's own AST-based checks.
- Every backend file named in station_board.TEACH exists on disk, so a tooltip never
  points at a file that moved (the #68 pattern).
"""

from __future__ import annotations

import ast
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


def test_no_demo_file_issues_an_http_call_directly():
    """The PUT-grep guard (#82's acceptance criteria; the invariant itself is ADR
    0032's: "a consumer cannot drive, because its own code holds no method that sends
    a PUT request"). station_board.py and algorithm.py are the two demo files that
    drive a write in this ticket's own code paths -- neither may import an HTTP client.
    Every write reaches a peer exclusively through Controller.push(), whose own PUT
    lives in the REST connector inside src/kapps_semantic_middleware/, not here.
    """
    forbidden = {"httpx", "requests", "urllib3", "aiohttp", "http"}
    for filename in ("station_board.py", "algorithm.py", "control_station.py"):
        path = REPO_ROOT / "demo" / "transferunits" / filename
        names = get_imports_and_names(path)
        violations = names & forbidden
        assert not violations, f"{filename} must not reference: {forbidden}. Found: {violations}"


def test_station_board_names_no_raw_put_call():
    """Belt and braces on top of the import check above: no literal `.put(` call
    anywhere in station_board.py's source, so a future edit cannot reintroduce a direct
    HTTP write by calling an already-imported client's method without adding a new
    import the check above would catch."""
    path = REPO_ROOT / "demo" / "transferunits" / "station_board.py"
    source = path.read_text(encoding="utf-8")
    assert ".put(" not in source, "station_board.py must drive writes only through controller.push()"


def test_teach_files_exist_on_disk():
    """Every backend file named in station_board.TEACH must exist, so a tooltip never
    lies about it (the #68 pattern, mirrored from
    test_launcher_index_guard.py::test_teach_files_exist_on_disk)."""
    from demo.transferunits import station_board

    missing = [
        entry["file"] for entry in station_board.TEACH.values() if not (REPO_ROOT / entry["file"]).is_file()
    ]

    assert not missing, f"TEACH names files that don't exist: {missing}"


# The domain terms of scenario 3. `algorithm.py` is allowed to name these; nothing else in
# the demo is (root ADR 0004, and station_board.py's own module docstring).
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
