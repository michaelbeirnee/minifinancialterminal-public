"""The daily production cycle: research vintage in, next-session orders out.

This module turns the research stack into a boring, deterministic daily job.
The separation of concerns is deliberate and one-directional:

* the **research engine** periodically promotes a frozen vintage (blend,
  sleeve plan, gates, evidence) into ``production_signal_vintages``;
* the **daily cycle** consumes only the latest approved vintage, scores
  today's signals, projects a risk-constrained target book, diffs it against
  the broker's positions and writes next-session orders to the ledger;
* a **hard risk gateway** sits between the optimizer and the broker — any
  failed check records the hypothetical orders and submits nothing;
* **reconciliation** ingests fills, rebuilds positions from the fill ledger
  and compares them with what the broker reports. Broker positions are the
  source of truth; a disagreement blocks the next cycle.

The information rule is ``cutoff today → execution tomorrow``: the decision
bar is the last close at or before ``as_of`` and orders are written for the
next session. Nothing timestamped after the cutoff can enter the target.

Two broker adapters exist. ``ledger`` needs no keys: submitted orders fill at
the next session's open during reconciliation, with configured costs — the
stage-one simulator. ``alpaca`` routes the same orders to Alpaca's
paper-trading API (and only that host, same hard line as the tick engine).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from ..backtest.alpha_risk import (
    build_alpha_sleeve_plan,
    build_borrow_panels,
    build_sleeve_target,
    project_portfolio_constraints,
)
from ..backtest.execution_research import build_execution_panels
from ..backtest.factor_risk import build_factor_risk_model, portfolio_risk_diagnostics
from ..backtest.multisource_research import (
    archive_current_snapshots,
    build_feature_panels,
    build_multisource_signal_library,
)
from ..backtest.signal_research import research_signal_suite
from ..config import settings
from ..data.provider import get_history, get_price_panel
from ..models import (
    ProductionOrder,
    ProductionPositionSnapshot,
    ProductionRun,
    ProductionSignalVintage,
)
from .broker import PAPER_HOST

QTY_TOLERANCE = 1e-6

# The fallback snapshot-capture universe: liquid, optionable US large caps
# across sectors. Every uncaptured day is permanently unrecoverable for the
# archive-first data families, so capture deliberately has a default universe
# rather than waiting for a vintage. Override with MFT_CAPTURE_UNIVERSE or an
# explicit symbol list.
DEFAULT_CAPTURE_UNIVERSE: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA", "ORCL", "CRM",
    "AMD", "QCOM", "TXN", "INTC", "MU", "ADBE", "NOW", "PANW", "SNOW", "UBER",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW", "AXP", "V",
    "MA", "PYPL", "COIN", "BRK-B", "SPGI", "CME", "ICE", "PGR", "CB", "MET",
    "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT", "AMGN", "GILD",
    "VRTX", "REGN", "ISRG", "CVS", "MDT", "BMY", "DHR", "SYK", "CI", "HUM",
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "PSX", "MPC", "VLO", "DVN",
    "WMT", "COST", "HD", "LOW", "TGT", "MCD", "SBUX", "NKE", "TJX", "DG",
    "PG", "KO", "PEP", "PM", "MO", "CL", "KMB", "MDLZ", "GIS", "KHC",
    "BA", "CAT", "DE", "GE", "HON", "LMT", "RTX", "UPS", "UNP", "FDX",
    "DIS", "NFLX", "CMCSA", "T", "VZ", "TMUS", "LIN", "APD", "FCX", "NEM",
    "NEE", "DUK", "SO", "D", "AEP", "PLD", "AMT", "EQIX", "O", "SPG",
)


def resolve_capture_universe(db: Session, symbols: Iterable[str] | None = None) -> list[str]:
    """Explicit list > latest approved vintage > MFT_CAPTURE_UNIVERSE > default."""

    explicit = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    if explicit:
        return list(dict.fromkeys(explicit))
    vintage = latest_approved_vintage(db)
    if vintage is not None:
        from_vintage = [s for s in vintage.symbols.split(",") if s]
        if from_vintage:
            return from_vintage
    configured = [s.strip().upper() for s in settings.capture_universe.split(",") if s.strip()]
    if configured:
        return list(dict.fromkeys(configured))
    return list(DEFAULT_CAPTURE_UNIVERSE)

DEFAULT_CYCLE_CONFIG: dict[str, Any] = {
    # Capital & sizing
    "initial_capital": 1_000_000.0,
    "gross_target": 1.0,
    "min_order_notional": 200.0,
    "max_order_count": 200,
    "max_adv_participation": 0.05,
    "limit_buffer_bps": 50.0,
    # Ledger-broker cost model (stage-one simulator)
    "commission_bps": 1.0,
    "slippage_bps": 2.0,
    # Risk projection (same knobs as the walk-forward engine)
    "max_name_weight": 0.20,
    "max_crowded_short_gross": 0.15,
    "crowded_short_threshold": 0.65,
    "target_annual_volatility": 0.12,
    "max_market_factor_exposure": 0.05,
    "max_style_factor_exposure": 0.15,
    "covariance_risk_aversion": 0.25,
    "factor_risk_lookback_days": 252,
    "factor_risk_refresh_days": 21,
    "factor_risk_min_observations": 80,
    "residual_covariance_shrinkage": 0.50,
    # Borrow proxies
    "base_borrow_bps": 30.0,
    "crowding_surcharge_bps": 900.0,
    "hard_to_borrow_short_float": 0.35,
    "hard_to_borrow_days_to_cover": 15.0,
    # Gateway limits
    "max_vintage_age_days": 45,
    "max_data_age_days": 7,
    "max_daily_loss_fraction": 0.05,
    "net_exposure_tolerance": 1e-6,
    "beta_exposure_tolerance": 0.02,
    "position_qty_tolerance": 1e-4,
    # History fetched for scoring/risk when prices are not injected
    "history_start": "2022-01-01",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Broker adapters (daily, synchronous)
# --------------------------------------------------------------------------- #
class LedgerBroker:
    """Zero-key daily broker: the order/fill ledger *is* the account.

    Positions and cash are always rebuilt from recorded fills, so the database
    cannot drift from itself; reconciliation later fills submitted orders at
    the next session's open with the configured cost model.
    """

    name = "ledger"

    def __init__(self, db: Session, initial_capital: float) -> None:
        self.db = db
        self.initial_capital = float(initial_capital)

    def positions(self) -> dict[str, float]:
        # Any recorded fill counts, whatever the order's final state — a
        # partially-filled-then-cancelled order still moved shares.
        rows = (
            self.db.query(ProductionOrder)
            .filter(ProductionOrder.fill_qty > 0.0)
            .all()
        )
        out: dict[str, float] = {}
        for row in rows:
            signed = float(row.fill_qty) if row.side == "buy" else -float(row.fill_qty)
            out[row.symbol] = out.get(row.symbol, 0.0) + signed
        return {sym: qty for sym, qty in out.items() if abs(qty) > QTY_TOLERANCE}

    def cash(self) -> float:
        rows = (
            self.db.query(ProductionOrder)
            .filter(ProductionOrder.fill_qty > 0.0)
            .all()
        )
        cash = self.initial_capital
        for row in rows:
            notional = float(row.fill_qty) * float(row.fill_price or 0.0)
            cash += -notional if row.side == "buy" else notional
            cash -= float(row.fees or 0.0)
        return cash

    def account(self, marks: Mapping[str, float] | None = None) -> dict[str, Any]:
        marks = marks or {}
        positions = self.positions()
        market_value = sum(qty * float(marks.get(sym, 0.0)) for sym, qty in positions.items())
        cash = self.cash()
        return {"equity": cash + market_value, "cash": cash, "positions": positions}

    def submit(self, order: ProductionOrder) -> None:
        order.status = "submitted"
        order.broker_id = f"ledger-{order.run_id}-{order.symbol}"

    def poll_and_fill(self, orders: list[ProductionOrder], today: date) -> list[str]:
        """Fill submitted orders whose decision date has a later session's open."""
        warnings: list[str] = []
        by_run: dict[int, ProductionRun] = {}
        for order in orders:
            run = by_run.get(order.run_id)
            if run is None:
                run = self.db.get(ProductionRun, order.run_id)
                by_run[order.run_id] = run
            if run is None or not run.as_of:
                warnings.append(f"{order.symbol}: order {order.id} has no run record")
                continue
            decision = date.fromisoformat(run.as_of)
            if decision >= today:
                continue  # execution session has not arrived yet
            open_price = self._next_open(order.symbol, decision)
            if open_price is None:
                warnings.append(f"{order.symbol}: no session open after {run.as_of} yet")
                continue
            config = dict(run.config or {})
            side = 1.0 if order.side == "buy" else -1.0
            slip = float(config.get("slippage_bps", 2.0)) / 1e4
            fill_price = open_price * (1.0 + side * slip)
            fees = abs(order.qty) * fill_price * float(config.get("commission_bps", 1.0)) / 1e4
            order.fill_qty = float(order.qty)
            order.fill_price = round(float(fill_price), 6)
            order.fees = round(float(fees), 6)
            order.filled_at = _utcnow_iso()
            order.status = "filled"
        return warnings

    def _next_open(self, symbol: str, decision: date) -> float | None:
        try:
            hist = get_history(symbol, decision.isoformat())
        except Exception:  # noqa: BLE001 - reported as a reconciliation warning
            return None
        idx = pd.DatetimeIndex(pd.to_datetime(hist.index))
        after = idx[idx > pd.Timestamp(decision)]
        if after.empty or "open" not in hist:
            return None
        value = hist.loc[after[0], "open"]
        return None if pd.isna(value) else float(value)


class AlpacaDailyBroker:
    """Daily-order adapter for Alpaca's paper account. Paper host only."""

    name = "alpaca"

    def __init__(self) -> None:
        base = settings.alpaca_paper_base.rstrip("/")
        if (urlparse(base).hostname or "") != PAPER_HOST:
            raise ValueError(
                f"Refusing to trade against {base!r}: only {PAPER_HOST} is allowed."
            )
        if not (settings.alpaca_api_key and settings.alpaca_api_secret):
            raise ValueError(
                "Alpaca execution needs MFT_ALPACA_API_KEY and MFT_ALPACA_API_SECRET "
                "(paper-account keys)."
            )
        self.base = base
        self._headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
        }

    def _request(self, method: str, path: str, json_body: Any = None) -> Any:
        import httpx

        resp = httpx.request(
            method, self.base + path, headers=self._headers, json=json_body, timeout=20.0
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"alpaca {method} {path} -> HTTP {resp.status_code}")
        return resp.json() if resp.content else None

    def positions(self) -> dict[str, float]:
        rows = self._request("GET", "/v2/positions") or []
        return {str(p["symbol"]).upper(): float(p.get("qty", 0.0)) for p in rows}

    def account(self, marks: Mapping[str, float] | None = None) -> dict[str, Any]:
        acct = self._request("GET", "/v2/account") or {}
        return {
            "equity": float(acct.get("equity", 0.0)),
            "cash": float(acct.get("cash", 0.0)),
            "positions": self.positions(),
        }

    def submit(self, order: ProductionOrder) -> None:
        body = {
            "symbol": order.symbol,
            "qty": str(abs(order.qty)),
            "side": order.side,
            "type": "limit" if order.limit_price is not None else "market",
            "time_in_force": "day",
        }
        if order.limit_price is not None:
            body["limit_price"] = str(round(float(order.limit_price), 2))
        resp = self._request("POST", "/v2/orders", body)
        order.broker_id = str(resp["id"])
        order.status = "submitted"

    def poll_and_fill(self, orders: list[ProductionOrder], today: date) -> list[str]:
        warnings: list[str] = []
        for order in orders:
            if not order.broker_id:
                continue
            try:
                resp = self._request("GET", f"/v2/orders/{order.broker_id}")
            except Exception as exc:  # noqa: BLE001 - poll again next cycle
                warnings.append(f"{order.symbol}: poll failed: {exc}")
                continue
            status = str(resp.get("status", ""))
            filled_qty = float(resp.get("filled_qty") or 0.0)
            if status == "filled":
                order.fill_qty = filled_qty or float(order.qty)
                order.fill_price = float(resp.get("filled_avg_price") or 0.0)
                order.filled_at = str(resp.get("filled_at") or _utcnow_iso())
                order.status = "filled"
            elif status in ("canceled", "expired", "done_for_day"):
                # A terminal state can still carry a partial fill — record it,
                # or the ledger's position rebuild diverges from the broker.
                if filled_qty > 0.0:
                    order.fill_qty = filled_qty
                    order.fill_price = float(resp.get("filled_avg_price") or 0.0)
                    order.filled_at = str(resp.get("filled_at") or _utcnow_iso())
                order.status = "cancelled"
                order.reason = f"broker: {status}" + (
                    f" ({filled_qty} of {order.qty} filled)" if filled_qty > 0.0 else ""
                )
            elif status == "rejected":
                order.status = "rejected"
                order.reason = "broker: rejected"
        return warnings


