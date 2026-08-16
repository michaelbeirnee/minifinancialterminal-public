"""Hedge-construction endpoints (design: docs/hedge-construction.md).

``GET /hedge/exposures`` measures what a hedge could target (build-order step
1). ``POST /hedge/analyze`` is step 5: it assembles the shared snapshot, the
historical shock distribution, and live option chains, then returns the
integer-sized candidate table — including the honest verdict that the right
move may be to sell rather than hedge.

The fetch helpers are module-level so tests can monkeypatch them and exercise
the whole endpoint offline.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..core.registry import execute
from ..data.provider import get_history
from ..database import get_db
from ..models import HEDGE_TERMINAL_STATES, HedgeRecord, User
from ..portfolio import (
    analytics,
    candidates,
    hedges,
    narrative,
    pricing,
    shocks,
    single,
    snapshot,
)
from ..schemas import (
    HedgeAnalyzeRequest,
    HedgeRecordCreate,
    HedgeRecordOut,
    HedgeRecordUpdate,
    HedgeSimulateRequest,
)
from .portfolio import _owned_portfolio, _transactions, _utcnow

router = APIRouter(prefix="/api/portfolios", tags=["hedging"])

#: The single-name simulator is not scoped to a book, so it gets its own
#: prefix. Same module, so it shares the monkeypatchable fetch helpers.
simulate_router = APIRouter(prefix="/api/hedge", tags=["hedging"])

#: History pulled for the single-name shock windows. Long enough to span more
#: than one regime; the response reports the period actually used.
SIMULATE_LOOKBACK_DAYS = 365 * 6


@router.get("/{portfolio_id}/hedge/exposures")
def hedge_exposures(
    portfolio_id: int,
    start: Optional[str] = None,
    end: Optional[str] = None,
    benchmark: Optional[str] = None,
    var_level: float = Query(0.05, gt=0, lt=0.5),
    horizon_days: int = Query(21, ge=1, le=250),
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """The three exposures a hedge can target, from one frozen snapshot.

    ``beta_dollars`` (linear market exposure), ``single_name_concentration``
    (what a collar would be for) and ``tail_loss`` (CVaR dollars at the
    horizon). Every estimate carries its uncertainty; a target that cannot be
    measured comes back null rather than guessed.
    """
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    transactions = _transactions(db, portfolio.id)
    if not transactions:
        raise HTTPException(status_code=400, detail="This portfolio has no transactions yet")

    bench = (benchmark or portfolio.benchmark or "SPY").upper()
    snap = snapshot.build(portfolio, transactions, start, end, benchmark=bench)
    if len(snap.returns) < hedges.MIN_OBSERVATIONS:
        raise HTTPException(
            status_code=400,
            detail="Need at least {} days of history to measure exposures".format(
                hedges.MIN_OBSERVATIONS
            ),
        )

    market = hedges.market_exposure(snap.returns, snap.benchmark_returns, snap.total_value)
    tail = hedges.tail_loss(
        snap.returns, snap.total_value, var_level=var_level, horizon_days=horizon_days
    )
    concentration = hedges.concentration_exposure(snap.rows, snap.panel)

    return {
        "as_of": snap.as_of,
        "portfolio": {"id": portfolio.id, "name": portfolio.name},
        "value": round(snap.total_value, 2),
        "currency": portfolio.base_currency,
        "benchmark": bench,
        "window": {
            "start": snap.frame.index[0].date().isoformat(),
            "end": snap.as_of,
            "observations": int(len(snap.returns)),
        },
        "horizon_days": horizon_days,
        "confidence": round(1 - var_level, 4),
        "estimator_version": hedges.ESTIMATOR_VERSION,
        "targets": {
            "beta_dollars": market,
            "single_name_concentration": concentration,
            "tail_loss": tail,
        },
        "source_timestamps": {
            "snapshot_taken_at": snap.taken_at.isoformat(),
            "quotes_at": snap.taken_at.isoformat(),
            "prices_through": snap.as_of,
        },
        "warnings": snap.warnings,
    }


# --------------------------------------------------------------------------- #
# Market data for the shock engine and the chains (monkeypatched in tests)
# --------------------------------------------------------------------------- #
def fetch_vol_closes(symbol: str, start: str, end: Optional[str]) -> Optional[pd.Series]:
    """Closing level of the vol index driving the IV shock dimension."""
    try:
        closes = get_history(symbol, start, end)["close"]
    except Exception:  # noqa: BLE001 - the caller degrades to frozen IV and says so
        return None
    closes.index = analytics._naive_index(pd.to_datetime(closes.index))
    return closes


def fetch_expirations(symbol: str) -> List[str]:
    result = execute("/derivatives/options/expirations", symbol=symbol)
    return [row["expiration"] for row in result.to_records() if row.get("expiration")]


def fetch_chain(symbol: str, expiration: str) -> pd.DataFrame:
    result = execute("/derivatives/options/chains", symbol=symbol, expiration=expiration)
    return pd.DataFrame(result.to_records())


def _chain_past(symbol: str, as_of: date, min_days: int, warnings: List[str]) -> pd.DataFrame:
    """The nearest listed expiry that outlives the horizon, as a chain frame.

    Only the expiries that could actually be used are downloaded — the
    solver's tenor rule is applied before the network call, not after.
    """
    try:
        expirations = fetch_expirations(symbol)
    except Exception as exc:  # noqa: BLE001 - no chain is a warning, not a 500
        warnings.append("No option expirations for {}: {}".format(symbol, exc))
        return pd.DataFrame()

    usable = [e for e in sorted(expirations) if (pd.Timestamp(e).date() - as_of).days >= min_days]
    for expiration in usable[:2]:
        try:
            chain = fetch_chain(symbol, expiration)
        except Exception as exc:  # noqa: BLE001 - try the next expiry
            warnings.append("Chain {} {} unavailable: {}".format(symbol, expiration, exc))
            continue
        if not chain.empty:
            return chain
    if not usable:
        warnings.append(
            "{} has no listed expiry at least {} days out".format(symbol, min_days)
        )
    return pd.DataFrame()


def _passes_liquidity(candidate: candidates.Candidate, request: HedgeAnalyzeRequest) -> bool:
    liquidity = candidate.liquidity or {}
    return (
        liquidity.get("min_open_interest", 0) >= request.min_open_interest
        and liquidity.get("max_relative_spread", 0.0) <= request.max_relative_spread
    )


# --------------------------------------------------------------------------- #
# Candidate construction
# --------------------------------------------------------------------------- #
@router.post("/{portfolio_id}/hedge/analyze")
def hedge_analyze(
    portfolio_id: int,
    request: HedgeAnalyzeRequest,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Integer-sized hedge candidates ranked on cost per unit of protection.

    The pipeline is the design doc's, in order: one frozen snapshot, a
    historical joint-shock distribution (index × holdings × vol), today's
    chains repriced under those shocks, then the cost table. Nothing here
    persists — putting a hedge on stays a human action.
    """
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    return _analyse(portfolio, _transactions(db, portfolio.id), request)


