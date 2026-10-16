"""Sphinx configuration for the kapps-semantic-middleware documentation site.

The decisions this file enacts:

- **Sphinx, not MkDocs**, because a real share of this site comes from ``src/``
  (117 of 127 public symbols carry a docstring).
- **pydata-sphinx-theme**, whose version switcher consumes the
  ``v/<version>/`` + ``latest/`` layout the circularfactory org already serves.
- **Markdown authoring via myst-nb**, which also renders the scenario notebooks.
- **The landing page is the README.** ``index.md`` is a stub, and ``setup()`` below hands
  Sphinx ``README.md`` in its place while it reads that page. The page is the artefact: the two
  cannot drift, and a README edit is a published-page edit. A link the README writes relative
  to the repository root is pointed at the same file in the public repository, at this
  release's tag, because a page on the documentation site has no repository root beside it.

This config is meant to be **vendored** into ``kapps_ogm`` and
``kapps_triplestore_interface`` with only the ``project``/``release`` block, the reference
pages and the pages the landing page's toctree lists changed -- the same per-repo rule the
release data under ``release/`` follows, and for the same reason: the three release at their
own pace.

One exception to that: the scenario-notebook block below is specific
to this repository, which is the only one of the three that has scenario
notebooks. A vendored copy drops it, rather than carrying a step that would
raise on a repository with no ``examples/``.

The footer's copyright text is read from the ``LICENSE`` two directories above this file, so a
vendored copy needs a ``Copyright (c) <year> <holder>`` line there, or the build raises.
"""

import re
import shutil
from importlib.metadata import version as _installed_version
from pathlib import Path

# -- The scenario notebooks -------------------------------------------------

# The two scenario pages ARE their notebooks. The explanation lives in their
# markdown cells rather than around them, so a separate page would do nothing but
# restate the first cell, and the two copies would drift. Sphinx cannot read a
# source file from outside its source directory, so the notebooks are copied in
# here -- before Sphinx enumerates sources, which is why this runs at import time
# rather than from ``setup(app)``.
#
# The copies are generated, so they are gitignored: nothing generated enters
# git, which is what keeps the one-commit-per-release rule honest.
# ``examples/`` ships to the release repo (the allowlist admits it), so the copy
# finds its source there too.
_NOTEBOOK_PAGES = {
    "scenario1_hello_world.ipynb": "scenarios/hello-world.ipynb",
    "scenario2_door.ipynb": "scenarios/door.ipynb",
}


def _copy_notebook_pages() -> None:
    here = Path(__file__).parent
    examples = here.parent.parent / "examples"
    for source_name, destination in _NOTEBOOK_PAGES.items():
        source = examples / source_name
        if not source.is_file():
            raise FileNotFoundError(
                f"{source} is missing, so the scenario pages cannot be built. "
                "The docs build reads the notebooks from examples/."
            )
        shutil.copyfile(source, here / destination)


_copy_notebook_pages()

# -- Project ----------------------------------------------------------------

project = "kapps-semantic-middleware"
author = "Etienne Hoffmann"


def _copyright_from_license(license_path: Path) -> str:
    """The footer's copyright text, read from the copyright lines of the LICENSE.

    ``Copyright (c) 2026 Etienne Hoffmann`` becomes ``2026, Etienne Hoffmann``, the form Sphinx
    prints. The LICENSE is the only place the holder and year are written; several lines join
    with ``; ``.
    """
    entries = re.findall(
        r"^Copyright(?: \(c\))? (\S+) (.+?)\s*$",
        license_path.read_text(encoding="utf-8"),
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if not entries:
        raise ValueError(
            f"{license_path} has no 'Copyright (c) <year> <holder>' line, so the docs footer "
            "cannot be derived from it."
        )
    return "; ".join(f"{year}, {holder}" for year, holder in entries)


copyright = _copyright_from_license(  # noqa: A001 - Sphinx requires this name
    Path(__file__).parent.parent.parent / "LICENSE"
)

release = _installed_version("kapps-semantic-middleware")
version = ".".join(release.split(".")[:2])

# The public repository, which the README's relative links resolve against.
_REPOSITORY = "circularfactory/kapps_semantic_middleware"

# -- Extensions -------------------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    # 7 call sites use Google-style `Args:` sections; without napoleon they
    # render as a literal block instead of a parameter table.
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    # The guide pages' dropdowns.
    "sphinx_design",
    # myst_nb registers myst_parser itself. Loading BOTH raises
    # "extension myst_parser is already registered" -- so only this one.
    "myst_nb",
    # The error map's diagram, written as a ```{mermaid} block. The default output, "raw",
    # needs no mermaid-cli on the runner: the page loads mermaid.js and draws it in the browser.
    "sphinxcontrib.mermaid",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "**.ipynb_checkpoints"]

