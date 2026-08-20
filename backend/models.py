"""ORM models.

The schema covers three things: who the user is (``users``, ``user_sessions``),
what they have saved — settings, named commands, run history, watchlists,
price alerts and backtest runs — and what they hold (``portfolios``,
``positions``, ``transactions``).

Newer tables store structured payloads in a portable ``JSON`` column rather
than hand-serialised text; ``backtest_runs`` predates that and keeps its
``*_json`` text columns so existing databases stay readable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=True
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    backtests: Mapped[List["BacktestRun"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    sessions: Mapped[List["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    settings: Mapped[List["UserSetting"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    saved_commands: Mapped[List["SavedCommand"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    command_runs: Mapped[List["CommandRun"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    saved_results: Mapped[List["SavedResult"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    watchlists: Mapped[List["Watchlist"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    alerts: Mapped[List["Alert"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    calendar_events: Mapped[List["CalendarEvent"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    valuation_models: Mapped[List["ValuationModel"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    portfolios: Mapped[List["Portfolio"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserSession(Base):
    """One row per issued access token.

    A JWT validates itself, which means it cannot be revoked on its own.
    Recording the token id lets ``/api/user/sessions`` list active logins and
    kill one without waiting for it to expire.
    """

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is None:
            return True
        expires = self.expires_at
        if expires.tzinfo is None:  # SQLite hands back naive datetimes
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > _utcnow()


class UserSetting(Base):
    """Per-user key/value preferences (theme, default provider, home symbols…)."""

    __tablename__ = "user_settings"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_settings_user_id_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    user: Mapped[User] = relationship(back_populates="settings")


class SavedCommand(Base):
    """A named, re-runnable platform command with its parameters."""

    __tablename__ = "saved_commands"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_saved_commands_user_id_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    command_path: Mapped[str] = mapped_column(String(255), nullable=False)
    parameters: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="saved_commands")


class CommandRun(Base):
    """Audit trail of executed platform commands.

    Written by the ``/api/v1`` layer on every call, which makes it the source
    for "recent activity", for re-running something from last week, and for
    seeing which provider actually served a request.
    """

    __tablename__ = "command_runs"
    __table_args__ = (Index("ix_command_runs_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    command_path: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    parameters: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    user: Mapped[Optional[User]] = relationship(back_populates="command_runs")


class SavedResult(Base):
    """A snapshot of a command's output, kept as the user saw it.

    Unlike ``saved_commands`` (which re-runs and gets fresh data), this stores
    the rows themselves — point-in-time research you can come back to, view,
    and export after the market has moved on.
    """

    __tablename__ = "saved_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    command_path: Mapped[str] = mapped_column(String(255), nullable=False)
    parameters: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    results: Mapped[Any] = mapped_column(JSON, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    user: Mapped[User] = relationship(back_populates="saved_results")


class Watchlist(Base):
    __tablename__ = "watchlists"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_watchlists_user_id_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    user: Mapped[User] = relationship(back_populates="watchlists")
    items: Mapped[List["WatchlistItem"]] = relationship(
        back_populates="watchlist",
        cascade="all, delete-orphan",
        order_by="WatchlistItem.position",
    )


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("watchlist_id", "symbol", name="uq_watchlist_items_watchlist_id_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), index=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), default="equity", nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    watchlist: Mapped[Watchlist] = relationship(back_populates="items")


class Alert(Base):
    """A saved price condition, evaluated on demand against live quotes."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    condition: Mapped[str] = mapped_column(String(24), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_triggered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trigger_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="alerts")


