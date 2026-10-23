"""Minimal state dashboard for a middleware instance.

This module turns a middleware instance into a diagnostic page. The page shows state the
instance already holds in memory. Boxes present slices of that state. The registry is the
extension point: a subproject registers boxes, and the page renders what was registered.

This module has two rules, stated as a contract:

1. No write path. There is no POST, PUT, PATCH or DELETE route here, and nothing this module
   calls writes. A dashboard shows state. It does not change it.
2. No traffic the page causes. Rendering the page or a box reads what the instance already
   holds. This module performs no graph query and no HTTP call to a peer while it serves. It
   reads the graph once: the name of the instance's resource, when the dashboard is enabled.

Both rules exist for the same reason: a diagnostic page must not perturb the thing it
diagnoses. A page that writes obscures the difference between what an operator commanded and
what a diagnostic did. A page that queries a peer adds load to that peer and can fail for
reasons unrelated to the middleware it is meant to inspect.

Rule 1 has one exception, outside this module: the Logs tab, in `dashboard_logs`, has a form
that sets a logger's level in this process. It changes what the process logs, and nothing in
the graph or on a device. The operator who presses it commands it, so it does not blur what a
diagnostic did with what an operator commanded.

Rule 2 binds this module and the boxes it registers. A subproject's box may fetch in its own
render, which is its own business and its own cost, so this module makes that safe rather than
impossible: a render declared with `async def` is awaited, and any other render runs in a worker
thread. Neither form runs inline on the event loop. A slow box therefore delays its own fragment
and nothing else -- not the instance's heartbeat, and not another box -- for as long as a worker
thread is free, which anyio's default limit puts at 40 concurrent blocking renders.

htmx loads each box fragment on page load and on a per-box poll interval. It is the only
moving part beyond the plain HTML shell and the fragments this module serves. It ships
vendored beside this module, so no install path fetches anything at build or run time.
"""

from __future__ import annotations

import html
import inspect
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from kapps_semantic_middleware.vocabulary import RDFS

logger = logging.getLogger(__name__)

# A box with no refresh interval never polls. None is the marker for "static".
DEFAULT_TAB = "System"
STATIC_DIR = Path(__file__).parent / "static"
#: A box id must be usable as an HTML id and as one path segment, unescaped.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

