"""Guard tests holding the dashboard's contract: no write, no traffic, no deps.

These tests inspect the source of dashboard.py and dashboard_boxes.py. They
mirror tests/test_plc_guard.py and tests/test_station_board_guard.py: an AST
walk for imports and names, a source-text scan for the rest. They enforce the
module docstring's two rules (no write path, no traffic the page causes) and
the public-API boundary that KSM's own boxes must walk the subproject path.
"""

from __future__ import annotations

import ast
import sys

import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

DASHBOARD_PATH = REPO_ROOT / "src" / "kapps_semantic_middleware" / "dashboard.py"
DASHBOARD_BOXES_PATH = (
    REPO_ROOT / "src" / "kapps_semantic_middleware" / "dashboard_boxes.py"
)

_WRITE_VERBS = ("post", "put", "patch", "delete")

#: The one function of dashboard.py that reads the graph: it reads the name of the instance's
#: resource once, when the dashboard is enabled, and no render reads it again. The traffic scan
#: skips its source; `test_the_one_read_runs_when_the_dashboard_is_enabled` holds where it runs.
_ENABLE_TIME_READ = "_instance_name"


def _module_path(name: str) -> Path:
    """The source file for one of the two dashboard modules."""
    return DASHBOARD_PATH if name == "dashboard" else DASHBOARD_BOXES_PATH


def _skip_if_no_src() -> None:
    """An installed copy carries no src/ tree, so the guard has nothing to check."""
    if not (REPO_ROOT / "src").is_dir():
        pytest.skip("no src/ tree: this is not a source checkout")


def _imported_module_names(filepath: Path) -> set[str]:
    """The root module name of every import statement in a Python file."""
    tree = ast.parse(filepath.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def _decorator_attr_calls(tree: ast.AST) -> list[tuple[str, int]]:
    """(name, line) for every attribute call in a decorator.

    A decorator names a function on a receiver, so the verb is an Attribute
    with a Call parent. A plain Name in a decorator position is a pass-through
    decorator, not a route verb.
    """
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) and not isinstance(
            node, ast.AsyncFunctionDef
        ):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(
                decorator.func, ast.Attribute
            ):
                hits.append((decorator.func.attr, decorator.lineno))
    return hits


def test_no_route_decorator_names_a_write_verb() -> None:
    """Neither module decorates a handler with post, put, patch, or delete.

    The dashboard is read-only. A write verb on a route would violate the
    module docstring's first rule. Report every offending file and line.
    """
    _skip_if_no_src()
    violations: list[str] = []
    for name, path in (
        ("dashboard", DASHBOARD_PATH),
        ("dashboard_boxes", DASHBOARD_BOXES_PATH),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for verb, lineno in _decorator_attr_calls(tree):
            if verb in _WRITE_VERBS:
                violations.append(f"{name}.py:{lineno}: router.{verb}")
    assert not violations, f"write-verb route decorators found: {violations}"


def _source_without_the_enable_time_read(path: Path) -> str:
    """The module's source, less the one function that reads the name at enable."""
    source = path.read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == _ENABLE_TIME_READ:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            source = source.replace(segment, "")
    return source


def test_no_traffic_imports_or_calls() -> None:
    """Neither module imports an HTTP client or names a graph query.

    The module docstring's second rule is that rendering the page or a box reads
    what the instance already holds. An HTTP client import or a SPARQL call would
    break that. The import check goes through the AST; the names go through the
    source text, because a call like ``ogm.db.query`` is an Attribute chain. The
    one read at enable is left out of the text, and the test below holds it there.
    """
    _skip_if_no_src()
    forbidden_imports = {"httpx", "requests", "urllib", "aiomqtt"}
    # The rule is about traffic, so it forbids the *calls* that query or write, not the
    # attribute path that reaches the client. `middleware.ogm.db.repository` is a plain
    # attribute read of a value the instance already holds, and the header box needs it to
    # name the repository. Forbidding `ogm.db` outright would ban that read while banning
    # no traffic, because a query is always a call.
    forbidden_names = (
        "SPARQL",
        "sparql",
        "ogm.db.query",
        "ogm.db.update",
        ".query(",
        ".update(",
        ".commit(",
        ".fetch(",
    )

    for path in (DASHBOARD_PATH, DASHBOARD_BOXES_PATH):
        imports = _imported_module_names(path)
        violations = imports & forbidden_imports
        assert not violations, (
            f"{path.name} must not import: {forbidden_imports}. Found: {violations}"
        )

        source = _source_without_the_enable_time_read(path)
        for name in forbidden_names:
            assert name not in source, f"{path.name} must not name {name!r}"


def test_the_one_read_runs_when_the_dashboard_is_enabled() -> None:
    """The name is read in `enable_dashboard` itself, and in no route and no render.

    The routes are functions nested in `enable_dashboard`, so a call inside one of
    them would run at every request. Only a call in the function's own body runs
    once, when the dashboard is enabled. `dashboard_boxes.py` does not call it.
    """
    _skip_if_no_src()
    tree = ast.parse(DASHBOARD_PATH.read_text(encoding="utf-8"))
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)

    def calls_in(nodes: list[ast.stmt], *, enter_functions: bool) -> list[int]:
        """Lines that call the read, entering nested functions only if asked."""
        lines = []
        pending: list[ast.AST] = list(nodes)
        while pending:
            node = pending.pop()
            if isinstance(node, functions) and not enter_functions:
                continue
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == _ENABLE_TIME_READ
            ):
                lines.append(node.lineno)
            pending.extend(ast.iter_child_nodes(node))
        return lines

    enable = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "enable_dashboard"
    )
    at_enable = calls_in(enable.body, enter_functions=False)
    everywhere = calls_in(tree.body, enter_functions=True)
    assert len(at_enable) == 1, f"enable_dashboard must read the name once: {at_enable}"
    assert everywhere == at_enable, (
        f"{_ENABLE_TIME_READ} is called outside enable_dashboard's own body, at lines "
        f"{sorted(set(everywhere) - set(at_enable))}: a route or a render would read the graph"
    )
    assert _ENABLE_TIME_READ not in DASHBOARD_BOXES_PATH.read_text(encoding="utf-8")


