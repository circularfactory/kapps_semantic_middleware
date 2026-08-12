"""Guard tests enforcing separation of concerns per ADR 0029.

- panel.py names none of: aiomqtt, asyncio, topic, publish
- transfer_unit.py names none of: fastapi, uvicorn, Request, HTMLResponse
"""

import ast
from pathlib import Path


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
            # Get the root name of attribute chains
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                names.add(base.id)

    return names


def test_panel_no_mqtt_or_asyncio():
    """panel.py must not import or reference MQTT/asyncio concepts."""
    panel_path = Path(__file__).parent.parent / "demo" / "transferunits" / "plc" / "panel.py"
    names = get_imports_and_names(panel_path)

    forbidden = {"aiomqtt", "asyncio", "topic", "publish"}
    violations = names & forbidden

    assert not violations, (
        f"panel.py must not reference: {forbidden}. Found: {violations}"
    )


def test_transfer_unit_no_fastapi():
    """transfer_unit.py must not import or reference FastAPI/web concepts."""
    tu_path = (
        Path(__file__).parent.parent / "demo" / "transferunits" / "plc" / "transfer_unit.py"
    )
    names = get_imports_and_names(tu_path)

    forbidden = {"fastapi", "uvicorn", "Request", "HTMLResponse"}
    violations = names & forbidden

    assert not violations, (
        f"transfer_unit.py must not reference: {forbidden}. Found: {violations}"
    )