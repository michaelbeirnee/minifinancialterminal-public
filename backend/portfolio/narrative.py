"""Narrative layer: plain language over a table the model may not change.

Step 8 of docs/hedge-construction.md, and deliberately last — the design puts
it behind "only after everything above is deterministic and tested", because
a fluent explanation of a wrong number is worse than no explanation at all.

The division of labour is stricter here than in thesis triage. There, the
model contributed world knowledge the scanners could not see. Here the engine
has already decided everything that matters — which hedge, how many
contracts, what it costs, and whether hedging is worth doing at all — and the
model's only job is to explain that decision and name what the numbers cannot
say. In particular:

* **The verdict is not the model's to make.** ``validate`` overwrites any
  recommendation that disagrees with the engine and records the attempt in
  ``contradicted_engine``. The entire point of the v2 redesign was to stop a
  plausible story from selling protection the tail maths did not justify; a
  narrative layer that could reverse the verdict would hand that back.
* **No new instruments.** A recommendation naming a construction that was not
  in the table is dropped.
* **No new numbers.** Figures in the prose are checked against the brief and
  the unrecognised ones are flagged, mirroring triage's unregistered-path
  check.

Needs ``MFT_ANTHROPIC_API_KEY``; without one the endpoint is switched off and
every deterministic hedge feature keeps working.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..config import settings

try:  # the platform's one paid dependency; absence is a supported state
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]

#: Numbers this size or larger in the prose are checked against the brief.
#: Small integers ("2 contracts", "3 of 5") are ordinary prose, not claims.
NUMBER_PATTERN = re.compile(r"\d[\d,]*\.?\d*")
NUMBER_MIN_DIGITS = 3


def availability() -> Dict[str, Any]:
    from ..assistant.service import availability as assistant_availability

    return assistant_availability()


# --------------------------------------------------------------------------- #
# Brief — deterministic, every number traceable to the engine
# --------------------------------------------------------------------------- #
def build_brief(analysis: Dict[str, Any], exposures: Optional[Dict[str, Any]] = None) -> str:
    """One compact brief of the hedge analysis. Nothing the engine did not say."""
    target = analysis.get("target", {})
    shocks = analysis.get("shocks", {})
    lines = [
        "BOOK {} · value ${:,.0f} · benchmark {}".format(
            analysis.get("portfolio", {}).get("name", "?"),
            analysis.get("value", 0),
            analysis.get("benchmark", "?"),
        ),
        "TAIL now ${:,.0f} (CVaR, {} sessions); asked to remove ${:,.0f}".format(
            abs(target.get("cvar_unhedged", 0)),
            shocks.get("horizon_days", "?"),
            target.get("reduction_sought", 0),
        ),
        "SHOCKS {} overlapping windows, only {} independent, {}–{}".format(
            shocks.get("windows", "?"),
            shocks.get("independent_windows", "?"),
            *(shocks.get("period") or ["?", "?"]),
        ),
    ]
    if exposures:
        concentration = (exposures.get("targets") or {}).get("single_name_concentration") or {}
        dominant = [p for p in concentration.get("positions", []) if p.get("dominant")]
        if dominant:
            lines.append(
                "CONCENTRATION dominant: "
                + ", ".join(
                    "{} at {:.0f}% of risk".format(p["symbol"], 100 * p["pct_of_risk"])
                    for p in dominant
                )
            )

    lines.append("CANDIDATES (engine-ranked, best first):")
    for row in analysis.get("rows", []):
        size = (
            "{} contracts".format(row["quantity"])
            if row.get("quantity") is not None
            else "${:,.0f} notional".format(row.get("notional", 0))
        )
        low, high = (row.get("protection_bps_ci95") or [0, 0])[:2]
        lines.append(
            "  {} on {} · {} · reaches goal={} · protection {:.0f}bps (CI {:.0f}–{:.0f}) · "
            "cost {:.0f}bps · cost per unit {} · if +10% {:,.0f}".format(
                row["kind"], row["underlying"], size, row.get("meets_target"),
                row.get("protection_bps", 0), low, high, row.get("cost_bps", 0),
                row.get("cost_per_unit_protection"),
                (row.get("upside_loss") or {}).get("+10%", 0),
            )
        )
    for item in analysis.get("excluded", []):
        lines.append("  EXCLUDED {}: {}".format(item.get("kind", "?"), item.get("reason", "?")))

    verdict = analysis.get("verdict", {})
    lines.append(
        "ENGINE VERDICT: {}{}".format(
            verdict.get("action", "?"),
            " — " + verdict["reason"] if verdict.get("reason") else "",
        )
    )
    for note in shocks.get("notes", [])[:4]:
        lines.append("  NOTE {}".format(note))
    for warning in analysis.get("warnings", [])[:4]:
        lines.append("  WARNING {}".format(warning))
    return "\n".join(lines)


SYSTEM_PROMPT = """You explain a hedging analysis that a deterministic engine has already completed.
The engine measured the portfolio's tail risk, priced today's option chains under
historical joint market/volatility shocks, solved for integer contract counts, and
ranked candidates. Its verdict is final.

YOUR JOB is to make that verdict understandable, and to name what the numbers
cannot say. You are not a second opinion.

RULES
1. NEVER contradict the engine verdict. If it says de_risk_by_selling, explain why
   hedging does not pay here — do not argue for a hedge. If it says hedge, explain
   what is being bought and surrendered. Your recommended_action must match.
