"""Triage: one model call that ranks candidates and adds what scanners cannot.

This is the first place in the thesis engine where a model appears, and its
role is deliberately narrow. The deterministic funnel found the candidates and
computed every number; the model's sole irreplaceable contribution is world
knowledge — "this company holds an Osaka casino license" is a fact no scanner
can emit. Everything it adds arrives as a *hypothesis* with instructions for
verifying it against platform commands, never as an assertion.

Cards also carry the family's measured base rate once the graded log holds
enough events to have one. That is the loop closing: what previous events in
this family actually did becomes the prior the model has to argue past, rather
than a hardcoded warning in a prompt.

The call is structured output (a forced tool call), not chat: one request over
compact anomaly cards, one validated JSON result. Anti-slop is enforced twice —
by the prompt rules, and mechanically afterwards: any ``verify_with`` path that
is not a registered command is flagged, and a response that references a symbol
not in the cards is dropped.

Like the assistant, this needs ``MFT_ANTHROPIC_API_KEY``; unlike everything
else in the thesis engine, it is switched off without one. The funnel, the
spine and every ``/thesis/*`` command keep working key-free.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import settings
from ..core.registry import REGISTRY

try:  # the platform's one paid dependency; absence is a supported state
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]


def availability() -> Dict[str, Any]:
    from ..assistant.service import availability as assistant_availability

    return assistant_availability()


# --------------------------------------------------------------------------- #
# Cards — deterministic, every number traceable to the funnel
# --------------------------------------------------------------------------- #
def build_card(row: Dict[str, Any], moves: Optional[Dict[str, Any]] = None,
               spy_moves: Optional[Dict[str, Any]] = None,
               base_rate: Optional[str] = None) -> str:
    """One compact anomaly card. ~120 tokens, nothing the funnel didn't say.

    ``base_rate`` is the measured history of this row's family, phrased by
    :func:`backend.thesis.memory.describe_base_rate`. It is the one number on
    the card the funnel did not compute — it comes from the graded log of what
    previous events in this family actually did — and it is omitted entirely
    until enough events have been graded to mean anything.
    """
    def pct(value: Any) -> str:
        return "{:+.1f}%".format(100 * value) if isinstance(value, (int, float)) else "n/a"

    lines = [
        "{} · {} · family={}".format(row["symbol"], row.get("issuer", "?"), row.get("family", "?")),
        "  cluster: officers={} (${:,.0f}) · board-backed=${:,.0f}{} · last filing {}".format(
            row.get("officer_buyers", 0), row.get("officer_value", 0),
            row.get("board_backed_value", 0),
            " via " + row["board_backed_via"] if row.get("board_backed_via") else "",
            row.get("last_filing", "?")),
        "  buyers: {} · total buyers={} · has_ceo_cfo={}".format(
            row.get("buyers", "?"), row.get("total_buyers", "?"), row.get("has_ceo_cfo", False)),
    ]
    if moves:
        lines.append("  price: 1m {} · 3m {} · 1y {}{}".format(
            pct(moves.get("one_month")), pct(moves.get("three_month")), pct(moves.get("one_year")),
            " (SPY 1y {})".format(pct(spy_moves.get("one_year"))) if spy_moves else ""))
    else:
        lines.append("  price: unavailable")
    if base_rate:
        lines.append("  base rate (3m, measured): {}".format(base_rate))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """You triage candidates that deterministic scanners surfaced from SEC insider-filing
data. You have two jobs.

RANK. Decide which candidates justify an expensive deep dive. Most do not.
Returning promote=false for the large majority is the expected, correct outcome —
you are not graded on how many ideas you find.

ENRICH. For candidates you do promote, contribute what the scanners cannot see:
what you know about this company, its industry, its pending catalysts. This is
the only stage where outside knowledge enters the system.

RULES
1. Never state a number that did not appear in a card. No prices, no multiples,
   no growth rates, no dates from memory. If you need a number, ask for it in
   verify_with.
2. Every leg is tagged source "signal" or "world_knowledge". A world_knowledge
   leg is a HYPOTHESIS, never an assertion. Phrase it as a claim to be checked
   and give verify_with commands that could refute it.
3. For each world_knowledge leg also give if_absent: what it means for the
   thesis if verification finds nothing. A leg with no such answer is not
   falsifiable and must not be written.