# The page draws with tokens, not raw colors, so light and dark both work from one set of
# definitions. This is the CSS constant the shell interpolates. Keeping it separate from the
# f-string that builds the document means no brace-doubling in the CSS itself.
_CSS = """
:root {
    --bg: #f5f5f5;
    --fg: #1a1a1a;
    --panel-bg: #ffffff;
    --panel-border: #d0d0d0;
    --accent: #005a9c;
    --muted: #666666;
    --error-bg: #ffe9e9;
    --error-border: #cc0000;
    --mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
@media (prefers-color-scheme: dark) {
    :root {
        --bg: #1a1a1a;
        --fg: #e0e0e0;
        --panel-bg: #242424;
        --panel-border: #3a3a3a;
        --accent: #7ab8e8;
        --muted: #999999;
        --error-bg: #3d1f1f;
        --error-border: #ff6666;
    }
}
body {
    font-family: var(--sans);
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--fg);
}
header {
    padding: 12px 20px;
    background: var(--panel-bg);
    border-bottom: 1px solid var(--panel-border);
}
header h1 {
    margin: 0;
    font-size: 1.2rem;
}
header p {
    margin: 4px 0 0;
    color: var(--muted);
    font-size: 0.85rem;
}
.tabs {
    display: flex;
    gap: 0;
    border-bottom: 1px solid var(--panel-border);
    padding: 0 20px;
    background: var(--panel-bg);
}
.tab-radio {
    position: absolute;
    opacity: 0;
    pointer-events: none;
}
.tab-label {
    padding: 8px 16px;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    color: var(--muted);
}
.tab-panel {
    display: none;
    padding: 20px;
}
.panels {
    max-width: 1200px;
    margin: 0 auto;
}
/* No `display` here. Every panel carries both `tab-panel` and `box-grid`, and the two
   selectors have the same specificity, so a `display` in this rule would win on document
   order and leave every panel visible at all times. The panel's own `display: none` above
   hides it, and the `:checked` rule that the shell generates turns the one selected panel
   into the grid. */
.box-grid {
    grid-template-columns: repeat(auto-fit, minmax(28rem, 1fr));
    gap: 16px;
}
.box {
    background: var(--panel-bg);
    border: 1px solid var(--panel-border);
    border-radius: 4px;
    overflow: hidden;
}
.box-header {
    padding: 8px 12px;
    border-bottom: 1px solid var(--panel-border);
    display: flex;
    justify-content: space-between;
    align-items: baseline;
}
.box-title {
    margin: 0;
    font-size: 0.95rem;
}
.panel-subtitle {
    color: var(--muted);
    font-size: 0.8rem;
}
.box-body {
    padding: 12px;
    font-family: var(--mono);
    font-size: 0.85rem;
}
.kv {
    width: 100%;
    border-collapse: collapse;
}
.kv th, .kv td {
    text-align: left;
    padding: 4px 8px;
    border-bottom: 1px solid var(--panel-border);
}
.kv th {
    color: var(--muted);
    width: 30%;
    white-space: nowrap;
}
/* A box is a grid cell, so it has a hard width. A table of IRIs is wider than that and
   would spill past the right edge with its text cut off. Two rules keep it inside: the
   wrapper scrolls sideways when the content genuinely needs more room, and long values
   break inside a word, because an IRI has no spaces to break at. */
.table-scroll {
    overflow-x: auto;
    max-width: 100%;
}
.hint {
    margin: 0 0 8px;
    color: var(--muted);
    font-family: var(--sans);
    font-size: 0.8rem;
    line-height: 1.4;
}
.chains {
    margin: 0 0 8px;
    padding-left: 1.2em;
}
.chains li {
    margin: 2px 0;
}
.box-body table {
    width: 100%;
    border-collapse: collapse;
}
.box-body table th,
.box-body table td {
    text-align: left;
    padding: 4px 8px;
    border-bottom: 1px solid var(--panel-border);
    overflow-wrap: anywhere;
    vertical-align: top;
}
.box-body table th {
    color: var(--muted);
    font-weight: normal;
    white-space: nowrap;
}
.error-box {
    background: var(--error-bg);
    border: 1px solid var(--error-border);
    padding: 8px;
    border-radius: 4px;
}
.error-box p {
    margin: 0 0 4px;
    font-weight: bold;
}
.error-box pre {
    margin: 0;
    white-space: pre-wrap;
}
"""

# htmx first, then the extension: `json-enc` registers itself against the htmx global, so
# the order is load order and not a preference. The extension is inert until an element
# asks for it with `hx-ext="json-enc"`, so a page whose boxes never post pays one request
# of about a kilobyte for it.
#
# Every URL the page names is relative, and starts with the page's own name: the page lives at
# `/dashboard`, without a trailing slash, so `dashboard/static/...` resolves next to it. That
# holds at the process's root and behind a proxy that serves the process under a path prefix,
# such as `/p/<process>/dashboard`. An absolute `/dashboard/static/...` would send the browser
# to the proxy's own root there, and the page would load no script.
_SCRIPT_TAG = (
    '<script src="dashboard/static/htmx-2.0.4.min.js"></script>'
    '<script src="dashboard/static/json-enc.js"></script>'
)


#: What a box's render returns: the HTML fragment itself, or something that resolves to it.
#: The second form is what an `async def` render returns, so a box that fetches can await its
#: own I/O.
RenderResult = Union[str, Awaitable[str]]

#: What a box's render is. This is `Callable[[Any], str]` widened by one option, so every
#: render that was legal before is still legal: a synchronous box needs no change. It takes
#: the middleware instance and returns a fragment, or awaits one.
Render = Callable[[Any], RenderResult]