def build_daily_broker(kind: str, db: Session, initial_capital: float) -> Any:
    kind = (kind or "ledger").strip().lower()
    if kind == "ledger":
        return LedgerBroker(db, initial_capital)
    if kind == "alpaca":
        return AlpacaDailyBroker()
    raise ValueError(f"Unknown daily broker {kind!r}: ledger or alpaca")


# --------------------------------------------------------------------------- #
# Research vintage promotion
# --------------------------------------------------------------------------- #
def promote_vintage(
    db: Session,
    *,
    symbols: Iterable[str],
    as_of: str,
    params: Mapping[str, Any],
    report: Mapping[str, Any],
    sleeve_plan: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
    notes: str = "",
    retire_previous: bool = True,
) -> ProductionSignalVintage:
    """Freeze a finished research report into the production registry."""

    blend = list(report.get("recommended_blend") or [])
    if not blend:
        raise ValueError("Refusing to promote a vintage with an empty recommended blend")
    evidence = [
        {
            "name": row.get("name"),
            "family": row.get("family"),
            "source": row.get("source"),
            "status": row.get("status"),
            "validated": row.get("validated"),
            "research_score": row.get("research_score"),
            "selection_score": row.get("selection_score"),
            "primary": row.get("primary"),
            "exclusion_reasons": row.get("exclusion_reasons"),
        }
        for row in report.get("signals", [])
    ]
    if retire_previous:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for old in (
            db.query(ProductionSignalVintage)
            .filter(ProductionSignalVintage.status == "approved")
            .all()
        ):
            old.status = "retired"
            old.retired_at = now
    vintage = ProductionSignalVintage(
        status="approved",
        as_of=str(as_of),
        symbols=",".join(str(s).upper() for s in symbols),
        params=dict(params),
        blend=blend,
        sleeves=list(sleeve_plan.get("sleeves", [])),
        evidence=evidence,
        config={
            **dict(config or {}),
            "validation": report.get("validation"),
            "research_controls_summary": {
                key: value
                for key, value in (report.get("research_controls") or {}).items()
                if key in ("group_neutralization", "false_discovery", "execution")
            },
        },
        notes=notes,
    )
    db.add(vintage)
    db.commit()
    db.refresh(vintage)
    return vintage


