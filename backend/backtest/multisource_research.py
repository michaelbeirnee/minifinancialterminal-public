"""Point-in-time multi-source signal research.

The price-only signal library in :mod:`signal_research` is intentionally cheap.
This module widens the research surface without relaxing the core rule: a
feature may only be scored on a date if the platform could actually have known
it on that date.

Historical today
----------------
* OHLCV/volume features come from dated market bars.
* Fundamental features use the existing filing-lagged ``multiples_history``
  implementation, so a quarter appears only after the configured publication
  lag.
* Earnings surprises and analyst actions are stamped onto the *next* trading
  day.  This is conservative around before/after-market releases.
* Peer relationships are rebuilt from trailing correlations only.

Archive first
-------------
Yahoo exposes analyst-estimate tables and option chains primarily as current
snapshots.  Backfilling today's snapshot into old dates would be look-ahead.
``archive_current_snapshots`` stores those features with the date they were
actually captured.  The research loader then uses only those archived rows and
expires stale snapshots rather than carrying them forever.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from .signal_research import (
    SIGNAL_SPECS,
    SignalLibraryOutput,
    SignalSpec,
    build_signal_library,
)
from .stat_arb import _rank_score, _rebalance_schedule, _rolling_beta
from ..models import ResearchFeatureSnapshot
from ..providers import yahoo


MULTISOURCE_SIGNAL_SPECS: tuple[SignalSpec, ...] = (
    SignalSpec(
        "volume_confirmed_momentum", "volume",
        "Recent relative strength reinforced by unusually heavy trading volume.",
        source="volume",
    ),
    SignalSpec(
        "volume_shock_reversal", "volume",
        "Fade one-day residual shocks when volume is unusually high.",
        source="volume",
    ),
    SignalSpec(
        "liquidity_improvement", "volume",
        "Favor names whose short-run Amihud illiquidity is improving versus its baseline.",
        source="volume",
    ),
    SignalSpec(
        "fcf_yield_value", "fundamental",
        "Free-cash-flow yield using only filing-lagged trailing fundamentals.",
        source="fundamentals",
    ),
    SignalSpec(
        "earnings_yield_value", "fundamental",
        "Inverse trailing P/E using only filing-lagged reported earnings.",
        source="fundamentals",
    ),
    SignalSpec(
        "sales_yield_value", "fundamental",
        "Inverse trailing price-to-sales using only filing-lagged revenue.",
        source="fundamentals",
    ),
    SignalSpec(
        "ebitda_yield_value", "fundamental",
        "Inverse enterprise-value/EBITDA using filing-lagged trailing EBITDA.",
        source="fundamentals",
    ),
    SignalSpec(
        "post_earnings_surprise", "event",
        "Decay a reported EPS surprise forward from the next trading day.",
        source="events",
    ),
    SignalSpec(
        "analyst_action_momentum", "event",
        "Net recent upgrades versus downgrades, known from the next trading day.",
        source="events",
    ),
    SignalSpec(
        "eps_revision_breadth", "estimates",
        "Breadth of upward versus downward EPS revisions captured point in time.",
        source="estimates_archive",
    ),
    SignalSpec(
        "eps_estimate_acceleration", "estimates",
        "Change in the current EPS estimate versus its 30-day-ago snapshot.",
        source="estimates_archive",
    ),
    SignalSpec(
        "target_price_upside", "estimates",
        "Consensus target-price upside captured point in time.",
        source="estimates_archive",
    ),
    SignalSpec(
        "low_estimate_dispersion", "estimates",
        "Favor tighter analyst estimate dispersion; wide disagreement scores lower.",
        source="estimates_archive",
    ),
    SignalSpec(
        "put_call_oi_contrarian", "options",
        "Contrarian score from the put/call open-interest imbalance.",
        source="options_archive",
    ),
    SignalSpec(
        "iv_richness", "options",
        "Favor lower ATM implied volatility relative to trailing realized volatility.",
        source="options_archive",
    ),
    SignalSpec(
        "downside_skew_contrarian", "options",
        "Contrarian score from downside put IV richness versus upside calls.",
        source="options_archive",
    ),
    SignalSpec(
        "iv_term_structure", "options",
        "Slope of longer-dated versus near-dated ATM implied volatility.",
        source="options_archive",
    ),
    SignalSpec(
        "peer_spread_reversal", "relationship",
        "Fade a stock-specific move relative to dynamically selected correlated peers.",
        source="cross_sectional_relationships",
    ),
    SignalSpec(
        "peer_catchup", "relationship",
        "Favor names lagging a recent move in their dynamically selected peers.",
        source="cross_sectional_relationships",
    ),
)

MULTISOURCE_SPEC_BY_NAME = {
    spec.name: spec for spec in (*SIGNAL_SPECS, *MULTISOURCE_SIGNAL_SPECS)
}


def current_symbol_classifications(
    symbols: Iterable[str],
    level: str = "sector",
) -> tuple[dict[str, str], dict[str, Any]]:
    """Best-effort current sector/industry labels for research diagnostics.

    Yahoo exposes the present company classification rather than a historical
    GICS timeline.  The research engine therefore treats this mapping as a
    *conservative filter only*: raw OOS evidence must already pass before the
    within-group result can matter.  A current classification can demote a
    signal that is really sector beta, but it cannot promote a failed signal.
    """

    level = str(level).strip().lower()
    if level not in {"sector", "industry"}:
        raise ValueError("classification level must be sector or industry")
    names = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
    groups: dict[str, str] = {}
    warnings: list[str] = []

    def fetch(symbol: str) -> tuple[str, str | None, str | None]:
        try:
            info = yahoo.info(symbol)
            value = str(info.get(level) or "").strip() or None
            return symbol, value, None
        except Exception as exc:  # noqa: BLE001 - one missing profile must not kill research
            return symbol, None, str(exc)

    workers = min(8, max(1, len(names)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, symbol) for symbol in names]
        for future in as_completed(futures):
            symbol, value, error = future.result()
            if value:
                groups[symbol] = value
            elif error:
                warnings.append(f"{symbol}: {error}")

    coverage = (len(groups) / len(names)) if names else 0.0
    distinct = len(set(groups.values()))
    return groups, {
        "requested_level": level,
        "mode": "current_snapshot_conservative_filter",
        "symbols": len(names),
        "classified_symbols": len(groups),
        "coverage": round(float(coverage), 6),
        "distinct_groups": distinct,
        "warnings": warnings[:12],
        "point_in_time": False,
        "note": (
            "Yahoo provides current sector/industry labels, not a historical classification timeline. "
            "The engine therefore requires raw OOS evidence to pass as well; this mapping can only filter, never promote."
        ),
    }


@dataclass(frozen=True)
class FeaturePanels:
    """Raw point-in-time panels aligned to the price calendar."""

    panels: dict[str, pd.DataFrame] = field(default_factory=dict)
    source_status: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class MultisourceLibraryOutput:
    library: SignalLibraryOutput
    specs: dict[str, SignalSpec]
    source_status: dict[str, dict[str, Any]]


def multisource_signal_catalog() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    archive_sources = {"estimates_archive", "options_archive"}
    for spec in (*SIGNAL_SPECS, *MULTISOURCE_SIGNAL_SPECS):
        rows.append(
            {
                "name": spec.name,
                "family": spec.family,
                "description": spec.description,
                "source": spec.source,
                "archive_required": spec.source in archive_sources,
            }
        )
    return rows


def _wide(index: pd.Index, columns: Iterable[str], fill: float | None = None) -> pd.DataFrame:
    frame = pd.DataFrame(index=index, columns=list(columns), dtype="float64")
    return frame if fill is None else frame.fillna(fill)


def _safe_numeric(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _normalized_columns(frame: pd.DataFrame) -> dict[str, Any]:
    def key(value: Any) -> str:
        return "".join(ch for ch in str(value).lower() if ch.isalnum())
    return {key(c): c for c in frame.columns}


def _find_col(frame: pd.DataFrame, *candidates: str) -> Any | None:
    cols = _normalized_columns(frame)
    for candidate in candidates:
        key = "".join(ch for ch in candidate.lower() if ch.isalnum())
        if key in cols:
            return cols[key]
    return None


def _row_by_period(frame: pd.DataFrame, preferred: tuple[str, ...] = ("0q", "+1q", "0y", "+1y")) -> pd.Series | None:
    if frame is None or frame.empty:
        return None
    index_map = {str(i).strip().lower(): i for i in frame.index}
    for label in preferred:
        if label in index_map:
            row = frame.loc[index_map[label]]
            return row.iloc[0] if isinstance(row, pd.DataFrame) else row
    return frame.iloc[0]


def _estimate_snapshot(symbol: str) -> dict[str, float]:
    """Current Yahoo estimate features.  These are *only* valid from capture time."""

    features: dict[str, float] = {}
    try:
        revisions = yahoo.estimates(symbol, "eps_revisions")
        row = _row_by_period(revisions)
        if row is not None:
            tmp = pd.DataFrame([row])
            up_col = _find_col(tmp, "upLast30days", "up_last_30_days")
            down_col = _find_col(tmp, "downLast30days", "down_last_30_days")
            up = _safe_numeric(row.get(up_col)) if up_col is not None else None
            down = _safe_numeric(row.get(down_col)) if down_col is not None else None
            if up is not None and down is not None:
                features["eps_revision_breadth"] = (up - down) / (up + down + 1.0)
    except Exception:  # noqa: BLE001 - snapshot can be partially populated
        pass

    try:
        trend = yahoo.estimates(symbol, "eps_trend")
        row = _row_by_period(trend)
        if row is not None:
            tmp = pd.DataFrame([row])
            current_col = _find_col(tmp, "current")
            ago_col = _find_col(tmp, "30daysAgo", "30_days_ago")
            current = _safe_numeric(row.get(current_col)) if current_col is not None else None
            ago = _safe_numeric(row.get(ago_col)) if ago_col is not None else None
            if current is not None and ago not in (None, 0.0):
                features["eps_estimate_change_30d"] = (current - ago) / max(abs(ago), 1e-9)
    except Exception:  # noqa: BLE001
        pass

    try:
        earnings = yahoo.estimates(symbol, "earnings")
        row = _row_by_period(earnings)
        if row is not None:
            tmp = pd.DataFrame([row])
            avg_col = _find_col(tmp, "avg", "avgEstimate")
            low_col = _find_col(tmp, "low", "lowEstimate")
            high_col = _find_col(tmp, "high", "highEstimate")
            avg = _safe_numeric(row.get(avg_col)) if avg_col is not None else None
            low = _safe_numeric(row.get(low_col)) if low_col is not None else None
            high = _safe_numeric(row.get(high_col)) if high_col is not None else None
            if avg not in (None, 0.0) and low is not None and high is not None:
                features["estimate_dispersion"] = (high - low) / max(abs(avg), 1e-9)
    except Exception:  # noqa: BLE001
        pass

    try:
        targets = yahoo.price_targets(symbol)
        quote = yahoo.quote(symbol)
        price = _safe_numeric(quote.get("last_price"))
        target = None
        for key in ("mean", "meanPrice", "current", "median", "medianPrice"):
            if key in targets:
                target = _safe_numeric(targets.get(key))
                if target is not None:
                    break
        if price not in (None, 0.0) and target is not None:
            features["target_price_upside"] = target / price - 1.0
    except Exception:  # noqa: BLE001
        pass

    return features


def _atm_iv(chain: pd.DataFrame, spot: float) -> float | None:
    if chain is None or chain.empty or spot <= 0:
        return None
    strike_col = _find_col(chain, "strike")
    iv_col = _find_col(chain, "implied_volatility", "impliedVolatility")
    if strike_col is None or iv_col is None:
        return None
    strike = pd.to_numeric(chain[strike_col], errors="coerce")
    iv = pd.to_numeric(chain[iv_col], errors="coerce")
    mask = strike.div(spot).between(0.95, 1.05) & iv.between(0.01, 5.0)
    values = iv[mask].dropna()
    return None if values.empty else float(values.median())


def _option_snapshot(symbol: str) -> dict[str, float]:
    """Current option-surface features, valid only from the capture date."""

    quote = yahoo.quote(symbol)
    spot = _safe_numeric(quote.get("last_price"))
    if spot is None or spot <= 0:
        return {}
    expiries = yahoo.option_expirations(symbol)
    if not expiries:
        return {}

    today = pd.Timestamp.today().normalize()
    dated: list[tuple[int, str]] = []
    for expiry in expiries:
        try:
            days = int((pd.Timestamp(expiry) - today).days)
        except Exception:  # noqa: BLE001
            continue
        if days > 3:
            dated.append((days, expiry))
    if not dated:
        return {}
    dated.sort()
    near_days, near_expiry = min(dated, key=lambda x: abs(x[0] - 30))
    far_candidates = [item for item in dated if item[0] >= near_days + 20]
    far = min(far_candidates, key=lambda x: abs(x[0] - 75)) if far_candidates else None

    near = yahoo.option_chain(symbol, near_expiry)
    iv_col = _find_col(near, "implied_volatility", "impliedVolatility")
    type_col = _find_col(near, "option_type", "type")
    strike_col = _find_col(near, "strike")
    oi_col = _find_col(near, "open_interest", "openInterest")
    if iv_col is None or type_col is None or strike_col is None:
        return {}

    iv = pd.to_numeric(near[iv_col], errors="coerce")
    strike = pd.to_numeric(near[strike_col], errors="coerce")
    side = near[type_col].astype(str).str.lower()
    valid_iv = iv.between(0.01, 5.0)
    moneyness = strike / spot
    atm = _atm_iv(near, spot)

    features: dict[str, float] = {}
    if oi_col is not None:
        oi = pd.to_numeric(near[oi_col], errors="coerce").fillna(0.0).clip(lower=0.0)
        put_oi = float(oi[side.str.startswith("p")].sum())
        call_oi = float(oi[side.str.startswith("c")].sum())
        features["put_call_oi_log"] = float(np.log((put_oi + 1.0) / (call_oi + 1.0)))

    put_mask = side.str.startswith("p") & moneyness.between(0.85, 0.98) & valid_iv
    call_mask = side.str.startswith("c") & moneyness.between(1.02, 1.15) & valid_iv
    put_iv = iv[put_mask].dropna()
    call_iv = iv[call_mask].dropna()
    if not put_iv.empty and not call_iv.empty:
        features["downside_skew"] = float(put_iv.median() - call_iv.median())

    if atm is not None:
        try:
            start = (today - pd.Timedelta(days=60)).date().isoformat()
            hist = yahoo.history(symbol, start=start, end=(today + pd.Timedelta(days=1)).date().isoformat())
            realized = pd.to_numeric(hist["close"], errors="coerce").pct_change().tail(20).std() * np.sqrt(252)
            if np.isfinite(realized):
                features["iv_realized_gap"] = float(atm - realized)
        except Exception:  # noqa: BLE001
            pass

    if far is not None and atm is not None:
        try:
            far_chain = yahoo.option_chain(symbol, far[1])
            far_atm = _atm_iv(far_chain, spot)
            if far_atm is not None:
                features["iv_term_slope"] = float(far_atm - atm)
        except Exception:  # noqa: BLE001
            pass
    return features


def _crowding_snapshot(symbol: str) -> dict[str, float]:
    """Current short-interest/crowding fields, valid only from capture date.

    Yahoo exposes reported short interest and days-to-cover, but not an actual
    stock-loan fee or locate availability.  Those fields therefore support
    portfolio crowding/borrow proxies only; they are never presented as prime
    broker borrow quotes.
    """

    info = yahoo.info(symbol)
    features: dict[str, float] = {}
    short_float = _safe_numeric(
        info.get("sharesShortPercentOfFloat", info.get("shortPercentOfFloat"))
    )
    shares_short = _safe_numeric(info.get("sharesShort"))
    float_shares = _safe_numeric(info.get("floatShares"))
    if short_float is None and shares_short is not None and float_shares not in (None, 0.0):
        short_float = shares_short / float_shares
    if short_float is not None:
        if abs(short_float) > 1.5:
            short_float /= 100.0
        features["short_percent_float"] = float(np.clip(short_float, 0.0, 1.0))

    short_ratio = _safe_numeric(info.get("shortRatio"))
    if short_ratio is not None and short_ratio >= 0.0:
        features["short_ratio"] = float(short_ratio)

    prior = _safe_numeric(info.get("sharesShortPriorMonth"))
    if shares_short is not None and prior not in (None, 0.0):
        features["short_interest_change"] = float(shares_short / prior - 1.0)
    return features


def archive_current_snapshots(
    symbols: Iterable[str],
    db: Session,
    include_estimates: bool = True,
    include_options: bool = True,
    include_crowding: bool = True,
) -> dict[str, Any]:
    """Capture today's non-backfillable features for future OOS/risk research."""

    syms = list(dict.fromkeys(str(s).upper() for s in symbols))
    as_of = date.today().isoformat()
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    def capture(sym: str) -> tuple[str, dict[str, dict[str, float]], list[str]]:
        payloads: dict[str, dict[str, float]] = {}
        errs: list[str] = []
        if include_estimates:
            try:
                values = _estimate_snapshot(sym)
                if values:
                    payloads["estimates"] = values
                else:
                    errs.append(f"{sym}: no estimate features")
            except Exception as exc:  # noqa: BLE001
                errs.append(f"{sym}: estimates: {exc}")
        if include_options:
            try:
                values = _option_snapshot(sym)
                if values:
                    payloads["options"] = values
                else:
                    errs.append(f"{sym}: no option features")
            except Exception as exc:  # noqa: BLE001
                errs.append(f"{sym}: options: {exc}")
        if include_crowding:
            try:
                values = _crowding_snapshot(sym)
                if values:
                    payloads["crowding"] = values
                else:
                    errs.append(f"{sym}: no short-interest/crowding features")
            except Exception as exc:  # noqa: BLE001
                errs.append(f"{sym}: crowding: {exc}")
        return sym, payloads, errs

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(syms)))) as pool:
        futures = [pool.submit(capture, sym) for sym in syms]
        captured = [future.result() for future in as_completed(futures)]

    for sym, payloads, errs in captured:
        warnings.extend(errs)
        for family, features in payloads.items():
            existing = (
                db.query(ResearchFeatureSnapshot)
                .filter(
                    ResearchFeatureSnapshot.as_of_date == as_of,
                    ResearchFeatureSnapshot.symbol == sym,
                    ResearchFeatureSnapshot.family == family,
                )
                .one_or_none()
            )
            if existing is None:
                existing = ResearchFeatureSnapshot(
                    as_of_date=as_of,
                    symbol=sym,
                    family=family,
                    provider="yahoo",
                    features=features,
                )
                db.add(existing)
            else:
                existing.features = features
                existing.provider = "yahoo"
            rows.append({"symbol": sym, "family": family, "features": features})
    db.commit()
    return {"as_of": as_of, "captured": rows, "warnings": warnings}