@dataclass(frozen=True)
class BoxSpec:
    """One box on the dashboard.

    `render` takes the middleware instance and returns an HTML fragment. The fragment is
    wrapped in a titled panel by the dashboard. Box authors may also use the `panel` helper
    themselves if they need the frame inside a larger fragment.

    `refresh_seconds` being None means the box never polls. The box is fetched once, on
    page load.
    """

    id: str  # letters, digits, hyphen, underscore; refused at registration otherwise
    title: str
    tab: str  # default "System"
    render: Render  # takes the middleware, returns an HTML fragment or awaits one
    refresh_seconds: Optional[float]  # None means the box never polls


class DashboardRegistry:
    """Registry of dashboard boxes.

    The registry is the extension point of this module. A subproject registers its own boxes
    and passes `register_default_boxes=False` to `enable_dashboard` when it wants only those.
    `hide` drops single boxes instead, the library's own among them, for a subproject that
    wants most of the defaults and not all of them.
    Registration order is preserved, so the page shows boxes in the order they were declared.
    """

    def __init__(self, *, dev: bool = True) -> None:
        self._boxes: Dict[str, BoxSpec] = {}
        self._tabs: List[str] = []
        self._hidden: Set[str] = set()
        self._dev = dev

    @property
    def dev(self) -> bool:
        """Whether this is a development page, which shows connection metadata.

        ``enable_dashboard(mw, dev=False)`` makes a page without it: the library's boxes leave
        out what is connection metadata, such as an MQTT topic or an error's message. A box of a
        subproject reads this to do the same. ``observation.is_connection_metadata`` names the
        fields of the instance's records that are connection metadata.
        """
        return self._dev

    def box(
        self,
        id: str,
        *,
        title: str,
        tab: str = DEFAULT_TAB,
        refresh_seconds: Optional[float] = None,
    ) -> Callable[[Render], Render]:
        """Register the decorated render function as a box.

        This is a decorator. The decorated function takes the middleware and returns an HTML
        fragment.

        A duplicate `id` replaces the earlier entry. That is deliberate: a subproject that
        overrides a default box can do so by re-registering with the same id. Registering
        also shows the id again when `hide` had hidden it, because registering a box is a
        request to put it on the page, and a box that stayed hidden after one would be a
        silent refusal.

        Raises:
            ValueError: If `id` is not URL-safe. The id becomes an HTML id and a path
                segment of `/dashboard/box/<id>`, so a slash or a space would render a slot
                the browser can never fill, with nothing reporting it.
        """

        if not _SAFE_ID.match(id):
            # A box id becomes an HTML id and a path segment of the fragment URL. A slash
            # turns `/dashboard/box/a/b` into a route that does not exist, and the failure is
            # silent: the page renders, the slot stays empty, and nothing reports it. A tab
            # title is prose and gets slugged, but a box id is an identifier a subproject
            # also passes back to hide or fetch one box, so changing it here would break the
            # caller's own reference. Refusing at registration is the honest answer.
            raise ValueError(
                f"box id {id!r} is not URL-safe. Use letters, digits, hyphen and underscore. "
                f"The id becomes an HTML id and a path segment of /dashboard/box/<id>."
            )

        def decorator(render: Render) -> Render:
            self._boxes[id] = BoxSpec(
                id=id,
                title=title,
                tab=tab,
                render=render,
                refresh_seconds=refresh_seconds,
            )
            self._hidden.discard(id)
            if tab not in self._tabs:
                self._tabs.append(tab)
            return render

        return decorator

    def hide(self, *box_ids: str) -> None:
        """Hide the named boxes, the library's own defaults included.

        A hidden box leaves the page, and its fragment route answers 404. A tab whose
        every box is hidden leaves the tab bar, because the alternative is a tab that
        opens an empty panel.

        Hiding marks rather than deletes. The box keeps its place in registration order,
        so showing it again would put it back where it was declared.

        Raises:
            ValueError: If an id was never registered. A hidden id renders nothing by
                definition, so a typo in one would remove nothing and report nothing.
                Nothing is hidden when this raises.
        """

        unknown = sorted(box_id for box_id in box_ids if box_id not in self._boxes)
        if unknown:
            raise ValueError(
                f"cannot hide box id(s) that were never registered: {', '.join(unknown)}. "
                f"Registered ids: {', '.join(self._boxes) or '(none)'}."
            )
        # Set union rather than the set's own update method: the traffic guard in
        # `tests/test_dashboard_guard.py` scans this module's source text for the names of
        # the graph-writing calls, and a set method of the same name reads alike to a text
        # scan -- in a comment as well as in code. Union says the same thing here.
        self._hidden |= set(box_ids)

    def specs(self) -> List[BoxSpec]:
        """Return every visible box, in registration order. Hidden boxes are left out."""
        return [
            spec for box_id, spec in self._boxes.items() if box_id not in self._hidden
        ]

    def tabs(self) -> List[str]:
        """Return tab names in declaration order. System first when present.

        A tab that no visible box is in is not returned: it would open an empty panel.
        """
        # System is the default tab. It reads best first. Any other tab that happens to be
        # named "System" also sorts first, which is the same intent.
        occupied = {spec.tab for spec in self.specs()}
        return sorted(
            (tab for tab in self._tabs if tab in occupied),
            key=lambda t: (t != DEFAULT_TAB, self._tabs.index(t)),
        )

    def get(self, box_id: str) -> Optional[BoxSpec]:
        """Return the box with the given id, or None. A hidden box answers None, which
        is what makes its fragment route answer 404."""
        if box_id in self._hidden:
            return None
        return self._boxes.get(box_id)


