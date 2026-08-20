"""Portfolio-level alpha sleeve, concentration and borrow-risk controls.

This module takes the signal set that survived statistical research and turns it
into a portfolio-level allocation.  It deliberately keeps three concepts
separate:

* **alpha sleeves** are interpretable signal families (momentum, reversal,
  fundamentals, events, options, ...), each with its own historical PnL stream;
* **risk budgets** are assigned from trailing sleeve volatility and the amount
  of validated research weight in the sleeve, then capped for single-sleeve,
  event and highly-correlated-cluster concentration;
* **security constraints** are applied after sleeves are combined: name limits,
  optional group-net limits, short availability and crowded-short limits.

Borrow/crowding inputs are point-in-time.  If no archived borrow/crowding data
exists, the engine charges a configurable general-collateral proxy fee and does
not invent hard-to-borrow history.  Archived short-interest fields can tighten
short availability and add a crowding surcharge from their observation date
forward.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from .research_controls import correlation_clusters
from .signal_research import SignalLibraryOutput, SignalSpec
from .stat_arb import _neutral_weights


@dataclass(frozen=True)
class BorrowPanels:
    annual_borrow_bps: pd.DataFrame
    shortable: pd.DataFrame
    crowding_score: pd.DataFrame
    source_status: dict[str, Any]


def _panel_like(index: pd.Index, columns: pd.Index, value: float) -> pd.DataFrame:
    return pd.DataFrame(float(value), index=index, columns=columns, dtype=float)


def _feature_panel(
    features: Mapping[str, pd.DataFrame] | None,
    name: str,
    index: pd.Index,
    columns: pd.Index,
) -> pd.DataFrame | None:
    if not features or name not in features:
        return None
    return features[name].reindex(index=index, columns=columns).astype(float)


def build_borrow_panels(
    prices: pd.DataFrame,
    features: Mapping[str, pd.DataFrame] | None = None,
    *,
    base_borrow_bps: float = 30.0,
    crowding_surcharge_bps: float = 900.0,
    crowding_short_float_threshold: float = 0.10,
    hard_to_borrow_short_float: float = 0.35,
    hard_to_borrow_days_to_cover: float = 15.0,
) -> BorrowPanels:
    """Build dated shortability and borrow-cost proxies.

    Supported point-in-time feature fields are ``borrow_fee_annual_bps`` and
    ``short_available`` when a future prime-broker/borrow feed is archived, and
    Yahoo-derived ``short_percent_float`` / ``short_ratio`` crowding snapshots.
    Yahoo does not publish an actual stock-loan fee, so crowding-based fees are
    explicitly a model proxy rather than observed borrow quotes.
    """

    prices = prices.sort_index()
    index, columns = prices.index, prices.columns
    base = max(0.0, float(base_borrow_bps))
    annual = _panel_like(index, columns, base)
    shortable = pd.DataFrame(True, index=index, columns=columns, dtype=bool)
    crowding = _panel_like(index, columns, 0.0)

    observed_fee = _feature_panel(features, "borrow_fee_annual_bps", index, columns)
    observed_available = _feature_panel(features, "short_available", index, columns)
    short_float = _feature_panel(features, "short_percent_float", index, columns)
    short_ratio = _feature_panel(features, "short_ratio", index, columns)

    has_real_fee = False
    has_real_availability = False
    has_crowding = False

    if observed_fee is not None:
        valid = observed_fee.notna() & np.isfinite(observed_fee) & (observed_fee >= 0.0)
        annual = annual.where(~valid, observed_fee)
        has_real_fee = bool(valid.any().any())

    if observed_available is not None:
        valid = observed_available.notna() & np.isfinite(observed_available)
        bool_panel = observed_available > 0.5
        shortable = shortable.where(~valid, bool_panel)
        has_real_availability = bool(valid.any().any())

    if short_float is not None:
        # Providers generally return a fraction (0.18 == 18%), but normalise a
        # percentage-style value defensively if one was archived.
        short_float = short_float.copy()
        short_float = short_float.where(short_float.abs() <= 1.5, short_float / 100.0)
        short_float = short_float.clip(lower=0.0, upper=1.0)
        has_crowding = bool(short_float.notna().any().any())
    if short_ratio is not None:
        short_ratio = short_ratio.clip(lower=0.0)
        has_crowding = has_crowding or bool(short_ratio.notna().any().any())

    short_component = (
        ((short_float - float(crowding_short_float_threshold)).clip(lower=0.0)
         / max(1e-6, 1.0 - float(crowding_short_float_threshold)))
        if short_float is not None
        else _panel_like(index, columns, 0.0)
    )
    ratio_component = (
        (short_ratio / max(1e-6, float(hard_to_borrow_days_to_cover))).clip(lower=0.0, upper=1.0)
        if short_ratio is not None
        else _panel_like(index, columns, 0.0)
    )
    crowding = (0.7 * short_component.fillna(0.0) + 0.3 * ratio_component.fillna(0.0)).clip(0.0, 1.0)

    # Only add the proxy surcharge where an archived crowding observation
    # actually exists. Missing history keeps the transparent GC base fee.
    crowding_observed = pd.DataFrame(False, index=index, columns=columns)
    if short_float is not None:
        crowding_observed |= short_float.notna()
    if short_ratio is not None:
        crowding_observed |= short_ratio.notna()
    proxy_fee = base + max(0.0, float(crowding_surcharge_bps)) * crowding
    if not has_real_fee:
        annual = annual.where(~crowding_observed, proxy_fee)
    else:
        observed_valid = observed_fee.notna() & np.isfinite(observed_fee) & (observed_fee >= 0.0)
        annual = annual.where(observed_valid | ~crowding_observed, proxy_fee)

    # A crowding snapshot can conservatively make a name unshortable.  It never
    # manufactures historical availability before the snapshot existed.
    proxy_unshortable = pd.DataFrame(False, index=index, columns=columns)
    if short_float is not None:
        proxy_unshortable |= short_float >= float(hard_to_borrow_short_float)
    if short_ratio is not None:
        proxy_unshortable |= short_ratio >= float(hard_to_borrow_days_to_cover)
    if has_real_availability:
        real_valid = observed_available.notna() & np.isfinite(observed_available)
        shortable = shortable.where(real_valid, ~proxy_unshortable)
    else:
        shortable &= ~proxy_unshortable

    mode = "gc_proxy_only"
    if has_real_fee or has_real_availability:
        mode = "archived_borrow_plus_proxy"
    elif has_crowding:
        mode = "archived_crowding_proxy"

    return BorrowPanels(
        annual_borrow_bps=annual.clip(lower=0.0),
        shortable=shortable.astype(bool),
        crowding_score=crowding,
        source_status={
            "mode": mode,
            "point_in_time": True,
            "observed_borrow_fee": has_real_fee,
            "observed_short_availability": has_real_availability,
            "archived_crowding": has_crowding,
            "base_borrow_bps": round(base, 6),
            "crowding_surcharge_bps": round(max(0.0, float(crowding_surcharge_bps)), 6),
            "note": (
                "Borrow fees are a general-collateral plus crowding proxy unless an archived "
                "borrow_fee_annual_bps/short_available feed is present."
            ),
        },
    )


def _signal_alpha_returns(
    prices: pd.DataFrame,
    library: SignalLibraryOutput,
    signal_names: list[str],
) -> pd.DataFrame:
    returns = prices.reindex(columns=library.beta.columns).pct_change(fill_method=None).fillna(0.0)
    out: dict[str, pd.Series] = {}
    for name in signal_names:
        component = library.components.get(name)
        if component is None:
            continue
        weights = _neutral_weights(
            component.reindex(index=prices.index, columns=library.beta.columns),
            library.beta.reindex(index=prices.index, columns=library.beta.columns),
            1.0,
        )
        held = weights.shift(1).fillna(0.0)
        out[name] = (held * returns).sum(axis=1)
    return pd.DataFrame(out, index=prices.index)


def _matrix_to_records(matrix: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for left in matrix.index:
        for right in matrix.columns:
            if str(left) >= str(right):
                continue
            value = matrix.loc[left, right]
            if pd.notna(value):
                rows.append({"left": str(left), "right": str(right), "correlation": round(float(value), 6)})
    return rows


def build_alpha_sleeve_plan(
    prices: pd.DataFrame,
    library: SignalLibraryOutput,
    report: Mapping[str, Any],
    specs: Mapping[str, SignalSpec],
    *,
    lookback: int = 126,
    correlation_threshold: float = 0.60,
    max_sleeve_budget: float = 0.45,
    max_cluster_budget: float = 0.65,
    event_budget_cap: float = 0.25,
    volatility_floor: float = 0.03,
) -> dict[str, Any]:
    """Create interpretable family sleeves and assign trailing risk budgets."""

    blend = [
        row for row in (report.get("recommended_blend") or [])
        if str(row.get("signal", "")) in library.components and float(row.get("weight", 0.0) or 0.0) > 0.0
    ]
    if not blend:
        return {"sleeves": [], "correlation_clusters": [], "correlations": [], "budget_sum": 0.0}

    blend_weight = {str(row["signal"]): float(row.get("weight", 0.0)) for row in blend}
    signal_names = list(blend_weight)
    alpha_returns = _signal_alpha_returns(prices, library, signal_names)
    trailing = alpha_returns.tail(max(20, int(lookback)))

    family_members: dict[str, list[str]] = {}
    for name in signal_names:
        family = str(specs.get(name, SignalSpec(name, "other", "")).family or "other")
        family_members.setdefault(family, []).append(name)

    sleeve_returns: dict[str, pd.Series] = {}
    sleeve_rows: list[dict[str, Any]] = []
    for family in sorted(family_members):
        members = family_members[family]
        member_total = sum(blend_weight[name] for name in members)
        within = {name: blend_weight[name] / member_total for name in members}
        sleeve_ret = sum((alpha_returns[name] * weight for name, weight in within.items()), pd.Series(0.0, index=prices.index))
        sleeve_returns[family] = sleeve_ret
        hist = sleeve_ret.tail(max(20, int(lookback))).dropna()
        ann_vol = float(hist.std(ddof=1) * np.sqrt(252)) if len(hist) >= 2 else np.nan
        quality_share = float(member_total)
        risk_score = quality_share / max(float(volatility_floor), ann_vol if np.isfinite(ann_vol) and ann_vol > 0 else float(volatility_floor))
        sleeve_rows.append({
            "name": family,
            "members": [
                {"signal": name, "weight": round(float(within[name]), 6)} for name in sorted(members)
            ],
            "quality_share": quality_share,
            "annualized_volatility": None if not np.isfinite(ann_vol) else ann_vol,
            "risk_score": risk_score,
            "is_event": family == "event",
        })

    sleeve_frame = pd.DataFrame(sleeve_returns)
    corr = sleeve_frame.tail(max(20, int(lookback))).corr(min_periods=max(10, int(lookback) // 4))
    clusters = correlation_clusters(corr, threshold=float(correlation_threshold)) if len(corr.columns) else []
    cluster_by_name: dict[str, int] = {}
    for i, members in enumerate(clusters, start=1):
        for name in members:
            cluster_by_name[name] = i

    total_score = sum(max(0.0, float(row["risk_score"])) for row in sleeve_rows)
    for row in sleeve_rows:
        raw = (max(0.0, float(row["risk_score"])) / total_score) if total_score > 0.0 else 0.0
        row["raw_risk_budget"] = raw
        row["risk_budget"] = min(raw, max(0.0, float(max_sleeve_budget)))
        row["correlation_cluster"] = cluster_by_name.get(str(row["name"]))

    # Highly correlated sleeves share a cluster budget.  The cap is applied by
    # proportional scaling and can intentionally leave some capital unallocated.
    for cluster_no, members in enumerate(clusters, start=1):
        if len(members) <= 1:
            continue
        selected = [row for row in sleeve_rows if row["name"] in members]
        total = sum(float(row["risk_budget"]) for row in selected)
        cap = max(0.0, float(max_cluster_budget))
        if total > cap > 0.0:
            scale = cap / total
            for row in selected:
                row["risk_budget"] = float(row["risk_budget"]) * scale

    event_rows = [row for row in sleeve_rows if row["is_event"]]
    event_total = sum(float(row["risk_budget"]) for row in event_rows)
    event_cap = max(0.0, float(event_budget_cap))
    if event_total > event_cap >= 0.0 and event_total > 0.0:
        scale = event_cap / event_total
        for row in event_rows:
            row["risk_budget"] = float(row["risk_budget"]) * scale

    for row in sleeve_rows:
        row["quality_share"] = round(float(row["quality_share"]), 6)
        if row["annualized_volatility"] is not None:
            row["annualized_volatility"] = round(float(row["annualized_volatility"]), 6)
        row["raw_risk_budget"] = round(float(row["raw_risk_budget"]), 6)
        row["risk_budget"] = round(float(row["risk_budget"]), 6)
        row.pop("risk_score", None)

    return {
        "sleeves": sleeve_rows,
        "correlation_clusters": [
            {"cluster": i, "sleeves": members} for i, members in enumerate(clusters, start=1)
        ],
        "correlations": _matrix_to_records(corr),
        "budget_sum": round(sum(float(row["risk_budget"]) for row in sleeve_rows), 6),
        "lookback_days": int(lookback),
        "correlation_threshold": round(float(correlation_threshold), 6),
    }


def build_sleeve_target(
    dt: pd.Timestamp,
    components: Mapping[str, pd.DataFrame],
    beta: pd.DataFrame,
    sleeve_plan: Mapping[str, Any],
    *,
    gross_target: float,
    min_names: int,
) -> tuple[pd.Series, list[dict[str, Any]]]:
    """Combine independently neutral sleeve targets using frozen risk budgets."""

    columns = beta.columns
    total = pd.Series(0.0, index=columns, dtype=float)
    used: list[dict[str, Any]] = []
    for sleeve in sleeve_plan.get("sleeves", []):
        budget = max(0.0, float(sleeve.get("risk_budget", 0.0) or 0.0))
        if budget <= 0.0:
            continue
        members = []
        score = pd.Series(0.0, index=columns, dtype=float)
        total_member_weight = 0.0
        for member in sleeve.get("members", []):
            name = str(member.get("signal", ""))
            if name not in components:
                continue
            row = components[name].reindex(index=[dt], columns=columns).iloc[0]
            finite = row.notna() & np.isfinite(row)
            if int(finite.sum()) < max(3, int(min_names)):
                continue
            weight = max(0.0, float(member.get("weight", 0.0) or 0.0))
            if weight <= 0.0:
                continue
            score = score.add(row.fillna(0.0) * weight, fill_value=0.0)
            total_member_weight += weight
            members.append({"signal": name, "weight": weight})
        if total_member_weight <= 0.0:
            continue
        score /= total_member_weight
        target = _neutral_weights(
            pd.DataFrame([score], index=[dt], columns=columns),
            beta.reindex(index=[dt], columns=columns),
            1.0,
        ).iloc[0]
        sleeve_gross = float(gross_target) * budget
        total = total.add(target * sleeve_gross, fill_value=0.0)
        used.append({
            "sleeve": str(sleeve.get("name", "other")),
            "risk_budget": round(budget, 6),
            "gross_budget": round(sleeve_gross, 6),
            "members": [
                {"signal": row["signal"], "weight": round(float(row["weight"]) / total_member_weight, 6)}
                for row in members
            ],
        })
    return total.fillna(0.0), used


def _group_exposures(weights: pd.Series, groups: Mapping[str, Any] | pd.Series | None) -> dict[str, float]:
    if groups is None:
        return {}
    source = groups if isinstance(groups, pd.Series) else pd.Series(dict(groups), dtype="object")
    buckets: dict[str, float] = {}
    for symbol, weight in weights.items():
        group = source.get(symbol)
        if group is None or (isinstance(group, float) and np.isnan(group)):
            continue
        text = str(group).strip()
        if text:
            buckets[text] = buckets.get(text, 0.0) + float(weight)
    return buckets


def project_portfolio_constraints(
    desired: pd.Series,
    beta: pd.Series,
    *,
    gross_limit: float,
    max_name_weight: float,
    shortable: pd.Series | None = None,
    crowding_score: pd.Series | None = None,
    crowded_short_threshold: float = 0.65,
    max_crowded_short_gross: float = 0.15,
    groups: Mapping[str, Any] | pd.Series | None = None,
    group_net_cap: float | None = None,
) -> tuple[pd.Series, dict[str, Any]]:
    """Project a neutral desired book into security-level risk constraints.

    SLSQP minimises squared distance to the sleeve-combined target while
    preserving dollar neutrality, beta neutrality (when identifiable), gross
    exposure, name limits, short-availability bounds, crowded-short gross and
    optional group-net limits. Failure is fail-closed: the function returns a
    flat book instead of silently violating a constraint.
    """

    desired = desired.astype(float).fillna(0.0)
    beta = beta.reindex(desired.index).astype(float)
    valid = beta.notna() & np.isfinite(beta)
    names = list(desired.index[valid])
    out = pd.Series(0.0, index=desired.index, dtype=float)
    if len(names) < 3 or float(gross_limit) <= 0.0:
        return out, {"status": "flat", "reason": "insufficient_valid_names"}

    d = desired.loc[names].to_numpy(dtype=float)
    b = beta.loc[names].to_numpy(dtype=float)
    name_cap = max(1e-6, float(max_name_weight))
    gross_cap = max(0.0, min(float(gross_limit), float(np.abs(d).sum()) if np.abs(d).sum() > 0 else float(gross_limit)))

    shortable_series = (
        shortable.reindex(names).fillna(True).astype(bool)
        if shortable is not None
        else pd.Series(True, index=names, dtype=bool)
    )
    crowd = (
        crowding_score.reindex(names).fillna(0.0).astype(float).clip(0.0, 1.0)
        if crowding_score is not None
        else pd.Series(0.0, index=names, dtype=float)
    )
    crowded_mask = crowd.to_numpy(dtype=float) >= float(crowded_short_threshold)
    crowded_cap = max(0.0, float(max_crowded_short_gross))

    bounds = [
        (0.0 if not bool(shortable_series.loc[name]) else -name_cap, name_cap)
        for name in names
    ]

    # Most daily decisions already satisfy the limits because each sleeve was
    # constructed neutral and the book is diversified. Avoid an optimizer call
    # when no constraint is binding; this keeps broad-universe walk-forwards
    # fast while preserving the exact same fail-closed path for constrained days.
    candidate = pd.Series(0.0, index=desired.index, dtype=float)
    candidate.loc[names] = d
    candidate_group = _group_exposures(candidate, groups)
    candidate_crowded = float(np.maximum(-d[crowded_mask], 0.0).sum()) if crowded_mask.any() else 0.0
    candidate_unshortable = any((d[i] < -1e-10) and (not bool(shortable_series.iloc[i])) for i in range(len(names)))
    group_ok = True
    if groups is not None and group_net_cap is not None:
        group_ok = all(abs(value) <= float(group_net_cap) + 1e-10 for value in candidate_group.values())
    neutral_ok = abs(float(candidate.sum())) <= 1e-9
    beta_ok = (
        abs(float((candidate * beta.reindex(candidate.index).fillna(0.0)).sum())) <= 1e-8
        if float(np.nanstd(b)) >= 1e-10 else neutral_ok
    )
    if (
        neutral_ok
        and beta_ok
        and float(candidate.abs().sum()) <= gross_cap + 1e-10
        and float(candidate.abs().max()) <= name_cap + 1e-10
        and not candidate_unshortable
        and candidate_crowded <= crowded_cap + 1e-10
        and group_ok
    ):
        return candidate, {
            "status": "ready",
            "gross_exposure": round(float(candidate.abs().sum()), 8),
            "net_exposure": round(float(candidate.sum()), 10),
            "beta_exposure": round(float((candidate * beta.reindex(candidate.index).fillna(0.0)).sum()), 10),
            "max_abs_name_weight": round(float(candidate.abs().max()), 8),
            "crowded_short_gross": round(candidate_crowded, 8),
            "unshortable_names": [str(name) for name in names if not bool(shortable_series.loc[name])],
            "group_net_exposures": {key: round(float(value), 8) for key, value in sorted(candidate_group.items())},
            "optimizer_message": "constraints_not_binding",
        }

    x0 = np.clip(d, [lo for lo, _ in bounds], [hi for _, hi in bounds])

    constraints: list[dict[str, Any]] = [
        {"type": "eq", "fun": lambda x: float(np.sum(x))},
        {"type": "ineq", "fun": lambda x, cap=gross_cap: float(cap - np.sum(np.abs(x)))},
    ]
    if float(np.nanstd(b)) >= 1e-10:
        constraints.append({"type": "eq", "fun": lambda x, bb=b: float(np.dot(x, bb))})

    if crowded_mask.any():
        constraints.append({
            "type": "ineq",
            "fun": lambda x, mask=crowded_mask, cap=crowded_cap: float(cap - np.sum(np.maximum(-x[mask], 0.0))),
        })

    group_members: dict[str, list[int]] = {}
    if groups is not None and group_net_cap is not None:
        source = groups if isinstance(groups, pd.Series) else pd.Series(dict(groups), dtype="object")
        for i, name in enumerate(names):
            value = source.get(name)
            if value is None or (isinstance(value, float) and np.isnan(value)):
                continue
            text = str(value).strip()
            if text:
                group_members.setdefault(text, []).append(i)
        cap = max(0.0, float(group_net_cap))
        for members in group_members.values():
            idx = np.asarray(members, dtype=int)
            constraints.append({
                "type": "ineq",
                "fun": lambda x, ii=idx, cc=cap: float(cc - abs(np.sum(x[ii]))),
            })

    scale = max(1e-8, float(np.dot(d, d)))
    result = minimize(
        lambda x: float(np.dot(x - d, x - d) / scale),
        x0,
        method="SLSQP",
        bounds=bounds,
        constraints=constraints,
        options={"maxiter": 500, "ftol": 1e-12, "disp": False},
    )
    if not bool(result.success) or not np.all(np.isfinite(result.x)):
        return out, {
            "status": "flat",
            "reason": "constraint_projection_failed",
            "optimizer_message": str(result.message),
        }

    out.loc[names] = result.x
    group_exp = _group_exposures(out, groups)
    crowded_short = float(np.maximum(-out.loc[names].to_numpy(dtype=float)[crowded_mask], 0.0).sum()) if crowded_mask.any() else 0.0
    unshortable = [str(name) for name in names if not bool(shortable_series.loc[name])]
    return out, {
        "status": "ready",
        "gross_exposure": round(float(out.abs().sum()), 8),
        "net_exposure": round(float(out.sum()), 10),
        "beta_exposure": round(float((out * beta.reindex(out.index).fillna(0.0)).sum()), 10),
        "max_abs_name_weight": round(float(out.abs().max()), 8),
        "crowded_short_gross": round(crowded_short, 8),
        "unshortable_names": unshortable,
        "group_net_exposures": {key: round(float(value), 8) for key, value in sorted(group_exp.items())},
        "optimizer_message": str(result.message),
    }


def daily_borrow_costs(
    held_weights: pd.DataFrame,
    borrow: BorrowPanels | None,
    *,
    default_annual_borrow_bps: float = 30.0,
) -> pd.Series:
    """Daily portfolio return drag from stock-loan costs on held shorts."""

    if held_weights.empty:
        return pd.Series(dtype=float, index=held_weights.index)
    if borrow is None:
        annual = _panel_like(held_weights.index, held_weights.columns, max(0.0, float(default_annual_borrow_bps)))
    else:
        annual = borrow.annual_borrow_bps.reindex(index=held_weights.index, columns=held_weights.columns)
        annual = annual.fillna(max(0.0, float(default_annual_borrow_bps))).clip(lower=0.0)
    shorts = (-held_weights.clip(upper=0.0)).astype(float)
    return (shorts * annual / 10_000.0 / 252.0).sum(axis=1).fillna(0.0)