def _analyse(portfolio, transactions, request: HedgeAnalyzeRequest) -> Dict[str, Any]:
    """The analysis itself, so the narrative endpoint can reuse it verbatim.

    Kept as a function rather than a second call to the endpoint so a
    narrative is always built on numbers this process computed, never on
    numbers a client handed back.
    """
    if not transactions:
        raise HTTPException(status_code=400, detail="This portfolio has no transactions yet")

    bench = (request.benchmark or portfolio.benchmark or "SPY").upper()
    snap = snapshot.build(portfolio, transactions, request.start, request.end, benchmark=bench)
    if snap.benchmark_closes is None or len(snap.benchmark_closes) <= request.horizon_days:
        raise HTTPException(
            status_code=400,
            detail="Need more than {} sessions of {} history to build shocks".format(
                request.horizon_days, bench
            ),
        )
    if not snap.rows:
        raise HTTPException(status_code=400, detail="This portfolio holds no positions to hedge")

    warnings = list(snap.warnings)
    as_of = pd.Timestamp(snap.as_of).date()

    vol_closes = None
    if request.vol_symbol:
        vol_closes = fetch_vol_closes(
            request.vol_symbol, snap.frame.index[0].date().isoformat(), request.end
        )
        if vol_closes is None:
            warnings.append(
                "{} history unavailable — IV frozen across shocks, which understates "
                "option protection".format(request.vol_symbol)
            )

    try:
        shock_set = shocks.build_shocks(
            snap.panel,
            snap.benchmark_closes,
            request.horizon_days,
            benchmark=bench,
            vol_closes=vol_closes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    book_pnls = shocks.book_pnl(shock_set, snap.rows)
    value = snap.total_value
    unhedged_cvar = shocks.cvar(book_pnls, request.var_level)
    target = abs(unhedged_cvar) * request.target_reduction_fraction

    beta_dollars = None
    market = hedges.market_exposure(snap.returns, snap.benchmark_returns, value)
    if market:
        beta_dollars = market["beta_dollars"]

    spot = float(snap.benchmark_closes.iloc[-1])
    spots = {bench: spot}
    min_days = (shocks.horizon_end(as_of, request.horizon_days) - as_of).days + \
        candidates.TENOR_BUFFER_DAYS

    built: List[candidates.Candidate] = []
    skipped: List[Dict[str, str]] = []
    wants_options = {"protective_put", "put_spread"} & set(request.instruments)
    if wants_options:
        chain = _chain_past(bench, as_of, min_days, warnings)
        if not chain.empty:
            option_candidates, option_skips = candidates.index_candidates(
                chain, bench, spot, as_of, request.horizon_days
            )
            built.extend(
                c for c in option_candidates if c.structure.kind in request.instruments
            )
            skipped.extend(option_skips)

    if "collar" in request.instruments:
        collar, collar_skips = _collar_for_dominant_name(
            snap, as_of, request, spots, warnings
        )
        if collar is not None:
            built.append(collar)
        skipped.extend(collar_skips)

    liquid = []
    for candidate in built:
        if _passes_liquidity(candidate, request):
            liquid.append(candidate)
        else:
            skipped.append(
                {
                    "kind": candidate.structure.kind,
                    "reason": "below the liquidity floor (OI {} / spread {})".format(
                        candidate.liquidity.get("min_open_interest"),
                        candidate.liquidity.get("max_relative_spread"),
                    ),
                }
            )

    linear: List[pricing.LinearHedge] = []
    if "short_etf" in request.instruments and beta_dollars:
        # The hedge instrument IS the benchmark, so its beta against the
        # benchmark is 1.0 by definition — not the book's beta, which is what
        # ``beta_dollars`` already carries.
        linear.append(pricing.LinearHedge("short_etf", bench, abs(beta_dollars), 1.0))
    elif "short_etf" in request.instruments:
        skipped.append({"kind": "short_etf", "reason": "beta could not be estimated"})

    table = candidates.cost_table(
        shock_set,
        book_pnls,
        value,
        liquid,
        spots,
        as_of,
        target_reduction=target,
        linear_hedges=linear,
        book_beta_dollars=beta_dollars,
        level=request.var_level,
        rate=request.rate,
        div_yield=request.div_yield,
        short_carry_annual=request.short_carry_annual,
        book_rows=snap.rows,
    )
    table["excluded"].extend(skipped)

    return {
        "as_of": snap.as_of,
        "portfolio": {"id": portfolio.id, "name": portfolio.name},
        "value": round(value, 2),
        "currency": portfolio.base_currency,
        "benchmark": bench,
        "estimator_version": hedges.ESTIMATOR_VERSION,
        "request": request.model_dump(),
        "target": {
            "cvar_unhedged": round(unhedged_cvar, 2),
            "reduction_sought": round(target, 2),
            "fraction": request.target_reduction_fraction,
        },
        "shocks": {
            "windows": shock_set.n_windows,
            "independent_windows": shock_set.n_independent,
            "horizon_days": shock_set.horizon_sessions,
            "period": list(shock_set.period),
            "vol_symbol": request.vol_symbol if vol_closes is not None else None,
            "fallback_symbols": shock_set.fallback_symbols,
            "notes": shock_set.notes,
        },
        **table,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# Step 5b: the same engine on one name, sized in dollars
# --------------------------------------------------------------------------- #
@simulate_router.post("/simulate")
def hedge_simulate(
    request: HedgeSimulateRequest,
    current: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Hedge a hypothetical position in one stock, or with one named contract.

    Same pipeline as the portfolio path and the same honest verdict, with the
    single-name substitution that matters: the shock driver is the stock's own
    return history rather than an index, because the tail being hedged is the
    name's own. Reads nothing from the database and writes nothing to it — a
    simulation is not a position and not a decision.
    """
    return _simulate(request)


def _simulate(request: HedgeSimulateRequest) -> Dict[str, Any]:
    symbol = request.symbol.upper().strip()
    start = request.start or (date.today() - timedelta(days=SIMULATE_LOOKBACK_DAYS)).isoformat()
    warnings: List[str] = []

    try:
        history = get_history(symbol, start, request.end)
    except Exception as exc:  # noqa: BLE001 - an unknown symbol is a 400, not a 500
        raise HTTPException(status_code=400, detail="No price history for {}: {}".format(symbol, exc))
    closes = history["close"].dropna()
    closes.index = analytics._naive_index(pd.to_datetime(closes.index))
    if len(closes) <= request.horizon_days:
        raise HTTPException(
            status_code=400,
            detail="Need more than {} sessions of {} history to build shocks; have {}".format(
                request.horizon_days, symbol, len(closes)
            ),
        )

    try:
        position = single.position_from_notional(symbol, closes, request.notional)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    as_of = closes.index[-1].date()
    vol_closes = None
    if request.vol_symbol:
        vol_closes = fetch_vol_closes(request.vol_symbol, start, request.end)
        if vol_closes is None:
            warnings.append(
                "{} history unavailable — IV frozen across shocks, which understates "
                "option protection".format(request.vol_symbol)
            )
        else:
            warnings.append(
                "IV shocks come from {}; a single name's own IV moves more than the index "
                "in a name-specific shock, so put protection here is conservative".format(
                    request.vol_symbol
                )
            )

    try:
        # The name is its own benchmark: the risk being hedged is idiosyncratic,
        # so the shock windows must be the stock's, not an index's.
        shock_set = shocks.build_shocks(
            position.panel,
            position.closes,
            request.horizon_days,
            benchmark=symbol,
            vol_closes=vol_closes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    book_pnls = shocks.book_pnl(shock_set, position.rows)
    value = position.market_value
    unhedged_cvar = shocks.cvar(book_pnls, request.var_level)
    target = abs(unhedged_cvar) * request.target_reduction_fraction

    min_days = (shocks.horizon_end(as_of, request.horizon_days) - as_of).days + \
        candidates.TENOR_BUFFER_DAYS
    built: List[candidates.Candidate] = []
    skipped: List[Dict[str, str]] = []

    if request.contract is not None:
        # The clicked contract's own expiry, not the solver's pick — the whole
        # point of this entry point is to price the one the user is looking at.
        try:
            chain = fetch_chain(symbol, request.contract.expiration)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400,
                detail="No chain for {} {}: {}".format(symbol, request.contract.expiration, exc),
            )
        candidate, contract_skips = single.contract_candidate(
            chain, position, as_of,
            request.contract.expiration, request.contract.strike,
            request.contract.option_type, request.contract.contracts,
        )
        if candidate is not None:
            built.append(candidate)
        skipped.extend(contract_skips)
    else:
        chain = _chain_past(symbol, as_of, min_days, warnings)
        if chain.empty:
            skipped.append(
                {"kind": "options", "reason": "no usable chain for {}".format(symbol)}
            )
        else:
            name_built, name_skips = single.name_candidates(
                chain, position, as_of, request.horizon_days, tuple(request.instruments)
            )
            built.extend(name_built)
            skipped.extend(name_skips)

    liquid = []
    for candidate in built:
        if _passes_liquidity(candidate, request):
            liquid.append(candidate)
        else:
            skipped.append(
                {
                    "kind": candidate.structure.kind,
                    "reason": "below the liquidity floor (OI {} / spread {})".format(
                        candidate.liquidity.get("min_open_interest"),
                        candidate.liquidity.get("max_relative_spread"),
                    ),
                }
            )

    table = candidates.cost_table(
        shock_set,
        book_pnls,
        value,
        liquid,
        {symbol: position.spot},
        as_of,
        target_reduction=target,
        linear_hedges=(),
        book_beta_dollars=value,   # a long position is beta 1.0 against itself
        level=request.var_level,
        rate=request.rate,
        div_yield=request.div_yield,
        book_rows=position.rows,
    )
    table["excluded"].extend(skipped)

    overwrites = [r for r in table["rows"] if r.get("kind") in single.OVERWRITE_KINDS]
    if overwrites:
        warnings.append(
            "A covered call is premium income, not protection: it caps the upside and "
            "cushions only the first few percent of a fall. It is priced here for "
            "comparison, never as downside cover."
        )

    return {
        "as_of": as_of.isoformat(),
        "symbol": symbol,
        "position": position.sizing(),
        "value": round(value, 2),
        "estimator_version": hedges.ESTIMATOR_VERSION,
        "request": request.model_dump(),
        "target": {
            "cvar_unhedged": round(unhedged_cvar, 2),
            "reduction_sought": round(target, 2),
            "fraction": request.target_reduction_fraction,
        },
        "shocks": {
            "windows": shock_set.n_windows,
            "independent_windows": shock_set.n_independent,
            "horizon_days": shock_set.horizon_sessions,
            "period": list(shock_set.period),
            "driver": symbol,
            "vol_symbol": request.vol_symbol if vol_closes is not None else None,
            "notes": shock_set.notes,
        },
        **table,
        "warnings": warnings,
    }


def _collar_for_dominant_name(
    snap: snapshot.Snapshot,
    as_of: date,
    request: HedgeAnalyzeRequest,
    spots: Dict[str, float],
    warnings: List[str],
):
    """Build a collar on whichever holding dominates the book's risk."""
    exposure = hedges.concentration_exposure(snap.rows, snap.panel)
    dominant = next((p for p in exposure["positions"] if p["dominant"]), None)
    if dominant is None:
        return None, [
            {"kind": "collar", "reason": "no single position carries a dominant share of risk"}
        ]

    symbol = dominant["symbol"]
    row = next((r for r in snap.rows if r["symbol"] == symbol), None)
    price = (row or {}).get("last_price")
    if not price:
        return None, [{"kind": "collar", "reason": "{} has no live quote".format(symbol)}]

    min_days = (shocks.horizon_end(as_of, request.horizon_days) - as_of).days + \
        candidates.TENOR_BUFFER_DAYS
    chain = _chain_past(symbol, as_of, min_days, warnings)
    if chain.empty:
        return None, [{"kind": "collar", "reason": "no usable chain for {}".format(symbol)}]

    spots[symbol] = float(price)
    return candidates.collar_candidate(
        chain, symbol, float(price), float(row["quantity"]), as_of, request.horizon_days
    )


# --------------------------------------------------------------------------- #
# Lifecycle log — decisions, not candidates
# --------------------------------------------------------------------------- #
#: Legal moves. A hedge can be abandoned from any live state, but nothing
#: comes back from the dead: closed and expired are terminal.
HEDGE_TRANSITIONS = {
    "proposed": {"accepted", "executed", "closed", "expired"},
    "accepted": {"executed", "closed", "expired"},
    "executed": {"rolled", "closed", "expired"},
    "rolled": {"rolled", "closed", "expired"},
    "closed": set(),
    "expired": set(),
}


def _owned_record(db: Session, user_id: int, portfolio_id: int, record_id: int) -> HedgeRecord:
    record = (
        db.query(HedgeRecord)
        .filter(
            HedgeRecord.id == record_id,
            HedgeRecord.portfolio_id == portfolio_id,
            HedgeRecord.user_id == user_id,
        )
        .first()
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Hedge record not found")
    return record


@router.post(
    "/{portfolio_id}/hedge/records",
    response_model=HedgeRecordOut,
    status_code=status.HTTP_201_CREATED,
)
def create_hedge_record(
    portfolio_id: int,
    payload: HedgeRecordCreate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HedgeRecord:
    """Record a hedge decision.

    Called when a human commits to a candidate — never when one is merely
    displayed. What was believed at the time (quotes, assumptions, estimator
    version, expected CVaR reduction and its CI) is frozen here so the
    scorecard later compares like with like.
    """
    _owned_portfolio(db, current.id, portfolio_id)
    if payload.quantity is None and payload.notional is None:
        raise HTTPException(
            status_code=400, detail="A hedge needs either a contract count or a notional"
        )

    record = HedgeRecord(
        user_id=current.id,
        portfolio_id=portfolio_id,
        **payload.model_dump(),
    )
    if payload.state == "executed":
        record.executed_at = _utcnow()
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/{portfolio_id}/hedge/records", response_model=List[HedgeRecordOut])
def list_hedge_records(
    portfolio_id: int,
    state: Optional[str] = None,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[HedgeRecord]:
    """Every hedge decision on this book, newest first."""
    _owned_portfolio(db, current.id, portfolio_id)
    query = db.query(HedgeRecord).filter(
        HedgeRecord.portfolio_id == portfolio_id, HedgeRecord.user_id == current.id
    )
    if state:
        query = query.filter(HedgeRecord.state == state)
    return query.order_by(HedgeRecord.proposed_at.desc(), HedgeRecord.id.desc()).all()


@router.patch("/{portfolio_id}/hedge/records/{record_id}", response_model=HedgeRecordOut)
def update_hedge_record(
    portfolio_id: int,
    record_id: int,
    payload: HedgeRecordUpdate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HedgeRecord:
    """Advance a hedge's state, or stamp what it actually returned.

    Illegal transitions are refused rather than silently applied: a closed
    hedge stays closed, so the log cannot be rewritten into a nicer story.
    """
    record = _owned_record(db, current.id, portfolio_id, record_id)
    fields = payload.model_dump(exclude_unset=True)

    new_state = fields.pop("state", None)
    if new_state and new_state != record.state:
        if new_state not in HEDGE_TRANSITIONS[record.state]:
            raise HTTPException(
                status_code=400,
                detail="A {} hedge cannot become {} — legal next states are {}".format(
                    record.state,
                    new_state,
                    ", ".join(sorted(HEDGE_TRANSITIONS[record.state])) or "none",
                ),
            )
        record.state = new_state
        if new_state == "executed" and record.executed_at is None:
            record.executed_at = _utcnow()
        if new_state in HEDGE_TERMINAL_STATES:
            record.closed_at = _utcnow()

    for field, value in fields.items():
        setattr(record, field, value)

    # Realised P&L is derivable once both ends are known, so a caller that
    # supplies the exit value alone still gets a graded record.
    if record.realised_hedge_pnl is None and record.exit_value is not None and record.entry_cost is not None:
        record.realised_hedge_pnl = record.exit_value - record.entry_cost

    db.commit()
    db.refresh(record)
    return record


@router.delete(
    "/{portfolio_id}/hedge/records/{record_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_hedge_record(
    portfolio_id: int,
    record_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Delete a record entered by mistake."""
    record = _owned_record(db, current.id, portfolio_id, record_id)
    db.delete(record)
    db.commit()


@router.get("/{portfolio_id}/hedge/scorecard")
def hedge_scorecard(
    portfolio_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Was the insurance worth it — answered from this book's own record.

    Only closed hedges with realised P&L count. The sample size is stated
    first and never buried: a handful of hedges cannot settle the question,
    and the payload says so rather than implying a verdict.
    """
    _owned_portfolio(db, current.id, portfolio_id)
    records = (
        db.query(HedgeRecord)
        .filter(HedgeRecord.portfolio_id == portfolio_id, HedgeRecord.user_id == current.id)
        .all()
    )
    by_state: Dict[str, int] = {}
    for record in records:
        by_state[record.state] = by_state.get(record.state, 0) + 1

    graded = [
        r for r in records
        if r.state in HEDGE_TERMINAL_STATES and r.realised_hedge_pnl is not None
    ]
    payload: Dict[str, Any] = {
        "portfolio_id": portfolio_id,
        "records": len(records),
        "by_state": by_state,
        "graded": len(graded),
    }
    if not graded:
        payload["note"] = (
            "No hedge has been closed out with a realised P&L yet — nothing to judge."
        )
        return payload

    hedge_pnl = sum(r.realised_hedge_pnl for r in graded)
    paid_for = sum(r.entry_cost for r in graded if r.entry_cost is not None)
    needed = [r for r in graded if r.realised_book_pnl is not None and r.realised_book_pnl < 0]
    paid_when_needed = [r for r in needed if r.realised_hedge_pnl > 0]

    payload.update(
        {
            # Already net of what was paid: realised = exit value - entry cost.
            "realised_hedge_pnl": round(hedge_pnl, 2),
            "premium_paid": round(paid_for, 2),
            "hedges_that_paid": sum(1 for r in graded if r.realised_hedge_pnl > 0),
            "book_down_episodes": len(needed),
            "paid_when_the_book_fell": len(paid_when_needed),
            "note": (
                "{} closed hedge(s) is far too small a sample to judge a hedging "
                "policy — read this as a log, not a verdict.".format(len(graded))
                if len(graded) < 10
                else "{} closed hedges on record.".format(len(graded))
            ),
        }
    )
    return payload


# --------------------------------------------------------------------------- #
# Narrative layer (docs/hedge-construction.md step 8)
# --------------------------------------------------------------------------- #
@router.get("/{portfolio_id}/hedge/narrate/status")
def narrate_status(
    portfolio_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Whether narration is switched on. Everything else works without it."""
    _owned_portfolio(db, current.id, portfolio_id)
    return narrative.availability()


@router.post("/{portfolio_id}/hedge/narrate")
def hedge_narrate(
    portfolio_id: int,
    request: HedgeAnalyzeRequest,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Plain-language explanation of an analysis this process just computed.

    The analysis is re-run server-side rather than accepted from the caller,
    so a narrative can never be built on numbers that did not come out of the
    engine. The model cannot change the verdict: ``validate`` puts any
    disagreement back and reports it in ``contradicted_engine``.
    """
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    state = narrative.availability()
    if not state["enabled"]:
        raise HTTPException(status_code=503, detail=state["reason"])

    analysis = _analyse(portfolio, _transactions(db, portfolio.id), request)
    exposures = None
    try:
        exposures = hedge_exposures(
            portfolio_id,
            start=request.start,
            end=request.end,
            benchmark=request.benchmark,
            var_level=request.var_level,
            horizon_days=request.horizon_days,
            current=current,
            db=db,
        )
    except HTTPException:  # concentration context is a nicety, not a requirement
        exposures = None

    brief = narrative.build_brief(analysis, exposures)
    try:
        narrated = narrative.run(brief, analysis)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return {"analysis": analysis, "brief": brief, "narrative": narrated}
