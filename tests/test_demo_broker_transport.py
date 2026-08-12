"""``demo.transferunits.middleware``'s transport seam (#79, ADR 0034).

``ensure_transport`` is the demo's fill of the library's transport hook: bring up this unit's
own in-process amqtt broker, idempotently, on a daemon thread. Real sockets throughout -- an
in-process broker is exactly what the demo runs, and a mock here would prove nothing about the
actual race ADR 0034 exists to close.
"""

from __future__ import annotations

import asyncio
import socket
import threading

import aiomqtt

from demo.transferunits.middleware import _listening, ensure_transport

# Distinct from conftest.py's MQTT_TEST_PORT (18831) and test_transfer_unit_plc.py's 18841:
# each of these live-broker tests needs a port nothing else in the suite is using at the
# same time.
_PORT = 18851


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestListening:
    """This class tests the plain probe ensure_transport's idempotency rests on."""

    def test_nothing_listening_is_false(self):
        assert _listening("127.0.0.1", _free_port()) is False

    def test_something_listening_is_true(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            assert _listening("127.0.0.1", port) is True


class TestEnsureTransport:
    """This class tests ensure_transport against real sockets."""

    def test_brings_up_a_broker_a_real_client_can_reach(self):
        port = _PORT
        ensure_transport("127.0.0.1", port)
        try:
            assert _listening("127.0.0.1", port)

            async def _connect_and_publish() -> None:
                async with aiomqtt.Client("127.0.0.1", port=port) as client:
                    await client.publish("demo/79/probe", b"1")

            asyncio.run(_connect_and_publish())
        finally:
            # No teardown API exists on purpose (ADR 0034: the library owns no lifetime, and
            # neither does this call) -- the broker's daemon thread dies with the test process.
            pass

    def test_is_idempotent_for_its_own_broker(self):
        """A second call at the same address the first call just brought up is a fast no-op,
        not a second bind attempt (which would raise on an address already in use)."""
        port = _PORT + 1
        ensure_transport("127.0.0.1", port)
        assert _listening("127.0.0.1", port)

        ensure_transport("127.0.0.1", port)  # must not raise
        assert _listening("127.0.0.1", port)

    def test_does_nothing_when_something_already_listens(self, monkeypatch):
        """ensure_transport never starts a second broker on top of one already at this
        address (ADR 0034: idempotent -- a broker already listening means nothing happens).

        Stubs the probe rather than reusing the ``mqtt_broker`` fixture's real broker: a bare
        TCP connect-and-immediately-close -- exactly what a real ``_listening`` probe against
        it would do -- leaves that amqtt broker unable to shut down cleanly afterwards. That
        is a fixture-teardown quirk of probing a live amqtt broker, not a property of
        ensure_transport, so it has no business here.
        """
        import demo.transferunits.middleware as mw

        called = threading.Event()
        real_thread = threading.Thread

        def spying_thread(*args, **kwargs):
            called.set()
            return real_thread(*args, **kwargs)

        monkeypatch.setattr(mw, "_listening", lambda host, port: True)
        monkeypatch.setattr(mw.threading, "Thread", spying_thread)

        ensure_transport("127.0.0.1", 18859)

        assert not called.is_set(), "a broker already listening must spawn no new thread"