def research_and_promote(
    db: Session,
    *,
    symbols: list[str],
    start: str,
    end: str | None = None,
    params: Mapping[str, Any] | None = None,
    research_kwargs: Mapping[str, Any] | None = None,
    sleeve_kwargs: Mapping[str, Any] | None = None,
    notes: str = "",
    prices: pd.DataFrame | None = None,
    features: Any = None,
) -> tuple[ProductionSignalVintage, dict[str, Any]]:
    """Run the full multisource research pass and freeze the result."""

    params = dict(params or {})
    if prices is None:
        prices = get_price_panel(symbols, start, end)
    if prices.empty:
        raise ValueError("No price data for requested symbols/period")
    features = features or build_feature_panels(prices, params=params, db=db)
    built = build_multisource_signal_library(prices, params=params, features=features, db=db)
    if not built.library.components:
        raise ValueError("No signals had point-in-time data in this period")
    execution_panels = build_execution_panels(prices, features.panels)
    report = research_signal_suite(
        prices,
        params=params,
        library=built.library,
        signal_specs=built.specs,
        execution_panels=execution_panels,
        **dict(research_kwargs or {}),
    )
    sleeve_plan = build_alpha_sleeve_plan(
        prices, built.library, report, built.specs, **dict(sleeve_kwargs or {})
    )
    vintage = promote_vintage(
        db,
        symbols=list(prices.columns),
        as_of=report["as_of"],
        params=params,
        report=report,
        sleeve_plan=sleeve_plan,
        config={"research_kwargs": dict(research_kwargs or {}), "sleeve_kwargs": dict(sleeve_kwargs or {})},
        notes=notes,
    )
    return vintage, report


def latest_approved_vintage(db: Session) -> ProductionSignalVintage | None:
    return (
        db.query(ProductionSignalVintage)
        .filter(ProductionSignalVintage.status == "approved")
        .order_by(ProductionSignalVintage.id.desc())
        .first()
    )


# --------------------------------------------------------------------------- #
# The daily cycle
# --------------------------------------------------------------------------- #
def run_daily_cycle(
    db: Session,
    *,
    orders_enabled: bool = False,
    broker_kind: str = "ledger",
    capture_snapshots: bool = True,
    as_of: str | None = None,
    today: date | None = None,
    prices: pd.DataFrame | None = None,
    features: Any = None,
    config: Mapping[str, Any] | None = None,
) -> ProductionRun:
    """Run one deterministic daily production cycle and persist it.

    ``today`` anchors the staleness and fill clocks (tests inject historical
    dates); ``as_of`` truncates the price panel so nothing after the cutoff
    can enter the target. Every stage lands in the run row, pass or fail.
    """

    cfg = {**DEFAULT_CYCLE_CONFIG, **dict(config or {})}
    today = today or date.today()
    stages: list[dict[str, Any]] = []
    gateway: list[dict[str, Any]] = []

    def stage(name: str, status: str, detail: str = "") -> None:
        stages.append({"stage": name, "status": status, "detail": detail})

    run = ProductionRun(
        as_of="",
        status="running",
        broker=broker_kind,
        orders_enabled=bool(orders_enabled),
        config=cfg,
    )

    def finish(status: str) -> ProductionRun:
        run.status = status
        run.stages = stages
        run.gateway = gateway
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    # -- 1. research vintage -------------------------------------------------
    vintage = latest_approved_vintage(db)
    if vintage is None:
        stage("vintage", "blocked", "no approved research vintage in the registry")
        return finish("blocked")
    run.vintage_id = vintage.id
    symbols = [s for s in vintage.symbols.split(",") if s]
    stage("vintage", "ok", f"vintage {vintage.id} as_of {vintage.as_of}, {len(symbols)} symbols")

    # -- 2. prices up to the information cutoff -------------------------------
    if prices is None:
        try:
            prices = get_price_panel(symbols, str(cfg["history_start"]), as_of)
        except Exception as exc:  # noqa: BLE001 - a data failure blocks the day
            stage("prices", "blocked", f"price fetch failed: {exc}")
            return finish("blocked")
    prices = prices.sort_index().astype(float)
    if as_of is not None:
        prices = prices.loc[: pd.Timestamp(as_of)]
    if prices.empty or prices.shape[1] < 3:
        stage("prices", "blocked", "not enough price data at the cutoff")
        return finish("blocked")
    decision_dt = prices.index[-1]
    run.as_of = str(decision_dt.date())
    stage("prices", "ok", f"{len(prices)} bars, decision bar {run.as_of}")

    data_age = (today - decision_dt.date()).days
    if data_age > int(cfg["max_data_age_days"]):
        gateway.append({"check": "data_fresh", "passed": False,
                        "detail": f"decision bar is {data_age} days old"})
        stage("gateway", "blocked", "stale market data")
        return finish("blocked")
    gateway.append({"check": "data_fresh", "passed": True, "detail": f"{data_age}d old"})

    vintage_age = (today - date.fromisoformat(vintage.as_of)).days
    if vintage_age > int(cfg["max_vintage_age_days"]):
        gateway.append({"check": "vintage_fresh", "passed": False,
                        "detail": f"vintage is {vintage_age} days old"})
        stage("gateway", "blocked", "stale research vintage")
        return finish("blocked")
    gateway.append({"check": "vintage_fresh", "passed": True, "detail": f"{vintage_age}d old"})

    # -- 3. reconciliation must be clean before new targets -------------------
    broker = build_daily_broker(broker_kind, db, float(cfg["initial_capital"]))
    recon = reconcile(db, broker_kind=broker_kind, today=today, marks=prices.iloc[-1].to_dict())
    if recon["discrepancies"]:
        gateway.append({"check": "reconciliation_clean", "passed": False,
                        "detail": f"{len(recon['discrepancies'])} position mismatches"})
        stage("gateway", "blocked", "ledger and broker positions disagree")
        return finish("blocked")
    gateway.append({"check": "reconciliation_clean", "passed": True,
                    "detail": f"{recon['fills_ingested']} fills ingested"})
    stage("reconcile", "ok", f"{recon['fills_ingested']} fills, 0 discrepancies")

    # -- 4. optional point-in-time snapshot capture ---------------------------
    if capture_snapshots:
        try:
            captured = archive_current_snapshots(symbols, db)
            stage("snapshots", "ok", f"{len(captured['captured'])} snapshots captured")
        except Exception as exc:  # noqa: BLE001 - capture is best-effort
            stage("snapshots", "warn", f"capture failed: {exc}")

    # -- 5. today's signal scores from the frozen vintage ---------------------
    vintage_signals = sorted({
        str(member.get("signal"))
        for sleeve in (vintage.sleeves or [])
        for member in sleeve.get("members", [])
    } | {str(row.get("signal")) for row in (vintage.blend or [])})
    try:
        features = features or build_feature_panels(prices, params=vintage.params, db=db)
        built = build_multisource_signal_library(
            prices, params=vintage.params, features=features, db=db,
            signals=vintage_signals or None,
        )
    except Exception as exc:  # noqa: BLE001 - scoring failure blocks the day
        stage("scores", "blocked", f"signal scoring failed: {exc}")
        return finish("blocked")
    stage("scores", "ok", f"{len(built.library.components)} vintage signals scored")

    # -- 6. sleeve target from the frozen plan --------------------------------
    target, used_sleeves = build_sleeve_target(
        decision_dt,
        built.library.components,
        built.library.beta,
        {"sleeves": vintage.sleeves or []},
        gross_target=float(cfg["gross_target"]),
        min_names=3,
    )
    stage("sleeves", "ok" if used_sleeves else "warn",
          f"{len(used_sleeves)} sleeves active" if used_sleeves else "no sleeve had data; flat target")

    # -- 7. risk model on its own refresh cadence -----------------------------
    prior = (
        db.query(ProductionRun)
        .filter(ProductionRun.status.in_(("recorded", "submitted")),
                ProductionRun.risk_model_as_of.isnot(None))
        .order_by(ProductionRun.id.desc())
        .first()
    )
    model_as_of = decision_dt
    if prior is not None:
        prior_dt = pd.Timestamp(prior.risk_model_as_of)
        age = int((decision_dt - prior_dt).days)
        if 0 <= age < int(cfg["factor_risk_refresh_days"]):
            model_as_of = prior_dt
    factor_model = None
    try:
        factor_model = build_factor_risk_model(
            prices,
            as_of=model_as_of,
            lookback=int(cfg["factor_risk_lookback_days"]),
            min_obs=int(cfg["factor_risk_min_observations"]),
            residual_shrinkage=float(cfg["residual_covariance_shrinkage"]),
        )
        run.risk_model_as_of = str(factor_model.as_of.date())
        stage("risk_model", "ok", f"covariance as of {run.risk_model_as_of}")
    except ValueError as exc:
        stage("risk_model", "blocked", f"factor risk model unavailable: {exc}")
        return finish("blocked")

    # -- 8. borrow / crowding panels ------------------------------------------
    borrow = build_borrow_panels(
        prices,
        features.panels if features is not None else {},
        base_borrow_bps=float(cfg["base_borrow_bps"]),
        crowding_surcharge_bps=float(cfg["crowding_surcharge_bps"]),
        hard_to_borrow_short_float=float(cfg["hard_to_borrow_short_float"]),
        hard_to_borrow_days_to_cover=float(cfg["hard_to_borrow_days_to_cover"]),
    )
    shortable_row = borrow.shortable.reindex(index=[decision_dt], columns=prices.columns).iloc[0]
    crowding_row = borrow.crowding_score.reindex(index=[decision_dt], columns=prices.columns).iloc[0]

    # -- 9. constrained projection --------------------------------------------
    beta_row = built.library.beta.reindex(index=[decision_dt], columns=prices.columns).iloc[0]
    projection_beta = beta_row.copy()
    modeled = set(factor_model.covariance.index)
    projection_beta.loc[[c for c in prices.columns if c not in modeled]] = np.nan
    factor_caps = {
        name: (
            float(cfg["max_market_factor_exposure"]) if name == "MKT"
            else float(cfg["max_style_factor_exposure"])
        )
        for name in factor_model.exposures.columns
    }
    target, constraint_info = project_portfolio_constraints(
        target,
        projection_beta,
        gross_limit=float(cfg["gross_target"]),
        max_name_weight=float(cfg["max_name_weight"]),
        shortable=shortable_row,
        crowding_score=crowding_row,
        crowded_short_threshold=float(cfg["crowded_short_threshold"]),
        max_crowded_short_gross=float(cfg["max_crowded_short_gross"]),
        covariance=factor_model.covariance,
        factor_exposures=factor_model.exposures,
        factor_exposure_caps=factor_caps,
        target_annual_vol=float(cfg["target_annual_volatility"]),
        risk_aversion=float(cfg["covariance_risk_aversion"]),
    )
    if constraint_info.get("status") != "ready":
        stage("projection", "blocked", f"risk projection failed: {constraint_info.get('reason')}")
        run.risk = {"constraints": constraint_info}
        return finish("blocked")
    risk_diag = portfolio_risk_diagnostics(target, factor_model)
    run.risk = {"constraints": constraint_info, "diagnostics": risk_diag}
    run.target = {sym: round(float(w), 8) for sym, w in target.items() if abs(float(w)) > 1e-10}
    stage("projection", "ok",
          f"gross {constraint_info['gross_exposure']:.2f}, "
          f"pred vol {risk_diag['predicted_annual_volatility']:.4f}")

    # -- 10. NAV and the order diff -------------------------------------------
    marks = prices.iloc[-1].to_dict()
    try:
        account = broker.account(marks)
    except Exception as exc:  # noqa: BLE001 - no account, no orders
        stage("account", "blocked", f"broker account unavailable: {exc}")
        return finish("blocked")
    nav = float(account["equity"])
    run.nav = round(nav, 2)
    if nav <= 0:
        stage("account", "blocked", "non-positive account equity")
        return finish("blocked")
    held = account["positions"]
    stage("account", "ok", f"nav {nav:,.0f}, {len(held)} open positions")

    adv_row = build_execution_panels(prices, features.panels if features is not None else {}) \
        .adv_dollars.reindex(index=[decision_dt], columns=prices.columns).iloc[0]

    orders, order_notes = _target_to_orders(
        target=target,
        held=held,
        marks=marks,
        nav=nav,
        adv_dollars=adv_row,
        shortable=shortable_row,
        cfg=cfg,
    )
    for note in order_notes:
        stage("orders", "warn", note)
    stage("orders", "ok", f"{len(orders)} orders planned")

    # -- 11. the hard gateway --------------------------------------------------
    submit_allowed = True

    def check(name: str, passed: bool, detail: str) -> None:
        nonlocal submit_allowed
        gateway.append({"check": name, "passed": bool(passed), "detail": detail})
        if not passed:
            submit_allowed = False

    check("kill_switch", bool(settings.trading_enabled),
          "MFT_TRADING_ENABLED is on" if settings.trading_enabled else "MFT_TRADING_ENABLED is off")
    check("orders_enabled", bool(orders_enabled),
          "orders_enabled requested" if orders_enabled else "record-only run")
    gross = float(target.abs().sum())
    check("gross_exposure", gross <= float(cfg["gross_target"]) + 1e-8, f"{gross:.4f}")
    net = float(target.sum())
    check("net_exposure", abs(net) <= float(cfg["net_exposure_tolerance"]), f"{net:.2e}")
    beta_exp = float(constraint_info.get("beta_exposure", 0.0))
    check("beta_exposure", abs(beta_exp) <= float(cfg["beta_exposure_tolerance"]), f"{beta_exp:.2e}")
    pred_vol = float(risk_diag.get("predicted_annual_volatility", 0.0))
    check("predicted_volatility", pred_vol <= float(cfg["target_annual_volatility"]) + 1e-6,
          f"{pred_vol:.4f}")
    max_name = float(target.abs().max()) if len(target) else 0.0
    check("name_concentration", max_name <= float(cfg["max_name_weight"]) + 1e-8, f"{max_name:.4f}")
    check("order_count", len(orders) <= int(cfg["max_order_count"]), str(len(orders)))
    prev_run = (
        db.query(ProductionRun)
        .filter(
            ProductionRun.nav.isnot(None),
            ProductionRun.id != run.id,
            ProductionRun.broker == broker_kind,  # NAVs from different brokers don't compare
        )
        .order_by(ProductionRun.id.desc())
        .first()
    )
    if prev_run is not None and prev_run.nav:
        loss = (float(prev_run.nav) - nav) / float(prev_run.nav)
        check("daily_loss", loss <= float(cfg["max_daily_loss_fraction"]), f"{loss:.4f}")
    else:
        gateway.append({"check": "daily_loss", "passed": True, "detail": "no prior NAV"})

    # -- 12. persist orders; submit only if every check passed -----------------
    db.add(run)
    db.flush()  # run.id for the order rows
    for spec in orders:
        row = ProductionOrder(run_id=run.id, **spec)
        if submit_allowed:
            try:
                broker.submit(row)
            except Exception as exc:  # noqa: BLE001 - a broker rejection is recorded
                row.status = "rejected"
                row.reason = f"submit failed: {exc}"
        db.add(row)
    stage("submit", "ok" if submit_allowed else "skipped",
          "orders submitted" if submit_allowed else "gateway kept orders as planned")
    return finish("submitted" if submit_allowed else "recorded")


