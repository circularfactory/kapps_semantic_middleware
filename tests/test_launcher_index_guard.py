"""Guard tests enforcing separation of concerns for the Launcher's index page (#72, ADR 0029).

- index.py names none of: subprocess, Popen, signal, SIGTERM
- launcher.py names none of: fastapi, uvicorn, HTMLResponse
- Every backend file named in index.TEACH exists on disk, so a tooltip never points at a
  file that moved.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


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


def test_index_no_subprocess_or_signal():
    """index.py must not import or reference subprocess/signal concepts."""
    index_path = REPO_ROOT / "demo" / "transferunits" / "index.py"
    names = get_imports_and_names(index_path)

    forbidden = {"subprocess", "Popen", "signal", "SIGTERM"}
    violations = names & forbidden

    assert not violations, f"index.py must not reference: {forbidden}. Found: {violations}"


def test_launcher_no_fastapi():
    """launcher.py must not import or reference FastAPI/uvicorn concepts."""
    launcher_path = REPO_ROOT / "demo" / "transferunits" / "launcher.py"
    names = get_imports_and_names(launcher_path)

    forbidden = {"fastapi", "uvicorn", "HTMLResponse"}
    violations = names & forbidden

    assert not violations, f"launcher.py must not reference: {forbidden}. Found: {violations}"


def test_teach_files_exist_on_disk():
    """Every backend file named in TEACH must exist, so a tooltip never lies about it."""
    from demo.transferunits.index import TEACH

    missing = [entry["file"] for entry in TEACH.values() if not (REPO_ROOT / entry["file"]).is_file()]

    assert not missing, f"TEACH names files that don't exist: {missing}"