4. Insiders trade on calendar, not conviction. Clusters just after an earnings
   release or in a routine open window are usually artifacts — say so in
   calendar_artifact_risk and decline rather than inventing a story.
5. Signals that are all one family, or all one actor, are not convergence.
6. A high score is not a thesis. If you cannot state a mechanism connecting the
   signals to a reason the price should change, set promote=false.
7. Prefer "I don't know enough about this company" (promote=false, say so in
   reason) to a plausible narrative. Unfamiliarity is a valid reason.
8. These are attention signals, not alpha signals. Where a card carries a
   "base rate" line, that is this platform's own measured record of every
   graded event in the same family — treat it as the prior your reasoning has
   to beat, and say in reason what makes this candidate unlike that average.
   A card with no base rate line has too few graded events to have earned one;
   that is not evidence in either direction, so do not read it as permission.
   promote=true means "worth a human's investigation time", nothing more."""

TRIAGE_TOOL: Dict[str, Any] = {
    "name": "triage_result",
    "description": "Return the triage verdict for every candidate card.",
    "input_schema": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "promote": {"type": "boolean"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                        "claim_sketch": {"type": ["string", "null"]},
                        "legs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "claim": {"type": "string"},
                                    "source": {"type": "string",
                                               "enum": ["signal", "world_knowledge"]},
                                    "supporting_signal": {"type": ["string", "null"]},
                                    "verify_with": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "path": {"type": "string"},
                                                "params": {"type": "object"},
                                                "expect": {"type": "string"},
                                            },
                                            "required": ["path", "expect"],
                                        },
                                    },
                                    "if_absent": {"type": ["string", "null"]},
                                },
                                "required": ["claim", "source"],
                            },
                        },
                        "calendar_artifact_risk": {"type": "string",
                                                   "enum": ["low", "medium", "high"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["symbol", "promote", "confidence", "reason"],
                },
            }
        },
        "required": ["candidates"],
    },
}


def validate(payload: Dict[str, Any], card_symbols: List[str]) -> Dict[str, Any]:
    """Mechanical anti-slop pass over the model's output.

    Drops verdicts for symbols that were never in the cards (invented
    candidates), flags ``verify_with`` paths that are not registered commands,
    and strips world-knowledge legs that omitted ``if_absent`` — a leg that
    cannot say what its absence would mean is not falsifiable.
    """
    known = {s.upper() for s in card_symbols}
    kept: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for cand in payload.get("candidates", []):
        symbol = str(cand.get("symbol", "")).upper()
        if symbol not in known:
            dropped.append(symbol or "(blank)")
            continue
        legs = []
        for leg in cand.get("legs") or []:
            if leg.get("source") == "world_knowledge" and not leg.get("if_absent"):
                leg = {**leg, "rejected": "world_knowledge leg without if_absent"}
                legs.append(leg)
                continue
            for check in leg.get("verify_with") or []:
                path = "/" + str(check.get("path", "")).strip().strip("/")
                check["path"] = path
                if path not in REGISTRY:
                    check["unknown_command"] = True
            legs.append(leg)
        kept.append({**cand, "symbol": symbol, "legs": legs})
    out: Dict[str, Any] = {"candidates": kept}
    if dropped:
        out["dropped_invented_symbols"] = dropped
    return out


def run(cards: List[str], card_symbols: List[str],
        client: Optional[Any] = None) -> Dict[str, Any]:
    """One structured call over the cards. ``client`` is injectable for tests."""
    state = availability()
    if not state["enabled"]:
        raise RuntimeError(state["reason"])

    if client is None:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    message = client.messages.create(
        model=settings.assistant_model,
        max_tokens=settings.assistant_max_tokens,
        system=SYSTEM_PROMPT,
        tools=[TRIAGE_TOOL],
        tool_choice={"type": "tool", "name": "triage_result"},
        output_config={"effort": settings.assistant_effort},
        messages=[{
            "role": "user",
            "content": (
                "Triage these {} candidates. One verdict per card, in the same "
                "order.\n\n{}".format(len(cards), "\n\n".join(cards))
            ),
        }],
    )
    blocks = [b for b in message.content if getattr(b, "type", None) == "tool_use"]
    if not blocks:
        raise RuntimeError("Model returned no structured verdict")
    return validate(dict(blocks[0].input), card_symbols)