#: The middleware no longer uses this note: an IRI cell shows its local name and keeps the
#: full IRI on hover, which needs no sentence. It stays importable, with this value, because
#: a consumer still imports it; a later release removes it.
SHORTENING_NOTE = (
    "Identifiers and generated paths are shortened for readability. "
    "Hold the pointer over any shortened value to read it in full."
)


def hint(text: str) -> str:
    """Render a short explanatory line above a table.

    Use it for any short explanatory line that a table needs above it.
    """
    return f'<p class="hint">{escape(text)}</p>'


def panel(title: str, body_html: str, *, subtitle: str = "") -> str:
    """Wrap a box fragment in a titled panel.

    The frame is deliberately plain: a header band and the body beneath. Box authors can call
    this to build nested panels, but the dashboard already wraps every fragment in one, so a
    box module's return value does not need its own call.
    """
    sub = f'<span class="panel-subtitle">{escape(subtitle)}</span>' if subtitle else ""
    return (
        '<section class="box">'
        '<header class="box-header">'
        f'<h2 class="box-title">{escape(title)}</h2>'
        f"{sub}"
        "</header>"
        '<div class="box-body">'
        f"{body_html}"
        "</div>"
        "</section>"
    )


def escape(value: Any) -> str:
    """`html.escape(str(value))`, with None rendered as an em dash."""
    if value is None:
        return "&mdash;"
    return html.escape(str(value))


def kv_table(rows: Sequence[Tuple[str, Any]]) -> str:
    """A two-column key/value table. Values are escaped."""
    body = "".join(
        f"<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>" for k, v in rows
    )
    return f'<table class="kv">{body}</table>'


