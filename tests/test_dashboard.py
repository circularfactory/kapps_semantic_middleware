"""Behaviour tests for the state dashboard.

These tests build a fake middleware and exercise the dashboard through a FastAPI
TestClient. No GraphDB and no network. The tests pin the dashboard's contract:
idempotent attach, three routes, default boxes, resilience to a raising box,
escaping, tabs, polling, duplicate-id replacement, and static-file traversal
protection.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kapps_triplestore_interface.utils.utils import (
    convert_multi_bindings_to_python_type,
)
from pydantic import BaseModel
from rdflib import Graph

from kapps_semantic_middleware.dashboard import (
    DashboardRegistry,
    _fragment_for_box,
    _serve_static_file,
    enable_dashboard,
    escape,
    kv_table,
)


class SpawnRequest(BaseModel):
    """The body of the worked example's workflow.

    Declared at module level on purpose: this file postpones its annotations, so FastAPI
    resolves an endpoint's type hints against the module, and a class defined inside a
    test function is not there to be found.
    """

    count: int


class FakeMiddleware:
    """The least middleware the dashboard needs: an app to attach routes to."""

    def __init__(self) -> None:
        self.app = FastAPI()


def _middleware_with_dashboard() -> FakeMiddleware:
    """A fake middleware with the default boxes enabled."""
    mw = FakeMiddleware()
    enable_dashboard(mw)
    return mw


def _spec_ids(registry: DashboardRegistry) -> set[str]:
    """The set of registered box ids."""
    return {spec.id for spec in registry.specs()}


def test_enable_dashboard_attaches_and_is_idempotent() -> None:
    """A second enable call returns the same registry and mounts no new routes."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw)
    assert isinstance(registry, DashboardRegistry)
    assert mw.dashboard is registry

    def dashboard_route_count() -> int:
        return sum(
            1
            for route in mw.app.routes
            if getattr(route, "path", "").startswith("/dashboard")
        )

    first_count = dashboard_route_count()
    registry2 = enable_dashboard(mw)
    assert registry2 is registry
    assert dashboard_route_count() == first_count


def test_the_three_routes_exist() -> None:
    """The dashboard mounts the page, the box fragment, and the static route."""
    mw = FakeMiddleware()
    enable_dashboard(mw)
    paths = {getattr(route, "path", "") for route in mw.app.routes}
    assert "/dashboard" in paths
    assert "/dashboard/box/{box_id}" in paths
    assert "/dashboard/static/{filename}" in paths


def test_shell_renders_with_vendored_script() -> None:
    """The shell page loads and names the pinned vendored htmx script."""
    mw = FakeMiddleware()
    enable_dashboard(mw)
    client = TestClient(mw.app)
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert 'src="dashboard/static/htmx-2.0.4.min.js"' in response.text


def test_each_default_box_renders() -> None:
    """Every default box serves a non-empty fragment."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw)
    client = TestClient(mw.app)
    for spec in registry.specs():
        response = client.get(f"/dashboard/box/{spec.id}")
        assert response.status_code == 200, f"box {spec.id} failed"
        assert response.text.strip() != ""


def test_unknown_box_id_returns_404() -> None:
    """A box id that was never registered gets a 404."""
    mw = FakeMiddleware()
    enable_dashboard(mw)
    client = TestClient(mw.app)
    response = client.get("/dashboard/box/no-such-box")
    assert response.status_code == 404


def test_a_box_that_raises_does_not_break_the_page() -> None:
    """One broken box shows its error and the rest of the page still renders."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw)

    @registry.box(id="broken", title="Broken", tab="System", refresh_seconds=None)
    def broken_box(middleware: Any) -> str:
        raise RuntimeError("boom")

    @registry.box(id="healthy", title="Healthy", tab="System", refresh_seconds=None)
    def healthy_box(middleware: Any) -> str:
        return "<p>fine</p>"

    client = TestClient(mw.app)

    broken_response = client.get("/dashboard/box/broken")
    assert broken_response.status_code == 200
    assert "boom" in broken_response.text

    shell_response = client.get("/dashboard")
    assert shell_response.status_code == 200

    healthy_response = client.get("/dashboard/box/healthy")
    assert healthy_response.status_code == 200
    assert "fine" in healthy_response.text


def test_register_default_boxes_false_gives_empty_registry() -> None:
    """Without default boxes the registry is empty and the shell still serves."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)
    assert len(registry.specs()) == 0
    client = TestClient(mw.app)
    response = client.get("/dashboard")
    assert response.status_code == 200


def test_escape_escapes_html_and_renders_none_as_em_dash() -> None:
    """escape turns angle brackets into entities and None into the em dash."""
    value = "<script>alert(1)</script>"
    escaped = escape(value)
    assert "<script>alert(1)" not in escaped
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in escaped
    assert escape(None) == "&mdash;"


def test_kv_table_escapes_key_and_value() -> None:
    """Both table cells go through escape, so markup cannot leak through."""
    table = kv_table([("<b>key</b>", "<i>value</i>")])
    assert "<b>key</b>" not in table
    assert "&lt;b&gt;key&lt;/b&gt;" in table
    assert "<i>value</i>" not in table
    assert "&lt;i&gt;value&lt;/i&gt;" in table


def test_tabs_put_system_first_and_both_appear() -> None:
    """System sorts first and every tab name is in the shell."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box(id="a", title="A", tab="System", refresh_seconds=None)
    def box_a(middleware: Any) -> str:
        return "<p>a</p>"

    @registry.box(id="b", title="B", tab="Other", refresh_seconds=None)
    def box_b(middleware: Any) -> str:
        return "<p>b</p>"

    tabs = registry.tabs()
    assert tabs[0] == "System"
    assert set(tabs) == {"System", "Other"}

    client = TestClient(mw.app)
    shell = client.get("/dashboard").text
    assert "System" in shell
    assert "Other" in shell


