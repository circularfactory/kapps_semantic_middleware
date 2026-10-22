"""The text files that the name guards scan, and the Unreleased section of the changelog."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCANNED_DIRS = (
    "src",
    "docs",
    "examples",
    "demo",
    "scripts",
    "tests",
    "tests_adr",
    "tests_release",
    "release",
)
SKIPPED_NAMES = {"uv.lock"}
SKIPPED_PARTS = {
    "__pycache__",
    ".venv",
    "node_modules",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
}


def _is_text(path: Path) -> bool:
    if path.name in SKIPPED_NAMES or SKIPPED_PARTS & set(path.parts):
        return False
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    return True


def text_files() -> Iterator[Path]:
    for entry in SCANNED_DIRS:
        for path in sorted((REPO_ROOT / entry).rglob("*")):
            if path.is_file() and _is_text(path):
                yield path
    for path in sorted(REPO_ROOT.glob("*")):
        if path.is_file() and _is_text(path):
            yield path


def unreleased_changelog_lines() -> list[str]:
    lines = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("## Unreleased")), None
    )
    if start is None:
        return []
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return lines[start:end]
