"""The dashboard and the activity page work at the process's root and under a path prefix.

A page that an instance serves names its own resources: the dashboard its scripts and its box
fragments, the activity page its stream. A launcher that serves every process through one port
does so under a path of its own for each, such as ``/p/<process>/...``. A page reached there
that names a resource by an absolute path, ``/dashboard/static/...``, sends the browser to the
launcher's own root, which has no such resource. The page then renders with no scripts, no boxes
and no stream, and nothing reports it.

So every page that KSM serves names its own resources by a path relative to the page. The pages
live at ``/dashboard`` and ``/activity``, without a trailing slash, so a relative path starts
with the page's own name: ``dashboard/static/...``, ``dashboard/box/<id>``, ``activity/stream``.

These tests read each page as a browser does: they take the URLs out of the HTML, resolve each
against the address the page was loaded from, and fetch it. They do this at the root and under
a prefix, reached in the two ways a prefix happens: an ASGI parent that mounts the instance's
app, and a proxy that strips the prefix before it forwards, as an HTTP reverse proxy does. The
second is how such a launcher reaches a process; the inner app never learns the prefix.

No store, no network.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from kapps_semantic_middleware import activity
from kapps_semantic_middleware.activity import enable_activity_feed
from kapps_semantic_middleware.dashboard import enable_dashboard

PREFIX = "/p/x"
BASE = "http://testserver"

#: The URL of a resource the page loads or sends a request to: an attribute of an element (a
#: script's ``src``, an htmx request, a link or a form), in double or single quotes, or the
#: argument of a script call. The pages use only some of these today; the guard below reads
#: them all, so a URL written in another of the forms is caught too.
_RESOURCE_URL = re.compile(
    r"""\b(?:src|href|action|hx-get|hx-post|hx-put|hx-patch|hx-delete)\s*=\s*"""
    r"""(?:"([^"]*)"|'([^']*)')"""
    r"""|\b(?:EventSource|fetch)\(\s*['"]([^'"]*)['"]"""
)


class FakeMiddleware:
    """The whole surface the two pages touch: an app to attach routes to, and no feed yet."""

    def __init__(self) -> None:
        self.app = FastAPI()
        self.activity_feed = None


def _resource_urls(html: str) -> list[str]:
    """Every URL in ``html`` that the page loads or sends a request to, as written."""
    return ["".join(groups) for groups in _RESOURCE_URL.findall(html)]


#: A way to serve the instance's app: it returns the app a browser reaches, and the prefix the
#: instance's pages live under there.
Serve = Callable[[ASGIApp], tuple[ASGIApp, str]]


def _at_the_root(app: ASGIApp) -> tuple[ASGIApp, str]:
    """The instance's app as the process serves it, and the path its pages live under."""
    return app, ""


def _mounted(app: ASGIApp) -> tuple[ASGIApp, str]:
    """An ASGI parent that mounts the instance's app under the prefix, and nothing else."""
    return Starlette(routes=[Mount(PREFIX, app=app)]), PREFIX


