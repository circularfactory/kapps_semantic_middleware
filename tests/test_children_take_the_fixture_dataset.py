"""A process that a test starts joins the dataset of the test's fixture, never a constant.

The live suite names its dataset once, in ``conftest.py``'s ``TEST_DATASET``, and every
test that needs it reaches it through the ``store`` fixture. A child process cannot take the
fixture: it gets a dataset name on its command line. The stop test once gave its hello child
the constant instead of the fixture's dataset. Both are ``Tests`` in a normal run, so nothing
showed it; but a run that pointed the fixture at a disposable dataset still had the child
register and deregister a hello Service in the shared ``Tests``.

Offline: these tests read a command line and the test sources, and reach no store.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import _hello_process

TESTS = Path(__file__).resolve().parent

DISPOSABLE = "ogm-disposable-probe"
"""A dataset name that no constant of the suite holds, so a child that shows it took the
fixture's."""


def test_the_hello_child_joins_the_dataset_of_the_fixture() -> None:
    """The command line the stop test starts, read back by the child's own parser."""
    fixture = SimpleNamespace(dataset=DISPOSABLE)  # the one attribute command() reads

    argv = _hello_process.command(fixture, 8983, activity_feed=True)

    assert argv[1] == str(Path(_hello_process.__file__).resolve())
    args = _hello_process.parse_args(argv[2:])
    assert args.dataset == DISPOSABLE
    assert args.port == 8983
    assert args.activity_feed


def _referenced_names(path: Path) -> set[str]:
    """Every name that ``path`` imports, reads or binds; strings and comments do not count."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
            names.add(node.name)
    return names


def test_only_the_fixture_reads_the_test_dataset_constant() -> None:
    """Every test takes its dataset from the ``store`` fixture, and so does every child.

    A runner can point the live suite at another dataset in two ways: by editing the
    constant, or by overriding the fixture. An edit moves a reader of the constant as well; an
    override does not, and that is how the stop test's child stayed on ``Tests``. With the
    fixture as the constant's only reader, either way moves every test and every child.
    """
    readers = sorted(
        path.name
        for path in TESTS.glob("*.py")
        if path.name != "conftest.py" and "TEST_DATASET" in _referenced_names(path)
    )

    assert readers == [], (
        f"{readers} read TEST_DATASET. Take the dataset from the store fixture "
        "(store.dataset), and pass that to a child process."
    )


def test_only_default_assertions_read_the_demo_dataset_constant() -> None:
    """A test that names ``DEMO_DATASET`` asserts the default; nothing else takes it.

    The factory launcher takes its dataset on the command line, so a child process or a live test
    takes ``store.dataset``. Only the tests that assert what the default is may name the
    constant.
    """
    readers = sorted(
        path.name for path in TESTS.glob("*.py") if "DEMO_DATASET" in _referenced_names(path)
    )

    assert readers == ["test_credentials.py", "test_launcher_factory.py"], (
        f"{readers} read DEMO_DATASET. Only a test that asserts the default may; a child or a "
        "live test takes store.dataset."
    )