def test_polling_trigger_has_every_only_when_interval_is_set() -> None:
    """The hx-trigger carries 'every' only for a box with a refresh interval."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box(id="static", title="Static", tab="System", refresh_seconds=None)
    def static_box(middleware: Any) -> str:
        return "<p>static</p>"

    @registry.box(id="polling", title="Polling", tab="System", refresh_seconds=5)
    def polling_box(middleware: Any) -> str:
        return "<p>polling</p>"

    client = TestClient(mw.app)
    shell = client.get("/dashboard").text

    assert 'hx-trigger="load"' in shell
    assert "every" not in shell.split('id="box-static"')[1].split(">")[0]
    assert 'hx-trigger="load, every 5s"' in shell


def test_duplicate_id_replaces_earlier_box() -> None:
    """Re-registering an id keeps the count and the later title wins."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box(id="dup", title="First", tab="System", refresh_seconds=None)
    def first_box(middleware: Any) -> str:
        return "<p>first</p>"

    @registry.box(id="dup", title="Second", tab="System", refresh_seconds=None)
    def second_box(middleware: Any) -> str:
        return "<p>second</p>"

    assert len(registry.specs()) == 1
    assert registry.specs()[0].title == "Second"


def test_static_file_served_and_traversal_rejected() -> None:
    """The vendored asset serves as JavaScript and traversal paths 404."""
    mw = FakeMiddleware()
    enable_dashboard(mw)
    client = TestClient(mw.app)

    response = client.get("/dashboard/static/htmx-2.0.4.min.js")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/javascript"

    # Call the guard directly, not through the client. An HTTP client normalises a
    # traversal path before the route sees it, so the client path would not exercise
    # the guard's path-resolution check.
    for path in (
        "../middleware.py",
        "../../pyproject.toml",
        "/etc/passwd",
        "",
        "nope.js",
    ):
        assert _serve_static_file(path).status_code == 404, (
            f"path {path!r} was not rejected"
        )


