"""The status page's warning for an address that other machines cannot reach.

A service writes its address into the graph, and other participants call it there. The default
address is `http://127.0.0.1:<port>`, which a participant on another machine reads as its own
machine. When the address of the instance is a loopback or an unspecified one, the System tab
shows a box that says so and shows what to set for a restart.

The box writes nothing and sends no traffic: the page stays read-only. The machine's host name
and addresses are read once, when the dashboard is enabled, as the instance's name is, and no
render reads them again. The box is registered only for such an address: the address is fixed at
construction, so a page with a reachable one has no box.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit

from kapps_semantic_middleware.dashboard import DashboardRegistry, escape

#: The id of the box on the page.
BOX_ID = "reachability"

_PLACEHOLDER = "<this machine's address>"


@dataclass(frozen=True)
class OwnNetwork:
    """The machine's host name and its non-loopback addresses, read once."""

    host_name: Optional[str]
    addresses: Tuple[str, ...]


def read_own_network() -> OwnNetwork:
    """Read the host name, and the non-loopback addresses that resolving it gives.

    No lookup is fatal: a machine that cannot resolve its own name gives no addresses, and the
    box then says so.
    """
    try:
        name: Optional[str] = socket.gethostname() or None
    except OSError:
        return OwnNetwork(None, ())
    found: list[str] = []
    if name:
        try:
            infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
        except OSError:
            infos = []
        for info in infos:
            text = str(info[4][0]).split("%", 1)[0]
            try:
                address = ipaddress.ip_address(text)
            except ValueError:
                continue
            if address.is_loopback or address.is_unspecified or address.is_link_local:
                continue
            if text not in found:
                found.append(text)
    return OwnNetwork(name, tuple(sorted(found)))


def advertises_an_unreachable_address(address: Any) -> bool:
    """Whether `address` names a loopback or an unspecified host."""
    try:
        host = urlsplit(str(address)).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return False
    return parsed.is_loopback or parsed.is_unspecified


def _port_of(middleware: Any) -> Any:
    try:
        port = urlsplit(str(middleware.address)).port
    except ValueError:
        port = None
    return port or getattr(middleware, "port", None) or "<port>"


def _url_host(address: str) -> str:
    return f"[{address}]" if ":" in address else address


def register_if_unreachable(registry: DashboardRegistry, middleware: Any) -> None:
    """Register the box when the instance's address is a loopback or an unspecified one.

    The machine's network is read here, once, and only for such an address.
    """
    if not advertises_an_unreachable_address(getattr(middleware, "address", None)):
        return
    network = read_own_network()

    @registry.box(id=BOX_ID, title="Reachability", tab="System")
    def reachability_box(mw: Any) -> str:
        warning = (
            '<div class="error-box"><p>Other machines cannot reach this service.</p></div>'
            "<p>It advertises a loopback or an unspecified address, so a participant on "
            "another machine reads an address that points at itself.</p>"
        )
        if not registry.dev:
            return warning

        port = _port_of(mw)
        named = f"<code>{escape(network.host_name)}</code>" if network.host_name else "unnamed"
        if network.addresses:
            listed = "".join(f"<li><code>{escape(a)}</code></li>" for a in network.addresses)
            machine = (
                f"<p>This machine is {named}. Its network "
                f"addresses:</p><ul>{listed}</ul>"
            )
            example = _url_host(network.addresses[0])
        else:
            machine = (
                f"<p>This machine is {named}. No network "
                "address other than a loopback one was found for it.</p>"
            )
            example = _PLACEHOLDER
        settings = (
            "<p>For a restart, set:</p><ul>"
            f'<li>the server runner listens on all interfaces: <code>host="0.0.0.0"</code>, '
            f"<code>port={escape(port)}</code>, or on one interface instead;</li>"
            f'<li>the middleware advertises it: <code>address="http://{escape(example)}:'
            f'{escape(port)}"</code>.</li></ul>'
        )
        caution = (
            "<p>Exposing the service makes its endpoints reachable from the network without "
            "authentication. A firewall, WSL or Docker may need a port forwarded.</p>"
        )
        return warning + machine + settings + caution
