"""The middleware names no triple store vendor, outside its deprecated names.

The triple store border of the KAPPS family: no layer above the triple store interface names a
vendor, not in its types, its errors, how it builds a store, or its texts. A new backend then
changes only the interface. This module checks the middleware's side of it, offline.

**The terms.** Each is matched in any case.

- ``GraphDB``: the vendor's store. The match covers ``GRAPHDB_``, the prefix of the deprecated
  variables, and every name built on it, such as ``graphdb_for``.
- ``Ontotext``: the vendor.
- ``SHACLShapeGraph``: the local name of RDF4J's shapes-graph IRI, the graph in which a GraphDB
  dataset keeps its SHACL shapes. A caller sets the shapes with the store's ``shapes_set``.
- ``repository``, ``repositories``: the store's unit, which the border calls a *dataset*. A git
  repository is not a vendor term. Each line that means one is listed in ``GIT_SENSE``, by its
  file and a fragment of the line.

**What is scanned.**

- Every file under ``SCANNED``, its contents and its path: the package (its code, its names, its
  environment variables, its CLI output and its dashboard labels are in its source), the
  demonstration, the examples, the shipped docs, the manifest, the example environment and the
  tests that ship. The suites of the records and of the release data are not scanned: they do
  not ship, and the first lists the members of the interface's adapter by name.
- The importable names of the package and their signatures: every public name that a module of
  the package defines, and each parameter of each public function, class and method.

**What is exempt.**

- The decision records: each file in a directory ``adr`` inside a directory ``docs``. They do
  not ship.
- The paths in ``EXEMPT``: the store reports that the tests recorded (they stay byte for byte),
  the local Docker store's compose file and configuration, the test that starts that stack, the
  changelog (the record of each release, and the deprecation note), and this module, which names
  the terms.
- In the files of ``REGIONS``, the lines from a ``vendor-check: off`` marker to the next
  ``vendor-check: on``: the deprecated names and variables, the deprecation note, and the texts
  that start or describe the local Docker store. A marker in any other file fails.
- In the importable names, the deprecated names in ``DEPRECATED`` and their parameters.

The contract release removes the deprecated names and the deprecated variables, and with them
most of the regions.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import kapps_semantic_middleware

REPO_ROOT = Path(__file__).resolve().parents[1]

SCANNED = (
    "src",
    "demo",
    "examples",
    "docs/mechanics",
    "docs/site",
    "tests",
    "README.md",
    "AGENTS.md",
    "CONTEXT-MAP.md",
    "CHANGELOG.md",
    "pyproject.toml",
    ".env.example",
)

VENDOR = re.compile(r"graphdb|ontotext|shaclshapegraph", re.IGNORECASE)
REPOSITORY = re.compile(r"repositor(?:y|ies)", re.IGNORECASE)

EXEMPT = {
    "tests/reports/": "the store reports that the tests recorded, byte for byte",
    "examples/docker/": "the local Docker store: its compose file and its configuration",
    "tests/test_shipped_stack_validates_shapes.py": "starts the local Docker store and checks its configuration",
    "CHANGELOG.md": "the record of each release, and the deprecation note",
    "tests/test_names_no_vendor.py": "this check, which names the terms",
}

REGIONS = {
    "src/kapps_semantic_middleware/credentials.py": "the deprecated names and variables",
    "src/kapps_semantic_middleware/seeding.py": "the deprecated name",
    "tests/test_credentials.py": "the tests of the deprecated names and variables, and the local Docker store's configuration",
    "tests/test_seeding.py": "the test of the deprecated name",
    "demo/transferunits/launcher.py": "the deprecated variables, which a PLC must not receive",
    "tests/test_launcher_seeding.py": "the deprecated variables, which a PLC must not receive",
    "src/kapps_semantic_middleware/examples_cli.py": "the texts that start and describe the local Docker store, and the name of its configuration",
    "README.md": "the local Docker store's setup steps, and the deprecation note",
    "docs/site/guide/08-provisioning-and-seeding.md": "the deprecation note",
    "tests/test_examples_cli.py": "the local Docker store's compose file and its configuration",
    "examples/CONTEXT.md": (
        "a paragraph that a rewrite of release/edits.toml matches byte for byte; the release's "
        "parity proof freezes its output, so rewording it means measuring that proof again"
    ),
}

OFF = "vendor-check: off"
ON = "vendor-check: on"

GIT_SENSE: dict[str, tuple[str, ...]] = {
    "README.md": (
        "This repository is the development side",
        "is a separate repository carrying",
        # The release replaces the section of these two with the two below.
        "Development happens in a private repository",
        "issues on this repository",
    ),
    "AGENTS.md": ("development-repository material",),
    "CONTEXT-MAP.md": ("development-repository material",),
    "src/kapps_semantic_middleware/CONTEXT.md": ("repository; those records are not distributed",),
    "demo/transferunits/CONTEXT.md": (
        "five contexts in this repository",
        "development-repository material",
    ),
    "demo/transferunits/README.md": ("three sibling repositories",),
    "examples/__init__.py": ("at the repository root",),
    "docs/site/index.md": ("is the repository's",),
    "tests/conftest.py": ("each build a throwaway repository to plant a violation in",),
    "tests/test_docs_landing_page.py": (
        '"_REPOSITORY",',
        "The content of this page is the repository's",
        "points_at_the_public_repository_at_the_release_tag",
    ),
    "pyproject.toml": (
        "and the repository root on the path unchanged",
        "They used to sit at the repository root",
    ),
    "docs/site/conf.py": (
        "the same file in the public repository",
        "has no repository root beside it",
        "to this repository, which is the only one",
        "raise on a repository with no",
        "The public repository, which the README",
        "_REPOSITORY",
    ),
}
"""The lines that mean a git repository, by file: a fragment of each line.

