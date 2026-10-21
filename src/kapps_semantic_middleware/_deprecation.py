"""The deprecated names of the middleware: each use of one emits one `DeprecationWarning`.

The warning names the name that replaces the old one, in the form that the triple store
interface uses for its own deprecated names. The release that removes the deprecated names
removes this module.
"""

from __future__ import annotations

import functools
import importlib
import warnings
from typing import Any, Callable, Mapping, TypeVar, cast

_Function = TypeVar("_Function", bound=Callable[..., Any])


def renamed(successor: str) -> Callable[[_Function], _Function]:
    """Mark a function as the deprecated old name of `successor`.

    Each call warns once, at the caller's line, and then runs the function, which runs the
    successor. The function keeps its name and its parameters. It carries `__deprecated__`, the
    attribute of PEP 702, with the message.
    """

    def mark(function: _Function) -> _Function:
        message = f"{function.__name__} is deprecated; use {successor}, which replaces it."

        @functools.wraps(function)
        def alias(*args: Any, **kwargs: Any) -> Any:
            warnings.warn(message, DeprecationWarning, stacklevel=2)
            return function(*args, **kwargs)

        alias.__deprecated__ = message  # type: ignore[attr-defined]
        return cast(_Function, alias)

    return mark


def constant(module: str, aliases: Mapping[str, str], namespace: dict[str, Any], name: str) -> Any:
    """The value of the deprecated constant `name` of `module`: its successor's value.

    A module's `__getattr__` calls this, so the deprecated name is not an attribute of the
    module and each use of it warns once, at the caller's line: an import of it and a read of it
    as an attribute. `aliases` maps each deprecated name to its successor, and `namespace` is the
    module's globals.

    Raises:
        AttributeError: If `name` is not a deprecated name of the module.
    """
    successor = aliases.get(name)
    if successor is None:
        raise AttributeError(f"module {module!r} has no attribute {name!r}")
    warnings.warn(
        f"{name} is deprecated; use {successor}, of which it is an alias.",
        DeprecationWarning,
        stacklevel=3,
    )
    return namespace[successor]


def moved(module: str, homes: Mapping[str, str], name: str) -> Any:
    """The object that the deprecated name `name` of `module` names in its new home.

    A module's `__getattr__` calls this, so the moved name is not an attribute of the old module
    and each use of it warns once, at the caller's line: an import of it and a read of it as an
    attribute. `homes` maps each moved name to the module that now holds it. The new home is
    imported here, at the first use, so the old module does not import it.

    Raises:
        AttributeError: If `name` is not a moved name of the module.
    """
    home = homes.get(name)
    if home is None:
        raise AttributeError(f"module {module!r} has no attribute {name!r}")
    warnings.warn(
        f"{name} is deprecated; use {home}.{name}, which replaces it.",
        DeprecationWarning,
        stacklevel=3,
    )
    return getattr(importlib.import_module(home), name)