async def _fragment_for_box(spec: BoxSpec, middleware: Any) -> str:
    """Render one box, or an error fragment when the render raises.

    A box that raises must not break the page. One broken box shows an error where it sits;
    the other boxes still render. A failure is reported in place and never 500s the page.

    Neither does a box that takes its time. A render declared with `async def` is awaited, and
    any other render runs in a worker thread rather than inline on the event loop. A box that
    reads the graph therefore costs its own fragment and nothing else: the instance keeps
    answering, its heartbeat keeps being written, and the other boxes keep rendering. Running a
    render inline is what made a single slow box stall the whole instance on every refresh tick
    of every open tab.

    A render is called in a thread unless it is a coroutine function, and its result is awaited
    when it turns out to be awaitable. The second check is not redundant: a `functools.partial`
    of an async function, and a class with `async def __call__`, are both awaitable without
    being coroutine functions, and either would otherwise reach the page as a coroutine's repr.
    """
    try:
        if inspect.iscoroutinefunction(spec.render):
            body = await spec.render(middleware)
        else:
            result = await anyio.to_thread.run_sync(spec.render, middleware)
            body = await result if inspect.isawaitable(result) else result
    except Exception as e:  # noqa: BLE001 - a box's render is untrusted third-party code
        logger.warning("Box %s failed to render: %s", spec.id, e)
        body = (
            '<div class="error-box">'
            f"<p>This box failed to render.</p>"
            f"<pre>{escape(e)}</pre>"
            "</div>"
        )
    return panel(spec.title, body)


def _tab_slugs(tabs: Sequence[str]) -> Dict[str, str]:
    """Map each tab name to an identifier that is legal in an HTML id and a CSS selector.

    A tab name is a title a subproject chooses, so it may hold a space, a slash, a dot or an
    apostrophe. `html.escape` leaves all of them alone, because it escapes text and not an
    identifier. The result was `id="tab-Line monitor"` and a selector
    `#tab-Line monitor:checked`, which no browser can parse: the rule is dropped, the panel
    keeps `display: none`, and the page renders a tab strip above nothing. `Line monitor` is
    such a tab, so this was reached on the first tab a project adds.

    The visible label keeps the original name. Only the identifier is slugged.

    A slug can collide: `A/B` and `A-B` both reduce to `a-b`. The second and later get the
    tab's index appended, so every identifier stays unique whatever the names are.

    Nothing carries the original title back into the markup as a separate attribute. It is
    already there: each label names its input through `for`, so `<label for="tab-a-b-1">A-B`
    maps the slug to the title. A second copy would say the same thing twice.
    """
    slugs: Dict[str, str] = {}
    seen: Set[str] = set()
    for index, tab in enumerate(tabs):
        base = re.sub(r"[^a-zA-Z0-9_-]+", "-", tab).strip("-").lower() or "tab"
        # A CSS identifier may not start with a digit. Prefixing every slug with `tab-`
        # already prevents that, and keeping the rule here means the slug is safe on its own.
        slug = base if base not in seen else f"{base}-{index}"
        seen.add(slug)
        slugs[tab] = slug
    return slugs