def test_no_template_engine_and_no_static_mount() -> None:
    """Neither module imports a template engine or static-files mount.

    The last is deliberate: the project mounts no StaticFiles, and one route
    serving one vendored file is smaller than a new mount.
    """
    _skip_if_no_src()
    forbidden_imports = {"jinja2", "jinja", "starlette", "fastapi"}
    # fastapi is allowed; only the templating submodule is forbidden. Check
    # the full import path, not just the root module.
    forbidden_full = {
        "jinja2",
        "jinja",
        "starlette.templating",
        "fastapi.staticfiles",
    }

    for path in (DASHBOARD_PATH, DASHBOARD_BOXES_PATH):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if (
                        alias.name.split(".")[0] in forbidden_imports
                        and alias.name in forbidden_full
                    ):
                        pytest.fail(f"{path.name} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module in forbidden_full:
                    pytest.fail(f"{path.name} imports {node.module}")


def test_vendored_htmx_exists_and_is_pinned_version() -> None:
    """The vendored htmx file is present, plausible in size, and pinned.

    The size bound is a guard against a truncated or replaced download. An
    exact byte count would break the first re-minify, so the bound is a range,
    not a pin.
    """
    _skip_if_no_src()
    asset = (
        REPO_ROOT / "src" / "kapps_semantic_middleware" / "static" / "htmx-2.0.4.min.js"
    )
    assert asset.is_file(), f"vendored htmx missing: {asset}"
    size = asset.stat().st_size
    assert 45000 <= size <= 55000, f"htmx size {size} outside expected range"
    text = asset.read_text(encoding="utf-8")
    assert 'version:"2.0.4"' in text


def test_dashboard_boxes_uses_only_public_api() -> None:
    """dashboard_boxes imports no underscored name from the dashboard module.

    This is the acceptance claim that KSM's own boxes walk the subproject
    path: if the public API is awkward, it must be awkward for KSM first.
    """
    _skip_if_no_src()
    tree = ast.parse(DASHBOARD_BOXES_PATH.read_text(encoding="utf-8"))
    violations: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "kapps_semantic_middleware.dashboard"
        ):
            for alias in node.names:
                if alias.name.startswith("_"):
                    violations.append(alias.name)
    assert not violations, (
        f"dashboard_boxes imports underscored names {violations}; "
        "KSM's own boxes must walk the subproject path through the public API only"
    )


def test_vendored_json_enc_exists_and_is_pinned() -> None:
    """The vendored json-enc extension is present, plausible in size, and the real file.

    The size bound is a guard against a truncated or replaced download, as a range rather
    than an exact count, so the first re-minify does not break it. Unlike htmx, this file
    carries no version string of its own, so the name it defines is what pins it, and the
    pinned release is named in the CHANGELOG.
    """
    _skip_if_no_src()
    asset = REPO_ROOT / "src" / "kapps_semantic_middleware" / "static" / "json-enc.js"
    assert asset.is_file(), f"vendored json-enc missing: {asset}"
    size = asset.stat().st_size
    assert 700 <= size <= 1500, f"json-enc size {size} outside expected range"
    assert "htmx.defineExtension('json-enc'" in asset.read_text(encoding="utf-8")