def _load_archived_panels(
    prices: pd.DataFrame,
    db: Session | None,
    params: Mapping[str, Any],
) -> tuple[dict[str, pd.DataFrame], dict[str, dict[str, Any]]]:
    panels: dict[str, pd.DataFrame] = {}
    status: dict[str, dict[str, Any]] = {}
    if db is None:
        return panels, {
            "estimates_archive": {"available": False, "reason": "no database session"},
            "options_archive": {"available": False, "reason": "no database session"},
            "crowding_archive": {"available": False, "reason": "no database session"},
            "borrow_archive": {"available": False, "reason": "no database session"},
        }

    start = prices.index.min().date().isoformat()
    end = prices.index.max().date().isoformat()
    rows = (
        db.query(ResearchFeatureSnapshot)
        .filter(
            ResearchFeatureSnapshot.symbol.in_(list(prices.columns)),
            ResearchFeatureSnapshot.as_of_date >= start,
            ResearchFeatureSnapshot.as_of_date <= end,
        )
        .order_by(ResearchFeatureSnapshot.as_of_date.asc())
        .all()
    )
    by_family: dict[str, list[ResearchFeatureSnapshot]] = {"estimates": [], "options": [], "crowding": [], "borrow": []}
    for row in rows:
        if row.family in by_family:
            by_family[row.family].append(row)

    stale_limits = {
        "estimates": max(1, int(params.get("estimate_archive_ffill_days", 30))),
        "options": max(1, int(params.get("options_archive_ffill_days", 5))),
        "crowding": max(1, int(params.get("crowding_archive_ffill_days", 15))),
        "borrow": max(1, int(params.get("borrow_archive_ffill_days", 5))),
    }
    for family, items in by_family.items():
        feature_names = sorted({key for item in items for key in (item.features or {})})
        for feature in feature_names:
            frame = _wide(prices.index, prices.columns)
            for sym in prices.columns:
                sym_items = [item for item in items if item.symbol == sym and feature in (item.features or {})]
                if not sym_items:
                    continue
                values = pd.Series(
                    {
                        pd.Timestamp(item.as_of_date): _safe_numeric(item.features.get(feature))
                        for item in sym_items
                    },
                    dtype="float64",
                ).dropna()
                if values.empty:
                    continue
                values = values[~values.index.duplicated(keep="last")].sort_index()
                # A snapshot captured on a non-trading day (weekend/holiday
                # archive run) has no matching bar; surface it on the first
                # session at/after capture instead of silently dropping it.
                positions = prices.index.searchsorted(values.index, side="left")
                tradable = positions < len(prices.index)
                if not tradable.any():
                    continue
                mapped = pd.Series(
                    values.to_numpy()[tradable], index=prices.index[positions[tradable]]
                )
                mapped = mapped[~mapped.index.duplicated(keep="last")]
                frame[sym] = mapped.reindex(prices.index).ffill(limit=stale_limits[family])
            panels[feature] = frame
        status[f"{family}_archive"] = {
            "available": bool(items),
            "rows": len(items),
            "features": feature_names,
            "forward_fill_business_days": stale_limits[family],
            "reason": None if items else "no point-in-time snapshots captured in this date range",
        }
    return panels, status


