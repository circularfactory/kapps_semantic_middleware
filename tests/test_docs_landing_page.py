"""The docs site's landing page is the README: ``conf.py`` hands it to Sphinx in place of the index.

As in ``test_docs_footer_from_license.py``, this executes only the landing-page part of
``docs/site/conf.py``, taken out of the source with ``ast``: running the whole file copies the
scenario notebooks into the docs tree.
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_SITE = _ROOT / "docs" / "site"
_CONF = _SITE / "conf.py"

_PUBLIC = "https://github.com/circularfactory/kapps_semantic_middleware"
_RAW = "https://raw.githubusercontent.com/circularfactory/kapps_semantic_middleware"

# The names of the landing-page part of conf.py.
_LANDING = {
    "_ROOT",
    "_REPOSITORY",
    "_INLINE_TARGET",
    "_URL_SCHEME",
    "_TOCTREE",
    "_public_target",
    "_landing_page",
    "_readme_as_landing_page",
}

# Every page the hand-written index listed in its toctree, so in the navigation bar.
_PAGES = ["concepts", "guide/index", "scenarios/index", "errors", "reference/index"]


def _is_landing_page_part(node: ast.stmt) -> bool:
    """A statement of the landing-page part of conf.py, or one of the two imports it needs."""
    if isinstance(node, ast.Import):
        return any(alias.name == "re" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.module == "pathlib"
    if isinstance(node, ast.Assign):
        return any(
            isinstance(target, ast.Name) and target.id in _LANDING
            for target in node.targets
        )
    return isinstance(node, ast.FunctionDef) and node.name in _LANDING


def _load_landing_page(release: str = "9.9.9") -> dict:
    tree = ast.parse(_CONF.read_text(encoding="utf-8"))
    wanted = [node for node in tree.body if _is_landing_page_part(node)]
    namespace: dict = {"__file__": str(_CONF), "release": release}
    exec(  # noqa: S102
        compile(ast.Module(body=wanted, type_ignores=[]), str(_CONF), "exec"), namespace
    )
    missing = _LANDING - namespace.keys()
    assert not missing, f"conf.py does not define {sorted(missing)}"
    return namespace


@pytest.fixture(scope="module")
def conf():
    return _load_landing_page()


def test_index_is_a_stub_that_names_the_readme():
    assert (_SITE / "index.md").read_text(encoding="utf-8") == (
        "# kapps-semantic-middleware\n"
        "\n"
        "The content of this page is the repository's `README.md`, which `conf.py` reads in\n"
        "when the site is built.\n"
    )


def test_sphinx_reads_the_readme_in_place_of_the_index(conf):
    source = ["the stub"]
    conf["_readme_as_landing_page"](None, "index", source)

    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert source[0].startswith(readme.splitlines()[0] + "\n")
    assert "## Questions" in source[0]


def test_other_pages_are_read_as_they_are(conf):
    source = ["# Concepts\n"]
    conf["_readme_as_landing_page"](None, "concepts", source)
    assert source == ["# Concepts\n"]


def test_a_relative_link_points_at_the_public_repository_at_the_release_tag(conf):
    page = conf["_landing_page"]()

    assert f"[`AGENTS.md`]({_PUBLIC}/blob/v9.9.9/AGENTS.md)" in page
    assert "](AGENTS.md)" not in page
    assert "<http://localhost:7200>" in page


def test_link_rewriting_leaves_urls_anchors_and_code_alone(tmp_path):
    conf = _load_landing_page("1.2.3")
    conf["_ROOT"] = tmp_path
    (tmp_path / "README.md").write_text(
        "# t\n"
        "\n"
        "[a](CHANGELOG.md) ![b](docs/x.png) [c](https://example.org/) [d](#questions)\n"
        "\n"
        "```text\n"
        "[e](inside/a/fence.md)\n"
        "```\n",
        encoding="utf-8",
    )

    page = conf["_landing_page"]()

    assert f"[a]({_PUBLIC}/blob/v1.2.3/CHANGELOG.md)" in page
    assert f"![b]({_RAW}/v1.2.3/docs/x.png)" in page
    assert "[c](https://example.org/)" in page
    assert "[d](#questions)" in page
    assert "[e](inside/a/fence.md)" in page


def test_the_navigation_keeps_every_page_of_the_hand_written_index(conf):
    toctree = conf["_TOCTREE"]
    entries = [line.strip() for line in toctree.splitlines()]

    assert ":hidden:" in entries
    assert [entry for entry in entries if entry in _PAGES] == _PAGES
    for page in _PAGES:
        assert (_SITE / f"{page}.md").is_file(), page