def test_checked_radio_and_panels_share_a_parent() -> None:
    """The tab radios sit in the same parent as the panels container.

    The shell shows one panel with `#tab-X:checked ~ .panels #panel-X`. `~` is the general
    sibling combinator, so it matches only while the radio and `.panels` share a parent. Put
    the radio inside the tab bar and the rule silently stops matching: every panel keeps
    `display: none`, and the page still renders a header and a tab strip, so it looks alive
    and shows nothing. A test that only greps the HTML for box ids cannot see that, because
    the ids are all present in the markup. This test reads the structure instead.
    """
    from html.parser import HTMLParser

    mw = _middleware_with_dashboard()
    shell = TestClient(mw.app).get("/dashboard").text

    class _Structure(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[str] = []
            self.radio_parents: list[str] = []
            self.panels_parents: list[str] = []

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            a = dict(attrs)
            parent = self.stack[-1] if self.stack else ""
            if tag == "input" and "tab-radio" in (a.get("class") or ""):
                self.radio_parents.append(parent)
            if "panels" in (a.get("class") or "").split():
                self.panels_parents.append(parent)
            if tag not in ("input", "meta", "link", "br", "hr"):
                self.stack.append(tag)

        def handle_endtag(self, tag: str) -> None:
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()

    s = _Structure()
    s.feed(shell)

    assert s.radio_parents, "no tab radio found in the shell"
    assert s.panels_parents, "no .panels container found in the shell"
    assert set(s.radio_parents) == set(s.panels_parents), (
        "a tab radio and the .panels container must share a parent, or the `~` selector "
        f"never matches and no panel is ever visible. radios in {set(s.radio_parents)}, "
        f".panels in {set(s.panels_parents)}"
    )


def test_panel_carries_the_grid_class() -> None:
    """Each panel carries `box-grid`, so the boxes lay out as a grid and not a column.

    The stylesheet defines `.box-grid`. A panel that does not carry the class leaves that
    rule applying to nothing, and the boxes stack vertically.
    """
    mw = _middleware_with_dashboard()
    shell = TestClient(mw.app).get("/dashboard").text
    assert 'class="tab-panel box-grid"' in shell, (
        "the panel must carry box-grid, otherwise the grid rule applies to no element"
    )


def _make_fake_spec_node(properties: dict) -> Any:
    """Build a fake spec node with a ``properties`` mapping."""
    return type("FakeSpecNode", (), {"properties": properties})()


def _make_fake_property_spec(nested: Any) -> Any:
    """Build a fake property spec with a nested sub-spec."""
    return type("FakePropertySpec", (), {"nested": nested})()


def test_spec_parameter_without_a_binding_is_still_shown() -> None:
    """A spec parameter with no binding is still shown on the held-data box.

    A parameter present in the spec and pruned, but matched by no binding
    descriptor, must stay visible, because hiding it defeats the projection.
    """
    from kapps_semantic_middleware.vocabulary import INF

    leaf_spec = _make_fake_spec_node(
        {str(INF.accessMode): object(), "hasValue": object()}
    )
    is_occupied_prop = _make_fake_property_spec(leaf_spec)
    temperature_leaf = _make_fake_spec_node(
        {str(INF.accessMode): object(), "hasValue": object()}
    )
    temperature_prop = _make_fake_property_spec(temperature_leaf)

    barrier_nested = _make_fake_spec_node(
        {
            "isOccupied": is_occupied_prop,
            "hasOPCUATemperature": temperature_prop,
        }
    )
    has_light_barrier = _make_fake_property_spec(barrier_nested)

    spec_root = _make_fake_spec_node({"hasLightBarrier": has_light_barrier})

    class FakeBinding:
        parameter_property = "isOccupied"
        resource_iri = "http://example.org/LightBarrier1"
        field_id = "isOccupied_lined"
        access_mode = "READ_WRITE"
        metadata = {}

    class FakePlan:
        # A class body does not close over an enclosing function's local of the same
        # name, so the source is named differently from the attribute.
        northbound_spec = spec_root
        class_scope = None
        bindings = [FakeBinding()]
        registrations = []
        southbound_by_property = {
            "isOccupied": frozenset({"http://example.org/hasMQTTBroker"}),
            "hasOPCUATemperature": frozenset(
                {"http://example.org/hasOPCUAServerEndpoint"}
            ),
        }

    mw = FakeMiddleware()
    mw._wiring = FakePlan()
    enable_dashboard(mw)
    client = TestClient(mw.app)
    response = client.get("/dashboard/box/held-data")
    assert response.status_code == 200
    assert "isOccupied" in response.text
    assert "hasOPCUATemperature" in response.text
    assert "No binding recognized" in response.text

    # The point of the acceptance criterion: the parameter's pruned metadata is shown, not
    # only counted. Asserting the parameter name and the sentence is not enough -- both were
    # present while the disclosure sat below a `continue` and never rendered, so the test
    # agreed with the defect.
    assert "hasOPCUAServerEndpoint" in response.text, (
        "a spec parameter no descriptor matched must still list the properties the "
        "northbound projection stripped, not only their count: that is the case the "
        "projection exists to catch"
    )
    assert "hasMQTTBroker" in response.text, (
        "the bound parameter's pruned metadata must be listed too"
    )


def test_model_names_read_through_schema_and_prefer_the_success_response() -> None:
    """The services box names request and response models from a real OpenAPI shape.

    Three cases, all present in this project's own generated document: a reference, an
    inline schema, and the 422 FastAPI adds to every operation. The reference sits one
    level below the media object, under `schema`, and testing the media object itself for
    `$ref` matched nothing, so both columns showed an em dash for every workflow.
    """
    from kapps_semantic_middleware.dashboard_boxes import _model_name

    # A reference, as `GET /workflows/{name}/description` carries.
    ref_carrier = {
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/WorkflowDescription"}
            }
        }
    }
    assert _model_name(ref_carrier) == "WorkflowDescription"

    # An inline schema, as a workflow returning `Dict[str, str]` carries. This is the
    # response a reader most wants named, and it has no `$ref` at all.
    inline_carrier = {
        "content": {
            "application/json": {
                "schema": {
                    "additionalProperties": {"type": "string"},
                    "type": "object",
                    "title": "Response Execute",
                }
            }
        }
    }
    assert _model_name(inline_carrier) == "Response Execute"

    # No schema at all.
    assert _model_name({"content": {"application/json": {}}}) == "&mdash;"
    assert _model_name(None) == "&mdash;"


def test_the_422_does_not_win_over_the_real_response() -> None:
    """A workflow reports what it returns, not FastAPI's generic validation error.

    Every operation FastAPI generates carries a 422 whose schema references
    `HTTPValidationError`. A loop over every response takes whichever comes first, which
    reported that error for every workflow on the page.
    """
    mw = FakeMiddleware()

    @mw.app.post("/workflows/demo/execute")
    async def demo() -> dict:
        """A demo workflow."""
        return {}

    enable_dashboard(mw)
    fragment = TestClient(mw.app).get("/dashboard/box/services").text
    assert "HTTPValidationError" not in fragment, (
        "the 422 FastAPI adds to every operation must not be reported as the workflow's "
        "response model"
    )


def test_a_tab_name_with_a_space_still_shows_its_panel() -> None:
    """A tab whose title is not a legal identifier still renders a visible panel.

    `UC2 monitor` is the tab the use case adds, so this is reached on the first subproject
    tab. `html.escape` leaves a space alone, so the id became `tab-UC2 monitor` and the
    selector `#tab-UC2 monitor:checked`, which no browser parses. The rule was dropped and
    the panel kept `display: none`, leaving a tab strip above nothing.
    """
    import re as _re

    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)
    for index, tab in enumerate(("UC2 monitor", "A/B", "Etienne's", "5 GHz")):
        # A safe box id on purpose: this test is about the tab title, and an unsafe box id
        # is refused at registration by its own rule.
        registry.box(id=f"box-{index}", title=tab, tab=tab)(lambda _mw: "<p>x</p>")

    shell = TestClient(mw.app).get("/dashboard").text

    panel_ids = _re.findall(r'id="panel-([^"]*)"', shell)
    radio_ids = _re.findall(r'id="tab-([^"]*)"', shell)
    assert panel_ids and radio_ids

    # Every identifier is legal in a CSS selector and an HTML id.
    legal = _re.compile(r"^[a-zA-Z0-9_-]+$")
    for ident in panel_ids + radio_ids:
        assert legal.match(ident), f"{ident!r} is not a legal identifier"

    # Every panel is reachable by a rule that names it, so it can actually become visible.
    for ident in panel_ids:
        assert f"#tab-{ident}:checked ~ .panels #panel-{ident}" in shell, (
            f"panel {ident!r} has no matching :checked rule, so it can never be shown"
        )

    # The reader still sees the original titles.
    for tab in ("UC2 monitor", "A/B", "Etienne&#x27;s", "5 GHz"):
        assert tab in shell