def _ohlcv_panels(prices: pd.DataFrame) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    fields = {name: _wide(prices.index, prices.columns) for name in ("open", "high", "low", "volume")}
    errors: list[str] = []

    def one(sym: str) -> tuple[str, pd.DataFrame | None, str | None]:
        try:
            start = prices.index.min().date().isoformat()
            end = (prices.index.max() + pd.Timedelta(days=1)).date().isoformat()
            return sym, yahoo.history(sym, start=start, end=end), None
        except Exception as exc:  # noqa: BLE001
            return sym, None, str(exc)

    with ThreadPoolExecutor(max_workers=min(8, len(prices.columns))) as pool:
        results = [f.result() for f in as_completed([pool.submit(one, s) for s in prices.columns])]
    for sym, frame, err in results:
        if frame is None or frame.empty:
            errors.append(f"{sym}: {err or 'no OHLCV'}")
            continue
        idx = pd.DatetimeIndex(pd.to_datetime(frame.index)).tz_localize(None).normalize()
        frame = frame.copy()
        frame.index = idx
        for field in fields:
            if field in frame:
                fields[field][sym] = pd.to_numeric(frame[field], errors="coerce").reindex(prices.index)
    coverage = float(fields["volume"].notna().sum().sum() / max(1, prices.size))
    return fields, {"available": coverage > 0.0, "coverage": round(coverage, 4), "warnings": errors}


