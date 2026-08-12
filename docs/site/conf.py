"""Sphinx configuration for the kapps-semantic-middleware documentation site.

The decisions this file enacts are recorded on #116:

- **Sphinx, not MkDocs**, because a real share of this site comes from ``src/``
  (117 of 127 public symbols carry a docstring).
- **pydata-sphinx-theme**, whose version switcher consumes the
  ``v/<version>/`` + ``latest/`` layout the circularfactory org already serves.
- **Markdown authoring via myst-nb**, which also renders the scenario notebooks.

This config is meant to be **vendored** into ``kapps_ogm`` and
``kapps_triplestore_interface`` with only the ``project``/``release`` block and
the reference pages changed -- the same per-repo rule #110 applied to the
release scripts, and for the same reason: the three release at their own pace.

One exception to that, for #137: the scenario-notebook block below is specific
to this repository, which is the only one of the three that has scenario
notebooks. A vendored copy drops it, rather than carrying a step that would
raise on a repository with no ``examples/``.
"""

import shutil
from importlib.metadata import version as _installed_version
from pathlib import Path

# -- The scenario notebooks -------------------------------------------------

# The two scenario pages ARE their notebooks. #135 put the explanation into the
# markdown cells rather than around them, so a separate page would do nothing but
# restate the first cell, and the two copies would drift. Sphinx cannot read a
# source file from outside its source directory, so the notebooks are copied in
# here -- before Sphinx enumerates sources, which is why this runs at import time
# rather than from ``setup(app)``.
#
# The copies are generated, so they are gitignored: #116 requires that nothing
# generated enters git, which is what keeps #110's one-commit-per-release honest.
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
copyright = "2026, Etienne Hoffmann"  # noqa: A001 - Sphinx requires this name

release = _installed_version("kapps-semantic-middleware")
version = ".".join(release.split(".")[:2])

# -- Extensions -------------------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    # 7 call sites use Google-style `Args:` sections; without napoleon they
    # render as a literal block instead of a parameter table.
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    # The landing page's card grid.
    "sphinx_design",
    # myst_nb registers myst_parser itself. Loading BOTH raises
    # "extension myst_parser is already registered" -- so only this one.
    "myst_nb",
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

# The scenario notebooks need a reachable GraphDB and an MQTT broker. A docs
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
    # time fails the very first release and every local build; the publish
    # workflow regenerates it from the tag list anyway.
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
