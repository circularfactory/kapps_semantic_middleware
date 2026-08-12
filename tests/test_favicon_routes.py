"""Offline tests for the quiet /favicon.ico route (#89 item 2).

Before this, every page load of the launcher index, the PLC panel, and the control
station logged a 404 for /favicon.ico -- the only console error on an otherwise clean
load. Each of the three apps now answers with a bare 204 instead. No GraphDB, no
network: none of the apps' lifespans are started here (the TestClient only runs
startup/shutdown when entered as a context manager, which none of these tests do), so
a plain, unconstructed SemanticMiddleware instance is enough to exercise its `app`
property.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestLauncherIndexFavicon:
    def test_index_answers_favicon_with_204(self):
        from demo.transferunits.index import app

        response = TestClient(app).get("/favicon.ico")

        assert response.status_code == 204
        assert response.content == b""


class TestPlcPanelFavicon:
    def test_panel_answers_favicon_with_204(self):
        from demo.transferunits.plc.panel import app

        response = TestClient(app).get("/favicon.ico")

        assert response.status_code == 204
        assert response.content == b""


class TestSemanticMiddlewareFavicon:
    """Covers both the control station (Controller) and any unit middleware: both build
    their app through SemanticMiddleware.app, and the route is added exactly once there.
    """

    def _build_middleware(self):
        from kapps_semantic_middleware import Mode, SemanticMiddleware

        return SemanticMiddleware(
            mode=Mode.RESOURCE,
            resource_iri="http://example.org/FaviconTestResource",
            service_class="http://example.org/FaviconTestService",
            ogm=object(),  # Never dereferenced before the app's lifespan runs.
            host="127.0.0.1",
            port=0,
            heartbeat_interval=None,
        )

    def test_middleware_app_answers_favicon_with_204(self):
        mw = self._build_middleware()

        response = TestClient(mw.app).get("/favicon.ico")

        assert response.status_code == 204
        assert response.content == b""

    def test_favicon_route_is_registered_exactly_once(self):
        """The `app` property is consulted on every access; the one-time guard must stop
        the favicon route (and the root-route swap) from being added twice."""
        mw = self._build_middleware()

        app = mw.app
        app = mw.app  # Second access -- must not duplicate routes.

        favicon_routes = [
            route
            for route in app.routes
            if getattr(route, "path", None) == "/favicon.ico"
        ]
        assert len(favicon_routes) == 1