# -- autodoc ----------------------------------------------------------------

autodoc_default_options = {
    "members": True,
    "show-inheritance": True,
    # Source order, not alphabetical: these modules are written to be read
    # top to bottom, and alphabetising them scrambles that.
    "member-order": "bysource",
}
# Types stay in the signature, so a reader sees the shape of a call without
# scrolling down to the parameter table.
autodoc_typehints = "signature"

# -- MyST / notebooks -------------------------------------------------------

myst_enable_extensions = ["colon_fence", "deflist", "fieldlist"]

# The scenario notebooks need a reachable triple store and an MQTT broker. A docs
# runner has neither, so stored outputs are rendered and nothing is executed.
# This is what lets the docs build stay independent of docker and of stream A.
nb_execution_mode = "off"

# -- intersphinx ------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# -- HTML -------------------------------------------------------------------

html_theme = "pydata_sphinx_theme"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_title = project

html_theme_options = {
    "logo": {
        # The SFB1574 mark, taken from the org's own site repo
        # (circularfactory.github.io, v/0.1/SFB_Logo.jpg) and resized. One
        # asset serves both themes: the mark carries its own white ground, so
        # custom.css gives it a matching chip in dark mode rather than a
        # recoloured variant of somebody else's logo.
        "image_light": "_static/sfb-logo.png",
        "image_dark": "_static/sfb-logo.png",
        "alt_text": "SFB 1574 Circular Factory",
        "text": project,
    },
    "navbar_end": ["theme-switcher", "version-switcher", "navbar-icon-links"],
    "switcher": {
        "json_url": (
            "https://circularfactory.github.io/kapps_semantic_middleware"
            "/latest/_static/switcher.json"
        ),
        "version_match": release,
    },
    # The switcher JSON is served by the site being built. Checking it at build
    # time fails the very first release and every local build. Publishing the
    # docs rebuilds it from the published versions anyway.
    "check_switcher": False,
    "show_version_warning_banner": True,
    "icon_links": [
        {
            "name": "GitHub",
            "url": "https://github.com/circularfactory/kapps_semantic_middleware",
            "icon": "fa-brands fa-github",
        },
        {
            "name": "PyPI",
            "url": "https://pypi.org/project/kapps-semantic-middleware/",
            "icon": "fa-brands fa-python",
        },
    ],
}

# -- The landing page is the README ------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent.parent

# A Markdown link or image with an inline target: `[text](target)` or `![alt](target)`.
_INLINE_TARGET = re.compile(r"(!?\[[^\]\n]*\]\()([^)\s]+)(\))")
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")

# The README has no toctree of its own, so the site's pages are attached here. Hidden:
# the navigation bar lists them, and the README reads as it does on GitHub.
_TOCTREE = """

```{toctree}
:hidden:
:maxdepth: 2

concepts
guide/index
scenarios/index
errors
reference/index
```
"""


def _public_target(match: re.Match[str]) -> str:
    opening, target, closing = match.groups()
    if _URL_SCHEME.match(target) or target.startswith(("#", "/")):
        return match.group(0)
    if opening.startswith("!"):
        url = f"https://raw.githubusercontent.com/{_REPOSITORY}/v{release}/{target}"
    else:
        url = f"https://github.com/{_REPOSITORY}/blob/v{release}/{target}"
    return f"{opening}{url}{closing}"


def _landing_page() -> str:
    lines, fenced = [], False
    for line in (_ROOT / "README.md").read_text(encoding="utf-8").splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        lines.append(line if fenced else _INLINE_TARGET.sub(_public_target, line))
    return "".join(lines) + _TOCTREE


def _readme_as_landing_page(app, docname, source):
    if docname == "index":
        source[0] = _landing_page()


def setup(app):
    app.connect("source-read", _readme_as_landing_page)
