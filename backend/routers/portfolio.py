"""Portfolio endpoints: holdings, blotter, P&L, and portfolio-level analytics.

Every route is scoped to the authenticated user through
:func:`_owned_portfolio`, so one account can never read or trade another's book.

The analytics routes deliberately delegate: ``/risk`` calls the same
``risk_metrics`` the ``/quantitative/performance`` command uses, and ``/factors``
calls the same regression ``/api/factors/analyze`` runs on bare symbols. The
only new thing is what they point at — a return series reconstructed from the
transaction log instead of a single ticker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, status
from scipy import stats as sps
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..data.provider import get_history
from ..database import get_db
from ..extensions.quantitative import risk_metrics
from ..factors.models import build_factors, factor_regression
from ..models import CASH_SIDES, Portfolio, Position, Transaction, User
from ..portfolio import analytics, snapshot
from ..portfolio.accounting import build_ledger, naive_utc, rebuild_positions
from ..schemas import (
    PortfolioCreate,
    PortfolioOut,
    PortfolioUpdate,
    PositionOut,
    TransactionCreate,
    TransactionOut,
    TransactionUpdate,
)

router = APIRouter(prefix="/api/portfolios", tags=["portfolios"])

TRADING_DAYS = 252
#: Sides that must name a security.
SYMBOL_REQUIRED = ("buy", "sell", "dividend")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Ownership helpers
# --------------------------------------------------------------------------- #
def _owned_portfolio(db: Session, user_id: int, portfolio_id: int) -> Portfolio:
    portfolio = (
        db.query(Portfolio)
        .filter(Portfolio.id == portfolio_id, Portfolio.user_id == user_id)
        .first()
    )
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return portfolio


def _clear_default(db: Session, user_id: int) -> None:
    db.query(Portfolio).filter(
        Portfolio.user_id == user_id, Portfolio.is_default.is_(True)
    ).update({"is_default": False})


def _transactions(db: Session, portfolio_id: int) -> List[Transaction]:
    return (
        db.query(Transaction)
        .filter(Transaction.portfolio_id == portfolio_id)
        .order_by(Transaction.trade_date, Transaction.id)
        .all()
    )


def _ledger(db: Session, portfolio: Portfolio):
    return build_ledger(_transactions(db, portfolio.id), portfolio.cost_basis_method)


# --------------------------------------------------------------------------- #
# Portfolios
# --------------------------------------------------------------------------- #
@router.get("", response_model=List[PortfolioOut])
def list_portfolios(
    current: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> List[Portfolio]:
    return (
        db.query(Portfolio)
        .filter(Portfolio.user_id == current.id)
        .order_by(Portfolio.is_default.desc(), Portfolio.name)
        .all()
    )


@router.post("", response_model=PortfolioOut, status_code=status.HTTP_201_CREATED)
def create_portfolio(
    payload: PortfolioCreate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Portfolio:
    clash = (
        db.query(Portfolio)
        .filter(Portfolio.user_id == current.id, Portfolio.name == payload.name)
        .first()
    )
    if clash:
        raise HTTPException(status_code=400, detail="You already have a portfolio with that name")

    if payload.is_default:
        _clear_default(db, current.id)
    portfolio = Portfolio(
        user_id=current.id,
        name=payload.name,
        description=payload.description,
        base_currency=payload.base_currency.upper(),
        cost_basis_method=payload.cost_basis_method,
        benchmark=payload.benchmark.upper(),
        is_default=payload.is_default,
    )
    db.add(portfolio)
    db.flush()
    if payload.initial_cash:
        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                side="deposit",
                quantity=payload.initial_cash,
                price=1.0,
                trade_date=naive_utc(_utcnow()),
                note="Opening balance",
            )
        )
        db.flush()
        rebuild_positions(db, portfolio)
    db.commit()
    db.refresh(portfolio)
    return portfolio


@router.get("/{portfolio_id}")
def get_portfolio(
    portfolio_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """The portfolio with its holdings, straight from the materialised table."""
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    return {
        "portfolio": PortfolioOut.model_validate(portfolio).model_dump(),
        "positions": [
            PositionOut.model_validate(p).model_dump()
            for p in portfolio.positions
            if abs(p.quantity) > 0
        ],
        "closed_positions": [
            PositionOut.model_validate(p).model_dump()
            for p in portfolio.positions
            if abs(p.quantity) == 0
        ],
        "transaction_count": db.query(Transaction)
        .filter(Transaction.portfolio_id == portfolio.id)
        .count(),
    }


@router.patch("/{portfolio_id}", response_model=PortfolioOut)
def update_portfolio(
    portfolio_id: int,
    payload: PortfolioUpdate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Portfolio:
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("is_default"):
        _clear_default(db, current.id)
    if "name" in fields:
        clash = (
            db.query(Portfolio)
            .filter(
                Portfolio.user_id == current.id,
                Portfolio.name == fields["name"],
                Portfolio.id != portfolio.id,
            )
            .first()
        )
        if clash:
            raise HTTPException(status_code=400, detail="You already have a portfolio with that name")
    method_changed = (
        "cost_basis_method" in fields and fields["cost_basis_method"] != portfolio.cost_basis_method
    )
    for field, value in fields.items():
        setattr(portfolio, field, value)
    if method_changed:
        # FIFO and average cost realise different amounts — re-state history.
        rebuild_positions(db, portfolio)
    db.commit()
    db.refresh(portfolio)
    return portfolio


@router.delete("/{portfolio_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_portfolio(
    portfolio_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    db.delete(_owned_portfolio(db, current.id, portfolio_id))
    db.commit()


@router.get("/{portfolio_id}/positions", response_model=List[PositionOut])
def list_positions(
    portfolio_id: int,
    include_closed: bool = False,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[Position]:
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    return [p for p in portfolio.positions if include_closed or abs(p.quantity) > 0]


# --------------------------------------------------------------------------- #
# Blotter
# --------------------------------------------------------------------------- #
def _validate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a transaction payload and reject the impossible ones."""
    side = payload.get("side")
    symbol = (payload.get("symbol") or "").strip().upper() or None
    if side in SYMBOL_REQUIRED and not symbol:
        raise HTTPException(status_code=400, detail="A {} needs a symbol".format(side))
    if side in CASH_SIDES:
        # Cash rows carry the amount in ``quantity``; a price would double-count.
        payload["price"] = 1.0
        if side not in ("dividend",):
            symbol = None
    payload["symbol"] = symbol
    # The column is naive, so store naive UTC rather than mixing the two.
    payload["trade_date"] = naive_utc(payload.get("trade_date") or _utcnow())
    return payload


