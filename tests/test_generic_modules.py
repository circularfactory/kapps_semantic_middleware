"""The generic modules hold no `cfc:` IRI, and every module is classified.

`AGENTS.md` lists each module of the package as generic, as specific to the circular factory
(Core's `cfc:` vocabulary), or as generic with cfc-specific parts still inside, and names those
parts. This module reads that list and the source of each module:

- A generic module may neither name a `cfc:` IRI nor import a cfc-specific module, so the
  generic core stays portable to a domain that is not modelled on Core.
- In a module with cfc-specific parts, a `cfc:` term may appear only inside the functions,
  methods, classes and module-level names that the list names as its parts. Its imports stand at
  module level, where Python puts them; what they bring in is checked where it is used.
- A module that the list does not name fails until it is classified. A module, or a part, listed
  as speaking `cfc:` that stops doing so fails until the list says so, so the list cannot go
  stale.

Prose is not checked: a docstring may name a `cfc:` term.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Set
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src/kapps_semantic_middleware"
AGENTS = REPO_ROOT / "AGENTS.md"
SECTION = "## Generic and circular-factory-specific modules"
ROW = re.compile(
    r"^\|\s*`(kapps_semantic_middleware[A-Za-z0-9_.]*)`\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|"
)
PART = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")

GENERIC = "generic"
SPECIFIC = "cfc-specific"
MIXED = "generic, with cfc-specific parts"

CORE_NAMESPACE = "https://w3id.org/circularfactory/Core"
CORE_NAMES = {"CFC", "CORE_NS", "CORE_ONTOLOGY"}
"""The names under which ``vocabulary`` gives Core's IRIs."""


@dataclass(frozen=True)
class _Listed:
    """A module's row in AGENTS.md: its kind, and the parts named for the third kind."""

    kind: str
    parts: frozenset[str]


@dataclass(frozen=True)
class _Use:
    """One place where a module speaks `cfc:`."""

    #: The definition it stands in: a top-level function, class or module-level name, or a
    #: method as ``Class.method``. Empty at module level.
    where: str
    what: str
    line: int
    #: Whether it is an import, which a module with cfc-specific parts may make at module level.
    imports: bool


def _classification() -> dict[str, _Listed]:
    """Each module that AGENTS.md lists, by its dotted name."""
    lines = AGENTS.read_text(encoding="utf-8").splitlines()
    start = lines.index(SECTION)
    listed: dict[str, _Listed] = {}
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        match = ROW.match(line)
        if match:
            module, kind, parts = match.groups()
            assert module not in listed, f"AGENTS.md lists {module} twice"
            named = frozenset(PART.findall(parts)) if kind == MIXED else frozenset()
            listed[module] = _Listed(kind, named)
    return listed


