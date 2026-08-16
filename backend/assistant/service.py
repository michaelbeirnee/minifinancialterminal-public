"""The chat loop: prompt + tools + Claude, streamed back as events.

Everything here is a synchronous generator. FastAPI runs a sync iterator passed
to ``StreamingResponse`` in a worker thread, and the platform's providers are
themselves blocking (``httpx`` sync, ``yfinance``), so a tool call inside the
loop would have to be bridged back to a thread anyway.

The generator yields plain dicts; the router serialises them as SSE. Event
shapes:

``{"type": "text",  "text": str}``            a chunk of the reply
``{"type": "tool",  "name": str, "input": dict}``   a tool call starting
``{"type": "tool_done", "name": str, "ok": bool}``  that tool call finished
``{"type": "done",  "usage": {...}}``         the turn is complete
``{"type": "error", "message": str}``         fatal; nothing more follows
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..models import User
from .prompt import system_blocks
from .tools import TOOL_DEFINITIONS, build_dispatch, run_tool

log = logging.getLogger(__name__)

try:  # the one paid dependency — absence is a supported state, not an error
    import anthropic
except ImportError:  # pragma: no cover - exercised by the /status endpoint
    anthropic = None  # type: ignore[assignment]

# Opus 5 can decline a request outright (a 200 with stop_reason "refusal").
# Server-side fallbacks re-run it on the recommended model instead of handing
# the user a dead end. It is a beta, so an org without it flipped on gets a 400
# — in which case we drop the parameter for the rest of the process rather than
# taking the whole assistant down over an optional safety net.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_fallbacks_available = True


def availability() -> Dict[str, Any]:
    """Why the assistant is or is not usable — drives the UI's empty state."""
    if anthropic is None:
        return {
            "enabled": False,
            "reason": "The 'anthropic' package is not installed. Run: pip install anthropic",
        }
    if not settings.anthropic_api_key:
        return {
            "enabled": False,
            "reason": (
                "No API key configured. Set MFT_ANTHROPIC_API_KEY to switch the "
                "assistant on — every other part of the terminal works without it."
            ),
        }
    return {"enabled": True, "reason": None, "model": settings.assistant_model}


def _client() -> "anthropic.Anthropic":
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _open_stream(client: "anthropic.Anthropic", **kwargs: Any):
    """Open a streaming request, degrading gracefully if the beta is unavailable."""
    global _fallbacks_available

    if _fallbacks_available:
        try:
            return client.beta.messages.stream(
                betas=[_FALLBACK_BETA], fallbacks="default", **kwargs
            )
        except anthropic.BadRequestError as exc:
            message = str(exc).lower()
            if "fallback" not in message and "beta" not in message:
                raise
            log.info("Server-side fallbacks unavailable, continuing without: %s", exc)
            _fallbacks_available = False

    return client.messages.stream(**kwargs)


def _normalise(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Trim the browser-supplied history to the turns we will actually send."""
    turns = [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()
    ]
    turns = turns[-settings.assistant_max_history :]
    # The API requires the first turn to be from the user; dropping a leading
    # assistant turn is the only thing trimming can break.
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


def stream_reply(
    history: List[Dict[str, Any]], db: Session, user: User
) -> Iterator[Dict[str, Any]]:
    state = availability()
    if not state["enabled"]:
        yield {"type": "error", "message": state["reason"]}
        return

    messages = _normalise(history)
    if not messages:
        yield {"type": "error", "message": "No message to answer."}
        return

    client = _client()
    dispatch = build_dispatch(db, user)
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    max_rounds = settings.assistant_max_tool_rounds

    try:
        for round_index in range(max_rounds + 1):
            last_round = round_index == max_rounds
            request: Dict[str, Any] = {
                "model": settings.assistant_model,
                "max_tokens": settings.assistant_max_tokens,
                "system": system_blocks(),
                "tools": TOOL_DEFINITIONS,
                "messages": messages,
                "output_config": {"effort": settings.assistant_effort},
            }
            if last_round:
                # Out of tool budget: answer with what has been gathered.
                request["tool_choice"] = {"type": "none"}

            with _open_stream(client, **request) as stream:
                for event in stream:
                    if (
                        event.type == "content_block_delta"
                        and event.delta.type == "text_delta"
                    ):
                        yield {"type": "text", "text": event.delta.text}
                final = stream.get_final_message()

            for field in usage:
                usage[field] += getattr(final.usage, field, 0) or 0

            if final.stop_reason == "refusal":
                yield {
                    "type": "error",
                    "message": (
                        "That request was declined by the model's safety filters. "
                        "Rephrasing it usually helps."
                    ),
                }
                return

            tool_calls = [b for b in final.content if b.type == "tool_use"]
            # ``tool_choice: none`` should make this unreachable on the last
            # round, but the turn has to end with a terminal event either way —
            # running tools whose results nothing will ever read is worse than
            # answering with what is already on screen.
            if not tool_calls or last_round:
                yield {"type": "done", "usage": usage}
                return

            messages.append({"role": "assistant", "content": final.content})

            results: List[Dict[str, Any]] = []
            for call in tool_calls:
                yield {"type": "tool", "name": call.name, "input": call.input}
                text, is_error = run_tool(dispatch, call.name, call.input)
                yield {"type": "tool_done", "name": call.name, "ok": not is_error}
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": text,
                        "is_error": is_error,
                    }
                )
            # Every result for a turn goes back in one user message — splitting
            # them teaches the model to stop calling tools in parallel.
            messages.append({"role": "user", "content": results})

    except anthropic.APIStatusError as exc:
        log.warning("Assistant API error: %s", exc)
        yield {"type": "error", "message": _friendly_api_error(exc)}
    except anthropic.APIConnectionError:
        yield {
            "type": "error",
            "message": "Could not reach the Claude API. Check your network connection.",
        }
    except Exception as exc:  # noqa: BLE001 - the stream must always terminate cleanly
        log.exception("Assistant failed")
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}


def _friendly_api_error(exc: "anthropic.APIStatusError") -> str:
    if exc.status_code == 401:
        return "The Anthropic API key was rejected. Check MFT_ANTHROPIC_API_KEY."
    if exc.status_code == 429:
        return "Rate limited by the Claude API. Wait a moment and try again."
    if exc.status_code >= 500:
        return "The Claude API is having trouble. Try again shortly."
    return f"Claude API error ({exc.status_code}): {exc.message}"
