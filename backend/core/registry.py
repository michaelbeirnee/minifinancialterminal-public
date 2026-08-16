"""Command registry.

A *command* is a plain Python function decorated with :func:`command` and
tagged with a router path such as ``/equity/price/historical``. Registering it
once gets you, for free:

* a REST endpoint (``backend/core/api.py`` walks the registry),
* an attribute on the Python interface (``mft.equity.price.historical(...)``),
* a menu entry in the CLI (``cli/terminal.py``),
* and its parameter list in ``/docs`` and in ``help``.

That is the whole reason the platform can carry a few hundred commands without
a few hundred hand-written routes.
"""
from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import UnknownCommandError, UnknownProviderError
from .models import MFTObject, build_object


@dataclass(frozen=True)
class CommandSpec:
    path: str
    func: Callable[..., Any]
    providers: Tuple[str, ...] = ()
    summary: str = ""
    methods: Tuple[str, ...] = ("GET",)
    examples: Tuple[str, ...] = ()

    # -- derived -----------------------------------------------------------
    @property
    def parts(self) -> List[str]:
        return [p for p in self.path.split("/") if p]

    @property
    def name(self) -> str:
        return self.parts[-1]

    @property
    def menu(self) -> str:
        """``/equity/price/historical`` -> ``equity/price``."""
        return "/".join(self.parts[:-1])

    @property
    def tag(self) -> str:
        return self.parts[0]

    @property
    def description(self) -> str:
        if self.summary:
            return self.summary
        doc = inspect.getdoc(self.func) or ""
        return doc.split("\n", 1)[0]

    @property
    def parameters(self) -> List[Dict[str, Any]]:
        sig = inspect.signature(self.func)
        try:
            hints = typing.get_type_hints(self.func)
        except Exception:  # noqa: BLE001 - never let introspection break help
            hints = {}
        out: List[Dict[str, Any]] = []
        for pname, p in sig.parameters.items():
            if pname.startswith("_") or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            annotation = hints.get(pname, p.annotation)
            out.append(
                {
                    "name": pname,
                    "type": _type_name(annotation),
                    "required": p.default is inspect.Parameter.empty,
                    "default": None if p.default is inspect.Parameter.empty else p.default,
                }
            )
        return out


def _type_name(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return "any"
    name = getattr(annotation, "__name__", None)
    if name:
        return name
    text = str(annotation).replace("typing.", "")
    return text


REGISTRY: Dict[str, CommandSpec] = {}


def command(
    path: str,
    *,
    providers: Sequence[str] = (),
    summary: str = "",
    methods: Sequence[str] = ("GET",),
    examples: Sequence[str] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``func`` as the platform command served at ``path``."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if path in REGISTRY:
            raise RuntimeError("Duplicate command path: {}".format(path))
        REGISTRY[path] = CommandSpec(
            path=path,
            func=func,
            providers=tuple(providers),
            summary=summary,
            methods=tuple(methods),
            examples=tuple(examples),
        )
        return func

    return decorator


# --------------------------------------------------------------------------- #
# Lookup / execution
# --------------------------------------------------------------------------- #
def get_spec(path: str) -> CommandSpec:
    path = "/" + path.strip("/").replace(".", "/")
    if path not in REGISTRY:
        raise UnknownCommandError("No command registered at {!r}".format(path))
    return REGISTRY[path]


def execute(path: str, **kwargs: Any) -> MFTObject:
    """Run a command by path and wrap its output in an :class:`MFTObject`."""
    spec = get_spec(path)
    accepted = {p["name"] for p in spec.parameters}
    unknown = set(kwargs) - accepted
    if unknown:
        raise TypeError(
            "{} got unexpected parameter(s): {}. Accepted: {}".format(
                spec.path, ", ".join(sorted(unknown)), ", ".join(sorted(accepted))
            )
        )
    raw = spec.func(**kwargs)
    return build_object(raw, command=spec.path, provider_hint=kwargs.get("provider"))


def paths() -> List[str]:
    return sorted(REGISTRY)


def menus() -> List[str]:
    return sorted({s.menu for s in REGISTRY.values()})


def tree() -> Dict[str, Any]:
    """Nested ``{segment: {...}}`` map of the whole command surface."""
    root: Dict[str, Any] = {}
    for spec in REGISTRY.values():
        node = root
        for part in spec.parts[:-1]:
            node = node.setdefault(part, {})
        node[spec.name] = spec
    return root


def children(menu: str) -> Tuple[List[str], List[CommandSpec]]:
    """Sub-menus and commands directly under ``menu`` (``""`` = root)."""
    prefix = [p for p in menu.split("/") if p]
    node: Any = tree()
    for part in prefix:
        node = node.get(part, {})
        if isinstance(node, CommandSpec):
            return [], []
    submenus = sorted(k for k, v in node.items() if isinstance(v, dict))
    cmds = sorted((v for v in node.values() if isinstance(v, CommandSpec)), key=lambda s: s.name)
    return submenus, cmds


def search(query: str, limit: int = 50) -> List[CommandSpec]:
    q = query.lower().strip()
    hits = [
        s
        for s in REGISTRY.values()
        if q in s.path.lower() or q in s.description.lower() or any(q == p for p in s.providers)
    ]
    return sorted(hits, key=lambda s: s.path)[:limit]


def resolve_provider(
    requested: Optional[str], available: Iterable[str], default: Optional[str] = None
) -> str:
    """Validate a ``provider=`` argument against a command's provider list."""
    options = list(available)
    if not requested:
        return default or options[0]
    key = requested.strip().lower()
    if key not in options:
        raise UnknownProviderError(
            "Unknown provider {!r}. Available: {}".format(requested, ", ".join(options))
        )
    return key


def coverage() -> Dict[str, Any]:
    """Command counts per top-level menu — used by ``/api/system/coverage``."""
    by_tag: Dict[str, int] = {}
    by_provider: Dict[str, int] = {}
    for spec in REGISTRY.values():
        by_tag[spec.tag] = by_tag.get(spec.tag, 0) + 1
        for p in spec.providers:
            by_provider[p] = by_provider.get(p, 0) + 1
    return {
        "total_commands": len(REGISTRY),
        "menus": len(menus()),
        "by_menu": dict(sorted(by_tag.items())),
        "by_provider": dict(sorted(by_provider.items(), key=lambda kv: -kv[1])),
    }
