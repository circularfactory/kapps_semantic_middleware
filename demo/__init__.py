"""Runnable demonstrations built on the middleware library.

Each subpackage is one complete scenario. Root ADR 0004 puts every scenario
part here, so the library in ``src/kapps_semantic_middleware`` stays generic
and names no domain term.

- ``transferunits``: the TransferUnit factory. It runs as several processes
  and a person drives it from a browser.
"""