Some lines are those that the release writes into the public tree, in place of a section that
does not ship. ``release/edits.toml`` holds them in the development tree."""

RELEASE_EDITS = REPO_ROOT / "release" / "edits.toml"
"""The release's edits of the public tree. ``release/`` never ships, so a public tree has none."""

DEPRECATED = {
    "kapps_semantic_middleware.credentials": {
        "graphdb_for",
        "graphdb_env_present",
        "credentials_for",
    },
    "kapps_semantic_middleware.seeding": {"clear_repository"},
}
"""The deprecated functions of the package, by module. The deprecated constants are not
attributes of their module, so the scan of the names does not see them."""


def _exempt(relative: str) -> bool:
    parts = relative.split("/")
    record = any(parts[i : i + 2] == ["docs", "adr"] for i in range(len(parts) - 1))
    return record or any(relative == path or relative.startswith(path) for path in EXEMPT)


def _files() -> list[Path]:
    found: list[Path] = []
    for entry in SCANNED:
        path = REPO_ROOT / entry
        candidates = [path] if path.is_file() else sorted(path.rglob("*"))
        for candidate in candidates:
            relative = candidate.relative_to(REPO_ROOT).as_posix()
            if not candidate.is_file() or _exempt(relative):
                continue
            if "__pycache__" in candidate.parts or ".ipynb_checkpoints" in candidate.parts:
                continue
            found.append(candidate)
    return found


def _offences_in(relative: str, text: str) -> list[str]:
    """Each line of ``text`` that names a term and no exemption covers."""
    offences: list[str] = []
    git_sense = GIT_SENSE.get(relative, ())
    off = False
    for number, line in enumerate(text.splitlines(), start=1):
        if OFF in line or ON in line:
            if relative not in REGIONS:
                offences.append(f"{relative}:{number}: a marker outside REGIONS")
            off = OFF in line
            continue
        if off:
            continue
        if VENDOR.search(line):
            offences.append(f"{relative}:{number}: {line.strip()}")
        elif REPOSITORY.search(line) and not any(fragment in line for fragment in git_sense):
            offences.append(f"{relative}:{number}: {line.strip()}")
    if off:
        offences.append(f"{relative}: a region that no `{ON}` ends")
    return offences


def test_no_text_names_the_vendor_or_its_unit():
    offences: list[str] = []
    for path in _files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if VENDOR.search(relative) or REPOSITORY.search(relative):
            offences.append(f"{relative}: the path")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # an image or a font
        offences.extend(_offences_in(relative, text))
    assert not offences, (
        "These lines name a triple store vendor or call the store's unit a repository. Say "
        "'the store' and 'dataset', or exempt the line in this module, with its reason:\n"
        + "\n".join(offences)
    )


def test_each_exemption_still_exempts_something():
    """An exemption that matches nothing is a stale one, and hides the next real offence."""
    for path in EXEMPT:
        assert (REPO_ROOT / path).exists(), f"EXEMPT names {path}, which does not exist"
    for path in REGIONS:
        assert OFF in (REPO_ROOT / path).read_text(encoding="utf-8"), (
            f"REGIONS names {path}, which has no region"
        )
    if not RELEASE_EDITS.is_file():
        return  # A public tree: the release rewrote some of the lines that GIT_SENSE names.
    edits = RELEASE_EDITS.read_text(encoding="utf-8")
    for path, fragments in GIT_SENSE.items():
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        for fragment in fragments:
            assert fragment in text or fragment in edits, (
                f"GIT_SENSE names {fragment!r}, which neither {path} nor the release's edits hold"
            )


def _modules() -> list[str]:
    package = kapps_semantic_middleware
    names = [package.__name__]
    for module in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
        names.append(module.name)
    return names


def _own(module_name: str, obj: object) -> bool:
    """Whether ``obj`` is defined in the module, not imported into it."""
    return getattr(obj, "__module__", module_name) == module_name


def _parameters(obj: object) -> list[str]:
    try:
        return list(inspect.signature(obj).parameters)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []


def test_no_importable_name_or_parameter_names_the_vendor_or_its_unit():
    offences: list[str] = []
    for module_name in _modules():
        module = importlib.import_module(module_name)
        deprecated = DEPRECATED.get(module_name, set())
        for name, obj in vars(module).items():
            if name.startswith("_") or inspect.ismodule(obj) or not _own(module_name, obj):
                continue
            if name in deprecated:
                continue
            where = f"{module_name}.{name}"
            names = [(where, name)]
            if callable(obj):
                names += [(f"{where}({p})", p) for p in _parameters(obj)]
            if inspect.isclass(obj):
                for member, value in vars(obj).items():
                    if member.startswith("_") and member != "__init__":
                        continue
                    names.append((f"{where}.{member}", member))
                    if callable(value):
                        names += [(f"{where}.{member}({p})", p) for p in _parameters(value)]
            offences += [
                place for place, n in names if VENDOR.search(n) or REPOSITORY.search(n)
            ]
    assert not offences, "These names name a vendor or the store's unit:\n" + "\n".join(
        offences
    )