def test_tab_slugs_stay_unique_when_they_collide() -> None:
    """Two different tab names that reduce to the same slug still get distinct ids.

    `A/B` and `A-B` both slug to `a-b`. Without a guard both panels would carry one id and
    the first rule would win for both.
    """
    from kapps_semantic_middleware.dashboard import _tab_slugs

    slugs = _tab_slugs(["A/B", "A-B", "A B"])
    assert len(set(slugs.values())) == 3, f"slugs collided: {slugs}"


def test_a_box_id_that_is_not_url_safe_is_refused() -> None:
    """An unsafe box id is refused at registration, not left to fail silently in a browser.

    A box id becomes an HTML id and a path segment of `/dashboard/box/<id>`. A slash makes
    that URL name a route which does not exist, and nothing reports it: the page renders,
    the slot stays empty, and the reader sees a gap.
    """
    registry = DashboardRegistry()
    for bad in ("a/b", "has space", "quote'", "dot.dot", ""):
        with pytest.raises(ValueError, match="not URL-safe"):
            registry.box(id=bad, title="x")(lambda _mw: "")

    # And the safe shapes still register.
    for good in ("held-data", "box_1", "ABC123"):
        registry.box(id=good, title="x")(lambda _mw: "")
    assert len(registry.specs()) == 3


def test_header_values_are_escaped_once() -> None:
    """A header value passes through one escape, not two.

    `kv_table` escapes every cell. Escaping again in the row list escaped the escape:
    `escape(None)` yields the entity `&mdash;`, the second pass turned its ampersand into
    `&amp;`, and the page showed the literal text `&mdash;`. An IRI holding `&` showed
    `&amp;` for the same reason.
    """
    mw = FakeMiddleware()
    # An absent value, and one holding a character that escaping changes.
    mw.resource_iri = "http://example.org/thing?a=1&b=2"
    mw.service_iri = None
    enable_dashboard(mw)
    fragment = TestClient(mw.app).get("/dashboard/box/header").text

    assert "&amp;mdash;" not in fragment, (
        "an absent value must render as an em dash, not as the literal text `&mdash;`"
    )
    assert "&mdash;" in fragment
    # One escape leaves `&amp;`; two leave `&amp;amp;`.
    assert "&amp;amp;" not in fragment, "the ampersand in an IRI was escaped twice"
    assert "a=1&amp;b=2" in fragment


def test_queue_depth_of_zero_is_shown_as_zero() -> None:
    """An empty queue reads `0`, not an em dash.

    Zero is a measurement and absence is not. A middleware with no queue attribute at all
    has nothing to report, and only that case renders the dash.
    """
    mw = FakeMiddleware()
    mw._operation_queue = []
    enable_dashboard(mw)
    fragment = TestClient(mw.app).get("/dashboard/box/header").text
    assert "<td>0</td>" in fragment, "an empty queue is a depth of zero, not an absence"


def test_hiding_a_box_leaves_the_others_in_registration_order() -> None:
    """Hiding a box does not reorder the boxes that stay."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    for box_id in ("a", "b", "c"):

        @registry.box(box_id, title=box_id.upper())
        def box(middleware: Any, _id: str = box_id) -> str:
            return f"<p>{_id}</p>"

    registry.hide("b")
    assert [spec.id for spec in registry.specs()] == ["a", "c"]


def test_a_hidden_box_is_not_on_the_page_and_its_fragment_is_404() -> None:
    """A hidden box leaves the page, and its fragment route stops answering."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box("visible", title="Visible")
    def visible_box(middleware: Any) -> str:
        return "<p>visible</p>"

    @registry.box("hidden", title="Hidden")
    def hidden_box(middleware: Any) -> str:
        return "<p>hidden</p>"

    registry.hide("hidden")
    client = TestClient(mw.app)
    shell = client.get("/dashboard").text
    assert 'id="box-visible"' in shell
    assert 'id="box-hidden"' not in shell
    assert registry.get("hidden") is None
    assert client.get("/dashboard/box/hidden").status_code == 404
    assert client.get("/dashboard/box/visible").status_code == 200


def test_a_default_box_can_be_hidden() -> None:
    """The library's own boxes are hideable by id, which is what a subproject needs."""
    mw = _middleware_with_dashboard()
    registry = mw.dashboard

    assert "held-data" in _spec_ids(registry)
    registry.hide("held-data", "connectors")
    assert "held-data" not in _spec_ids(registry)
    assert "connectors" not in _spec_ids(registry)
    assert _spec_ids(registry), "hiding two defaults must not empty the page"


