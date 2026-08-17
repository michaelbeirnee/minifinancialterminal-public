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

from typing import Any, Dict, Iterable, List, Optional, Sequence

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
def describe_concentration(rows: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """The subject's largest self-disclosed counterparty, in one line.

    Read from the company's *own* annual report, so ``exposure_pct`` is a share
    of its own books — the direction that bears on a thesis about this company.
    ``None`` when the filer named nobody, which is the normal case for a large
    cap and is emphatically not evidence of diversification: the disclosure
    rule demands the percentage, never the name.
    """
    best: Optional[Dict[str, Any]] = None
    for row in rows or []:
        pct = row.get("exposure_pct")
        if not isinstance(pct, (int, float)):
            continue
        if best is None or pct > best["exposure_pct"]:
            best = row
    if best is None:
        return None
    return "{} {} = {:.0f}% of {} ({} {})".format(
        best.get("relationship") or "counterparty",
        best.get("symbol") or best.get("company") or "?",
        float(best["exposure_pct"]),
        best.get("pct_of") or "revenue",
        best.get("form") or "?",
        str(best.get("filing_date") or "")[:10],
    )


def describe_congress(rows: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """Congressional disclosures naming this symbol, in one line.

    Counts people and directions rather than money, because the amounts are
    brackets and summing brackets produces a number that was never traded.
    ``None`` when nobody disclosed — which, covering 100 of 535 members, is
    the normal case and is not evidence that nobody traded it.
    """
    if not rows:
        return None
    members = {r.get("member") for r in rows if r.get("member")}
    sides: Dict[str, int] = {}
    for row in rows:
        side = str(row.get("side") or "unknown")
        sides[side] = sides.get(side, 0) + 1
    from ..providers.congress import is_member_account

    latest = max((str(r.get("filing_date") or "") for r in rows), default="")
    directed = sum(1 for r in rows if is_member_account(r.get("owner")))
    breakdown = ", ".join("{} {}".format(count, side)
                          for side, count in sorted(sides.items(), key=lambda kv: -kv[1]))
    return "{} member(s), {} ({} in the member's own account) · latest filing {}".format(
        len(members), breakdown, directed, latest or "?")


def build_card(row: Dict[str, Any], moves: Optional[Dict[str, Any]] = None,
               spy_moves: Optional[Dict[str, Any]] = None,
               base_rate: Optional[str] = None,
               concentration: Optional[str] = None,
               congress: Optional[str] = None,
               detail: Sequence[str] = ()) -> str:
    """One compact anomaly card. ~130 tokens, nothing unsourced.

    ``detail`` is the funnel's own numbers, rendered by the source that emitted
    the row (:attr:`backend.thesis.sources.Source.detail`). Everything else here
    is the frame every source shares, which is why this function knows what a
    price move is and does not know what an officer is.

    ``base_rate`` is the measured history of this row's family, phrased by
    :func:`backend.thesis.memory.describe_base_rate`. It is the one number on
    the card the funnel did not compute — it comes from the graded log of what
    previous events in this family actually did — and it is omitted entirely
    until enough events have been graded to mean anything.

    ``concentration`` is the other: a counterparty this company named in its
    own annual report, from :func:`describe_concentration`. Insider buying at a
    supplier whose single customer is most of its revenue is a different claim
    from the same cluster at a diversified name, and nothing in Form 4 data
    says which one you are looking at.

    ``congress`` is the same idea applied to the other set of disclosed
    insiders: what members of Congress filed on this symbol, from
    :func:`describe_congress`. It is a second population trading the same name,
    never a corroboration of the first — they are different people, disclosing
    under a different statute, for reasons the filings do not give.
    """
    def pct(value: Any) -> str:
        return "{:+.1f}%".format(100 * value) if isinstance(value, (int, float)) else "n/a"

    lines = [
        "{} · {} · family={}".format(row["symbol"], row.get("issuer", "?"), row.get("family", "?")),
        *detail,
    ]
    if moves:
        lines.append("  price: 1m {} · 3m {} · 1y {}{}".format(
            pct(moves.get("one_month")), pct(moves.get("three_month")), pct(moves.get("one_year")),
            " (SPY 1y {})".format(pct(spy_moves.get("one_year"))) if spy_moves else ""))
    else:
        lines.append("  price: unavailable")
    if concentration:
        lines.append("  concentration (self-disclosed): {}".format(concentration))
    if congress:
        lines.append("  congress (Senate STOCK Act): {}".format(congress))
    if base_rate:
        lines.append("  base rate (3m, measured): {}".format(base_rate))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #
_PROMPT_HEAD = """You triage investment-idea candidates that a deterministic scanner surfaced.
You have two jobs.

RANK. Decide which candidates justify an expensive deep dive. Most do not.
Returning promote=false for the large majority is the expected, correct outcome —
you are not graded on how many ideas you find.

ENRICH. For candidates you do promote, contribute what the scanners cannot see:
what you know about this company, its industry, its pending catalysts. This is
the only stage where outside knowledge enters the system."""

#: True whatever the funnel was.
_CORE_RULES = (
    """Never state a number that did not appear in a card. No prices, no multiples,
   no growth rates, no dates from memory. If you need a number, ask for it in
   verify_with.""",
    """Every leg is tagged source "signal" or "world_knowledge". A world_knowledge
   leg is a HYPOTHESIS, never an assertion. Phrase it as a claim to be checked
   and give verify_with commands that could refute it.""",
    """For each world_knowledge leg also give if_absent: what it means for the
   thesis if verification finds nothing. A leg with no such answer is not
   falsifiable and must not be written.""",
)

_TAIL_RULES = (
    """A high score is not a thesis. If you cannot state a mechanism connecting the
   signals to a reason the price should change, set promote=false. Set direction
   to long, short or neutral for every candidate based on the claim, not the
   scanner: a drawdown or crowded short can support either direction. Use neutral
   when the evidence only earns a watchlist slot.""",
    """Prefer "I don't know enough about this company" (promote=false, say so in
   reason) to a plausible narrative. Unfamiliarity is a valid reason.""",
    """These are attention signals, not alpha signals. Where a card carries a
   "base rate" line, that is this platform's own measured record of every
   graded event in the same family — treat it as the prior your reasoning has
   to beat, and say in reason what makes this candidate unlike that average.
   A card with no base rate line has too few graded events to have earned one;
   that is not evidence in either direction, so do not read it as permission.
   promote=true means "worth a human's investigation time", nothing more.""",
)

#: What to say when a source did not declare its own failure mode. Deliberately
#: weaker than any real source's rule — a funnel that cannot name the way it
#: lies has not earned a specific warning.
_GENERIC_ARTIFACT_RULE = (
    """Ask what would make this candidate an artifact of how the scanner works
   rather than a fact about the company, say so in calendar_artifact_risk, and
   decline rather than inventing a story. Signals that are all one family, or
   all one actor, are not convergence."""
)

#: One per shared card enrichment. Added only when that line can actually
#: appear, because a rule explaining a line the model never sees is noise.
ENRICHMENT_RULES: Dict[str, str] = {
    "concentration": (
        """A "concentration" line is a counterparty the company named in its own
   annual report, so it is a share of THIS company's books and can be up to a
   year stale — the form and filing date are on the line. Absence of the line
   means the filing named nobody, which is the norm for a large cap; it is
   never evidence of diversification. Where the line is present, say in reason
   whether the candidate's mechanism runs through that counterparty, and route
   verify_with at the counterparty when it does."""
    ),
    "congress": (
        """A "congress" line is what members of the US Senate disclosed on this
   symbol under the STOCK Act. It is a DIFFERENT population from whatever this
   funnel selected on, disclosing under a different statute for reasons the
   filing never states, so it is not corroboration and must not be described as
   insiders agreeing. The amounts are brackets and the filing can lag the trade
   by 45 days. Trades outside the member's own account may never have been
   theirs. Coverage is the Senate alone, so silence means nothing either way.
   Treat the line as one more thing to explain, not as a reason on its own; a
   leg built on it needs a mechanism and an if_absent like any other."""
    ),
}


def system_prompt(source: Optional[Any] = None,
                  enrichments: Iterable[str] = ()) -> str:
    """The triage prompt for one funnel.

    ``source`` is a :class:`backend.thesis.sources.Source`; it contributes the
    scope line — what the scanner selected on, so the model is not guessing
    from the card format — and the artifact rule, which is how *this* funnel
    characteristically produces false positives. ``enrichments`` names the
    shared card lines that were actually attached, so their rules appear only
    when the line can.
    """
    blocks = [_PROMPT_HEAD]
    if source is not None and getattr(source, "scope", ""):
        blocks.append("WHAT THIS FUNNEL SELECTED ON\n{}".format(source.scope))

    rules = list(_CORE_RULES)
    rules.append(getattr(source, "artifact_rule", "") or _GENERIC_ARTIFACT_RULE)
    rules.extend(_TAIL_RULES)
    rules.extend(ENRICHMENT_RULES[key] for key in enrichments if key in ENRICHMENT_RULES)

    blocks.append("RULES\n" + "\n".join(
        "{}. {}".format(number, rule) for number, rule in enumerate(rules, start=1)))
    return "\n\n".join(blocks)


#: The prompt with no source named — what a caller triaging raw cards gets.
SYSTEM_PROMPT = system_prompt()

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
                        "direction": {"type": "string",
                                      "enum": ["long", "short", "neutral"]},
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
                    "required": ["symbol", "promote", "confidence", "direction", "reason"],
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
        direction = str(cand.get("direction") or "neutral").strip().lower()
        if direction not in ("long", "short", "neutral"):
            direction = "neutral"
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
        kept.append({**cand, "symbol": symbol, "direction": direction, "legs": legs})
    out: Dict[str, Any] = {"candidates": kept}
    if dropped:
        out["dropped_invented_symbols"] = dropped
    return out


def run(cards: List[str], card_symbols: List[str],
        client: Optional[Any] = None, source: Optional[Any] = None,
        enrichments: Iterable[str] = ()) -> Dict[str, Any]:
    """One structured call over the cards. ``client`` is injectable for tests.

    ``source`` and ``enrichments`` shape the prompt — see :func:`system_prompt`.
    Both are optional: cards built by hand still triage, they just get the
    generic artifact rule.
    """
    state = availability()
    if not state["enabled"]:
        raise RuntimeError(state["reason"])

    if client is None:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    message = client.messages.create(
        model=settings.assistant_model,
        max_tokens=settings.assistant_max_tokens,
        system=system_prompt(source, enrichments),
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