def _shell_html(
    registry: DashboardRegistry,
    *,
    instance_name: Optional[str],
    wanted_tabs: Sequence[Optional[str]],
) -> str:
    """The dashboard shell: tab bar and all box panels.

    Tabs are CSS-only. Every tab's panel is in the document. A radio input plus a sibling
    selector shows one at a time. No JavaScript of our own. htmx is only for the box
    fragments.

    The box elements ask htmx to load their fragment on page load. When the box has a refresh
    interval, htmx also polls on that interval. A box with refresh_seconds=None gets
    `hx-trigger="load"` alone and never polls.

    `wanted_tabs` are tab names in order of preference: the tab of the link's query, then the
    `first_tab` of `enable_dashboard`. The first of them that is a tab of the bar opens, and
    the first tab of the bar opens when none is. The page marks the radio of that tab as
    checked. The choice is made here, on the server, so a link can open a tab with no script.

    `instance_name` is the name of the instance's resource. The title and the heading say
    "<instance_name> Dashboard", so each browser tab shows which instance it is. Without a
    name, for an instance that represents no resource, they say "Middleware Dashboard".

    An empty registry is a supported state: a subproject calls
    `enable_dashboard(mw, register_default_boxes=False)` and registers its own boxes
    afterwards, so the shell can legitimately be requested before any box exists. When there
    are no tabs, the document has an empty tab bar and a line saying no boxes are registered,
    and the `:checked` rule that needs a first tab is skipped.
    """
    tabs = registry.tabs()

    # The radio inputs must all be siblings of the panels they switch. Each input is the
    # first child of the container; the label that follows is its click target. A sibling
    # selector `:checked ~ .panels .panel:not(:target)` would be too broad, so instead each
    # input is a sibling of the whole panel wrapper and the selector uses the input's id.
    # The radios and the labels are emitted separately, and that separation is load-bearing.
    # `~` is the general sibling combinator, so `#tab-X:checked ~ .panels` matches only while
    # the radio and `.panels` share one parent. Putting the radio inside the tab bar breaks
    # that: its parent becomes the tab bar, the rule stops matching, and every panel stays at
    # `display: none` while the page still renders a header and a tab strip. A label reaches
    # its input by `for=` from anywhere in the document, so the label can live in the tab bar
    # while the radio stays hoisted beside the panels.
    slugs = _tab_slugs(tabs)

    opened = next(
        (tab for tab in wanted_tabs if tab in tabs), tabs[0] if tabs else None
    )

    tab_inputs = []
    tab_labels = []
    for tab in tabs:
        checked = " checked" if tab == opened else ""
        slug = slugs[tab]
        tab_inputs.append(
            f'<input type="radio" name="tab" id="tab-{slug}" class="tab-radio"{checked}>'
        )
        tab_labels.append(
            f'<label for="tab-{slug}" class="tab-label">{escape(tab)}</label>'
        )

    # The :checked rule must reference a tab that exists. An empty registry has none, so the
    # rule is omitted entirely. Without it, no panel is ever shown. That is correct: there is
    # nothing to show.
    # `display: grid` and not `block`: the panel is the grid that lays its boxes out in
    # columns, and this rule is the only place a panel is ever shown. The grid's columns and
    # gap come from `.box-grid`, which deliberately sets no display of its own.
    checked_rules = "".join(
        f"#tab-{slugs[tab]}:checked ~ .panels #panel-{slugs[tab]} {{ display: grid; }}"
        for tab in tabs
    )
    # The selected tab's label takes the accent. The radio is beside the tab strip and not
    # before its labels, so an adjacent-sibling rule (`.tab-radio:checked + .tab-label`) never
    # matches; the rule goes from the checked radio to the label named by its `for`, in the
    # strip, one rule per tab like the panel rules above.
    label_rules = "".join(
        f'#tab-{slugs[tab]}:checked ~ .tabs label[for="tab-{slugs[tab]}"] '
        "{ color: var(--fg); border-bottom-color: var(--accent); }"
        for tab in tabs
    )

    panels_html = []
    for tab in tabs:
        boxes = [spec for spec in registry.specs() if spec.tab == tab]
        boxes_html = []
        for spec in boxes:
            trigger = (
                f"load, every {int(spec.refresh_seconds)}s"
                if spec.refresh_seconds is not None
                else "load"
            )
            # Relative, like the script tags: see `_SCRIPT_TAG`.
            boxes_html.append(
                f'<div id="box-{escape(spec.id)}" hx-get="dashboard/box/{escape(spec.id)}" '
                f'hx-trigger="{escape(trigger)}" class="box-slot"></div>'
            )
        panels_html.append(
            f'<section class="tab-panel box-grid" id="panel-{slugs[tab]}">'
            f"{''.join(boxes_html)}"
            "</section>"
        )

    if not tabs:
        panels_html.append(
            '<section class="tab-panel"><p>No boxes registered.</p></section>'
        )

    heading = (
        f"{escape(instance_name)} Dashboard" if instance_name else "Middleware Dashboard"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{heading}</title>
<style>
{_CSS}
{checked_rules}
{label_rules}
</style>
</head>
<body>
<header>
    <h1>{heading}</h1>
    <p>Visualization of the middleware state</p>
</header>
{"".join(tab_inputs)}
<div class="tabs">
    {"".join(tab_labels)}
</div>
<div class="panels">
    {"".join(panels_html)}
</div>
{_SCRIPT_TAG}
</body>
</html>
"""


def _instance_name(middleware: Any) -> Optional[str]:
    """The name of the instance's resource: its `rdfs:label`, else the local name of its IRI.

    None for an instance that represents no resource. Its page keeps the general title.

    This is the one read of the graph in this module, and `enable_dashboard` makes it once, so
    no render of the page reads the store. A resource may carry several labels, and the query
    chooses the same one every time. A label without a language tag comes first, then an
    English one (`en`, `en-GB`, ...), then the others. Within each group the first language tag
    in code-point order wins, and within one tag the first label in code-point order. A value
    that is an IRI, or that holds only white space, is not a name.

    The name is a convenience. A store that cannot answer does not stop the dashboard: the
    failure is logged, and the page names the resource by the local name of its IRI.
    """
    resource = getattr(middleware, "resource_iri", None)
    if resource is None:
        return None
    store = getattr(getattr(middleware, "ogm", None), "db", None)
    if store is None:
        return _local_name(str(resource))
    try:
        answer = store.query(
            rf"""
            SELECT (STR(?label) AS ?name) WHERE {{
                <{resource}> <{RDFS.label}> ?label .
                FILTER (isLiteral(?label) && REGEX(STR(?label), "\\S"))
            }}
            ORDER BY
                (IF(LANG(?label) = "", 0, IF(LANGMATCHES(LANG(?label), "en"), 1, 2)))
                LANG(?label)
                STR(?label)
            LIMIT 1
            """,
            convert_bindings=True,
        )
    except Exception as exc:  # noqa: BLE001 - the name is a convenience, never fatal
        logger.warning(
            "Could not read the rdfs:label of %s for the dashboard's title; "
            "the page names it by its IRI: %s",
            resource,
            exc,
        )
        return _local_name(str(resource))
    rows = (
        answer.get("results", {}).get("bindings", []) if isinstance(answer, dict) else []
    )
    return str(rows[0]["name"]) if rows else _local_name(str(resource))


def _local_name(iri: str) -> str:
    """The part of an IRI after its last `#` or `/`. A trailing `/` or `#` is not a part."""
    trimmed = iri.rstrip("#/")
    return re.split(r"[#/]", trimmed)[-1] or iri


def _serve_static_file(path: str) -> Response:
    """Resolve and serve one vendored asset from the static directory.

    The requested path is joined onto the static directory and resolved. The resolved path
    must stay inside the directory; otherwise the request is a traversal attempt and gets 404.
    """
    requested = path.lstrip("/")
    full_path = (STATIC_DIR / requested).resolve()
    static_root = STATIC_DIR.resolve()
    try:
        full_path.relative_to(static_root)
    except ValueError:
        return HTMLResponse(status_code=404)
    if not full_path.is_file():
        return HTMLResponse(status_code=404)

    suffix = full_path.suffix.lower()
    if suffix == ".js":
        media_type = "application/javascript"
    elif suffix == ".css":
        media_type = "text/css"
    else:
        media_type = "application/octet-stream"

    return Response(content=full_path.read_bytes(), media_type=media_type)


def enable_dashboard(
    middleware: Any,
    *,
    register_default_boxes: bool = True,
    first_tab: Optional[str] = None,
    dev: bool = True,
) -> DashboardRegistry:
    """Enable the dashboard on a middleware instance.

    Builds a ``DashboardRegistry``, optionally registers the default boxes, and mounts the
    routes on ``middleware.app``. The default boxes are the four of the System tab and the
    Logs tab, which attaches the process's log handler to the root logger, once per process.
    A second call on the same middleware returns the existing registry without mounting
    anything twice, and ignores the options of that second call.

    The registry is stored on the middleware as ``middleware.dashboard``.

    The page, its boxes and its static files are left out of the instance's API description
    (``/openapi.json``, which ``/docs`` renders): they are not part of the instance's API.

    The page is named after the instance's resource: its ``rdfs:label``, else the local name
    of its IRI, as in "Module 1 Dashboard". The name is read from the graph here, once, and
    no render reads it again.

    ``first_tab`` names the tab that the page opens on, by the title the tab bar shows. A link
    overrides it with the query ``?tab=<tab name>``. A name that no tab has, in either place,
    is passed over: the query falls back to ``first_tab``, and ``first_tab`` to the first tab
    of the bar. ``first_tab`` is not checked here, because a subproject registers its boxes,
    and so its tabs, after this call. The first render that cannot open it logs a warning,
    once.

    ``dev`` says whether the page is a development page. The default is one: it shows the
    connection metadata of the instance, such as the MQTT topic of each parameter, the protocol
    properties that the northbound projection stripped, and the message of a connection's or of
    the graph binding's last error, which may name a host, a port or a URL. ``dev=False`` leaves
    all of that out, for a deployment that is not a laboratory one; the boxes then show counts,
    states, ages and the type of an error. The registry's ``dev`` says which the page is.
    """
    # `getattr(..., None) is not None` rather than `hasattr`: a middleware that declares
    # `self.dashboard = None` when the feature is off would otherwise satisfy `hasattr`
    # and be handed its own `None` back as a registry.
    existing = getattr(middleware, "dashboard", None)
    if existing is not None:
        return existing

    registry = DashboardRegistry(dev=dev)
    instance_name = _instance_name(middleware)

    if register_default_boxes:
        # Import inside the function to break the cycle: dashboard_boxes imports from this
        # module (the registry and helpers), so it cannot be imported at module top.
        from kapps_semantic_middleware import dashboard_boxes

        dashboard_boxes.register(registry)

    router = APIRouter()

    # A typo in `first_tab` would open the first tab and report nothing. It cannot be checked
    # here, because the tabs are registered after this call, so the first render that cannot
    # open it says so, once: a page reloaded every few seconds must not fill the log.
    first_tab_reported = False

    # The page, its boxes and its static files are not part of the instance's API: they are left
    # out of its API description, so a reader of `/docs` sees the instance's services only. The
    # redirect below is left out for the same reason.
    @router.get("/dashboard", include_in_schema=False)
    async def get_dashboard_page(tab: Optional[str] = None) -> HTMLResponse:
        nonlocal first_tab_reported
        if first_tab is not None and not first_tab_reported:
            tabs = registry.tabs()
            if first_tab not in tabs:
                first_tab_reported = True
                logger.warning(
                    "The dashboard's first_tab %r is not a tab of the page, so the page opens "
                    "on its first tab. The tabs are: %s.",
                    first_tab,
                    ", ".join(tabs) or "(none)",
                )
        return HTMLResponse(
            _shell_html(
                registry, instance_name=instance_name, wanted_tabs=(tab, first_tab)
            )
        )

    # The page's relative paths resolve against its own address, so it is served at one form
    # only, without the trailing slash. The other form redirects to it by a relative path,
    # which keeps any prefix that a proxy serves the process under, and keeps the query.
    # Starlette's own redirect of a trailing slash names an absolute URL built from the path
    # the process sees, and so drops that prefix.
    @router.get("/dashboard/", include_in_schema=False)
    async def redirect_to_the_dashboard_page(request: Request) -> RedirectResponse:
        query = request.url.query
        return RedirectResponse(f"../dashboard?{query}" if query else "../dashboard")

    @router.get("/dashboard/box/{box_id}", include_in_schema=False)
    async def get_dashboard_box(box_id: str) -> HTMLResponse:
        spec = registry.get(box_id)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Unknown box: {box_id}")
        return HTMLResponse(await _fragment_for_box(spec, middleware))

    @router.get("/dashboard/static/{filename}", include_in_schema=False)
    async def get_dashboard_static(filename: str) -> Response:
        return _serve_static_file(filename)

    if register_default_boxes:
        # The Logs tab is a default box too, after the four of the System tab. It brings the
        # page's one write route, which sets a logger's level; see `dashboard_logs`.
        from kapps_semantic_middleware import dashboard_logs

        dashboard_logs.register(registry, router)

    middleware.app.include_router(router)
    middleware.dashboard = registry
    return registry