def _behind_a_proxy(app: ASGIApp) -> tuple[ASGIApp, str]:
    """A proxy that strips the prefix and forwards, as an HTTP reverse proxy does.

    The inner app sees ``/dashboard``, not ``/p/x/dashboard``, and no ``root_path``: it does not
    learn the prefix. A request outside the prefix gets 404, as a request to a path that the
    launcher does not serve would.
    """

    async def proxy(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        path: str = scope["path"]
        if not path.startswith(PREFIX + "/"):
            await PlainTextResponse("outside the prefix", status_code=404)(
                scope, receive, send
            )
            return
        inner = path[len(PREFIX) :]
        await app({**scope, "path": inner, "raw_path": inner.encode()}, receive, send)

    return proxy, PREFIX


SETUPS = pytest.mark.parametrize(
    "serve",
    [_at_the_root, _mounted, _behind_a_proxy],
    ids=["at the root", "mounted under a prefix", "behind a prefix-stripping proxy"],
)


#: A page is addressed without a trailing slash. Someone who types one still reaches the page,
#: by a redirect that keeps the prefix: the relative paths on the page resolve against the
#: page's address, so the page must not be served at the second form as well.
SLASHES = pytest.mark.parametrize(
    "slash", ["", "/"], ids=["page", "page with a trailing slash"]
)


def _open(client: TestClient, url: str) -> tuple[str, Any]:
    """GET ``url`` as a browser does, and return where it ended and the response.

    A redirect's ``Location`` is resolved against the URL that answered it, as a browser
    resolves it, so a relative redirect and an absolute one are followed alike.
    """
    for _ in range(5):
        response = client.get(url, follow_redirects=False)
        if not response.is_redirect:
            return url, response
        url = urljoin(url, response.headers["location"])
    raise AssertionError(f"more than five redirects, last to {url}")


@SETUPS
@SLASHES
def test_the_dashboard_loads_its_scripts_and_boxes(serve: Serve, slash: str) -> None:
    """Every script and every box fragment that the page names is reached where it was loaded."""
    mw = FakeMiddleware()
    enable_dashboard(mw)
    app, prefix = serve(mw.app)
    client = TestClient(app)

    page_url, page = _open(client, f"{BASE}{prefix}/dashboard{slash}")
    assert page.status_code == 200, f"{prefix}/dashboard{slash} ended at {page_url}"
    assert urlsplit(page_url).path == f"{prefix}/dashboard"
    urls = _resource_urls(page.text)
    assert len(urls) == 2 + len(mw.dashboard.specs()), (
        f"the page should name two scripts and one fragment per box, and names {urls}"
    )

    for url in urls:
        resolved = urljoin(page_url, url)
        assert urlsplit(resolved).path.startswith(f"{prefix}/dashboard/"), (
            f"{url!r} on {page_url} resolves to {resolved}, outside the page's own process"
        )
        response = client.get(resolved)
        assert response.status_code == 200, (
            f"{url!r} resolves to {resolved}: {response.status_code}"
        )
        assert response.text.strip() != ""


@SETUPS
def test_the_logs_form_posts_to_its_own_instance(serve: Serve) -> None:
    """The Logs box's form reaches the level route where the page was loaded.

    A box is placed into the page, so a URL in it resolves against the page's address, not
    against the fragment's.
    """
    name = "tests.pages_under_a_prefix.logs_form"
    mw = FakeMiddleware()
    enable_dashboard(mw)
    app, prefix = serve(mw.app)
    client = TestClient(app)
    page_url = f"{BASE}{prefix}/dashboard"
    box = client.get(urljoin(page_url, "dashboard/box/logs")).text
    [form_url] = re.findall(r'<form [^>]*hx-post="([^"]*)"', box)

    resolved = urljoin(page_url, form_url)
    try:
        response = client.post(resolved, json={"logger": name, "level": "ERROR"})
        assert urlsplit(resolved).path == f"{prefix}/dashboard/logs/level"
        assert response.status_code == 200, f"{form_url!r} resolves to {resolved}"
        assert logging.getLogger(name).level == logging.ERROR
    finally:
        logging.getLogger(name).setLevel(logging.NOTSET)


@SETUPS
@SLASHES
def test_the_activity_page_opens_its_stream(
    serve: Serve, slash: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream that the page opens is reached where the page was loaded, and carries a record.

    A stream ends after its lifetime, and a test client reads a response to its end. The
    lifetime is set to nothing, so the stream sends what is buffered and ends at once.
    """
    monkeypatch.setattr(activity, "STREAM_LIFETIME_SECONDS", 0.0)
    logger_name = "tests.pages_under_a_prefix"
    mw = FakeMiddleware()
    enable_activity_feed(mw, logger_name=logger_name)
    logging.getLogger(logger_name).info("a record through the prefix")
    app, prefix = serve(mw.app)
    client = TestClient(app)

    page_url, page = _open(client, f"{BASE}{prefix}/activity{slash}")
    assert page.status_code == 200, f"{prefix}/activity{slash} ended at {page_url}"
    assert urlsplit(page_url).path == f"{prefix}/activity"
    urls = _resource_urls(page.text)
    assert len(urls) == 1, (
        f"the page should name its stream and nothing else, and names {urls}"
    )

    resolved = urljoin(page_url, urls[0])
    assert urlsplit(resolved).path == f"{prefix}/activity/stream", (
        f"{urls[0]!r} on {page_url} resolves to {resolved}, not to the page's own stream"
    )
    stream = client.get(resolved)
    assert stream.status_code == 200, (
        f"{urls[0]!r} resolves to {resolved}: {stream.status_code}"
    )
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "a record through the prefix" in stream.text


def test_no_page_and_no_redirect_names_an_absolute_path() -> None:
    """The HTML and the redirects that KSM serves name every URL relative to the page.

    The tests above follow each URL under a prefix. This one reads every page, every default
    box fragment and every redirect that KSM serves, and names each absolute URL it finds, so a
    new one is caught where it is written and not only by the resource it fails to load.
    """
    mw = FakeMiddleware()
    enable_dashboard(mw)
    enable_activity_feed(mw, logger_name="tests.pages_under_a_prefix")
    client = TestClient(mw.app)

    served = {path: client.get(path).text for path in ("/dashboard", "/activity")}
    for spec in mw.dashboard.specs():
        served[f"/dashboard/box/{spec.id}"] = client.get(
            f"/dashboard/box/{spec.id}"
        ).text
    urls = {path: _resource_urls(html) for path, html in served.items()}
    for path in ("/dashboard/", "/activity/"):
        response = client.get(path, follow_redirects=False)
        assert response.is_redirect, f"{path} should redirect to the page"
        urls[path] = [response.headers["location"]]

    # Each page and each redirect names at least one URL, so the scan below reads something.
    pages_and_redirects = ("/dashboard", "/activity", "/dashboard/", "/activity/")
    assert all(urls[path] for path in pages_and_redirects), f"a URL is missing: {urls}"
    absolute = {
        path: [url for url in found if url.startswith("/") or "://" in url]
        for path, found in urls.items()
    }
    assert not any(absolute.values()), f"absolute URLs served: {absolute}"


@pytest.mark.parametrize("page", ["dashboard", "activity"])
def test_the_trailing_slash_redirect_keeps_the_query(page: str) -> None:
    """The redirect to the page keeps the query, as the redirect it replaces did."""
    mw = FakeMiddleware()
    enable_dashboard(mw)
    enable_activity_feed(mw, logger_name="tests.pages_under_a_prefix")
    app, prefix = _behind_a_proxy(mw.app)

    page_url, response = _open(TestClient(app), f"{BASE}{prefix}/{page}/?tab=a&b=1")
    assert response.status_code == 200
    assert page_url == f"{BASE}{prefix}/{page}?tab=a&b=1"


@SETUPS
@SLASHES
def test_the_tab_in_the_query_opens_under_a_prefix(serve: Serve, slash: str) -> None:
    """A link to ``dashboard?tab=WMS`` opens on WMS wherever the page is served.

    A launcher links one process's page under its own prefix, and a person may type the
    trailing slash. The query reaches the page through both, and the page opens the tab it
    names.
    """
    mw = FakeMiddleware()
    registry = enable_dashboard(mw)

    @registry.box("spawn", title="Spawn", tab="WMS")
    def spawn_box(middleware: Any) -> str:
        return "<p>spawn</p>"

    app, prefix = serve(mw.app)
    page_url, page = _open(TestClient(app), f"{BASE}{prefix}/dashboard{slash}?tab=WMS")

    assert page.status_code == 200
    assert page_url == f"{BASE}{prefix}/dashboard?tab=WMS"
    assert re.findall(r'id="tab-([^"]+)" class="tab-radio" checked', page.text) == ["wms"]
