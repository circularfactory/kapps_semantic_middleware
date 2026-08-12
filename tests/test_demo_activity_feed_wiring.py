"""Regression tests for the demo's activity-feed wiring (#88).

ADR 0029 gives every middleware a library-level /activity feed, and #67 built it. #88
found it switched off everywhere in the demo anyway, because neither runner script
passed ``activity_feed=True`` to its middleware constructor. These tests pin the
wiring at that seam -- the keyword arguments each runner's ``main()`` passes to
``SemanticMiddleware``/``Controller`` -- so a future refactor cannot silently regress
it again the way #67 -> #88 did.

No GraphDB, no broker, no network: every collaborator ``main()`` touches
(``graphdb_for``, ``OGM``, ``ClassScope``/``Controller``, ``run_server``,
``bind_free_socket``) is replaced with a stand-in.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock


class _FakeListeningSocket:
    """Stands in for the real, still-listening socket bind_free_socket returns.

    main() only reads the assigned port back off it and hands it to run_server,
    which is itself stubbed out below, so nothing here needs to be a real socket.
    """

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 54321)


async def _noop_run_server(*args, **kwargs) -> None:
    return None


def test_transferunit_middleware_enables_the_activity_feed(monkeypatch):
    """The per-unit middleware runner must pass activity_feed=True.

    #67 built the feed as an opt-in. #88 found it dark everywhere in the demo because
    this one keyword argument was never passed at the constructor call.
    """
    import demo.transferunits.middleware as mw_runner

    semantic_middleware_cls = MagicMock(return_value=MagicMock())

    monkeypatch.setattr(mw_runner, "SemanticMiddleware", semantic_middleware_cls)
    monkeypatch.setattr(mw_runner, "graphdb_for", MagicMock())
    monkeypatch.setattr(mw_runner, "OGM", MagicMock())
    monkeypatch.setattr(mw_runner, "ClassScope", MagicMock())
    monkeypatch.setattr(mw_runner, "run_server", _noop_run_server)
    monkeypatch.setattr(mw_runner, "bind_free_socket", lambda host: _FakeListeningSocket())
    monkeypatch.setattr("sys.argv", ["prog", "--unit-index", "1"])

    asyncio.run(mw_runner.main())

    _, kwargs = semantic_middleware_cls.call_args
    assert kwargs.get("activity_feed") is True


def test_transferunit_middleware_passes_its_own_ensure_transport(monkeypatch):
    """The per-unit middleware runner must fill the library's transport seam (#79, ADR 0034)
    with its own ``ensure_transport`` -- the in-process broker starter -- not leave it unset."""
    import demo.transferunits.middleware as mw_runner

    semantic_middleware_cls = MagicMock(return_value=MagicMock())

    monkeypatch.setattr(mw_runner, "SemanticMiddleware", semantic_middleware_cls)
    monkeypatch.setattr(mw_runner, "graphdb_for", MagicMock())
    monkeypatch.setattr(mw_runner, "OGM", MagicMock())
    monkeypatch.setattr(mw_runner, "ClassScope", MagicMock())
    monkeypatch.setattr(mw_runner, "run_server", _noop_run_server)
    monkeypatch.setattr(mw_runner, "bind_free_socket", lambda host: _FakeListeningSocket())
    monkeypatch.setattr("sys.argv", ["prog", "--unit-index", "1"])

    asyncio.run(mw_runner.main())

    _, kwargs = semantic_middleware_cls.call_args
    assert kwargs.get("ensure_transport") is mw_runner.ensure_transport


def test_control_station_enables_the_activity_feed(monkeypatch):
    """The control-station runner must pass activity_feed=True to Controller.

    Same regression as the unit middleware: the constructor supports the feed, but
    the runner never opted in, so /activity 404'd on the control station too.
    """
    import demo.transferunits.control_station as cs_runner

    controller_cls = MagicMock(return_value=MagicMock())

    monkeypatch.setattr(cs_runner, "Controller", controller_cls)
    monkeypatch.setattr(cs_runner, "graphdb_for", MagicMock())
    monkeypatch.setattr(cs_runner, "OGM", MagicMock())
    monkeypatch.setattr(cs_runner, "run_server", _noop_run_server)
    monkeypatch.setattr(cs_runner, "bind_free_socket", lambda host: _FakeListeningSocket())
    monkeypatch.setattr("sys.argv", ["prog"])

    asyncio.run(cs_runner.main())

    _, kwargs = controller_cls.call_args
    assert kwargs.get("activity_feed") is True