@router.get("/{portfolio_id}/transactions", response_model=List[TransactionOut])
def list_transactions(
    portfolio_id: int,
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    limit: int = Query(200, ge=1, le=2000),
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> List[Transaction]:
    """The blotter, newest first."""
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    query = db.query(Transaction).filter(Transaction.portfolio_id == portfolio.id)
    if symbol:
        query = query.filter(Transaction.symbol == symbol.strip().upper())
    if side:
        query = query.filter(Transaction.side == side)
    return (
        query.order_by(Transaction.trade_date.desc(), Transaction.id.desc()).limit(limit).all()
    )


@router.post(
    "/{portfolio_id}/transactions",
    response_model=TransactionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_transaction(
    portfolio_id: int,
    payload: TransactionCreate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Transaction:
    """Record a trade or cash movement, then re-derive the holdings."""
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    fields = _validate(payload.model_dump())
    txn = Transaction(portfolio_id=portfolio.id, **fields)
    db.add(txn)
    db.flush()
    rebuild_positions(db, portfolio)
    db.commit()
    db.refresh(txn)
    return txn


@router.patch("/{portfolio_id}/transactions/{transaction_id}", response_model=TransactionOut)
def update_transaction(
    portfolio_id: int,
    transaction_id: int,
    payload: TransactionUpdate,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Transaction:
    """Correct an entry. Positions are re-derived, so a fat finger is fixable."""
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    txn = _owned_transaction(db, portfolio.id, transaction_id)
    fields = payload.model_dump(exclude_unset=True)
    merged = {
        "side": fields.get("side", txn.side),
        "symbol": fields.get("symbol", txn.symbol),
        "price": fields.get("price", txn.price),
        "trade_date": fields.get("trade_date", txn.trade_date),
    }
    merged = _validate(merged)
    fields["side"], fields["symbol"] = merged["side"], merged["symbol"]
    fields["price"], fields["trade_date"] = merged["price"], merged["trade_date"]
    for field, value in fields.items():
        setattr(txn, field, value)
    db.flush()
    rebuild_positions(db, portfolio)
    db.commit()
    db.refresh(txn)
    return txn


@router.delete(
    "/{portfolio_id}/transactions/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_transaction(
    portfolio_id: int,
    transaction_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    db.delete(_owned_transaction(db, portfolio.id, transaction_id))
    db.flush()
    rebuild_positions(db, portfolio)
    db.commit()


def _owned_transaction(db: Session, portfolio_id: int, transaction_id: int) -> Transaction:
    txn = (
        db.query(Transaction)
        .filter(Transaction.id == transaction_id, Transaction.portfolio_id == portfolio_id)
        .first()
    )
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return txn


# --------------------------------------------------------------------------- #
# Valuation
# --------------------------------------------------------------------------- #
@router.get("/{portfolio_id}/summary")
def summary(
    portfolio_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Holdings marked to live quotes: market value, unrealised and realised P&L."""
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    ledger = _ledger(db, portfolio)
    quotes, warnings = analytics.live_quotes([h.symbol for h in ledger.open_holdings])
    rows, totals = analytics.mark_to_market(ledger, quotes)
    if totals["stale_quotes"]:
        warnings.append(
            "No live quote for {} — valued at cost.".format(", ".join(totals["stale_quotes"]))
        )
    return {
        "portfolio": PortfolioOut.model_validate(portfolio).model_dump(),
        "totals": totals,
        "positions": rows,
        "concentration": analytics.concentration(rows),
        "movers": {
            "gainers": sorted(
                [r for r in rows if (r["day_change"] or 0) > 0],
                key=lambda r: r["day_change"], reverse=True,
            )[:5],
            "losers": sorted(
                [r for r in rows if (r["day_change"] or 0) < 0],
                key=lambda r: r["day_change"],
            )[:5],
        },
        "warnings": warnings,
    }


@router.get("/{portfolio_id}/allocation")
def allocation(
    portfolio_id: int,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Where the money actually sits — by sector, industry, country and asset type."""
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    ledger = _ledger(db, portfolio)
    quotes, warnings = analytics.live_quotes([h.symbol for h in ledger.open_holdings])
    rows, totals = analytics.mark_to_market(ledger, quotes)
    if not rows:
        return {"warnings": ["This portfolio holds nothing yet"], "totals": totals,
                "by_symbol": [], "by_sector": [], "by_industry": [], "by_country": [],
                "by_asset_type": []}

    classified = analytics.classify([r["symbol"] for r in rows])
    for row in rows:
        row.update(classified.get(row["symbol"], {}))

    return {
        "totals": totals,
        "by_symbol": [
            {"symbol": r["symbol"], "name": r["name"], "market_value": r["market_value"],
             "weight": r["weight"], "sector": r.get("sector")}
            for r in rows
        ],
        "by_sector": analytics.group_exposure(rows, "sector"),
        "by_industry": analytics.group_exposure(rows, "industry"),
        "by_country": analytics.group_exposure(rows, "country"),
        "by_asset_type": analytics.group_exposure(rows, "asset_type"),
        "cash_weight": (totals["cash"] / totals["total_value"]) if totals["total_value"] else None,
        "concentration": analytics.concentration(rows),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# Performance
# --------------------------------------------------------------------------- #
def _thin(frame: pd.DataFrame, limit: int) -> pd.DataFrame:
    """At most ``limit`` evenly spaced rows, always including the latest."""
    step = max(1, -(-len(frame) // limit))  # ceiling division keeps the cap real
    thinned = frame.iloc[::step]
    if frame.index[-1] not in thinned.index:
        thinned = pd.concat([thinned, frame.iloc[[-1]]])
    return thinned


def _series_payload(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    """The equity curve as plottable records."""
    return [
        {
            "date": stamp.date().isoformat(),
            "total_value": round(float(row["total_value"]), 4),
            "holdings_value": round(float(row["holdings_value"]), 4),
            "cash": round(float(row["cash"]), 4),
            "return": round(float(row["return"]), 8),
        }
        for stamp, row in frame.iterrows()
    ]


@router.get("/{portfolio_id}/performance")
def performance(
    portfolio_id: int,
    start: Optional[str] = None,
    end: Optional[str] = None,
    risk_free_rate: float = 0.0,
    benchmark: Optional[str] = None,
    points: int = Query(400, ge=20, le=5000),
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """The equity curve and how good it was.

    Returns are time-weighted (deposits and withdrawals neutralised daily), so
    they are comparable with the benchmark. ``money_weighted_return`` is the
    IRR of the actual cash flows, which is what the account holder earned.
    """
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    transactions = _transactions(db, portfolio.id)
    if not transactions:
        raise HTTPException(status_code=400, detail="This portfolio has no transactions yet")

    frame, warnings = analytics.value_series(transactions, start, end)
    if frame.empty:
        raise HTTPException(status_code=400, detail="Not enough history to build a return series")

    returns = analytics.returns_series(frame)
    metrics = risk_metrics(returns, risk_free_rate) if len(returns) > 2 else {}
    ending_value = float(frame["total_value"].iloc[-1])

    thinned = _thin(frame, points)
    bench_symbol = (benchmark or portfolio.benchmark or "SPY").upper()
    bench = _benchmark_block(
        bench_symbol, frame, returns, risk_free_rate, warnings, thinned.index
    )

    return {
        "portfolio": {"id": portfolio.id, "name": portfolio.name},
        "period": {
            "start": frame.index[0].date().isoformat(),
            "end": frame.index[-1].date().isoformat(),
            "days": int(len(frame)),
        },
        "totals": {
            "starting_value": round(float(frame["total_value"].iloc[0]), 4),
            "ending_value": round(ending_value, 4),
            "net_deposits": round(float(frame["external_flow"].sum()), 4),
            "time_weighted_return": round(analytics.cumulative_return(frame), 6),
            # Annualised, so it sits next to the money-weighted figure on the
            # same footing — the two answer different questions (how the
            # holdings did, versus what the account holder actually earned).
            "time_weighted_return_annual": metrics.get("cagr"),
            "money_weighted_return_annual": analytics.money_weighted_return(
                transactions, ending_value, frame.index[-1]
            ),
        },
        "metrics": metrics,
        "benchmark": bench,
        "series": _series_payload(thinned),
        "warnings": warnings,
    }


def _benchmark_block(
    symbol: str,
    frame: pd.DataFrame,
    returns: pd.Series,
    risk_free_rate: float,
    warnings: List[str],
    sample: pd.DatetimeIndex,
) -> Optional[Dict[str, Any]]:
    """Benchmark returns aligned to the portfolio's own dates, plus beta/alpha.

    ``series`` is the growth of 1 unit, sampled on exactly the dates the equity
    curve is sampled on, so a caller can plot the two against each other without
    re-deriving the alignment.
    """
    try:
        closes = get_history(
            symbol,
            frame.index[0].date().isoformat(),
            (frame.index[-1].date() + timedelta(days=1)).isoformat(),
        )["close"]
    except Exception as exc:  # noqa: BLE001 - a missing benchmark is not fatal
        warnings.append("Benchmark {} unavailable: {}".format(symbol, exc))
        return None

    closes.index = analytics._naive_index(pd.to_datetime(closes.index))
    aligned = closes.reindex(frame.index).ffill().bfill()
    bench_returns = aligned.pct_change().fillna(0.0)

    paired = pd.concat([returns.rename("p"), bench_returns.rename("b")], axis=1).dropna()
    beta = alpha = correlation = tracking_error = information_ratio = None
    if len(paired) >= 20 and paired["b"].std(ddof=1):
        daily_rf = risk_free_rate / TRADING_DAYS
        slope, intercept, r, _p, _se = sps.linregress(paired["b"] - daily_rf, paired["p"] - daily_rf)
        active = paired["p"] - paired["b"]
        beta, alpha, correlation = float(slope), float(intercept * TRADING_DAYS), float(r)
        tracking_error = float(active.std(ddof=1) * np.sqrt(TRADING_DAYS))
        information_ratio = (
            float(active.mean() * TRADING_DAYS / tracking_error) if tracking_error else None
        )

    growth = (1 + bench_returns).cumprod()
    total = float(growth.iloc[-1] - 1)
    return {
        "symbol": symbol,
        "total_return": round(total, 6),
        "excess_return": round(analytics.cumulative_return(frame) - total, 6),
        "beta": beta,
        "alpha_annual": alpha,
        "correlation": correlation,
        "tracking_error": tracking_error,
        "information_ratio": information_ratio,
        "series": [round(float(v), 6) for v in growth.reindex(sample).values],
    }


# --------------------------------------------------------------------------- #
# Risk
# --------------------------------------------------------------------------- #
@router.get("/{portfolio_id}/risk")
def risk(
    portfolio_id: int,
    start: Optional[str] = None,
    end: Optional[str] = None,
    var_level: float = Query(0.05, gt=0, lt=0.5),
    horizon_days: int = Query(1, ge=1, le=250),
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Portfolio-level risk: VaR in dollars, volatility, and who is causing it."""
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    transactions = _transactions(db, portfolio.id)
    if not transactions:
        raise HTTPException(status_code=400, detail="This portfolio has no transactions yet")

    snap = snapshot.build(portfolio, transactions, start, end)
    returns = snap.returns
    if len(returns) < 20:
        raise HTTPException(
            status_code=400, detail="Need at least 20 days of history to measure risk"
        )

    frame, rows, value = snap.frame, snap.rows, snap.total_value

    metrics = risk_metrics(returns, var_level=var_level)
    scale = np.sqrt(horizon_days)
    historical_var = float(returns.quantile(var_level))
    tail = returns[returns <= historical_var]
    parametric_var = float(
        returns.mean() + returns.std(ddof=1) * sps.norm.ppf(var_level)
    )

    return {
        "as_of": frame.index[-1].date().isoformat(),
        "total_value": round(value, 4),
        "confidence": round(1 - var_level, 4),
        "horizon_days": horizon_days,
        "value_at_risk": {
            "historical_pct": round(historical_var * scale, 6),
            "historical_amount": round(historical_var * scale * value, 2),
            "parametric_pct": round(parametric_var * scale, 6),
            "parametric_amount": round(parametric_var * scale * value, 2),
            "conditional_pct": round(float(tail.mean()), 6) if len(tail) else None,
            "conditional_amount": round(float(tail.mean()) * scale * value, 2)
            if len(tail)
            else None,
        },
        "volatility": {
            "daily": round(float(returns.std(ddof=1)), 6),
            "annualised": metrics.get("annualised_volatility"),
            "downside": round(
                float(returns[returns < 0].std(ddof=1) * np.sqrt(TRADING_DAYS)), 6
            )
            if (returns < 0).sum() > 1
            else None,
        },
        "drawdown": {
            "max": metrics.get("max_drawdown"),
            "ulcer_index": metrics.get("ulcer_index"),
            "current": round(
                float((1 + returns).cumprod().iloc[-1] / (1 + returns).cumprod().max() - 1), 6
            ),
        },
        "concentration": analytics.concentration(rows),
        "risk_contribution": analytics.risk_contribution(rows, snap.panel),
        "warnings": snap.warnings,
    }


# --------------------------------------------------------------------------- #
# Factor exposure
# --------------------------------------------------------------------------- #
@router.get("/{portfolio_id}/factors")
def factors(
    portfolio_id: int,
    start: Optional[str] = None,
    end: Optional[str] = None,
    rf_annual: float = 0.0,
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """What the portfolio is actually exposed to.

    The factors are built from the portfolio's own holdings, then the
    portfolio's time-weighted returns are regressed on them — the same
    regression ``/api/factors/analyze`` runs per symbol, pointed at the book as
    a whole so the betas are holdings-weighted rather than eyeballed.
    """
    portfolio = _owned_portfolio(db, current.id, portfolio_id)
    transactions = _transactions(db, portfolio.id)
    if not transactions:
        raise HTTPException(status_code=400, detail="This portfolio has no transactions yet")

    snap = snapshot.build(portfolio, transactions, start, end)
    returns = snap.returns
    if len(returns) < 30:
        raise HTTPException(
            status_code=400, detail="Need at least 30 days of history for a factor regression"
        )

    rows, warnings = snap.rows, snap.warnings
    symbols = snap.symbols
    if len(symbols) < 2:
        raise HTTPException(
            status_code=400,
            detail="Factor construction needs at least 2 holdings — this portfolio has {}".format(
                len(symbols)
            ),
        )

    panel = snap.panel.dropna()
    if panel.shape[0] < 60 or panel.shape[1] < 2:
        raise HTTPException(
            status_code=400, detail="Not enough overlapping price history to build factors"
        )

    try:
        built = build_factors(panel, rf_annual)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    aligned = returns.reindex(built.index).dropna()
    portfolio_fit = factor_regression(aligned, built.loc[aligned.index], rf_annual)

    holding_returns = panel.pct_change().dropna()
    weights = {r["symbol"]: r["weight"] for r in rows}
    holdings = []
    for symbol in panel.columns:
        fit = factor_regression(holding_returns[symbol], built, rf_annual)
        holdings.append({"symbol": symbol, "weight": round(weights.get(symbol, 0.0), 6), **fit})

    return {
        "portfolio": {"id": portfolio.id, "name": portfolio.name},
        "period": {
            "start": built.index[0].date().isoformat(),
            "end": built.index[-1].date().isoformat(),
            "observations": int(len(built)),
        },
        "factors": list(built.columns),
        "factor_means_annual": {
            c: round(float(built[c].mean() * TRADING_DAYS), 6) for c in built.columns
        },
        "exposure": portfolio_fit,
        "holdings": holdings,
        "warnings": warnings,
    }