def test_a_tab_whose_every_box_is_hidden_leaves_the_tab_bar() -> None:
    """An emptied tab goes, and a tab that keeps one box stays."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box("only", title="Only", tab="Lonely")
    def only_box(middleware: Any) -> str:
        return "<p>only</p>"

    @registry.box("kept", title="Kept", tab="Shared")
    def kept_box(middleware: Any) -> str:
        return "<p>kept</p>"

    @registry.box("dropped", title="Dropped", tab="Shared")
    def dropped_box(middleware: Any) -> str:
        return "<p>dropped</p>"

    assert "Lonely" in registry.tabs()
    registry.hide("only", "dropped")
    assert "Lonely" not in registry.tabs()
    assert "Shared" in registry.tabs()
    assert "Lonely" not in TestClient(mw.app).get("/dashboard").text


def test_hiding_an_unregistered_id_raises_and_hides_nothing() -> None:
    """An id that was never registered is a typo, so it is refused loudly."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box("keep", title="Keep")
    def keep_box(middleware: Any) -> str:
        return "<p>keep</p>"

    with pytest.raises(ValueError, match="no-such-box"):
        registry.hide("keep", "no-such-box")
    assert _spec_ids(registry) == {"keep"}, "a refused call must hide nothing at all"


def test_hiding_is_repeatable_and_an_empty_call_does_nothing() -> None:
    """Hiding a hidden box is harmless, and hide() with no ids changes nothing."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box("twice", title="Twice")
    def twice_box(middleware: Any) -> str:
        return "<p>twice</p>"

    registry.hide("twice")
    registry.hide("twice")
    assert _spec_ids(registry) == set()
    registry.hide()
    assert _spec_ids(registry) == set()


def test_registering_a_hidden_id_shows_it_again() -> None:
    """Registering a box is a request to show it, so it clears an earlier hide.

    A subproject that hides a default box and then registers its own under that id gets
    its own box on the page. The alternative is a registration that silently renders
    nothing.
    """
    mw = _middleware_with_dashboard()
    registry = mw.dashboard
    registry.hide("held-data")
    assert "held-data" not in _spec_ids(registry)

    @registry.box("held-data", title="Our Own Held Data")
    def our_own(middleware: Any) -> str:
        return "<p>ours</p>"

    assert "held-data" in _spec_ids(registry)
    assert registry.get("held-data").title == "Our Own Held Data"
    assert TestClient(mw.app).get("/dashboard/box/held-data").status_code == 200


def test_the_page_loads_json_enc_after_htmx() -> None:
    """The extension registers against the htmx global, so it must load after it."""
    mw = _middleware_with_dashboard()
    shell = TestClient(mw.app).get("/dashboard").text
    assert shell.index('src="dashboard/static/htmx-2.0.4.min.js"') < shell.index(
        'src="dashboard/static/json-enc.js"'
    )
    assert (
        TestClient(mw.app).get("/dashboard/static/json-enc.js").status_code == 200
    ), "the vendored extension must be served, or hx-ext json-enc is dead markup"


def test_a_box_form_posts_a_json_body_to_its_own_workflow() -> None:
    """The worked example of the documentation, end to end.

    The box renders a form that asks htmx to encode the fields as JSON. The workflow route
    on the same instance takes a JSON body. The round trip proves why the extension is
    vendored: without it the browser would send form-encoded fields, which that route
    refuses. The form names the route relative to the page, so the browser resolves it
    against the page's address, as this test does.
    """
    mw = FakeMiddleware()

    @mw.app.post("/workflows/spawn_box/execute")
    async def spawn(arg: SpawnRequest) -> dict:
        """Spawn boxes on this instance."""
        return {"spawned": arg.count}

    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box("spawn", title="Spawn a box", tab="Actions")
    def spawn_box(middleware: Any) -> str:
        return (
            '<form hx-post="workflows/spawn_box/execute" hx-ext="json-enc" '
            'hx-swap="none">'
            '<input type="number" name="count" value="1">'
            '<button type="submit">Spawn</button>'
            "</form>"
        )

    client = TestClient(mw.app)
    fragment = client.get("/dashboard/box/spawn").text
    assert 'hx-ext="json-enc"' in fragment
    assert 'hx-swap="none"' in fragment
    assert 'hx-post="workflows/spawn_box/execute"' in fragment
    target = urljoin("http://testserver/dashboard", "workflows/spawn_box/execute")

    # What the extension turns the form into: a JSON body, accepted by the route.
    assert client.post(target, json={"count": 3}).json() == {"spawned": 3}
    # What the form would send without it, refused by the same route.
    assert client.post(target, data={"count": 3}).status_code == 422


def test_only_the_selected_tab_is_shown() -> None:
    """A panel is hidden until its tab is selected, and the stylesheet keeps it that way.

    Every panel carries both `tab-panel` and `box-grid`. The two class selectors have the
    same specificity, so a `display` in the `box-grid` rule wins on document order and
    every panel stays visible whatever the radio says: both tabs' boxes on screen at once,
    one stacked and one in columns. The page renders and looks plausible, which is why the
    markup tests did not see it. These assertions pin the cascade instead of the markup.
    """
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box("one", title="One", tab="System")
    def one_box(middleware: Any) -> str:
        return "<p>one</p>"

    @registry.box("two", title="Two", tab="Actions")
    def two_box(middleware: Any) -> str:
        return "<p>two</p>"

    page = TestClient(mw.app).get("/dashboard").text
    style = page[page.index("<style>") : page.index("</style>")]

    assert ".tab-panel {\n    display: none;" in style, (
        "a panel must start hidden, or every tab's boxes show at once"
    )

    grid_rule = style[style.index(".box-grid {") :]
    grid_rule = grid_rule[: grid_rule.index("}")]
    assert "display:" not in grid_rule, (
        "`.box-grid` must set no display: it has the same specificity as `.tab-panel` and "
        f"comes later, so it would override the panel's own hiding. Rule was: {grid_rule!r}"
    )

    # One rule per tab, and each one shows its panel as the grid its boxes are laid out in.
    for slug in ("system", "actions"):
        assert (
            f"#tab-{slug}:checked ~ .panels #panel-{slug} {{ display: grid; }}" in style
        ), f"no reveal rule for the {slug} tab"
    assert style.count(":checked ~ .panels") == 2, (
        "exactly one reveal rule per tab, or a panel is shown that no tab selects"
    )


# ---------------------------------------------------------------------------
# A render that takes its time: off the event loop, in either form
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_synchronous_render_still_renders() -> None:
    """The form every existing box uses keeps working, now through a worker thread."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box("plain", title="Plain")
    def plain_box(middleware: Any) -> str:
        return "<p>held in memory</p>"

    fragment = await _fragment_for_box(registry.get("plain"), mw)
    assert "held in memory" in fragment
    assert "failed to render" not in fragment


