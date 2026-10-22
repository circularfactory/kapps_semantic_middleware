"""The dashboard marks the selected tab, in a browser.

The rule that highlights a label is in the stylesheet, and only a browser can say whether it
matches: a test of the markup passed while the old rule matched nothing. The instance is served
in this process; the test skips where no browser is installed (see `_browser.py`).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from playwright.sync_api import Page, expect

from kapps_semantic_middleware.dashboard import enable_dashboard
from tests.test_multiline_records_in_a_browser import _serving

pytestmark = pytest.mark.browser


class _Instance:
    def __init__(self) -> None:
        self.app = FastAPI()


def test_the_selected_tab_label_shows_the_accent_and_the_others_do_not(
    page: Page,
) -> None:
    instance = _Instance()
    registry = enable_dashboard(instance, register_default_boxes=False)
    for index, tab in enumerate(("First", "Line monitor")):
        registry.box(id=f"box-{index}", title=tab, tab=tab)(lambda _mw: "<p>x</p>")

    with _serving(instance.app) as address:
        page.goto(f"{address}/dashboard")
        first = page.locator("label.tab-label", has_text="First")
        second = page.locator("label.tab-label", has_text="Line monitor")

        underline = "el => getComputedStyle(el).borderBottomColor"
        colour = "el => getComputedStyle(el).color"
        marked = (first.evaluate(underline), first.evaluate(colour))
        unmarked = (second.evaluate(underline), second.evaluate(colour))
        assert marked != unmarked
        assert marked[0] != "rgba(0, 0, 0, 0)"
        assert unmarked[0] == "rgba(0, 0, 0, 0)"
        assert marked[1] != unmarked[1]

        second.click()
        expect(second).to_have_css("border-bottom-color", marked[0])
        expect(second).to_have_css("color", marked[1])
        expect(first).to_have_css("border-bottom-color", unmarked[0])
        expect(first).to_have_css("color", unmarked[1])
