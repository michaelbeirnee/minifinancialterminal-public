"""Deterministic top-down and bottom-up context for one public security.

This module deliberately does not write an investment narrative. It assembles
traceable evidence, applies a small transparent mechanical read, and leaves the
causal exposure bridge explicit. A later model or human can synthesize the
packet without inventing the market data that went into it.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from ..core.registry import execute as registry_execute
from ..core.utils import one_symbol

Runner = Callable[..., Any]

HORIZONS = ("one_month", "three_month", "ytd", "one_year")
MOVE_THRESHOLDS = {
    "one_month": 0.03,
    "three_month": 0.05,
    "ytd": 0.05,
    "one_year": 0.10,
}

# Yahoo's company sectors on the left; the liquid SPDR proxy on the right.
SECTOR_ETFS = {
    "technology": "XLK",
    "healthcare": "XLV",
    "health care": "XLV",
    "financial services": "XLF",
    "financials": "XLF",
    "energy": "XLE",
    "consumer cyclical": "XLY",
    "consumer discretionary": "XLY",
    "consumer defensive": "XLP",
    "consumer staples": "XLP",
    "industrials": "XLI",
    "basic materials": "XLB",
    "materials": "XLB",
    "utilities": "XLU",
    "real estate": "XLRE",
    "communication services": "XLC",
}

SECTOR_FRAMEWORKS: Dict[str, Dict[str, List[str]]] = {
    "technology": {
        "drivers": ["real yields and valuation duration", "enterprise and consumer capex",
                    "US-dollar and geographic revenue exposure"],
        "focus": ["organic growth and backlog", "gross/operating margin and cash conversion",
                  "stock-based compensation and dilution"],
        "valuation": ["growth-adjusted EV/revenue", "FCF yield", "mature-state margins"],
        "traps": ["AI narrative without monetization", "SBC masking economics",
                  "cyclical demand mistaken for secular growth"],
    },
    "healthcare": {
        "drivers": ["rates and funding conditions", "reimbursement and policy",
                    "procedure volumes or clinical/regulatory events"],
        "focus": ["volume and pricing", "pipeline or product-cycle evidence",
                  "patent, reimbursement and payer exposure"],
        "valuation": ["risk-adjusted NPV", "patent-adjusted earnings", "peer multiples"],
        "traps": ["binary risk hidden by screens", "patent cliffs", "reimbursement pressure"],
    },
    "financial services": {
        "drivers": ["yield-curve shape", "credit conditions and losses",
                    "deposit competition and regulation"],
        "focus": ["ROTCE and tangible book", "NIM and deposit beta", "capital and credit quality"],
        "valuation": ["P/TBV versus sustainable ROTCE", "capital-return capacity"],
        "traps": ["low P/E on unsustainably low losses", "duration marks",
                  "capital return constrained by regulation"],
    },
    "energy": {
        "drivers": ["oil and gas prices", "OPEC/supply discipline", "global growth and inventories"],
        "focus": ["FCF at strip and mid-cycle", "decline rates and inventory depth",
                  "capex and capital returns"],
        "valuation": ["FCF yield at strip and mid-cycle", "NAV", "EV/DACF"],
        "traps": ["spot prices capitalized as permanent", "understated maintenance capex",
                  "inventory deterioration"],
    },
    "consumer cyclical": {
        "drivers": ["employment and real income", "consumer credit", "rates and housing"],
        "focus": ["traffic versus price", "inventory and promotions", "unit economics and margins"],
        "valuation": ["normalized P/E", "FCF yield", "EV/EBITDAR where relevant"],
        "traps": ["price-led comps called demand", "temporary input-cost relief",
                  "credit stress arriving with a lag"],
    },
    "consumer defensive": {
        "drivers": ["inflation and input costs", "consumer trade-down", "currency exposure"],
        "focus": ["volume versus pricing", "gross margin", "brand investment and market share"],
        "valuation": ["P/E versus organic growth", "FCF yield", "dividend coverage"],
        "traps": ["pricing masking volume loss", "underinvestment supporting cash flow",
                  "defensive multiple mistaken for low risk"],
    },
    "industrials": {
        "drivers": ["industrial production and PMIs", "rates and capital spending",
                    "government/infrastructure demand"],
        "focus": ["organic orders and backlog", "book-to-bill and conversion",
                  "price/cost and segment margins"],
        "valuation": ["mid-cycle P/E", "EV/EBIT", "FCF yield"],
        "traps": ["late-cycle order strength", "backlog not converting",
                  "temporary price/cost tailwinds"],
    },
    "basic materials": {
        "drivers": ["commodity prices", "China/global industrial demand", "energy and freight costs"],
        "focus": ["cost-curve position", "reserves and production profile", "sustaining capex"],
        "valuation": ["NAV", "cycle-normalized EV/EBITDA", "FCF yield"],
        "traps": ["spot prices used as normalized", "grade or reserve decline",
                  "jurisdiction and permitting risk"],
    },
    "utilities": {
        "drivers": ["long rates", "regulation and allowed returns", "power demand and fuel costs"],
        "focus": ["rate-base growth", "funding and equity issuance", "project execution"],
        "valuation": ["rate-base-growth-adjusted P/E", "dividend coverage", "peer premium/discount"],
        "traps": ["regulatory lag", "unfunded capex", "catastrophe or liability risk"],
    },
    "real estate": {
        "drivers": ["real rates and cap rates", "credit availability", "property-specific demand"],
        "focus": ["same-store NOI and leasing", "occupancy and tenant concentration",
                  "debt maturity and floating-rate exposure"],
        "valuation": ["NAV discount/premium", "P/AFFO", "implied cap rate"],
        "traps": ["stale private-market cap rates", "dividend as false support",
                  "refinancing risk"],
    },
    "communication services": {
        "drivers": ["advertising and consumer demand", "rates and capital intensity",
                    "regulation and platform competition"],
        "focus": ["subscribers/users and monetization", "churn and pricing", "capex and FCF"],
        "valuation": ["FCF yield", "EV/EBITDA", "sum of the parts"],
        "traps": ["FCF boosted by underinvestment", "leverage overwhelming equity value",
                  "engagement without monetization"],
    },
}

GENERIC_FRAMEWORK = {
    "drivers": ["growth and financial conditions", "rates, currency and input costs",
                "industry demand and regulation"],
    "focus": ["organic revenue", "margins and cash conversion", "balance-sheet resilience"],
    "valuation": ["peer-relative multiples", "history-relative multiples", "FCF support"],
    "traps": ["mixed reporting periods", "cyclical rebound called structural growth",
              "valuation already discounting the narrative"],
}


def _sector_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "health care": "healthcare", "financials": "financial services",
        "consumer discretionary": "consumer cyclical",
        "consumer staples": "consumer defensive", "materials": "basic materials",
    }
    return aliases.get(text, text)


def _one(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        return dict(value[0])
    return {}


def _call(key: str, lane: str, path: str, params: Dict[str, Any],
          runner: Runner) -> Dict[str, Any]:
    try:
        obj = runner(path, **params)
        return {
            "key": key, "lane": lane, "path": path, "params": params,
            "status": "ok", "provider": getattr(obj, "provider", None),
            "warnings": list(getattr(obj, "warnings", []) or []),
            "results": getattr(obj, "results", []),
            "extra": dict(getattr(obj, "extra", {}) or {}),
        }
    except Exception as exc:  # noqa: BLE001 - a workbench lane degrades independently
        return {
            "key": key, "lane": lane, "path": path, "params": params,
            "status": "error", "provider": None, "warnings": [],
            "results": [], "extra": {}, "error": str(exc)[:300],
        }


def _top_down(overview: Dict[str, Any], sector_row: Dict[str, Any],
              performance: Dict[str, Dict[str, Any]], symbol: str,
              benchmark: str, sector_etf: Optional[str], horizon: str) -> Dict[str, Any]:
    score = 0
    reasons: List[str] = []
    observations = 0

    regime = str(overview.get("regime") or "UNKNOWN").upper()
    if regime != "UNKNOWN":
        observations += 1
        delta = 1 if regime == "RISK-ON" else (-1 if regime == "RISK-OFF" else 0)
        score += delta
        reasons.append("Market regime is {}.".format(regime.lower()))

    sector_relative = sector_row.get("relative_{}".format(horizon))
    sector_return = (performance.get(sector_etf) or {}).get(horizon) if sector_etf else None
    benchmark_return = (performance.get(benchmark) or {}).get(horizon)
    # The ranked sector command is the preferred source, but the already-fetched
    # performance rows can preserve the observation if that wider scan fails.
    if (not isinstance(sector_relative, (int, float))
            and isinstance(sector_return, (int, float))
            and isinstance(benchmark_return, (int, float))):
        sector_relative = sector_return - benchmark_return
    if isinstance(sector_relative, (int, float)):
        observations += 1
        threshold = MOVE_THRESHOLDS[horizon]
        score += 1 if sector_relative >= threshold else (-1 if sector_relative <= -threshold else 0)
        reasons.append("Sector ETF is {:+.1%} versus {} over {}.".format(
            sector_relative, benchmark, horizon.replace("_", " ")))

    subject_return = (performance.get(symbol) or {}).get(horizon)
    relative_return = None
    if isinstance(subject_return, (int, float)) and isinstance(benchmark_return, (int, float)):
        relative_return = subject_return - benchmark_return
        observations += 1
        threshold = MOVE_THRESHOLDS[horizon]
        score += 1 if relative_return >= threshold else (-1 if relative_return <= -threshold else 0)
        reasons.append("{} is {:+.1%} versus {} over {}.".format(
            symbol, relative_return, benchmark, horizon.replace("_", " ")))

    state = "unknown" if not observations else (
        "constructive" if score >= 2 else "challenged" if score <= -2 else "mixed"
    )
    return {
        "state": state,
        "score": score,
        "observations": observations,
        "regime": regime,
        "signals": list(overview.get("signals") or [])[:8],
        "sector": sector_row,
        "sector_etf": sector_etf,
        "sector_return": sector_return,
        "sector_relative": sector_relative,
        "subject_return": subject_return,
        "benchmark_return": benchmark_return,
        "relative_return": relative_return,
        "reasons": reasons,
    }


def _bottom_up(metrics: Dict[str, Any], consensus: Dict[str, Any],
               framework: Dict[str, List[str]]) -> Dict[str, Any]:
    score = 0
    reasons: List[str] = []
    observations = 0

    def growth_read(field: str, label: str) -> None:
        nonlocal score, observations
        value = metrics.get(field)
        if not isinstance(value, (int, float)):
            return
        observations += 1
        score += 1 if value >= 0.10 else (-1 if value < 0 else 0)
        reasons.append("{} is {:+.1%}.".format(label, value))

    growth_read("revenue_growth", "Reported revenue growth")
    growth_read("earnings_growth", "Reported earnings growth")

    margin = metrics.get("operating_margin")
    if isinstance(margin, (int, float)):
        observations += 1
        score += 1 if margin > 0 else -1
        reasons.append("Operating margin is {:+.1%}.".format(margin))

    fcf = metrics.get("free_cash_flow")
    if isinstance(fcf, (int, float)):
        observations += 1
        score += 1 if fcf > 0 else -1
        reasons.append("Reported free cash flow is {}.".format(
            "positive" if fcf > 0 else "negative"))

    state = "unknown" if not observations else (
        "constructive" if score >= 2 else "challenged" if score <= -2 else "mixed"
    )
    return {
        "state": state,
        "score": score,
        "observations": observations,
        "metrics": metrics,
        "consensus": consensus,
        "framework": framework,
        "reasons": reasons,
        "caveat": (
            "Mechanical snapshot only. The sector framework names the operating metrics "
            "that must replace generic ratios during deeper work."
        ),
    }


def _alignment(top_state: str, bottom_state: str) -> Dict[str, str]:
    if "unknown" in (top_state, bottom_state):
        return {"key": "incomplete", "label": "Evidence incomplete",
                "reading": "One lane lacks enough observations for a joined read."}
    pair = (top_state, bottom_state)
    if pair == ("constructive", "constructive"):
        return {"key": "aligned_constructive", "label": "Aligned constructive",
                "reading": "Market/sector context and company snapshot point the same way."}
    if pair == ("challenged", "challenged"):
        return {"key": "aligned_challenged", "label": "Aligned challenged",
                "reading": "Both lanes show pressure; this is a reject or short-research queue, not a conclusion."}
    if pair == ("constructive", "challenged"):
        return {"key": "theme_without_company_proof", "label": "Theme without company proof",
                "reading": "The context is constructive, but the company snapshot has not earned exposure attribution."}
    if pair == ("challenged", "constructive"):
        return {"key": "idiosyncratic_strength", "label": "Idiosyncratic strength",
                "reading": "Company evidence is stronger than its context; test resilience and hedge needs."}
    return {"key": "mixed", "label": "Mixed evidence",
            "reading": "The two lanes do not yet produce a clean research posture."}


def build_context(symbol: str, benchmark: str = "SPY", horizon: str = "three_month",
                  runner: Optional[Runner] = None) -> Dict[str, Any]:
    """Build a reusable research packet; every source is named and degradable."""
    sym = one_symbol(symbol)
    bench = one_symbol(benchmark)
    if sym == bench:
        raise ValueError("symbol and benchmark must be different")
    horizon = str(horizon).strip().lower()
    if horizon not in HORIZONS:
        raise ValueError("horizon must be one of {}".format(", ".join(HORIZONS)))
    run = runner or registry_execute

    profile_call = _call(
        "profile", "bottom_up", "/equity/profile",
        {"symbol": sym, "provider": "yahoo"}, run,
    )
    profile = _one(profile_call["results"])
    sector_key = _sector_key(profile.get("sector"))
    sector_etf = SECTOR_ETFS.get(sector_key)

    performance_symbols = [sym, bench]
    if sector_etf and sector_etf not in performance_symbols:
        performance_symbols.append(sector_etf)
    specs = [
        ("overview", "top_down", "/overview/brief", {}),
        ("sector_rotation", "top_down", "/thesis/sector_rotation", {"limit": 11}),
        ("performance", "top_down", "/equity/price/performance",
         {"symbol": ",".join(performance_symbols)}),
        ("metrics", "bottom_up", "/equity/fundamental/metrics", {"symbol": sym}),
        ("consensus", "bottom_up", "/equity/estimates/consensus", {"symbol": sym}),
    ]
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        calls = list(pool.map(lambda spec: _call(*spec, runner=run), specs))
    calls.insert(0, profile_call)
    by_key = {call["key"]: call for call in calls}

    overview = _one(by_key["overview"]["results"])
    sector_rows = by_key["sector_rotation"]["results"]
    sector_row = next((dict(row) for row in sector_rows
                       if isinstance(row, Mapping) and row.get("symbol") == sector_etf), {})
    performance = {
        str(row.get("symbol")): dict(row)
        for row in by_key["performance"]["results"]
        if isinstance(row, Mapping) and row.get("symbol")
    }
    metrics = _one(by_key["metrics"]["results"])
    consensus = _one(by_key["consensus"]["results"])
    framework = SECTOR_FRAMEWORKS.get(sector_key, GENERIC_FRAMEWORK)

    top = _top_down(
        overview, sector_row, performance, sym, bench, sector_etf, horizon
    )
    bottom = _bottom_up(metrics, consensus, framework)
    alignment = _alignment(top["state"], bottom["state"])

    source_manifest = [{k: call.get(k) for k in (
        "key", "lane", "path", "params", "status", "provider", "warnings", "error"
    ) if call.get(k) not in (None, [], {})} for call in calls]
    failures = [call for call in calls if call["status"] == "error"]
    provider_warnings = [
        "{}: {}".format(call["key"], warning)
        for call in calls for warning in call.get("warnings", [])
    ]
    warnings = ["{}: {}".format(call["key"], call.get("error", "unavailable"))
                for call in failures] + provider_warnings
    successful = len(calls) - len(failures)

    return {
        "schema": "research_context.v1",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "subject": {
            "symbol": sym, "name": profile.get("name") or sym,
            "sector": profile.get("sector"), "industry": profile.get("industry"),
            "benchmark": bench, "sector_etf": sector_etf,
        },
        "settings": {"horizon": horizon, "benchmark": bench},
        "assessment": {
            "top_down_state": top["state"],
            "bottom_up_state": bottom["state"],
            "alignment": alignment,
            "coverage": {
                "successful": successful, "total": len(calls),
                "ratio": round(successful / len(calls), 4),
            },
            "disclaimer": (
                "A joined context packet is a research-priority aid, not a recommendation. "
                "The mechanical states are transparent prompts for deeper work."
            ),
        },
        "top_down": top,
        "bottom_up": bottom,
        "exposure_bridge": {
            "status": "needs_exposure_proof",
            "driver_prompts": framework["drivers"],
            "chain": [
                {"step": "driver", "question": "Which macro or sector driver matters?"},
                {"step": "exposure", "question": "Where is that exposure disclosed?"},
                {"step": "financial", "question": "Which revenue, margin or cash-flow line changes?"},
                {"step": "expectations", "question": "What does the current valuation already assume?"},
            ],
        },
        "sources": source_manifest,
        "warnings": warnings,
    }
