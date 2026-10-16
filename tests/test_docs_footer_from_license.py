"""The docs footer's copyright text follows the LICENSE (the config derives it, nothing copies it).

``docs/site/conf.py`` stays one file, because it is vendored to the sibling projects, and
running it copies the scenario notebooks into the docs tree. A test must not write there, so this
one takes the helper's definition out of the source with ``ast`` and executes only that.
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_CONF = _ROOT / "docs" / "site" / "conf.py"


def _load_helper():
    tree = ast.parse(_CONF.read_text(encoding="utf-8"))
    wanted = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.FunctionDef) and node.name == "_copyright_from_license"
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name == "re" for alias in node.names)
        )
        or (isinstance(node, ast.ImportFrom) and node.module == "pathlib")
    ]
    namespace: dict = {}
    exec(
        compile(ast.Module(body=wanted, type_ignores=[]), str(_CONF), "exec"), namespace
    )  # noqa: S102
    return namespace["_copyright_from_license"]


@pytest.fixture(scope="module")
def helper():
    return _load_helper()


def test_footer_text_is_what_the_license_gives(helper):
    assert helper(_ROOT / "LICENSE") == "2026, Etienne Hoffmann"


def test_footer_follows_a_changed_license(helper, tmp_path):
    license_file = tmp_path / "LICENSE"
    license_file.write_text(
        "MIT License\n\nCopyright (c) 2031 Someone Else\n\nPermission...\n"
    )
    assert helper(license_file) == "2031, Someone Else"


def test_several_copyright_lines_join(helper, tmp_path):
    license_file = tmp_path / "LICENSE"
    license_file.write_text("Copyright (c) 2026 A B\nCopyright (c) 2027 C D\n")
    assert helper(license_file) == "2026, A B; 2027, C D"


def test_license_without_copyright_line_is_refused(helper, tmp_path):
    license_file = tmp_path / "LICENSE"
    license_file.write_text("MIT License\n")
    with pytest.raises(ValueError, match="no 'Copyright"):
        helper(license_file)
