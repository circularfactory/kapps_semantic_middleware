"""Every resource-mode middleware an example constructs must also be served (#44).

Both existing scenarios used to construct a middleware for their *client* — scenario 1's
planner, scenario 2's mobile robot — and never run it. Because `uvicorn` never started them,
`on_start_up` never fired: no Service individual, no `svc:address`, no heartbeat, no
event-trigger route. The instances existed only to hold an `ogm`, which made
`mode=Mode.RESOURCE` a label with no runtime consequence — and a reader would reasonably
conclude that is how clients are meant to work.

This is a **static** check rather than a runtime one, and deliberately so. A runtime assertion
inside one scenario proves that scenario; parsing every example proves the pattern cannot come
back in a *new* one, which is what #44 was actually protecting — it was filed because the
habit was about to be copied into scenario 3.

No GraphDB, no network: this reads the source.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

KNOWN_UNSERVED: dict[str, int] = {
    # Empty on purpose. The one entry that lived here, `scenario3_transferunit.py`, was retired
    # with its notebook and `mock_transferunit.py` when the factory demo landed — ADR 0029
    # promised that retirement, and `demo/transferunits/` is now the only scenario 3. Its
    # exemption went with the file; see #62 for the cross-loop deadlock that earned it.
    #
    # Keep this dict and the test below even while it is empty. A future example that cannot
    # serve its middleware gets an entry here with its issue number, not a silent skip.
}
"""Examples that legitimately do not serve a middleware yet, each with its issue.

An exemption is a ticket, not a silence. Listing it here keeps the guard failing for every
*other* example while making the debt visible, and the test below asserts each entry is still
earning its place — an exemption that starts passing has to be removed."""


def _example_scripts():
    """Every example that constructs a middleware at all."""
    return sorted(
        path
        for path in EXAMPLES.glob("*.py")
        if "SemanticMiddleware(" in path.read_text(encoding="utf-8")
    )


def _constructed_and_served(tree: ast.AST) -> tuple[set, set]:
    """The names bound to a `SemanticMiddleware(...)`, and the names handed to a server.

    "Handed to a server" is any call whose name contains `start_server` taking the name as its
    first positional argument. Matching on the helper rather than on `uvicorn` directly is what
    keeps this honest about *this* repo's examples, all of which route through one helper.
    """
    constructed, served = set(), set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name) and func.id == "SemanticMiddleware":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constructed.add(target.id)

        if isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if "start_server" in name and isinstance(node.args[0], ast.Name):
                served.add(node.args[0].id)

    return constructed, served


@pytest.mark.parametrize(
    "script", _example_scripts(), ids=lambda p: p.name
)
def test_every_constructed_middleware_is_served(script):
    if script.name in KNOWN_UNSERVED:
        pytest.skip(f"known unserved, tracked as #{KNOWN_UNSERVED[script.name]}")

    tree = ast.parse(script.read_text(encoding="utf-8"))

    constructed, served = _constructed_and_served(tree)
    unserved = constructed - served

    assert not unserved, (
        f"{script.name} constructs {sorted(unserved)} in resource mode and never serves "
        f"it. `on_start_up` will not fire, so it registers no Service, advertises no "
        f"svc:address, and holds no heartbeat — `mode=Mode.RESOURCE` becomes a label with "
        f"no runtime consequence (#44). Pass it to `_start_server` and stop it on the way "
        f"out, or drop the middleware and use a bare OGM if it genuinely is not a peer."
    )


def test_the_guard_would_catch_a_regression():
    """The check has to be able to fail, or it proves nothing.

    Guards this test itself: if `_constructed_and_served` ever stopped recognising the
    construction pattern, every example would pass vacuously.
    """
    source = (
        "client = SemanticMiddleware(mode=Mode.RESOURCE, resource_iri='x')\n"
        "served = SemanticMiddleware(mode=Mode.RESOURCE, resource_iri='y')\n"
        "server, thread = _start_server(served, 8000)\n"
    )

    constructed, served = _constructed_and_served(ast.parse(source))

    assert constructed == {"client", "served"}
    assert served == {"served"}
    assert constructed - served == {"client"}


def test_there_is_something_to_check():
    """A glob that matched nothing would make the parametrized test silently vacuous."""
    assert _example_scripts()


@pytest.mark.parametrize("name", sorted(KNOWN_UNSERVED))
def test_an_exemption_is_still_earning_its_place(name):
    """An exemption that no longer applies must be deleted, not left to rot.

    Without this, a fixed example would keep its skip forever and the guard would quietly stop
    covering it.
    """
    script = EXAMPLES / name
    assert script.exists(), f"{name} is exempted but no longer exists — drop the entry"

    constructed, served = _constructed_and_served(ast.parse(script.read_text(encoding="utf-8")))

    assert constructed - served, (
        f"{name} now serves every middleware it constructs. Remove it from KNOWN_UNSERVED "
        f"and close #{KNOWN_UNSERVED[name]}."
    )
