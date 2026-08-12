"""The TransferUnit factory: a small factory you start with one command.

ADR 0029 gives every participant its own process. An N-unit factory runs as
2N+2 processes today: one PLC and one middleware instance per unit, plus a
control station and the Launcher.

Start it with ``python -m demo.transferunits``. The Launcher then prints the
address of its index page, and that page links to every other screen.

The modules here:

- ``launcher``: seeds the graph, starts every child process, and stops them
  in order.
- ``index``: the Launcher's own web page, which shows the live topology.
- ``middleware``: the entry point that serves one middleware instance for one
  unit.
- ``control_station``: the entry point for the controller process.
- ``controller``: the controller itself, which is a middleware instance.
- ``station_board``: the controller's screen.
- ``algorithm``: the control loop. It is the only module here allowed to name
  a domain term.
- ``seed``: the starting facts this factory writes into the graph.
- ``plc``: one unit's mock PLC, and the panel that drives it.
"""
