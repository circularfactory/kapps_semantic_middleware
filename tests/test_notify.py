"""Sending a notification with ``mw.notify``, offline.

The sender finds the resource's one live Service in the graph and posts the Operation's IRI
to that Service's event trigger. It writes nothing. Every way the notification cannot be
delivered raises ``NotificationUndelivered``. The sender's store is faked; the receiver is a
real middleware app served over HTTP without its startup, so the real REST route is crossed
and nothing reaches a triple store.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from kapps_ogm import Node
from kapps_triplestore_interface import IRI
from kapps_semantic_middleware import SemanticMiddleware
from kapps_semantic_middleware.errors import NotificationUndelivered
from kapps_semantic_middleware.vocabulary import SVC

SENDER = "http://example.org/test#Planner"
RECEIVER = "http://example.org/test#Module1"
SERVICE_CLASS = "http://example.org/test#ModuleService"
OPERATION = "http://example.org/test#Transport1"


class _SenderStore:
    """The sender's view of the graph: the live Services of the receiver and their addresses."""

    def __init__(self, addresses: dict):
        self._addresses = addresses  # Service IRI -> svc:address

    def query(self, query, update=False, convert_bindings=False):
        if update:
            raise AssertionError(f"notify() wrote to the store: {query}")
        assert f"<{RECEIVER}>" in query
        assert f"<{SVC.address}>" in query  # only a Service with an address is live
        bindings = [{"svc": svc, "addr": addr} for svc, addr in self._addresses.items()]
        return {"results": {"bindings": bindings}}

    def __getattr__(self, name):
        raise AssertionError(f"notify() used the store's {name}")


class _SenderOGM:
    def __init__(self, addresses: dict):
        self.db = _SenderStore(addresses)

    def __getattr__(self, name):
        raise AssertionError(f"notify() used the OGM's {name}")


class _ReceiverOGM:
    """The receiver's OGM: it fetches anything after `delay` seconds, or raises `error`."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.error: Exception | None = None

    def fetch(self, **kwargs):
        time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return Node(id=kwargs["instance_iri"])

    def __getattr__(self, name):
        raise AssertionError(f"the receiver used the OGM's {name}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _sender(addresses: dict) -> SemanticMiddleware:
    return SemanticMiddleware(
        mode="resource",
        resource_iri=SENDER,
        service_class=SERVICE_CLASS,
        ogm=_SenderOGM(addresses),
        heartbeat_interval=None,
    )


@pytest.fixture
def receiver():
    """A receiver with a notification handler, served on a free port without its startup."""
    port = _free_port()
    ogm = _ReceiverOGM()
    mw = SemanticMiddleware(
        mode="resource",
        resource_iri=RECEIVER,
        service_class=SERVICE_CLASS,
        ogm=ogm,
        port=port,
        heartbeat_interval=None,
    )
    received: list = []
    mw.on_notification(received.append)
    config = uvicorn.Config(
        mw.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    mw.received = received
    mw.test_ogm = ogm
    yield mw
    server.should_exit = True
    thread.join(timeout=10)


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.02)


def test_notify_hands_the_operation_to_the_receivers_handler(receiver):
    sender = _sender({"http://example.org/test#Module1Service": receiver.address})

    sender.notify(operation=OPERATION, resource=RECEIVER)

    _wait_for(lambda: receiver.received)
    assert [operation.id for operation in receiver.received] == [IRI(OPERATION)]


def test_a_resource_with_no_live_service_is_not_notified():
    sender = _sender({})

    with pytest.raises(NotificationUndelivered, match="no live Service") as raised:
        sender.notify(operation=OPERATION, resource=RECEIVER)
    assert RECEIVER in str(raised.value)


def test_a_resource_with_two_live_services_is_refused_and_both_are_named(receiver):
    first = "http://example.org/test#Module1Controller"
    second = "http://example.org/test#Module1Monitor"
    sender = _sender({first: receiver.address, second: receiver.address})

    with pytest.raises(
        NotificationUndelivered, match="more than one live Service"
    ) as raised:
        sender.notify(operation=OPERATION, resource=RECEIVER)
    assert first in str(raised.value) and second in str(raised.value)
    time.sleep(0.2)
    assert receiver.received == []


def test_a_post_that_cannot_connect_raises():
    sender = _sender({"http://example.org/test#Module1Service": "http://127.0.0.1:9"})

    with pytest.raises(NotificationUndelivered, match="did not take") as raised:
        sender.notify(operation=OPERATION, resource=RECEIVER)
    assert raised.value.__cause__ is not None


@pytest.mark.parametrize(
    "address, cause",
    [
        ("http://[::1", httpx.InvalidURL),
        (f"http://{'a' * 64}.org:9", UnicodeError),  # a host label longer than 63 characters
    ],
    ids=["invalid-url", "label-too-long"],
)
def test_a_malformed_address_raises(address, cause):
    """A malformed address raises ``NotificationUndelivered`` too, although the error of the post
    is not one of httpx's ``HTTPError``s."""
    sender = _sender({"http://example.org/test#Module1Service": address})

    with pytest.raises(NotificationUndelivered, match="did not take") as raised:
        sender.notify(operation=OPERATION, resource=RECEIVER)
    assert isinstance(raised.value.__cause__, cause)


def test_a_receiver_that_answers_with_an_error_raises(receiver):
    receiver.test_ogm.error = RuntimeError("the store is down")
    sender = _sender({"http://example.org/test#Module1Service": receiver.address})

    with pytest.raises(NotificationUndelivered, match="did not take") as raised:
        sender.notify(operation=OPERATION, resource=RECEIVER)
    assert isinstance(raised.value.__cause__, httpx.HTTPStatusError)
    assert receiver.received == []


def test_a_receiver_that_does_not_answer_in_time_raises(receiver):
    receiver.test_ogm.delay = 2.0
    sender = _sender({"http://example.org/test#Module1Service": receiver.address})

    with pytest.raises(NotificationUndelivered, match="Timeout"):
        sender.notify(operation=OPERATION, resource=RECEIVER, timeout=0.3)


def test_notify_is_resource_mode_only():
    watchdog = SemanticMiddleware(mode="watchdog", ogm=_SenderOGM({}))
    with pytest.raises(RuntimeError, match="resource mode"):
        watchdog.notify(operation=OPERATION, resource=RECEIVER)
