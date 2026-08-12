"""Entry point for running the TransferUnit PLC with panel.

This script takes five command-line flags: ``--unit-index``, ``--broker``, ``--broker-port``,
``--panel-port``, and ``--ramp-rate``. A ``--panel-port`` value of 0 asks the OS for a free
port. The script prints one line to stdout with the bound port. FastAPI runs on the PLC
event loop. It does not run uvicorn on a separate thread.
"""

from __future__ import annotations

import argparse
import asyncio
import socket

from .transfer_unit import DEFAULT_RAMP_RATE, TransferUnit
from .panel import app, configure_plc


def bind_free_socket(host: str) -> socket.socket:
    """Bind and listen on an OS-assigned free port, without releasing it.

    Reading back a discovered port, closing the socket, and letting uvicorn bind a new
    one reopens the allocate-hand-off-bind race ADR 0029 credits self-allocation with
    removing -- another process could take the port in between. Handing uvicorn this same,
    still-listening socket instead of a bare port number closes that gap.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(1)
    return sock


async def run_server(host: str, port: int, sock: socket.socket | None = None) -> None:
    """Run the FastAPI server using asyncio directly."""
    config = __import__("uvicorn").Config(app=app, host=host, port=port, log_level="warning")
    server = __import__("uvicorn").Server(config=config)
    await server.serve(sockets=[sock] if sock is not None else None)


async def main() -> None:
    parser = argparse.ArgumentParser(description="TransferUnit PLC with panel")
    parser.add_argument("--unit-index", type=int, default=1, help="Unit index (default: 1)")
    parser.add_argument("--broker", type=str, default="127.0.0.1", help="MQTT broker host")
    # 1883 here is only the bare fallback for a standalone run with the flag omitted -- the
    # launcher always passes --broker-port <seed.broker_port(unit_index)> explicitly (#79).
    parser.add_argument("--broker-port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--panel-port", type=int, default=0, help="Panel HTTP port (0 = free)")
    # A belt ramps toward its setpoint rather than snapping to it (#83); this is the "slow PID
    # controller" rate, in speed-units/s^2. Exposed as a flag -- unlike publish_interval -- so a
    # presentation can slow it down or a lap-time measurement (#82) can speed it up with no code
    # change.
    parser.add_argument(
        "--ramp-rate",
        type=float,
        default=DEFAULT_RAMP_RATE,
        help=f"Belt ramp rate, speed-units/s^2 (default: {DEFAULT_RAMP_RATE})",
    )
    args = parser.parse_args()

    # Determine panel port
    if args.panel_port == 0:
        sock = bind_free_socket("127.0.0.1")
        panel_port = sock.getsockname()[1]
    else:
        sock, panel_port = None, args.panel_port

    # Create and configure PLC
    plc = TransferUnit(
        unit_index=args.unit_index,
        broker=args.broker,
        port=args.broker_port,
        ramp_rate=args.ramp_rate,
    )
    configure_plc(plc)

    # Start PLC and server concurrently
    async with plc:
        print(f"Panel running on http://127.0.0.1:{panel_port}/", flush=True)
        await run_server("127.0.0.1", panel_port, sock)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass