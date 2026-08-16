"""Deep dive: verify one promoted candidate's legs against the platform.

Triage hands over a candidate whose world-knowledge legs are unverified
hypotheses. This layer runs the verification: an agentic loop with the same
three read-only data tools the assistant uses (search, describe, run), a
system prompt whose stance is *refutation*, and a forced structured finish —
the dossier. Per leg: a verdict (``verified`` / ``refuted`` / ``unverifiable``,
and unverifiable is an acceptable answer), the commands whose output was
decisive (so the caller can freeze them as thesis evidence), and proposed
falsifiers in exactly the spine's check format.

The model never writes to the database. The router turns a dossier into a
*draft* thesis — evidence re-run and frozen server-side, falsifiers validated
against the registry — and a human reviews or deletes it. Deadlines are
returned as relative day counts and converted to dates server-side, so the
model never states a calendar date.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..assistant.tools import (
    TOOL_DEFINITIONS,
    _describe_command,
    _run_command,
    _search_commands,
    run_tool,
)
from ..config import settings
from ..core.registry import REGISTRY

try:  # the platform's one paid dependency; absence is a supported state
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]

#: The assistant's read-only data tools, minus the user-context one — a deep
#: dive is about a company, not about the caller's portfolio.
_DATA_TOOLS = [t for t in TOOL_DEFINITIONS if t["name"] != "get_user_context"]
_DISPATCH = {
    "search_commands": _search_commands,
    "describe_command": _describe_command,
    "run_command": _run_command,
}

_COMPARATORS = {"lt", "le", "gt", "ge", "eq", "ne"}

SYSTEM_PROMPT = """You verify an investment-idea candidate against a financial data platform.
Your stance is REFUTATION: for each leg of the candidate, try to prove it wrong
with data. A leg survives only if the data you actually retrieved supports it.

Method, per leg:
- Run the suggested verify_with commands first, then whatever else the leg
  needs. Use search_commands when unsure what exists; describe_command before
  guessing parameters.
- Verdict "verified" requires rows you retrieved in THIS conversation that
  support the claim. "refuted" requires rows that contradict it. If the
  platform cannot settle it, say "unverifiable" — that is a respectable
  answer and much better than stretching.
- Never cite a number you did not retrieve here. Your world knowledge may
  propose where to look; only retrieved data may decide.

Then finish by calling deepdive_result exactly once:
- evidence: for each leg, the command calls whose output was decisive. These
  will be re-run and frozen server-side, so cite only calls that worked.
- falsifiers: conditions that would BREAK the thesis, in the platform's check
  format (command path + numeric field from rows you actually saw + comparator
  + threshold). The comparator describes failure, not success. Give deadlines
  as review_by_days / by_date_days from today, never calendar dates.
- proceed=false is a good outcome when legs failed — say why in summary."""

DOSSIER_TOOL: Dict[str, Any] = {
    "name": "deepdive_result",
    "description": "Finish the deep dive with the structured dossier. Call exactly once, after verification.",
    "input_schema": {
        "type": "object",
        "properties": {
            "proceed": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "claim": {"type": "string",
                      "description": "The thesis in one falsifiable sentence."},
            "review_by_days": {"type": "integer",
                               "description": "Days from today by which the claim should have played out."},
            "summary": {"type": "string"},
            "legs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "verdict": {"type": "string",
                                    "enum": ["verified", "refuted", "unverifiable"]},
                        "notes": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "params": {"type": "object"},
                                    "note": {"type": "string"},
                                },
                                "required": ["path"],
                            },
                        },
                        "falsifiers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "path": {"type": "string"},
                                    "params": {"type": "object"},
                                    "field": {"type": "string"},
                                    "comparator": {"type": "string",
                                                   "enum": ["lt", "le", "gt", "ge", "eq", "ne"]},
                                    "threshold": {"type": "number"},
                                    "by_date_days": {"type": ["integer", "null"]},
                                },
                                "required": ["name", "path", "field", "comparator", "threshold"],
                            },
                        },
                    },
                    "required": ["claim", "verdict"],
                },
            },
        },
        "required": ["proceed", "confidence", "claim", "summary", "legs"],
    },
}


def validate_dossier(dossier: Dict[str, Any]) -> Dict[str, Any]:
    """Mechanical pass: unregistered paths and bad comparators are flagged.

    Nothing is silently dropped — a flagged item is visible in the response
    and simply skipped by the draft-creation step, so the human sees what the
    model tried to claim.
    """
    for leg in dossier.get("legs", []):
        for cite in leg.get("evidence") or []:
            path = "/" + str(cite.get("path", "")).strip().strip("/")
            cite["path"] = path
            if path not in REGISTRY:
                cite["unknown_command"] = True
        for check in leg.get("falsifiers") or []:
            path = "/" + str(check.get("path", "")).strip().strip("/")
            check["path"] = path
            if path not in REGISTRY:
                check["unknown_command"] = True
            if str(check.get("comparator")) not in _COMPARATORS:
                check["invalid_comparator"] = True
    return dossier


def run(candidate: Dict[str, Any], client: Optional[Any] = None,
        max_rounds: Optional[int] = None) -> Dict[str, Any]:
    """Verify one triage candidate. Returns the validated dossier.

    ``client`` is injectable for tests. The loop mirrors the assistant's: all
    tool results for a round go back in one user message, and the final round
    forces the dossier call so the loop always terminates with a result.
    """
    from .triage import availability

    state = availability()
    if not state["enabled"]:
        raise RuntimeError(state["reason"])
    if client is None:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    rounds = max_rounds if max_rounds is not None else settings.assistant_max_tool_rounds
    tools = _DATA_TOOLS + [DOSSIER_TOOL]
    messages: List[Dict[str, Any]] = [{
        "role": "user",
        "content": (
            "Verify this candidate and finish with deepdive_result:\n\n"
            + json.dumps(candidate, default=str, indent=2)
        ),
    }]

    for round_index in range(rounds + 1):
        request: Dict[str, Any] = {
            "model": settings.assistant_model,
            "max_tokens": settings.assistant_max_tokens,
            "system": SYSTEM_PROMPT,
            "tools": tools,
            "messages": messages,
            "output_config": {"effort": settings.assistant_effort},
        }
        if round_index == rounds:  # out of budget: produce the dossier now
            request["tool_choice"] = {"type": "tool", "name": "deepdive_result"}

        message = client.messages.create(**request)
        calls = [b for b in message.content if getattr(b, "type", None) == "tool_use"]

        finish = [b for b in calls if b.name == "deepdive_result"]
        if finish:
            return validate_dossier(dict(finish[0].input))

        if not calls:
            # Text without tools: nudge once toward the structured finish.
            messages.append({"role": "assistant", "content": message.content})
            messages.append({"role": "user",
                             "content": "Finish by calling deepdive_result."})
            continue

        messages.append({"role": "assistant", "content": message.content})
        results = []
        for call in calls:
            text, is_error = run_tool(_DISPATCH, call.name, call.input)
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": text, "is_error": is_error})
        messages.append({"role": "user", "content": results})

    raise RuntimeError("Deep dive ended without a dossier")