def _fundamental_panels(
    prices: pd.DataFrame,
    lag_days: int,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    fields = {name: _wide(prices.index, prices.columns) for name in (
        "fcf_yield", "pe_trailing", "ps_trailing", "ev_ebitda"
    )}
    errors: list[str] = []

    # Local import avoids registering the full extension tree on cheap price-only tests.
    from ..extensions.equity_fundamental import multiples_history

    def one(sym: str) -> tuple[str, pd.DataFrame | None, str | None]:
        try:
            result = multiples_history(
                sym,
                start_date=prices.index.min().date().isoformat(),
                end_date=(prices.index.max() + pd.Timedelta(days=1)).date().isoformat(),
                lag_days=lag_days,
                provider="sec",
            )
            return sym, result.data.copy(), None
        except Exception as exc:  # noqa: BLE001 - ETFs/foreign listings may not file
            return sym, None, str(exc)

    with ThreadPoolExecutor(max_workers=min(6, len(prices.columns))) as pool:
        results = [f.result() for f in as_completed([pool.submit(one, s) for s in prices.columns])]
    for sym, frame, err in results:
        if frame is None or frame.empty:
            errors.append(f"{sym}: {err or 'no filing history'}")
            continue
        idx = pd.DatetimeIndex(pd.to_datetime(frame.index)).tz_localize(None).normalize()
        frame = frame.copy()
        frame.index = idx
        for field in fields:
            if field in frame:
                fields[field][sym] = pd.to_numeric(frame[field], errors="coerce").reindex(prices.index)
    nonempty = sum(int(frame.notna().any().any()) for frame in fields.values())
    return fields, {
        "available": nonempty > 0,
        "lag_days": lag_days,
        "features": [name for name, frame in fields.items() if frame.notna().any().any()],
        "warnings": errors,
    }


def _event_date_index(frame: pd.DataFrame) -> pd.DatetimeIndex:
    if isinstance(frame.index, pd.DatetimeIndex):
        idx = pd.to_datetime(frame.index, errors="coerce", utc=True).tz_convert(None)
        if idx.notna().any():
            return pd.DatetimeIndex(idx)
    col = _find_col(frame, "earnings_date", "earnings date", "grade_date", "gradedate", "date")
    if col is None:
        return pd.DatetimeIndex([pd.NaT] * len(frame))
    return pd.DatetimeIndex(pd.to_datetime(frame[col], errors="coerce", utc=True).dt.tz_convert(None))


def _next_session(index: pd.DatetimeIndex, when: pd.Timestamp) -> pd.Timestamp | None:
    if pd.isna(when):
        return None
    pos = int(index.searchsorted(pd.Timestamp(when).normalize(), side="right"))
    return None if pos >= len(index) else index[pos]


def _decay_events(raw: pd.DataFrame, half_life: float, max_days: int) -> pd.DataFrame:
    out = _wide(raw.index, raw.columns, fill=0.0)
    decay = float(np.exp(np.log(0.5) / max(half_life, 1e-6)))
    for col in raw.columns:
        state = 0.0
        age = max_days + 1
        values: list[float] = []
        for value in raw[col].fillna(0.0).to_numpy(dtype="float64"):
            if abs(value) > 1e-15:
                state = float(value)
                age = 0
            elif age <= max_days:
                state *= decay
                age += 1
            else:
                state = 0.0
            values.append(state if age <= max_days else 0.0)
        out[col] = values
    return out


def _event_panels(prices: pd.DataFrame, params: Mapping[str, Any]) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    earnings_raw = _wide(prices.index, prices.columns, fill=0.0)
    analyst_raw = _wide(prices.index, prices.columns, fill=0.0)
    errors: list[str] = []
    idx = pd.DatetimeIndex(prices.index)

    def one(sym: str) -> tuple[str, pd.DataFrame | None, pd.DataFrame | None, list[str]]:
        local_errors: list[str] = []
        earnings = upgrades = None
        try:
            earnings = yahoo.earnings_dates(sym, limit=max(24, int(params.get("earnings_event_limit", 80))))
        except Exception as exc:  # noqa: BLE001
            local_errors.append(f"earnings: {exc}")
        try:
            upgrades = yahoo.upgrades_downgrades(sym)
        except Exception as exc:  # noqa: BLE001
            local_errors.append(f"analyst: {exc}")
        return sym, earnings, upgrades, local_errors

    with ThreadPoolExecutor(max_workers=min(8, len(prices.columns))) as pool:
        results = [f.result() for f in as_completed([pool.submit(one, s) for s in prices.columns])]

    for sym, earnings, upgrades, local_errors in results:
        errors.extend(f"{sym}: {e}" for e in local_errors)
        if earnings is not None and not earnings.empty:
            dates = _event_date_index(earnings)
            surprise_col = _find_col(earnings, "surprise(%)", "surprise_percent", "surprise")
            actual_col = _find_col(earnings, "reported eps", "reported_eps", "actual")
            estimate_col = _find_col(earnings, "eps estimate", "eps_estimate", "estimate")
            for row_no, (_, row) in enumerate(earnings.iterrows()):
                surprise = _safe_numeric(row.get(surprise_col)) if surprise_col is not None else None
                if surprise is not None and abs(surprise) > 2.0:
                    surprise /= 100.0
                if surprise is None and actual_col is not None and estimate_col is not None:
                    actual = _safe_numeric(row.get(actual_col))
                    estimate = _safe_numeric(row.get(estimate_col))
                    if actual is not None and estimate not in (None, 0.0):
                        surprise = (actual - estimate) / max(abs(estimate), 1e-9)
                known = _next_session(idx, dates[row_no]) if row_no < len(dates) else None
                if known is not None and surprise is not None:
                    earnings_raw.loc[known, sym] += float(np.clip(surprise, -2.0, 2.0))

        if upgrades is not None and not upgrades.empty:
            dates = _event_date_index(upgrades)
            action_col = _find_col(upgrades, "action")
            for row_no, (_, row) in enumerate(upgrades.iterrows()):
                action = str(row.get(action_col, "")).strip().lower() if action_col is not None else ""
                score = 1.0 if "up" in action else -1.0 if "down" in action else 0.0
                known = _next_session(idx, dates[row_no]) if row_no < len(dates) else None
                if known is not None and score:
                    analyst_raw.loc[known, sym] += score

    panels = {
        "earnings_surprise_decay": _decay_events(
            earnings_raw,
            half_life=float(params.get("earnings_surprise_half_life", 5.0)),
            max_days=int(params.get("earnings_surprise_max_days", 21)),
        ),
        "analyst_action_decay": _decay_events(
            analyst_raw,
            half_life=float(params.get("analyst_action_half_life", 10.0)),
            max_days=int(params.get("analyst_action_max_days", 42)),
        ),
    }
    available = any(frame.abs().sum().sum() > 0 for frame in panels.values())
    return panels, {"available": available, "warnings": errors}


def build_feature_panels(
    prices: pd.DataFrame,
    params: Mapping[str, Any] | None = None,
    db: Session | None = None,
) -> FeaturePanels:
    """Fetch/align every point-in-time data family used by multi-source research."""

    params = params or {}
    prices = prices.sort_index().astype(float)
    panels: dict[str, pd.DataFrame] = {}
    status: dict[str, dict[str, Any]] = {}

    if bool(params.get("include_volume", True)):
        block, meta = _ohlcv_panels(prices)
        panels.update(block)
        status["volume"] = meta
    if bool(params.get("include_fundamentals", True)):
        block, meta = _fundamental_panels(
            prices, lag_days=max(0, int(params.get("fundamental_lag_days", 45)))
        )
        panels.update(block)
        status["fundamentals"] = meta
    if bool(params.get("include_events", True)):
        block, meta = _event_panels(prices, params)
        panels.update(block)
        status["events"] = meta
    if bool(params.get("include_archived_snapshots", True)):
        block, meta = _load_archived_panels(prices, db, params)
        panels.update(block)
        status.update(meta)
    return FeaturePanels(panels=panels, source_status=status)


def _relationship_panels(prices: pd.DataFrame, params: Mapping[str, Any]) -> dict[str, pd.DataFrame]:
    returns = prices.pct_change(fill_method=None)
    window = max(20, int(params.get("peer_window", 63)))
    top_k = max(1, min(int(params.get("peer_count", 3)), max(1, prices.shape[1] - 1)))
    refresh = max(1, int(params.get("peer_refresh_days", 5)))
    spread_lookback = max(2, int(params.get("peer_spread_lookback", 5)))
    z_window = max(20, int(params.get("peer_z_window", 63)))

    peer_return = _wide(prices.index, prices.columns)
    # Peer sets refresh on the same calendar-anchored schedule the strategies
    # use, so the same date selects the same peers regardless of where the
    # requested window starts.
    schedule = _rebalance_schedule(prices.index, refresh).to_numpy()
    refresh_positions = [int(i) for i in np.flatnonzero(schedule) if i >= window]
    for n, start in enumerate(refresh_positions):
        stop = refresh_positions[n + 1] if n + 1 < len(refresh_positions) else len(prices)
        hist = returns.iloc[start - window:start]
        corr = hist.corr(min_periods=max(10, window // 3))
        block = returns.iloc[start:stop]
        for sym in prices.columns:
            peers = corr[sym].drop(labels=[sym], errors="ignore").dropna().nlargest(top_k).index
            if len(peers):
                peer_return.loc[prices.index[start:stop], sym] = block[list(peers)].mean(axis=1).to_numpy()

    relative = returns - peer_return
    spread = relative.rolling(spread_lookback, min_periods=spread_lookback).sum()
    mean = spread.rolling(z_window, min_periods=z_window).mean()
    sd = spread.rolling(z_window, min_periods=z_window).std()
    spread_reversal = -spread.sub(mean).div(sd.replace(0.0, np.nan))

    peer_move = peer_return.shift(1).rolling(3, min_periods=3).sum()
    own_move = returns.shift(1).rolling(3, min_periods=3).sum()
    catchup = peer_move - own_move
    return {"peer_spread_reversal_raw": spread_reversal, "peer_catchup_raw": catchup}


def build_multisource_signal_library(
    prices: pd.DataFrame,
    params: Mapping[str, Any] | None = None,
    features: FeaturePanels | None = None,
    db: Session | None = None,
    signals: Iterable[str] | None = None,
) -> MultisourceLibraryOutput:
    """Blend price-derived and external point-in-time signal panels into one library."""

    params = dict(params or {})
    prices = prices.sort_index().astype(float)
    requested = None if signals is None else list(dict.fromkeys(str(s) for s in signals))
    if requested is not None:
        unknown = [name for name in requested if name not in MULTISOURCE_SPEC_BY_NAME]
        if unknown:
            raise ValueError(f"Unknown multi-source signals: {unknown}")

    price_names = [spec.name for spec in SIGNAL_SPECS]
    wanted_price = price_names if requested is None else [n for n in requested if n in price_names]
    if wanted_price:
        base = build_signal_library(prices, params=params, signals=wanted_price)
        components = dict(base.components)
        beta = base.beta
        residual = base.residual_returns
    else:
        returns = prices.pct_change(fill_method=None)
        universe, beta = _rolling_beta(returns, max(10, int(params.get("beta_window", 63))))
        residual = returns - beta.mul(universe, axis=0)
        components = {}

    features = features or build_feature_panels(prices, params=params, db=db)
    p = features.panels
    raw: dict[str, pd.DataFrame] = {}

    if "volume" in p:
        volume = p["volume"].replace(0.0, np.nan)
        rel_volume = np.log(volume.div(volume.rolling(20, min_periods=20).median()))
        recent_resid = residual.rolling(5, min_periods=5).sum()
        raw["volume_confirmed_momentum"] = recent_resid * rel_volume.clip(lower=0.0)
        raw["volume_shock_reversal"] = -residual * rel_volume.clip(lower=0.0)
        dollar_volume = volume * prices
        illiq = prices.pct_change(fill_method=None).abs().div(dollar_volume.replace(0.0, np.nan))
        short = illiq.rolling(10, min_periods=10).median()
        long = illiq.rolling(60, min_periods=40).median()
        raw["liquidity_improvement"] = -short.div(long.replace(0.0, np.nan))

    if "fcf_yield" in p:
        raw["fcf_yield_value"] = p["fcf_yield"]
    if "pe_trailing" in p:
        raw["earnings_yield_value"] = 1.0 / p["pe_trailing"].where(p["pe_trailing"] > 0)
    if "ps_trailing" in p:
        raw["sales_yield_value"] = 1.0 / p["ps_trailing"].where(p["ps_trailing"] > 0)
    if "ev_ebitda" in p:
        raw["ebitda_yield_value"] = 1.0 / p["ev_ebitda"].where(p["ev_ebitda"] > 0)

    if "earnings_surprise_decay" in p:
        raw["post_earnings_surprise"] = p["earnings_surprise_decay"]
    if "analyst_action_decay" in p:
        raw["analyst_action_momentum"] = p["analyst_action_decay"]

    archive_map = {
        "eps_revision_breadth": ("eps_revision_breadth", 1.0),
        "eps_estimate_acceleration": ("eps_estimate_change_30d", 1.0),
        "target_price_upside": ("target_price_upside", 1.0),
        "low_estimate_dispersion": ("estimate_dispersion", -1.0),
        "put_call_oi_contrarian": ("put_call_oi_log", -1.0),
        "iv_richness": ("iv_realized_gap", -1.0),
        "downside_skew_contrarian": ("downside_skew", -1.0),
        "iv_term_structure": ("iv_term_slope", 1.0),
    }
    for signal_name, (feature_name, sign) in archive_map.items():
        if feature_name in p:
            raw[signal_name] = sign * p[feature_name]

    peer_names = ("peer_spread_reversal", "peer_catchup")
    if requested is None or any(name in requested for name in peer_names):
        relationship = _relationship_panels(prices, params)
        raw["peer_spread_reversal"] = relationship["peer_spread_reversal_raw"]
        raw["peer_catchup"] = relationship["peer_catchup_raw"]

    wanted_external = (
        [spec.name for spec in MULTISOURCE_SIGNAL_SPECS]
        if requested is None
        else [n for n in requested if n not in price_names]
    )
    for name in wanted_external:
        frame = raw.get(name)
        if frame is not None and frame.notna().any().any():
            components[name] = _rank_score(frame.reindex(index=prices.index, columns=prices.columns))

    source_status = dict(features.source_status)
    source_status.setdefault(
        "price",
        {"available": any(name in components for name in price_names), "coverage": 1.0},
    )
    source_status["cross_sectional_relationships"] = {
        "available": any(name in components for name in ("peer_spread_reversal", "peer_catchup")),
        "peer_window": max(20, int(params.get("peer_window", 63))),
        "peer_count": max(1, min(int(params.get("peer_count", 3)), max(1, prices.shape[1] - 1))),
    }

    return MultisourceLibraryOutput(
        library=SignalLibraryOutput(components=components, beta=beta, residual_returns=residual),
        specs={name: MULTISOURCE_SPEC_BY_NAME[name] for name in components},
        source_status=source_status,
    )
