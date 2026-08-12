"""Runnable example scenarios for the middleware library.

On disk this is ``examples/`` at the repository root (root ADR 0004); in an installed wheel the
same files ship under ``kapps_semantic_middleware.examples`` (the build remaps them), so the
``kapps-examples`` console script can locate and copy them out of ``site-packages`` into a
directory the user can actually open and run. The scenario scripts themselves use flat imports
(``import seed``), so they run from whatever directory they are copied into, not from here.
"""
