"""The assistant's tools — its hands on the platform.

Four tools, all read-only:

* ``search_commands``   — find a command by keyword
* ``describe_command``  — full signature, providers and a worked example
* ``run_command``       — execute any registered command
* ``get_user_context``  — the caller's own portfolios, watchlists and alerts

``run_command`` routes through :func:`backend.core.registry.execute`, which only
resolves paths that are actually in the registry and rejects unknown keyword
arguments — so the tool surface is exactly the platform's own data layer, and
nothing in it writes. ``get_user_context`` filters on ``user_id`` like every
other user-scoped query in the app.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..core import docs
from ..core.registry import REGISTRY, execute, get_spec, search
from ..models import Alert, Portfolio, User, Watchlist

# Anthropic tool definitions. Order is fixed and the schemas are static — they
# render ahead of the system prompt, so any churn here would invalidate the
# prompt cache on every request.
TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "search_commands",
        "description": (
            "Search the command registry by keyword. Use this whenever you are not "
            "certain which command covers a topic — it matches against command paths, "
            "descriptions and provider names. Returns paths with their one-line "
            "summaries. Search before guessing a path."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword to search for, e.g. 'yield curve', 'insider', 'rsi'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (default 15).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "describe_command",
        "description": (
            "Get the full specification for one command: its docstring, every "
            "parameter with type and default, the providers that can serve it, and a "
            "runnable example. Call this before run_command when you are unsure of a "
            "command's parameters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Command path, e.g. '/equity/price/quote'.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Execute a platform command and return its rows. This is how you answer "
            "questions with real data instead of from memory. Every command is a "
            "read-only data lookup. Parameters are passed as a JSON object matching "
            "the command's signature; omit optional parameters to take their "
            "defaults. Large results are truncated — narrow the request with the "
            "command's own parameters rather than asking for everything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Command path, e.g. '/equity/fundamental/ratios'.",
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Parameters for the command, e.g. "
                        '{"symbol": "AAPL", "period": "annual"}. Omit for commands '
                        "that take none."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_user_context",
        "description": (
            "Read the signed-in person's own saved work: their portfolios (with "
            "holdings, cost basis and realised P&L), watchlists and price alerts. "
            "Call this for any question about 'my' holdings, 'my' watchlist or how "
            "'I' am doing. It returns book values from the transaction log — for "
            "current market value, follow up with run_command to price the symbols."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


# --------------------------------------------------------------------------- #
# Implementations
# --------------------------------------------------------------------------- #
def _search_commands(query: str, limit: int = 15) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 15), 40))
    hits = search(str(query), limit=limit)
    if not hits:
        return {
            "query": query,
            "matches": [],
            "note": "No command matched. Try a broader keyword, or a menu name such as "
            "'equity', 'economy', 'technical'.",
        }
    return {
        "query": query,
        "matches": [
            {"path": s.path, "summary": s.description, "providers": list(s.providers)}
            for s in hits
        ],
    }


def _describe_command(path: str) -> Dict[str, Any]:
    spec = get_spec(str(path))
    out: Dict[str, Any] = {
        "path": spec.path,
        "summary": spec.description,
        "providers": list(spec.providers),
        "documentation": docs.full_doc(spec),
        "parameters": [
            {**p, "means": docs.describe_param(spec, p["name"])} for p in spec.parameters
        ],
    }
    example = docs.example_for(spec)
    if example:
        out["example"] = example
    return out


def _run_command(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = dict(params or {})
    obj = execute(str(path), **params)

    results = obj.results
    payload: Any
    truncated = 0
    if isinstance(results, list):
        # Floor the cap so a small misconfiguration can't invert the slice below.
        limit = max(10, settings.assistant_max_tool_rows)
        if len(results) > limit:
            # Keep both ends: the head shows the shape, the tail is usually the
            # most recent observation in a time series.
            head, tail = results[: limit - 5], results[-5:]
            truncated = len(results) - len(head) - len(tail)
            payload = head + tail
        else:
            payload = results
    else:
        payload = results

    out: Dict[str, Any] = {
        "command": obj.command or path,
        "provider": obj.provider,
        "row_count": len(results) if isinstance(results, list) else 1,
        "results": payload,
    }
    if truncated:
        out["truncated"] = (
            f"{truncated} rows omitted from the middle of the result. The first and "
            f"last rows are shown. Narrow the request if you need the rest."
        )
    if obj.warnings:
        out["warnings"] = obj.warnings
    if obj.extra:
        out["extra"] = obj.extra
    return out


def _get_user_context(db: Session, user: User) -> Dict[str, Any]:
    portfolios = db.query(Portfolio).filter(Portfolio.user_id == user.id).all()
    watchlists = db.query(Watchlist).filter(Watchlist.user_id == user.id).all()
    alerts = (
        db.query(Alert).filter(Alert.user_id == user.id, Alert.is_active.is_(True)).all()
    )

    return {
        "note": (
            "Quantities and cost basis are book values derived from the transaction "
            "log. They are not marked to market — price the symbols with run_command "
            "for current value."
        ),
        "portfolios": [
            {
                "name": p.name,
                "is_default": p.is_default,
                "base_currency": p.base_currency,
                "cost_basis_method": p.cost_basis_method,
                "benchmark": p.benchmark,
                "cash": round(p.cash, 2),
                "holdings": [
                    {
                        "symbol": pos.symbol,
                        "quantity": pos.quantity,
                        "avg_cost": round(pos.avg_cost, 4),
                        "cost_basis": round(pos.cost_basis, 2),
                        "realized_pnl": round(pos.realized_pnl, 2),
                        "dividends": round(pos.dividends, 2),
                    }
                    for pos in p.positions
                    if pos.quantity
                ],
            }
            for p in portfolios
        ],
        "watchlists": [
            {
                "name": w.name,
                "is_default": w.is_default,
                "symbols": [i.symbol for i in w.items],
            }
            for w in watchlists
        ],
        "active_alerts": [
            {
                "symbol": a.symbol,
                "condition": a.condition,
                "threshold": a.threshold,
                "note": a.note,
                "times_triggered": a.trigger_count,
            }
            for a in alerts
        ],
    }


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def build_dispatch(db: Session, user: User) -> Dict[str, Callable[..., Any]]:
    """Bind the user-scoped tools to this request's session and account."""
    return {
        "search_commands": _search_commands,
        "describe_command": _describe_command,
        "run_command": _run_command,
        "get_user_context": lambda: _get_user_context(db, user),
    }


def run_tool(dispatch: Dict[str, Callable[..., Any]], name: str, payload: Any) -> tuple[str, bool]:
    """Execute one tool call, returning ``(result_text, is_error)``.

    Tool failures are returned to the model as ``is_error`` results rather than
    raised: a mistyped parameter or a rate-limited provider is something the
    assistant can recover from on the next turn, and killing the whole reply
    over it would be worse than letting it try again.
    """
    fn = dispatch.get(name)
    if fn is None:
        return f"Unknown tool {name!r}.", True

    kwargs = payload if isinstance(payload, dict) else {}
    try:
        result = fn(**kwargs)
    except TypeError as exc:  # bad arguments — the model can fix these itself
        return f"Invalid arguments for {name}: {exc}", True
    except Exception as exc:  # noqa: BLE001 - provider errors are the common case
        return f"{type(exc).__name__}: {exc}", True

    try:
        return json.dumps(result, default=str), False
    except (TypeError, ValueError) as exc:
        return f"Result could not be serialised: {exc}", True


def command_exists(path: str) -> bool:
    return "/" + str(path).strip("/").replace(".", "/") in REGISTRY
