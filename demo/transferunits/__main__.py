"""Entry point for the Launcher: seeds, spawns, serves the index page, tears down.

Builds launcher.py's Factory and wires it into index.py's routes, then runs uvicorn on the
Launcher's fixed port. Every other port in the factory is dynamic (ADR 0029), so this one is
the only bookmarkable address -- the index page is the only way a person finds anything else.
"""

from __future__ import annotations

import argparse

import uvicorn

from . import index
from .launcher import Factory

LAUNCHER_HOST = "127.0.0.1"
LAUNCHER_PORT = 8080


def main() -> None:
    parser = argparse.ArgumentParser(description="TransferUnit factory launcher")
    parser.add_argument("--units", type=int, default=2, help="Number of TransferUnits (default: 2)")
    parser.add_argument(
        "--force", action="store_true", help="Clear the graph even if a live factory runs"
    )
    args = parser.parse_args()

    factory = Factory()
    launcher_address = f"http://{LAUNCHER_HOST}:{LAUNCHER_PORT}/"

    try:
        factory.start(args.units, force=args.force)
        index.configure_factory(factory, launcher_address)
        print(f"\nFactory running. Index page at {launcher_address}", flush=True)
        uvicorn.run(index.app, host=LAUNCHER_HOST, port=LAUNCHER_PORT, log_level="warning")
    finally:
        print("\nStopping factory...", flush=True)
        factory.stop_all()


if __name__ == "__main__":
    main()