2. Never state a number that is not in the brief. No prices, no percentages, no
   dates from memory. Quote the brief's figures or write no figure.
3. Only discuss constructions that appear in the candidate list. Never introduce an
   instrument the engine did not price.
4. Lead with the trade-off in plain words: what is given up, what is protected
   against, and what remains unprotected. A hedge that removes market risk from a
   book whose tail is single-name risk has not made it safe — say so plainly.
5. Treat the sample honestly. Overlapping windows are not independent observations;
   if the independent count is small, the protection estimates are noisy and the
   confidence intervals are the honest range. Say that rather than quoting a point
   estimate as fact.
6. In limits_of_this_analysis, put what would change the answer and what the model
   cannot see — upcoming events, positions the book may hold elsewhere, liquidity
   on the day. This is the one place your outside knowledge belongs, and it belongs
   as caution, not as a new recommendation.
7. Plain English. No jargon the brief did not use. Short sentences."""

NARRATIVE_TOOL: Dict[str, Any] = {
    "name": "hedge_narrative",
    "description": "Explain the engine's hedging verdict in plain language.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": "One sentence a portfolio owner would understand.",
            },
            "recommended_action": {
                "type": "string",
                "enum": ["hedge", "de_risk_by_selling"],
                "description": "Must match the engine verdict exactly.",
            },
            "what_you_give_up": {"type": "string"},
            "what_stays_unprotected": {"type": "string"},
            "why_this_candidate": {"type": ["string", "null"]},
            "candidate_kind": {
                "type": ["string", "null"],
                "description": "Which construction, from the candidate list only.",
            },
            "sample_caution": {"type": "string"},
            "limits_of_this_analysis": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "headline",
            "recommended_action",
            "what_you_give_up",
            "what_stays_unprotected",
            "sample_caution",
        ],
    },
}


# --------------------------------------------------------------------------- #
# Mechanical checks — the engine wins every disagreement
# --------------------------------------------------------------------------- #
def validate(payload: Dict[str, Any], analysis: Dict[str, Any], brief: str) -> Dict[str, Any]:
    """Force the narrative back into agreement with the engine.

    Contradictions are corrected and recorded rather than silently accepted:
    the caller (and the user) can see that the model tried to say something
    the numbers did not support.
    """
    out = dict(payload)
    flags: List[str] = []

    engine_action = (analysis.get("verdict") or {}).get("action")
    if engine_action and out.get("recommended_action") != engine_action:
        flags.append(
            "model recommended '{}' but the engine's verdict is '{}' — overridden".format(
                out.get("recommended_action"), engine_action
            )
        )
        out["recommended_action"] = engine_action

    kinds = {row["kind"] for row in analysis.get("rows", [])}
    if out.get("candidate_kind") and out["candidate_kind"] not in kinds:
        flags.append(
            "model named '{}', which the engine never priced — dropped".format(
                out["candidate_kind"]
            )
        )
        out["candidate_kind"] = None
        out["why_this_candidate"] = None
    if engine_action == "de_risk_by_selling" and out.get("candidate_kind"):
        # Naming a pick while the verdict says sell reads as a recommendation.
        flags.append("verdict is to sell, so the named candidate was dropped")
        out["candidate_kind"] = None

    unknown = _unsupported_numbers(out, brief)
    if unknown:
        flags.append(
            "figures not found in the brief (treat as unverified): " + ", ".join(unknown)
        )

    out["contradicted_engine"] = flags
    return out


def _prose(payload: Dict[str, Any]) -> str:
    parts: List[str] = []
    for value in payload.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(v for v in value if isinstance(v, str))
    return " ".join(parts)


def _unsupported_numbers(payload: Dict[str, Any], brief: str) -> List[str]:
    """Figures in the prose that never appeared in the brief."""
    haystack = brief.replace(",", "")
    unknown: List[str] = []
    for token in NUMBER_PATTERN.findall(_prose(payload)):
        digits = token.replace(",", "").replace(".", "")
        if len(digits) < NUMBER_MIN_DIGITS:
            continue
        bare = token.replace(",", "")
        if bare in haystack or bare.rstrip("0").rstrip(".") in haystack:
            continue
        if bare not in unknown:
            unknown.append(bare)
    return unknown[:10]


def run(
    brief: str,
    analysis: Dict[str, Any],
    client: Optional[Any] = None,
) -> Dict[str, Any]:
    """One structured call over the brief. ``client`` is injectable for tests."""
    state = availability()
    if not state["enabled"]:
        raise RuntimeError(state["reason"])

    if client is None:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    message = client.messages.create(
        model=settings.assistant_model,
        max_tokens=settings.assistant_max_tokens,
        system=SYSTEM_PROMPT,
        tools=[NARRATIVE_TOOL],
        tool_choice={"type": "tool", "name": "hedge_narrative"},
        output_config={"effort": settings.assistant_effort},
        messages=[{
            "role": "user",
            "content": "Explain this hedging analysis.\n\n{}".format(brief),
        }],
    )
    blocks = [b for b in message.content if getattr(b, "type", None) == "tool_use"]
    if not blocks:
        raise RuntimeError("Model returned no structured narrative")
    return validate(dict(blocks[0].input), analysis, brief)
