"""The status page warns of an advertised address that other machines cannot reach.

The box shows for a loopback and for an unspecified address, never for a reachable one. It lists
the machine's addresses and the settings of a restart on a development page, and only the
warning on a page with `dev=False`. The machine's network is read once, when the dashboard is
enabled.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kapps_semantic_middleware import _reachability
from kapps_semantic_middleware.dashboard import enable_dashboard
from kapps_semantic_middleware._reachability import (
    BOX_ID,
    OwnNetwork,
    advertises_an_unreachable_address,
)


class _Instance:
    def __init__(self, address: str, port: int = 8000) -> None:
        self.app = FastAPI()
        self.address = address
        self.port = port


def _lookup(monkeypatch: pytest.MonkeyPatch, network: OwnNetwork) -> list[int]:
    reads: list[int] = []

    def read() -> OwnNetwork:
        reads.append(1)
        return network

    monkeypatch.setattr(_reachability, "read_own_network", read)
    return reads


def _box(instance: _Instance, **options: Any) -> str | None:
    registry = enable_dashboard(instance, **options)
    if registry.get(BOX_ID) is None:
        return None
    return TestClient(instance.app).get(f"/dashboard/box/{BOX_ID}").text


NETWORK = OwnNetwork("lab-pc", ("192.168.1.20", "fd00::5"))


@pytest.mark.parametrize(
    "address",
    [
        "http://127.0.0.1:8000",
        "http://127.0.0.5:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
        "http://0.0.0.0:8000",
        "http://[::]:8000",
    ],
)
def test_a_loopback_or_unspecified_address_shows_the_warning_and_the_helper(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lookup(monkeypatch, NETWORK)
    text = _box(_Instance(address))
    assert text is not None
    assert "Other machines cannot reach this service" in text
    assert "lab-pc" in text and "192.168.1.20" in text and "fd00::5" in text
    assert 'host="0.0.0.0"' in text
    assert 'address="http://192.168.1.20:8000"' in text
    assert "without authentication" in text
    assert "firewall" in text.lower()


@pytest.mark.parametrize(
    "address", ["http://192.168.1.20:8000", "http://lab-pc:8000", "https://svc.example/x"]
)
def test_a_reachable_address_shows_no_box_and_reads_no_network(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = _lookup(monkeypatch, NETWORK)
    assert _box(_Instance(address)) is None
    assert reads == []


def test_without_a_non_loopback_address_the_box_says_so_and_uses_a_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lookup(monkeypatch, OwnNetwork("lab-pc", ()))
    text = _box(_Instance("http://127.0.0.1:8000"))
    assert text is not None
    assert "No network address other than a loopback one was found" in text
    assert "&lt;this machine&#x27;s address&gt;:8000" in text


def test_a_page_with_dev_false_names_no_address_host_or_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lookup(monkeypatch, NETWORK)
    text = _box(_Instance("http://127.0.0.1:8000"), dev=False)
    assert text is not None
    assert "Other machines cannot reach this service" in text
    for secret in ("lab-pc", "192.168.1.20", "127.0.0.1", "http://", "8000", "0.0.0.0"):
        assert secret not in text


def test_the_network_is_read_once_not_per_render(monkeypatch: pytest.MonkeyPatch) -> None:
    reads = _lookup(monkeypatch, NETWORK)
    instance = _Instance("http://127.0.0.1:8000")
    enable_dashboard(instance)
    client = TestClient(instance.app)
    client.get(f"/dashboard/box/{BOX_ID}")
    client.get(f"/dashboard/box/{BOX_ID}")
    assert reads == [1]


def test_a_box_without_default_boxes_is_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    reads = _lookup(monkeypatch, NETWORK)
    assert _box(_Instance("http://127.0.0.1:8000"), register_default_boxes=False) is None
    assert reads == []


def test_the_real_lookup_never_raises_and_lists_no_loopback_address() -> None:
    network = _reachability.read_own_network()
    assert not any(advertises_an_unreachable_address(f"http://{a}") for a in network.addresses)


def test_a_malformed_address_is_not_flagged() -> None:
    assert not advertises_an_unreachable_address("http://[bad")
    assert not advertises_an_unreachable_address(None)


def test_the_lookup_finds_none_when_resolving_fails_or_gives_only_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_reachability.socket, "gethostname", lambda: "lab-pc")

    def fails(*args: Any, **kwargs: Any) -> Any:
        raise OSError("no resolver")

    monkeypatch.setattr(_reachability.socket, "getaddrinfo", fails)
    assert _reachability.read_own_network() == OwnNetwork("lab-pc", ())

    def loopback_only(*args: Any, **kwargs: Any) -> Any:
        return [(2, 1, 6, "", ("127.0.1.1", 0)), (10, 1, 6, "", ("::1", 0, 0, 0))]

    monkeypatch.setattr(_reachability.socket, "getaddrinfo", loopback_only)
    assert _reachability.read_own_network() == OwnNetwork("lab-pc", ())


def test_a_machine_without_a_host_name_is_called_unnamed(monkeypatch: pytest.MonkeyPatch) -> None:
    _lookup(monkeypatch, OwnNetwork(None, ()))
    text = _box(_Instance("http://127.0.0.1:8000"))
    assert text is not None and "unnamed" in text and "None" not in text
