"""Dotted Python interface over the command registry.

    >>> from backend.core.interface import mft
    >>> mft.equity.price.historical(symbol="AAPL", start_date="2024-01-01").to_df()
    >>> mft.economy.cpi(country="united_states")
    >>> mft.search("yield curve")

Namespaces are generated from the registry at attribute-access time, so a new
``@command`` shows up here the moment its module is imported.
"""
from __future__ import annotations

import functools
import inspect
import textwrap
from typing import Any, Dict, List, Optional

from .errors import UnknownCommandError
from .models import MFTObject
from .registry import CommandSpec, children, coverage, execute, get_spec
from .registry import search as _search


class Command:
    """A callable bound to one registry entry."""

    def __init__(self, spec: CommandSpec) -> None:
        self._spec = spec
        functools.update_wrapper(self, spec.func)
        self.__doc__ = self._build_doc()

    def __call__(self, **kwargs: Any) -> MFTObject:
        return execute(self._spec.path, **kwargs)

    def _build_doc(self) -> str:
        s = self._spec
        lines = [s.description or s.path, ""]
        if s.providers:
            lines += ["Providers: " + ", ".join(s.providers), ""]
        lines.append("Parameters")
        lines.append("----------")
        for p in s.parameters:
            default = "" if p["required"] else " = {!r}".format(p["default"])
            lines.append("{} : {}{}".format(p["name"], p["type"], default))
        body = inspect.getdoc(s.func) or ""
        if body:
            lines += ["", textwrap.dedent(body)]
        return "\n".join(lines)

    def __repr__(self) -> str:
        return "<command {}({})>".format(
            self._spec.path, ", ".join(p["name"] for p in self._spec.parameters)
        )


class Namespace:
    """A menu node, e.g. ``mft.equity`` or ``mft.equity.price``."""

    def __init__(self, prefix: str = "") -> None:
        object.__setattr__(self, "_prefix", prefix)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        prefix = object.__getattribute__(self, "_prefix")
        path = "{}/{}".format(prefix, name)
        try:
            spec = get_spec(path)
        except UnknownCommandError:
            spec = None  # not a command — it may still be a sub-menu
        if spec is not None:
            return Command(spec)
        submenus, cmds = children(path.lstrip("/"))
        if submenus or cmds:
            return Namespace(path)
        raise AttributeError(
            "No command or menu at {!r}. Try mft.search({!r}).".format(path, name)
        )

    def __dir__(self) -> List[str]:
        prefix = object.__getattribute__(self, "_prefix")
        submenus, cmds = children(prefix.lstrip("/"))
        return sorted(submenus + [c.name for c in cmds])

    def __repr__(self) -> str:
        prefix = object.__getattribute__(self, "_prefix") or "/"
        submenus, cmds = children(prefix.lstrip("/"))
        parts = ["Menu {}".format(prefix)]
        if submenus:
            parts.append("  sub-menus: " + ", ".join(submenus))
        if cmds:
            parts.append("  commands: " + ", ".join(c.name for c in cmds))
        return "\n".join(parts)


class Terminal(Namespace):
    """Root of the Python interface."""

    def search(self, query: str, limit: int = 50) -> List[str]:
        return ["{:<48} {}".format(s.path, s.description) for s in _search(query, limit)]

    def coverage(self) -> Dict[str, Any]:
        return coverage()

    def help(self, path: Optional[str] = None) -> str:
        if path is None:
            return repr(self)
        try:
            return Command(get_spec(path)).__doc__ or ""
        except Exception:  # noqa: BLE001
            return repr(Namespace("/" + path.strip("/")))

    def __call__(self, path: str, **kwargs: Any) -> MFTObject:
        """``mft("/equity/price/historical", symbol="AAPL")`` — dynamic dispatch."""
        return execute(path, **kwargs)


mft = Terminal()