@pytest.mark.asyncio
async def test_an_async_render_is_awaited() -> None:
    """A box that fetches declares `async def`, and the page shows what it returned.

    Without the await the page would hold the repr of a coroutine, which renders as text and
    reports nothing.
    """
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box("fetching", title="Fetching")
    async def fetching_box(middleware: Any) -> str:
        await asyncio.sleep(0.01)
        return "<p>read from the graph</p>"

    fragment = await _fragment_for_box(registry.get("fetching"), mw)
    assert "read from the graph" in fragment
    assert "coroutine" not in fragment


@pytest.mark.asyncio
async def test_an_awaitable_result_is_awaited_even_from_a_plain_callable() -> None:
    """A partial of an async render is awaitable without being a coroutine function.

    `inspect.iscoroutinefunction` is false for it, so the result has to be checked as well.
    A subproject that binds an argument this way would otherwise see a coroutine's repr.
    """
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    async def render_with_prefix(prefix: str, middleware: Any) -> str:
        return f"<p>{prefix} from the graph</p>"

    registry.box("bound", title="Bound")(
        functools.partial(render_with_prefix, "read")
    )

    fragment = await _fragment_for_box(registry.get("bound"), mw)
    assert "read from the graph" in fragment
    assert "coroutine" not in fragment


@pytest.mark.asyncio
async def test_a_blocking_render_does_not_block_the_event_loop() -> None:
    """A render that sleeps lets the loop keep running, which is what a heartbeat needs.

    The counter is the loop itself: it can only advance while the loop is free. Run inline, a
    300 ms render would leave it at zero, and on a real instance the heartbeat write and every
    other box's fragment would wait that long on every refresh tick.
    """
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box("slow", title="Slow")
    def slow_box(middleware: Any) -> str:
        time.sleep(0.3)
        return "<p>a graph query took its time</p>"

    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        fragment = await _fragment_for_box(registry.get("slow"), mw)
    finally:
        ticker.cancel()

    assert "took its time" in fragment
    assert ticks >= 10, (
        f"the loop advanced only {ticks} times while a 300 ms render ran; it is running "
        "inline rather than in a worker thread"
    )


@pytest.mark.asyncio
async def test_a_slow_box_does_not_delay_another_boxs_fragment() -> None:
    """Two fragments in flight: the quick one answers first, and does not wait for the slow one."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box("slow", title="Slow")
    def slow_box(middleware: Any) -> str:
        time.sleep(0.3)
        return "<p>slow</p>"

    @registry.box("quick", title="Quick")
    def quick_box(middleware: Any) -> str:
        return "<p>quick</p>"

    finished: list[str] = []

    async def render(box_id: str) -> None:
        await _fragment_for_box(registry.get(box_id), mw)
        finished.append(box_id)

    await asyncio.gather(render("slow"), render("quick"))
    assert finished == ["quick", "slow"], (
        f"fragments finished in the order {finished}; a slow box must not hold up a quick one"
    )


@pytest.mark.asyncio
async def test_an_async_render_that_raises_still_shows_its_error_in_place() -> None:
    """The error panel covers both render forms, so one broken box never breaks the page."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, register_default_boxes=False)

    @registry.box("broken", title="Broken")
    async def broken_box(middleware: Any) -> str:
        raise RuntimeError("the store refused the read")

    fragment = await _fragment_for_box(registry.get("broken"), mw)
    assert "failed to render" in fragment
    assert "the store refused the read" in fragment


# ---------------------------------------------------------------------------
# The page names its instance (KSM 402)
# ---------------------------------------------------------------------------

#: The IRI of the resource that the instances below represent.
MODULE = "http://example.org/jms#Module1"


