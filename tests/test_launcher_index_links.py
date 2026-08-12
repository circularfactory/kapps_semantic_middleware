"""Guard test for the index page's links following the browsing host, not a hardcoded
127.0.0.1 (#85). A page reached through an SSH tunnel or IDE port-forward has a different
hostname than the one baked into a child's dynamically-allocated address; the anchor's
href must be rebuilt from `location`, while the box keeps showing the child's real address
and its source (#68) unchanged.

No JS runtime here -- this asserts against the template's source text, the same way
test_launcher_index_guard.py asserts against Python source text.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TEMPLATE = REPO_ROOT / "demo" / "transferunits" / "templates" / "index.html"


def test_href_for_helper_is_defined():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "function hrefFor(" in html, "expected a hrefFor() helper building hrefs from location"


def test_addresses_are_not_used_as_a_raw_href():
    """The child/launcher address must not be interpolated directly into an href -- it
    must be passed through hrefFor() first."""
    html = TEMPLATE.read_text(encoding="utf-8")

    assert 'href="${node.address}"' not in html
    assert 'href="${s.launcher.address}"' not in html


def test_box_and_launcher_links_go_through_href_for():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "href=\"${hrefFor(node.address)}\"" in html
    assert "href=\"${hrefFor(s.launcher.address)}\"" in html


def test_the_displayed_address_text_is_still_the_real_one():
    """Only the href changes -- the visible text and the source chip must still show the
    child's real address (#68)."""
    html = TEMPLATE.read_text(encoding="utf-8")

    assert ">${node.address}<" in html
    assert 'data-src="${node.source}"' in html