class CalendarEvent(Base):
    """A dated note the user put on their own calendar.

    The "Custom / Notes" event type. Everything else on the calendar is fetched
    from a public feed on demand and never stored; this is the one type that is
    the user's own writing, so it lives here and is scoped to their account.

    ``event_date`` is a plain ``YYYY-MM-DD`` string rather than a Date column
    because that is what the feeds normalise to and what the calendar grid keys
    on — storing a datetime would invite a timezone shifting a note onto the
    wrong day, which for a calendar is the whole ballgame.
    """

    __tablename__ = "calendar_events"
    __table_args__ = (Index("ix_calendar_events_user_id_event_date", "user_id", "event_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_date: Mapped[str] = mapped_column(String(10), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    symbol: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    time: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    importance: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    user: Mapped[User] = relationship(back_populates="calendar_events")


class ResearchFeatureSnapshot(Base):
    """Point-in-time research features that cannot be honestly backfilled.

    Analyst-estimate tables, option chains, crowding fields and optional borrow
    feeds are captured as dated snapshots. Research code may forward-fill them
    only from ``as_of_date`` onward, never into history before the platform
    observed them.
    """

    __tablename__ = "research_feature_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "as_of_date", "symbol", "family",
            name="uq_research_feature_snapshots_date_symbol_family",
        ),
        Index("ix_research_feature_snapshots_family_date", "family", "as_of_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    as_of_date: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    family: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="yahoo", nullable=False)
    features: Mapped[Any] = mapped_column(JSON, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class ProductionSignalVintage(Base):
    """A frozen, approved research vintage the daily trading job may consume.

    Research promotes into this registry; the live scorer reads only from it.
    The blend/sleeve plan and every gate that produced them are stored verbatim
    so a vintage can be audited later — a new experiment never touches capital
    just because yesterday's backtest looked good.
    """

    __tablename__ = "production_signal_vintages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="approved", index=True, nullable=False)
    as_of: Mapped[str] = mapped_column(String(10), nullable=False)  # research decision date
    symbols: Mapped[str] = mapped_column(Text, nullable=False)      # comma-joined universe
    params: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    blend: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    sleeves: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    evidence: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    config: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ProductionRun(Base):
    """One deterministic daily production cycle: cutoff today, execute tomorrow."""

    __tablename__ = "production_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    as_of: Mapped[str] = mapped_column(String(10), index=True, nullable=False)  # decision bar date
    status: Mapped[str] = mapped_column(String(16), default="recorded", index=True, nullable=False)
    vintage_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("production_signal_vintages.id"), nullable=True
    )
    broker: Mapped[str] = mapped_column(String(16), default="ledger", nullable=False)
    orders_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    nav: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    risk_model_as_of: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    target: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    risk: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    gateway: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    stages: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    config: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class ProductionOrder(Base):
    """The order ledger: planned → submitted → filled, with decision context.

    ``decision_price`` is the close the target was formed on; fills arrive via
    reconciliation. The row keeps enough to later build an empirical cost model
    (decision vs fill price, fees, unfilled quantity).
    """

    __tablename__ = "production_orders"
    __table_args__ = (Index("ix_production_orders_run_symbol", "run_id", "symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("production_runs.id"), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # buy | sell
    qty: Mapped[float] = mapped_column(Float, nullable=False)     # unsigned shares
    limit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    decision_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="planned", index=True, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    broker_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    fill_qty: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fill_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fees: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    filled_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class ProductionPositionSnapshot(Base):
    """Dated position records from both books, so reconciliation has history.

    ``source`` is ``ledger`` (positions rebuilt from recorded fills) or
    ``broker`` (what the venue reports). Broker rows are the source of truth
    for actual holdings; a ledger/broker disagreement blocks the next cycle.
    """

    __tablename__ = "production_position_snapshots"
    __table_args__ = (Index("ix_production_positions_asof_source", "as_of", "source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    as_of: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class BacktestRun(Base):
    """Persisted record of a backtest so users can review past runs / reports."""

    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    symbols: Mapped[str] = mapped_column(String(255), nullable=False)  # CSV
    start: Mapped[str] = mapped_column(String(32))
    end: Mapped[str] = mapped_column(String(32))
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    sharpe: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    owner: Mapped[User] = relationship(back_populates="backtests")


# --------------------------------------------------------------------------- #
# Portfolios
#
# ``transactions`` is the source of truth: every holding, every cost basis and
# every realised gain is derived from the log by
# :mod:`backend.portfolio.accounting`. ``positions`` is a materialised view of
# that derivation — rebuilt in full whenever the log changes — so listing
# holdings is one indexed query rather than a replay of years of trades.
# --------------------------------------------------------------------------- #
COST_BASIS_METHODS = ("fifo", "average")

#: Sides that move shares. Everything else only moves cash.
TRADE_SIDES = ("buy", "sell")
CASH_SIDES = ("deposit", "withdraw", "dividend", "fee", "interest")
TRANSACTION_SIDES = TRADE_SIDES + CASH_SIDES


class Portfolio(Base):
    __tablename__ = "portfolios"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_portfolios_user_id_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    base_currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    #: How a sale is matched against open lots — see ``COST_BASIS_METHODS``.
    cost_basis_method: Mapped[str] = mapped_column(String(16), default="fifo", nullable=False)
    #: What performance is measured against.
    benchmark: Mapped[str] = mapped_column(String(32), default="SPY", nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Cash held in the account, derived from the transaction log on rebuild.
    cash: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    user: Mapped[User] = relationship(back_populates="portfolios")
    positions: Mapped[List["Position"]] = relationship(
        back_populates="portfolio",
        cascade="all, delete-orphan",
        order_by="Position.symbol",
    )
    transactions: Mapped[List["Transaction"]] = relationship(
        back_populates="portfolio",
        cascade="all, delete-orphan",
        order_by="Transaction.trade_date",
    )


class Position(Base):
    """A holding as of the latest transaction — derived, never edited directly.

    ``quantity`` is signed: negative means the account is short. ``avg_cost`` is
    the cost of the lots still open (fees included), so ``quantity * avg_cost``
    is the money currently at risk in the name.
    """

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "symbol", name="uq_positions_portfolio_id_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True, nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), default="equity", nullable=False)
    quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cost_basis: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fees_paid: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    dividends: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Open tax lots, ``[{"quantity": ..., "price": ...}, ...]``, oldest first.
    lots: Mapped[Any] = mapped_column(JSON, default=list)
    first_trade_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_trade_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    portfolio: Mapped[Portfolio] = relationship(back_populates="positions")


class Transaction(Base):
    """One entry in the blotter.

    For ``buy``/``sell`` rows ``quantity`` is shares and ``price`` is per share.
    For the cash sides (``deposit``, ``withdraw``, ``dividend``, ``fee``,
    ``interest``) ``quantity`` carries the cash amount and ``price`` is 1, so
    ``quantity * price`` is the gross value of any row.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_portfolio_id_trade_date", "portfolio_id", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True, nullable=False
    )
    symbol: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    fees: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), default="equity", nullable=False)
    trade_date: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    portfolio: Mapped[Portfolio] = relationship(back_populates="transactions")

    @property
    def gross(self) -> float:
        return float(self.quantity) * float(self.price)

    @property
    def cash_flow(self) -> float:
        """Signed effect on the cash balance."""
        gross = self.gross
        fees = float(self.fees or 0.0)
        if self.side in ("buy", "withdraw", "fee"):
            return -(gross + fees)
        return gross - fees

    @property
    def is_external(self) -> bool:
        """True when money crossed the account boundary.

        Deposits and withdrawals change the balance without being performance:
        the return calculation has to neutralise them.
        """
        return self.side in ("deposit", "withdraw")


# --------------------------------------------------------------------------- #
# Theses
#
# A thesis is a falsifiable claim, not a memo. ``thesis_evidence`` freezes the
# rows the claim was built on (point-in-time, so later drift can't rewrite the
# record), and ``thesis_checks`` are executable falsifiers: each one names a
# platform command, a field and a breaking condition, and evaluation re-runs
# the command and compares. A thesis with no breached check that survives to
# its review date resolved honestly; one with a breached check is broken and
# says exactly which link snapped.
# --------------------------------------------------------------------------- #
THESIS_STATUSES = ("open", "supported", "broken", "expired", "closed")
CHECK_STATUSES = ("holding", "broken", "expired", "error")
CHECK_COMPARATORS = ("lt", "le", "gt", "ge", "eq", "ne")


class Thesis(Base):
    __tablename__ = "theses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The claim in one falsifiable sentence: "X happens by T because M".
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    symbols: Mapped[str] = mapped_column(String(255), default="", nullable=False)  # CSV
    direction: Mapped[str] = mapped_column(String(8), default="long", nullable=False)
    #: Where the idea came from: "manual" or a signal family name.
    source: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    #: The date by which the claim should have played out.
    review_by: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: Author's stated probability the thesis resolves, 0-1 (for calibration).
    prior: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: When a human last looked at this. NULL means nobody has — which is the
    #: state a deep-dive draft is born in, and the only thing separating a
    #: machine's proposal from a claim someone owns. Drafts are graded from
    #: creation regardless: review decides whether a thesis is *yours*, never
    #: whether it counts.
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    evidence: Mapped[List["ThesisEvidence"]] = relationship(
        back_populates="thesis", cascade="all, delete-orphan"
    )
    checks: Mapped[List["ThesisCheck"]] = relationship(
        back_populates="thesis", cascade="all, delete-orphan"
    )


class ThesisEvidence(Base):
    """A frozen snapshot of a command's output at the moment it was cited.

    Unlike ``saved_results`` this is bound to a thesis and immutable by intent:
    the point is to be able to audit, months later, what the claim was actually
    built on rather than what the data says now.
    """

    __tablename__ = "thesis_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thesis_id: Mapped[int] = mapped_column(
        ForeignKey("theses.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Which leg of the claim this supports, free-form ("insider buying").
    leg: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    command_path: Mapped[str] = mapped_column(String(255), nullable=False)
    parameters: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    results: Mapped[Any] = mapped_column(JSON, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    as_of: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    thesis: Mapped[Thesis] = relationship(back_populates="evidence")


class ThesisCheck(Base):
    """An executable falsifier: the condition that, if true, breaks the thesis.

    ``field`` is read from the LAST row of the command's result (the most
    recent observation in a time series). The check breaks when
    ``value <comparator> threshold`` holds — the comparator describes failure,
    not success. A check whose ``by_date`` passes unbreached expires as held.
    """

    __tablename__ = "thesis_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thesis_id: Mapped[int] = mapped_column(
        ForeignKey("theses.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    command_path: Mapped[str] = mapped_column(String(255), nullable=False)
    parameters: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    comparator: Mapped[str] = mapped_column(String(8), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    by_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="holding", nullable=False)
    last_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    breached_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    thesis: Mapped[Thesis] = relationship(back_populates="checks")


# --------------------------------------------------------------------------- #
# Thesis-engine memory
#
# The engine's rule is: everything is recorded, then studied, then learned
# from. ``signal_events`` is the append-mostly log of every scanner firing,
# keyed idempotently so re-scans update rather than duplicate. Outcome columns
# start NULL and are stamped by the grader once each horizon has elapsed —
# never predicted, never backfilled from hindsight. ``triage_records`` and
# ``deepdive_records`` keep every model verdict, including declines, so the
# model's judgment can itself be graded against realised outcomes later.
# --------------------------------------------------------------------------- #
class SignalEvent(Base):
    __tablename__ = "signal_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_signal_events_event_key"),
        Index("ix_signal_events_family_known_on", "family", "known_on"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Idempotency key: family | symbol | anchor date. Re-scans upsert.
    event_key: Mapped[str] = mapped_column(String(120), nullable=False)
    family: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    issuer_cik: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: The date the market could first know — filing date, never trade date.
    known_on: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: What the scanner saw: buyers, values, links — enough to audit later.
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    first_recorded_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    #: Realised excess returns vs the benchmark, stamped by the grader once
    #: each horizon has elapsed. NULL means "not yet gradeable".
    benchmark: Mapped[str] = mapped_column(String(16), default="SPY", nullable=False)
    fwd_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fwd_3m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fwd_6m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fwd_12m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    graded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SignalRun(Base):
    """One scanner execution — the provenance line for a batch of events."""

    __tablename__ = "signal_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    parameters: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    events_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    events_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class TriageRecord(Base):
    """One triage call: the cards the model saw and the verdict it returned."""

    __tablename__ = "triage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    parameters: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    cards: Mapped[Any] = mapped_column(JSON, default=list)
    verdict: Mapped[Any] = mapped_column(JSON, default=dict)
    promoted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ran_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class DeepDiveRecord(Base):
    """One deep dive: candidate in, dossier out — declines included."""

    __tablename__ = "deepdive_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    candidate: Mapped[Any] = mapped_column(JSON, default=dict)
    dossier: Mapped[Any] = mapped_column(JSON, default=dict)
    proceeded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    draft_thesis_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


#: The states a hedge moves through. Unlike ``signal_events`` — which records
#: every candidate a scanner surfaces — this table records *decisions*: a
#: candidate merely displayed in the UI is never written here.
HEDGE_STATES = ("proposed", "accepted", "executed", "rolled", "closed", "expired")

#: States after which the hedge is no longer live and can be graded.
HEDGE_TERMINAL_STATES = ("closed", "expired")


class HedgeRecord(Base):
    """One hedge decision, from proposal through to its realised P&L.

    The point of the table is to answer "was the insurance worth it?" from our
    own record rather than priors, so it freezes what was believed at the time
    — the quote snapshot, the estimator version, the exposure being hedged and
    the CVaR reduction expected (with its confidence interval) — alongside what
    actually happened.
    """

    __tablename__ = "hedge_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True, nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), default="proposed", index=True, nullable=False)

    #: Construction and instrument: "protective_put", "collar", "short_etf"…
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    underlying: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    #: Contracts for option structures; None for a notional-sized linear hedge.
    quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notional: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    #: What was believed when the decision was made — never recomputed.
    legs: Mapped[Any] = mapped_column(JSON, default=list)
    quote_snapshot: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    assumptions: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    estimator_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    target_exposure: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    expected_cvar_reduction: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_cvar_reduction_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_cvar_reduction_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cost_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    protection_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: Book value at decision time — the denominator realised P&L is judged in.
    portfolio_value_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    #: What actually happened, filled in when the hedge is closed out.
    entry_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realised_hedge_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realised_book_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    proposed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class ValuationModel(Base):
    """One saved DCF: the assumptions, and what they were worth when saved.

    The assumptions are the model — they are what the operator actually
    authored, and re-running them against today's statements is the point of
    coming back. The valuation is stored alongside them anyway, frozen, so a
    model opened in six months shows what it said at the time next to what it
    says now. A model whose answer has moved without its assumptions changing
    is telling you something about the business.
    """

    __tablename__ = "valuation_models"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_valuation_models_user_id_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="dcf", nullable=False)

    #: Every input the operator controls — see backend/valuation/dcf.Assumptions.
    assumptions: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    #: The result as saved, and the market it was saved against.
    valuation: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    value_per_share: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_at_save: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    user: Mapped[User] = relationship(back_populates="valuation_models")