class GraphStore:
    """The store as the dashboard reaches it, ``middleware.ogm.db``, answered by an rdflib graph.

    ``query`` runs the SPARQL that it is given against the graph, and returns the answer in the
    shape of the interface's ``GraphDB.query``: the SPARQL JSON results, with the bindings
    converted as ``convert_bindings=True`` converts them. So the query that the dashboard sends
    is the query that is tested, not a stand-in for it. ``reads`` counts the queries.
    """

    def __init__(self, turtle: str = "", error: Exception | None = None) -> None:
        self.graph = Graph()
        self.graph.parse(
            data=(
                "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
                "@prefix ex: <http://example.org/jms#> .\n" + turtle
            ),
            format="turtle",
        )
        self.error = error
        self.reads = 0

    def query(
        self, query: str, update: bool = False, convert_bindings: bool = False
    ) -> dict:
        self.reads += 1
        if self.error is not None:
            raise self.error
        answer = json.loads(self.graph.query(query).serialize(format="json"))
        if convert_bindings:
            answer["results"]["bindings"] = convert_multi_bindings_to_python_type(
                answer["results"]["bindings"]
            )
        return answer


def _instance(resource_iri: str | None, store: GraphStore | None = None) -> FakeMiddleware:
    """A fake middleware that represents ``resource_iri``, with ``store`` as its graph."""
    mw = FakeMiddleware()
    mw.resource_iri = resource_iri
    mw.ogm = SimpleNamespace(db=store if store is not None else GraphStore())
    return mw


def _title_and_heading(mw: FakeMiddleware) -> tuple[str, str]:
    """The page's `<title>` and its `<h1>`, as the markup holds them."""
    page = TestClient(mw.app).get("/dashboard").text
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    heading = re.search(r"<h1>(.*?)</h1>", page, re.S)
    assert title is not None and heading is not None, page
    return title.group(1), heading.group(1)


def test_the_title_names_the_resource_by_its_label() -> None:
    """A tab shows which resource its page belongs to: the label, then "— dashboard"."""
    mw = _instance(MODULE, GraphStore('ex:Module1 rdfs:label "Module 1" .'))
    enable_dashboard(mw)

    assert _title_and_heading(mw) == ("Module 1 — dashboard", "Module 1 — dashboard")


@pytest.mark.parametrize(
    ("resource_iri", "name"),
    [
        ("http://example.org/jms#WarehouseManagementSystem1", "WarehouseManagementSystem1"),
        ("http://example.org/modules/Module1", "Module1"),
        ("http://example.org/modules/Module1/", "Module1"),
    ],
    ids=["after the hash", "after the last slash", "before a trailing slash"],
)
def test_without_a_label_the_title_names_the_resource_by_its_local_name(
    resource_iri: str, name: str
) -> None:
    """A resource with no `rdfs:label` is named by the last part of its IRI."""
    mw = _instance(resource_iri, GraphStore('ex:Other rdfs:label "Other" .'))
    enable_dashboard(mw)

    assert _title_and_heading(mw) == (f"{name} — dashboard", f"{name} — dashboard")


def test_an_instance_without_a_resource_keeps_the_general_title() -> None:
    """An instance that represents no resource has no name to show, and reads nothing."""
    store = GraphStore()
    mw = _instance(None, store)
    enable_dashboard(mw)

    assert _title_and_heading(mw) == ("Middleware Dashboard", "Middleware Dashboard")
    assert store.reads == 0


def test_the_name_is_escaped_in_the_title_and_the_heading() -> None:
    """A label is text from the graph, so markup in it is shown, not run."""
    mw = _instance(
        MODULE,
        GraphStore('ex:Module1 rdfs:label "<script>alert(1)</script> & Co" .'),
    )
    enable_dashboard(mw)

    escaped = "&lt;script&gt;alert(1)&lt;/script&gt; &amp; Co — dashboard"
    assert _title_and_heading(mw) == (escaped, escaped)


@pytest.mark.parametrize(
    ("labels", "name"),
    [
        (['"Modul 1"@de', '"Module 1"@en'], "Module 1"),
        (['"Modul 1"@de', '"Module 1"@en', '"Module one"'], "Module one"),
        (['"Modul 1"@de', '"Module 1"@en-GB'], "Module 1"),
        (['"Alpha"@en-GB', '"Zeta"@en'], "Zeta"),
        (['"B"', '"A"'], "A"),
        (['"Module"@fr', '"Modul"@de'], "Modul"),
        (['ex:NotALiteral', '"  "', '"Module 1"@de'], "Module 1"),
    ],
    ids=[
        "English over another language",
        "plain over English",
        "a regional English is English",
        "within a group the first language tag before the first text",
        "the first in text order among equals",
        "the first language tag when none is plain or English",
        "not an IRI and not a blank",
    ],
)
def test_of_several_labels_one_is_chosen_whatever_their_order(
    labels: list[str], name: str
) -> None:
    """A resource may carry several `rdfs:label` values; the same one names it every time.

    A label without a language tag comes first, then an English one, then the others; within
    a group the first language tag wins, and within one tag the first label, each in
    code-point order. The labels are stored in both orders, so a choice that followed the
    store's order would differ between the two.
    """
    for ordered in (labels, labels[::-1]):
        turtle = "".join(f"ex:Module1 rdfs:label {label} .\n" for label in ordered)
        mw = _instance(MODULE, GraphStore(turtle))
        enable_dashboard(mw)

        assert _title_and_heading(mw)[0] == f"{name} — dashboard", ordered