def _modules() -> dict[str, Path]:
    """Each module of the package, by its dotted name."""
    found = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        parts = list(path.relative_to(PACKAGE_DIR.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        found[".".join(parts)] = path
    return found


def _bound_in(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The names that ``function`` binds itself: its parameters and the names it assigns."""
    arguments = function.args
    every = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    every += [arg for arg in (arguments.vararg, arguments.kwarg) if arg is not None]
    bound = {arg.arg for arg in every}
    bound |= {
        node.id
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    return bound


def _uses(source: str, module: str, specific: set[str]) -> list[_Use]:
    """Where ``source`` names a `cfc:` IRI, or imports or uses a module in ``specific``.

    A string that stands as a statement of its own is prose, a docstring, and is skipped. A name
    that a function binds itself is its own, not an import of the same name.
    """
    tree = ast.parse(source)
    prose = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    package = module if module.count(".") == 0 else module.rsplit(".", 1)[0]

    def imported_from(node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
        return f"{base}.{node.module}" if node.module else base

    cfc_names = set(CORE_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and imported_from(node) in specific:
            cfc_names |= {alias.asname or alias.name for alias in node.names}

    uses: list[_Use] = []

    def note(node: ast.AST, where: list[str], shadowed: set[str]) -> None:
        here = ".".join(where)
        if isinstance(node, ast.Name) and node.id in cfc_names - shadowed:
            uses.append(_Use(here, node.id, node.lineno, imports=False))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in prose and (CORE_NAMESPACE in node.value or "cfc:" in node.value):
                uses.append(_Use(here, repr(node.value[:60]), node.lineno, imports=False))
        elif isinstance(node, ast.ImportFrom):
            source_module = imported_from(node)
            names = {f"{source_module}.{alias.name}" for alias in node.names} | {source_module}
            core = {alias.name for alias in node.names} & CORE_NAMES
            for name in sorted((names & specific) | core):
                uses.append(_Use(here, f"imports {name}", node.lineno, imports=True))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in specific:
                    uses.append(_Use(here, f"imports {alias.name}", node.lineno, imports=True))

    def visit(node: ast.AST, where: list[str], shadowed: set[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, [*where, child.name], shadowed | _bound_in(child))
            elif isinstance(child, ast.ClassDef):
                visit(child, [*where, child.name], shadowed)
            elif not where and isinstance(child, (ast.Assign, ast.AnnAssign)):
                target = child.targets[0] if isinstance(child, ast.Assign) else child.target
                name = [target.id] if isinstance(target, ast.Name) else []
                visit(child, name, shadowed)
                if name:
                    note(target, name, shadowed)
            else:
                note(child, where, shadowed)
                visit(child, where, shadowed)

    visit(tree, [], set())
    return uses


def _defined(source: str) -> set[str]:
    """The names a part can have: each top-level function, class and module-level name, and
    each method as ``Class.method``."""
    names = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        if isinstance(node, ast.ClassDef):
            names |= {
                f"{node.name}.{member.name}"
                for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        if isinstance(node, ast.Assign):
            names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _in_part(use: _Use, parts: Set[str]) -> bool:
    return any(use.where == part or use.where.startswith(f"{part}.") for part in parts)


def _outside_parts(source: str, module: str, specific: set[str], parts: frozenset[str]) -> list:
    """The uses of a module with cfc-specific parts that stand outside those parts."""
    return [
        use
        for use in _uses(source, module, specific)
        if not (use.imports and not use.where) and not _in_part(use, parts)
    ]


# --------------------------------------------------------------------------- #
# The list and the modules.
# --------------------------------------------------------------------------- #


def _modules_of(kind: str) -> list[str]:
    return sorted(module for module, listed in _classification().items() if listed.kind == kind)


def _specific() -> set[str]:
    return set(_modules_of(SPECIFIC))


def _source(module: str) -> str:
    return _modules()[module].read_text(encoding="utf-8")


def test_agents_md_classifies_every_module_and_names_no_other():
    listed = _classification()
    modules = _modules()
    assert sorted(set(modules) - set(listed)) == [], "classify these modules in AGENTS.md"
    assert sorted(set(listed) - set(modules)) == [], "AGENTS.md lists modules that do not exist"
    assert {entry.kind for entry in listed.values()} <= {GENERIC, SPECIFIC, MIXED}


@pytest.mark.parametrize("module", _modules_of(GENERIC))
def test_a_generic_module_holds_no_cfc_iri_and_imports_no_cfc_specific_module(module):
    assert _uses(_source(module), module, _specific()) == []


@pytest.mark.parametrize("module", _modules_of(SPECIFIC) + _modules_of(MIXED))
def test_a_module_listed_as_speaking_cfc_still_does(module):
    """When one stops, list it as generic, so the list keeps telling the truth."""
    assert _uses(_source(module), module, _specific()) != [], f"list {module} as generic"


@pytest.mark.parametrize("module", _modules_of(MIXED))
def test_a_module_with_cfc_specific_parts_speaks_cfc_only_inside_them(module):
    parts = _classification()[module].parts
    source = _source(module)
    assert parts, f"AGENTS.md names no cfc-specific part of {module}"
    assert sorted(parts - _defined(source)) == [], f"{module} defines no such part"
    assert _outside_parts(source, module, _specific(), parts) == []


@pytest.mark.parametrize("module", _modules_of(MIXED))
def test_each_listed_part_still_speaks_cfc(module):
    """A part that stops speaking `cfc:` comes off the list, so the list stays the truth."""
    parts = _classification()[module].parts
    uses = _uses(_source(module), module, _specific())
    assert sorted(part for part in parts if not any(_in_part(use, {part}) for use in uses)) == []


# --------------------------------------------------------------------------- #
# The check itself, on sources written for it.
# --------------------------------------------------------------------------- #

POSSESSION = "kapps_semantic_middleware.possession"


@pytest.mark.parametrize(
    "source",
    [
        "from kapps_semantic_middleware.vocabulary import CFC\nX = CFC.Workpiece\n",
        "X = 'https://w3id.org/circularfactory/Core#Workpiece'\n",
        "QUERY = f'SELECT ?s WHERE {{ ?s cfc:hasPossessor ?o }}'\n",
        "from kapps_semantic_middleware.possession import workpiece\n",
        "from .possession import workpiece\n",
        "import kapps_semantic_middleware.possession\n",
    ],
)
def test_the_check_finds_a_cfc_iri_or_a_cfc_specific_import(source):
    assert _uses(source, "kapps_semantic_middleware.commit", {POSSESSION}) != []


def test_the_check_skips_prose():
    source = '"""Possession is ``cfc:PossessionState``."""\n\ndef f():\n    """cfc:x"""\n'
    assert _uses(source, "kapps_semantic_middleware.commit", set()) == []


def test_the_check_skips_a_parameter_with_the_name_of_an_import():
    source = (
        "from kapps_semantic_middleware.possession import workpiece\n"
        "def move(workpiece):\n"
        "    return workpiece\n"
    )
    uses = _uses(source, "kapps_semantic_middleware.middleware", {POSSESSION})
    assert [use.where for use in uses] == [""]  # the import alone


def test_a_cfc_term_outside_the_listed_parts_is_found():
    source = (
        "from kapps_semantic_middleware.vocabulary import CFC\n"
        "class Middleware:\n"
        "    def handover(self):\n"
        "        return CFC.hasPossessor\n"
        "    def create(self):\n"
        "        return CFC.Workpiece\n"
    )
    module = "kapps_semantic_middleware.middleware"
    outside = _outside_parts(source, module, set(), frozenset({"Middleware.handover"}))
    assert [use.where for use in outside] == ["Middleware.create"]