def _target_to_orders(
    *,
    target: pd.Series,
    held: Mapping[str, float],
    marks: Mapping[str, float],
    nav: float,
    adv_dollars: pd.Series,
    shortable: pd.Series,
    cfg: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Diff target weights against held shares and emit next-session orders.

    Capacity is applied as one uniform scale on the whole delta (the same rule
    the walk-forward engine uses) so a binding ADV limit cannot manufacture
    net exposure. Unshortable sells that would open or extend a short are
    dropped and reported.
    """

    notes: list[str] = []
    deltas: dict[str, float] = {}
    for symbol in sorted(set(target.index) | set(held)):
        price = float(marks.get(symbol, 0.0) or 0.0)
        if price <= 0:
            if abs(float(target.get(symbol, 0.0))) > 1e-10:
                notes.append(f"{symbol}: no mark price; skipped")
            elif abs(float(held.get(symbol, 0.0))) > QTY_TOLERANCE:
                # A held name that left the priced universe cannot be closed by
                # this cycle — surface it instead of silently carrying it.
                notes.append(f"{symbol}: held position has no mark price and cannot be traded")
            continue
        desired_shares = float(target.get(symbol, 0.0)) * nav / price
        delta = desired_shares - float(held.get(symbol, 0.0))
        if abs(delta) * price < float(cfg["min_order_notional"]):
            continue
        deltas[symbol] = delta

    # Uniform ADV capacity scale across the whole trade vector.
    scale = 1.0
    for symbol, delta in deltas.items():
        adv = adv_dollars.get(symbol)
        if pd.isna(adv) or float(adv) <= 0:
            continue
        required = abs(delta) * float(marks[symbol])
        cap = float(cfg["max_adv_participation"]) * float(adv)
        if required > cap > 0:
            scale = min(scale, cap / required)
    if scale < 1.0:
        notes.append(f"ADV capacity scaled the trade vector to {scale:.3f}")

    buffer = float(cfg["limit_buffer_bps"]) / 1e4
    orders: list[dict[str, Any]] = []
    for symbol, delta in sorted(deltas.items()):
        delta *= scale
        price = float(marks[symbol])
        if abs(delta) * price < float(cfg["min_order_notional"]):
            continue
        side = "buy" if delta > 0 else "sell"
        ends_short = (float(held.get(symbol, 0.0)) + delta) < -QTY_TOLERANCE
        if side == "sell" and ends_short and not bool(shortable.get(symbol, True)):
            notes.append(f"{symbol}: short unavailable; order dropped")
            continue
        limit = price * (1.0 + buffer) if side == "buy" else price * (1.0 - buffer)
        orders.append({
            "symbol": symbol,
            "side": side,
            "qty": round(abs(delta), 4),
            "limit_price": round(limit, 4),
            "decision_price": round(price, 6),
            "status": "planned",
        })
    return orders, notes


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
def reconcile(
    db: Session,
    *,
    broker_kind: str = "ledger",
    today: date | None = None,
    marks: Mapping[str, float] | None = None,
    initial_capital: float | None = None,
) -> dict[str, Any]:
    """Ingest fills, rebuild ledger positions and compare with the broker.

    The ledger's positions come only from recorded fills; the broker's from
    its API (for the ledger broker the two coincide by construction, which is
    exactly the property that makes any later divergence meaningful).
    """

    today = today or date.today()
    capital = float(initial_capital or DEFAULT_CYCLE_CONFIG["initial_capital"])
    first = db.query(ProductionRun).order_by(ProductionRun.id.asc()).first()
    if first is not None and first.config:
        capital = float(first.config.get("initial_capital", capital))
    broker = build_daily_broker(broker_kind, db, capital)

    open_orders = (
        db.query(ProductionOrder)
        .filter(ProductionOrder.status == "submitted")
        .all()
    )
    warnings = broker.poll_and_fill(open_orders, today)
    fills = [o for o in open_orders if o.status == "filled"]
    db.commit()

    ledger_positions = LedgerBroker(db, capital).positions()
    try:
        broker_positions = broker.positions()
    except Exception as exc:  # noqa: BLE001 - no broker view, no comparison
        broker_positions = None
        warnings.append(f"broker positions unavailable: {exc}")

    tolerance = float(DEFAULT_CYCLE_CONFIG["position_qty_tolerance"])
    discrepancies: list[dict[str, Any]] = []
    if broker_positions is not None:
        for symbol in sorted(set(ledger_positions) | set(broker_positions)):
            ledger_qty = float(ledger_positions.get(symbol, 0.0))
            broker_qty = float(broker_positions.get(symbol, 0.0))
            if abs(ledger_qty - broker_qty) > tolerance:
                discrepancies.append({
                    "symbol": symbol,
                    "ledger_qty": round(ledger_qty, 6),
                    "broker_qty": round(broker_qty, 6),
                })

    as_of = today.isoformat()
    db.query(ProductionPositionSnapshot).filter(
        ProductionPositionSnapshot.as_of == as_of
    ).delete()
    marks = marks or {}
    for source, book in (("ledger", ledger_positions), ("broker", broker_positions or {})):
        for symbol, qty in book.items():
            db.add(ProductionPositionSnapshot(
                as_of=as_of, source=source, symbol=symbol, qty=float(qty),
                price=(None if marks.get(symbol) is None else float(marks[symbol])),
            ))
    db.commit()

    return {
        "as_of": as_of,
        "fills_ingested": len(fills),
        "open_orders_remaining": sum(1 for o in open_orders if o.status == "submitted"),
        "ledger_positions": {k: round(v, 6) for k, v in sorted(ledger_positions.items())},
        "broker_positions": (
            None if broker_positions is None
            else {k: round(v, 6) for k, v in sorted(broker_positions.items())}
        ),
        "discrepancies": discrepancies,
        "warnings": warnings,
    }