def test_the_name_is_read_once_when_the_dashboard_is_enabled() -> None:
    """One read of the store at enable; the page and its boxes, however often, read nothing."""
    store = GraphStore('ex:Module1 rdfs:label "Module 1" .')
    mw = _instance(MODULE, store)
    registry = enable_dashboard(mw)
    enable_dashboard(mw)
    assert store.reads == 1

    client = TestClient(mw.app)
    for _ in range(3):
        assert client.get("/dashboard").status_code == 200
        for spec in registry.specs():
            client.get(f"/dashboard/box/{spec.id}")
    assert store.reads == 1, "a render read the store"


def test_a_failed_read_names_the_resource_by_its_local_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The name is a convenience: a store that cannot answer does not stop the dashboard."""
    mw = _instance(MODULE, GraphStore(error=ConnectionError("the store is down")))
    with caplog.at_level(logging.WARNING, logger="kapps_semantic_middleware.dashboard"):
        enable_dashboard(mw)

    assert _title_and_heading(mw)[0] == "Module1 — dashboard"
    assert any(
        MODULE in record.getMessage() and "the store is down" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


# ---------------------------------------------------------------------------
# The tab that the page opens on (KSM 407)
# ---------------------------------------------------------------------------


def _wms_page(**options: Any) -> FakeMiddleware:
    """A page with the library's own boxes on "System", and a box of its own on "WMS"."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw, **options)

    @registry.box("spawn", title="Spawn", tab="WMS")
    def spawn_box(middleware: Any) -> str:
        return "<p>spawn</p>"

    return mw


def _opened_tab(page: str) -> str:
    """The id of the one tab whose radio the page marks as checked."""
    checked = re.findall(r'<input type="radio" name="tab" id="tab-([^"]+)"[^>]*\bchecked\b', page)
    assert len(checked) == 1, f"one tab must be open, and these are: {checked}"
    return checked[0]


def test_without_a_query_the_page_opens_on_the_first_tab() -> None:
    """What every page did before: System is first in the bar, and it is open."""
    page = TestClient(_wms_page().app).get("/dashboard").text
    assert _opened_tab(page) == "system"


def test_the_query_opens_the_tab_it_names() -> None:
    """A link to `dashboard?tab=WMS` opens on WMS; System and its boxes stay on the page."""
    mw = _wms_page()
    page = TestClient(mw.app).get("/dashboard", params={"tab": "WMS"}).text

    assert _opened_tab(page) == "wms"
    assert re.findall(r'<label for="tab-([^"]+)"', page) == ["system", "wms"]
    for spec in mw.dashboard.specs():
        assert f'hx-get="dashboard/box/{spec.id}"' in page, f"box {spec.id} left the page"


@pytest.mark.parametrize(
    ("first_tab", "query", "opened"),
    [
        ("WMS", None, "wms"),
        ("WMS", "System", "system"),
        (None, "Nope", "system"),
        ("WMS", "Nope", "wms"),
        ("Nope", None, "system"),
        ("Nope", "WMS", "wms"),
    ],
    ids=[
        "the keyword opens its tab",
        "the query overrides the keyword",
        "an unknown query opens the first tab",
        "an unknown query opens the keyword's tab",
        "an unknown keyword opens the first tab",
        "the query overrides an unknown keyword",
    ],
)
def test_the_keyword_sets_the_tab_that_opens_and_the_query_overrides_it(
    first_tab: str | None, query: str | None, opened: str
) -> None:
    """`first_tab` is the tab a page opens on; `?tab=` overrides it for one link.

    A name that no tab has opens the tab that would open without it, so a stale link or a
    typo still shows the page.
    """
    mw = _wms_page(first_tab=first_tab)
    params = {"tab": query} if query is not None else {}
    page = TestClient(mw.app).get("/dashboard", params=params).text

    assert _opened_tab(page) == opened


def test_the_query_names_a_tab_by_its_title_with_a_space() -> None:
    """A tab is named by the title the bar shows, not by the id the page gives it."""
    mw = FakeMiddleware()
    registry = enable_dashboard(mw)

    @registry.box("monitor", title="Monitor", tab="UC2 monitor")
    def monitor_box(middleware: Any) -> str:
        return "<p>monitor</p>"

    client = TestClient(mw.app)
    assert _opened_tab(client.get("/dashboard?tab=UC2%20monitor").text) == "uc2-monitor"
    assert _opened_tab(client.get("/dashboard?tab=uc2-monitor").text) == "system"


def test_a_first_tab_that_names_no_tab_is_reported_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo in `first_tab` opens the first tab, and the log names the typo, once.

    The name cannot be checked at enable, because the boxes, and so the tabs, are registered
    after the call. The first render that cannot open it says so; a page that is reloaded
    every few seconds does not fill the log.
    """
    client = TestClient(_wms_page(first_tab="wms").app)
    with caplog.at_level(logging.WARNING, logger="kapps_semantic_middleware.dashboard"):
        for _ in range(3):
            assert _opened_tab(client.get("/dashboard").text) == "system"

    reports = [r.getMessage() for r in caplog.records if "'wms'" in r.getMessage()]
    assert len(reports) == 1, [r.getMessage() for r in caplog.records]
    assert "System" in reports[0] and "WMS" in reports[0], reports[0]
