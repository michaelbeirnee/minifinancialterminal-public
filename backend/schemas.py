"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, EmailStr, Field

ORM = {"from_attributes": True}


# --- Auth ---
class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    full_name: Optional[str] = Field(default=None, max_length=128)


class UserOut(BaseModel):
    id: int
    username: str
    email: EmailStr
    full_name: Optional[str] = None
    is_active: bool = True
    is_admin: bool = False
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    login_count: int = 0

    model_config = ORM


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = Field(default=None, max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class SessionOut(BaseModel):
    id: int
    jti: str
    issued_at: datetime
    expires_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    is_active: bool = True

    model_config = ORM


# --- Saved actions ---
class SettingIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: Any = None


class SettingOut(BaseModel):
    key: str
    value: Any = None
    updated_at: Optional[datetime] = None

    model_config = ORM


class SavedCommandCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    command_path: str = Field(min_length=1, max_length=255)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None
    is_favorite: bool = False


class SavedCommandUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    command_path: Optional[str] = Field(default=None, min_length=1, max_length=255)
    parameters: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    is_favorite: Optional[bool] = None


class SavedCommandOut(BaseModel):
    id: int
    name: str
    command_path: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None
    is_favorite: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    run_count: int = 0

    model_config = ORM


class SavedResultCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    command_path: str = Field(min_length=1, max_length=255)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    results: List[Dict[str, Any]] = Field(min_length=1)
    provider: Optional[str] = Field(default=None, max_length=32)
    note: Optional[str] = None


class SavedResultOut(BaseModel):
    """List item — the payload itself is fetched per-id."""

    id: int
    name: str
    command_path: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    provider: Optional[str] = None
    row_count: int = 0
    truncated: bool = False
    note: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ORM


class SavedResultFull(SavedResultOut):
    results: List[Dict[str, Any]] = Field(default_factory=list)


class CommandRunOut(BaseModel):
    id: int
    command_path: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    provider: Optional[str] = None
    status: str = "ok"
    row_count: Optional[int] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ORM


class WatchlistItemIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    asset_type: str = Field(default="equity", max_length=16)
    note: Optional[str] = None


class WatchlistItemOut(BaseModel):
    id: int
    symbol: str
    asset_type: str = "equity"
    note: Optional[str] = None
    position: int = 0
    added_at: Optional[datetime] = None

    model_config = ORM


class WatchlistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: Optional[str] = None
    is_default: bool = False
    symbols: List[str] = Field(default_factory=list)


class WatchlistUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = None
    is_default: Optional[bool] = None


class WatchlistOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    is_default: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    items: List[WatchlistItemOut] = Field(default_factory=list)

    model_config = ORM


ALERT_CONDITIONS = ("price_above", "price_below", "pct_change_above", "pct_change_below")


class AlertCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    condition: str = Field(default="price_above",
                           pattern="^(price_above|price_below|pct_change_above|pct_change_below)$")
    threshold: float
    note: Optional[str] = None
    is_active: bool = True


class AlertUpdate(BaseModel):
    condition: Optional[str] = Field(
        default=None, pattern="^(price_above|price_below|pct_change_above|pct_change_below)$"
    )
    threshold: Optional[float] = None
    note: Optional[str] = None
    is_active: Optional[bool] = None


class AlertOut(BaseModel):
    id: int
    symbol: str
    condition: str
    threshold: float
    note: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    last_triggered_at: Optional[datetime] = None
    last_value: Optional[float] = None
    trigger_count: int = 0

    model_config = ORM


# --- Calendar (the user's own "Custom / Notes" events) ---
#: The calendar grid keys on plain dates, so the API takes and returns them as
#: ``YYYY-MM-DD`` strings — no timezone can shift a note onto the wrong day.
EVENT_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


class CalendarEventCreate(BaseModel):
    event_date: str = Field(pattern=EVENT_DATE_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    symbol: Optional[str] = Field(default=None, max_length=32)
    detail: Optional[str] = None
    time: Optional[str] = Field(default=None, max_length=24)
    importance: int = Field(default=2, ge=1, le=3)


class CalendarEventUpdate(BaseModel):
    event_date: Optional[str] = Field(default=None, pattern=EVENT_DATE_PATTERN)
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    symbol: Optional[str] = Field(default=None, max_length=32)
    detail: Optional[str] = None
    time: Optional[str] = Field(default=None, max_length=24)
    importance: Optional[int] = Field(default=None, ge=1, le=3)


class CalendarEventOut(BaseModel):
    id: int
    event_date: str
    title: str
    symbol: Optional[str] = None
    detail: Optional[str] = None
    time: Optional[str] = None
    importance: int = 2
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ORM


# --- Portfolios ---
COST_BASIS_PATTERN = "^(fifo|average)$"
SIDE_PATTERN = "^(buy|sell|deposit|withdraw|dividend|fee|interest)$"


class PortfolioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: Optional[str] = None
    base_currency: str = Field(default="USD", max_length=8)
    cost_basis_method: str = Field(default="fifo", pattern=COST_BASIS_PATTERN)
    benchmark: str = Field(default="SPY", max_length=32)
    is_default: bool = False
    #: Optional opening cash, booked as the first deposit.
    initial_cash: float = Field(default=0.0, ge=0)


class PortfolioUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = None
    base_currency: Optional[str] = Field(default=None, max_length=8)
    cost_basis_method: Optional[str] = Field(default=None, pattern=COST_BASIS_PATTERN)
    benchmark: Optional[str] = Field(default=None, max_length=32)
    is_default: Optional[bool] = None


class PortfolioOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    base_currency: str = "USD"
    cost_basis_method: str = "fifo"
    benchmark: str = "SPY"
    is_default: bool = False
    cash: float = 0.0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ORM


class PositionOut(BaseModel):
    id: int
    symbol: str
    asset_type: str = "equity"
    quantity: float = 0.0
    avg_cost: float = 0.0
    cost_basis: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    dividends: float = 0.0
    lots: List[Dict[str, Any]] = Field(default_factory=list)
    first_trade_at: Optional[datetime] = None
    last_trade_at: Optional[datetime] = None

    model_config = ORM


class TransactionCreate(BaseModel):
    side: str = Field(default="buy", pattern=SIDE_PATTERN)
    symbol: Optional[str] = Field(default=None, max_length=32)
    quantity: float = Field(gt=0)
    price: float = Field(default=1.0, ge=0)
    fees: float = Field(default=0.0, ge=0)
    asset_type: str = Field(default="equity", max_length=16)
    trade_date: Optional[datetime] = None
    note: Optional[str] = None


class TransactionUpdate(BaseModel):
    side: Optional[str] = Field(default=None, pattern=SIDE_PATTERN)
    symbol: Optional[str] = Field(default=None, max_length=32)
    quantity: Optional[float] = Field(default=None, gt=0)
    price: Optional[float] = Field(default=None, ge=0)
    fees: Optional[float] = Field(default=None, ge=0)
    asset_type: Optional[str] = Field(default=None, max_length=16)
    trade_date: Optional[datetime] = None
    note: Optional[str] = None


class TransactionOut(BaseModel):
    id: int
    side: str
    symbol: Optional[str] = None
    quantity: float
    price: float = 1.0
    fees: float = 0.0
    asset_type: str = "equity"
    trade_date: Optional[datetime] = None
    note: Optional[str] = None
    cash_flow: float = 0.0
    created_at: Optional[datetime] = None

    model_config = ORM


# --- Data ---
class HistoryRequest(BaseModel):
    symbol: str
    start: str = "2022-01-01"
    end: Optional[str] = None


# --- Factors ---
class FactorRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=25)
    start: str = "2022-01-01"
    end: Optional[str] = None
    rf_annual: float = 0.0


# --- Backtest ---
class BacktestRequest(BaseModel):
    strategy: str = "sma_crossover"
    symbols: list[str] = Field(min_length=1, max_length=25)
    start: str = "2022-01-01"
    end: Optional[str] = None
    engine: str = Field(default="vectorized", pattern="^(vectorized|event_driven)$")
    params: dict = Field(default_factory=dict)
    commission_bps: float = 1.0
    slippage_bps: float = 2.0
    initial_capital: float = 100_000.0
    benchmark: Optional[str] = "SPY"
    # Position-sizing overlays (see backend.backtest.sizing).
    vol_target: Optional[float] = Field(default=None, gt=0, le=1.0)  # annualized, e.g. 0.15
    vol_lookback: int = Field(default=20, ge=5, le=252)
    max_leverage: float = Field(default=2.0, gt=0, le=5.0)
    stop_loss: Optional[float] = Field(default=None, gt=0, lt=1.0)  # fraction, e.g. 0.10
    trailing_stop: bool = True
    # Attach a block-bootstrap Monte Carlo section to the response.
    monte_carlo: bool = False


class BacktestSweepRequest(BaseModel):
    strategy: str = "sma_crossover"
    symbols: list[str] = Field(min_length=1, max_length=25)
    start: str = "2022-01-01"
    end: Optional[str] = None
    param_grid: dict[str, list] = Field(default_factory=dict)
    metric: str = Field(default="sharpe", pattern="^(sharpe|sortino|total_return|cagr|calmar)$")
    commission_bps: float = 1.0
    slippage_bps: float = 2.0
    initial_capital: float = 100_000.0


class WalkForwardRequest(BaseModel):
    strategy: str = "sma_crossover"
    symbols: list[str] = Field(min_length=1, max_length=25)
    start: str = "2020-01-01"
    end: Optional[str] = None
    params: dict = Field(default_factory=dict)
    param_grid: Optional[dict[str, list]] = None  # re-fit each fold when given
    train_days: int = Field(default=252, ge=60, le=1260)
    test_days: int = Field(default=63, ge=10, le=252)
    purge_days: int = Field(default=5, ge=0, le=63)
    metric: str = Field(default="sharpe", pattern="^(sharpe|sortino|total_return|cagr|calmar)$")
    commission_bps: float = 1.0
    slippage_bps: float = 2.0
    initial_capital: float = 100_000.0


class CostSensitivityRequest(BaseModel):
    strategy: str = "sma_crossover"
    symbols: list[str] = Field(min_length=1, max_length=25)
    start: str = "2022-01-01"
    end: Optional[str] = None
    params: dict = Field(default_factory=dict)
    multipliers: list[float] = Field(default=[0.0, 0.5, 1.0, 2.0, 4.0], max_length=12)
    commission_bps: float = 1.0
    slippage_bps: float = 2.0
    initial_capital: float = 100_000.0


class BacktestRunOut(BaseModel):
    id: int
    strategy: str
    symbols: str
    start: str
    end: str
    sharpe: Optional[float]
    total_return: Optional[float]
    created_at: Optional[date] = None

    model_config = {"from_attributes": True}


# --- Assistant ---
class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=16000)


class ChatRequest(BaseModel):
    messages: List[ChatMessage] = Field(min_length=1, max_length=80)


# --- Theses ---
THESIS_COMPARATOR_PATTERN = "^(lt|le|gt|ge|eq|ne)$"


class ThesisCheckCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    command_path: str = Field(min_length=1, max_length=255)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    field: str = Field(min_length=1, max_length=64)
    comparator: str = Field(pattern=THESIS_COMPARATOR_PATTERN)
    threshold: float
    by_date: Optional[datetime] = None
    note: Optional[str] = None


class ThesisCheckOut(BaseModel):
    id: int
    name: str
    command_path: str
    parameters: Dict[str, Any] = {}
    field: str
    comparator: str
    threshold: float
    by_date: Optional[datetime] = None
    status: str = "holding"
    last_value: Optional[float] = None
    last_error: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    breached_at: Optional[datetime] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ORM


class ThesisEvidenceCreate(BaseModel):
    """Snapshot a command's current output as evidence for one leg."""

    leg: Optional[str] = Field(default=None, max_length=128)
    command_path: str = Field(min_length=1, max_length=255)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    note: Optional[str] = None
    max_rows: int = Field(default=100, ge=1, le=1000)


class ThesisEvidenceOut(BaseModel):
    id: int
    leg: Optional[str] = None
    command_path: str
    parameters: Dict[str, Any] = {}
    provider: Optional[str] = None
    row_count: int = 0
    truncated: bool = False
    results: Any = None
    note: Optional[str] = None
    as_of: Optional[datetime] = None

    model_config = ORM


class ThesisCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    claim: str = Field(min_length=1)
    symbols: str = Field(default="", max_length=255)
    direction: str = Field(default="long", pattern="^(long|short|neutral)$")
    source: str = Field(default="manual", max_length=64)
    review_by: Optional[datetime] = None
    prior: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    notes: Optional[str] = None


class ThesisUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    claim: Optional[str] = None
    symbols: Optional[str] = Field(default=None, max_length=255)
    review_by: Optional[datetime] = None
    prior: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    notes: Optional[str] = None
    status: Optional[str] = Field(default=None, pattern="^(open|closed)$")
    outcome_note: Optional[str] = None
    #: Sets or clears ``reviewed_at``: a human taking ownership of a draft, or
    #: putting it back on the queue.
    reviewed: Optional[bool] = None


class ThesisOut(BaseModel):
    id: int
    title: str
    claim: str
    symbols: str = ""
    direction: str = "long"
    source: str = "manual"
    status: str = "open"
    review_by: Optional[datetime] = None
    prior: Optional[float] = None
    notes: Optional[str] = None
    outcome_note: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    #: NULL means no human has reviewed it — the state a deep-dive draft
    #: starts in.
    reviewed_at: Optional[datetime] = None

    model_config = ORM


class ThesisDetailOut(ThesisOut):
    evidence: List[ThesisEvidenceOut] = []
    checks: List[ThesisCheckOut] = []


# --- Hedging ---
class HedgeAnalyzeRequest(BaseModel):
    """What to hedge, over what horizon, with which instruments.

    ``target_reduction_fraction`` is the share of the book's own unhedged
    CVaR to remove — scale-free, so the same request means the same thing to
    a 20k book and a 20m one.
    """

    horizon_days: int = Field(default=21, ge=5, le=250)
    var_level: float = Field(default=0.05, gt=0.0, lt=0.5)
    target_reduction_fraction: float = Field(default=0.5, gt=0.0, le=1.0)
    instruments: List[str] = Field(
        default_factory=lambda: ["protective_put", "put_spread", "short_etf", "collar"]
    )
    #: Liquidity floors a candidate must clear to be ranked.
    min_open_interest: int = Field(default=0, ge=0)
    max_relative_spread: float = Field(default=1.0, gt=0.0)
    #: Annualised borrow + dividend cost of a short ETF hedge.
    short_carry_annual: float = Field(default=0.005, ge=0.0, le=0.5)
    rate: float = Field(default=0.0, ge=0.0, le=0.25)
    div_yield: float = Field(default=0.0, ge=0.0, le=0.25)
    start: Optional[str] = None
    end: Optional[str] = None
    benchmark: Optional[str] = None
    #: Vol index driving the IV dimension of the shocks. Null freezes IV,
    #: which understates option protection — the response says so.
    vol_symbol: Optional[str] = "^VIX"


class HedgeContract(BaseModel):
    """One listed contract to simulate, instead of engine-picked strikes."""

    expiration: str
    strike: float = Field(gt=0.0)
    option_type: str = Field(pattern="^(call|put)$")
    #: Null solves the count against the target; a number pins it. A call is
    #: additionally capped at the contracts the position covers.
    contracts: Optional[int] = Field(default=None, ge=1, le=1000)


class HedgeSimulateRequest(BaseModel):
    """Hedge one name, sized in dollars, against its own return history.

    The portfolio request asks what protects a diversified book against its
    benchmark. This asks the single-name question, so there is no benchmark
    and no linear hedge: shorting the name against itself is a sale, not a
    hedge, and the verdict already has a word for that.
    """

    symbol: str = Field(min_length=1, max_length=16)
    #: Dollars of the name to protect. Floors to whole shares, then to whole
    #: contracts; the response reports both roundings.
    notional: float = Field(default=25_000.0, gt=0.0, le=1e10)
    horizon_days: int = Field(default=21, ge=5, le=250)
    var_level: float = Field(default=0.05, gt=0.0, lt=0.5)
    target_reduction_fraction: float = Field(default=0.5, gt=0.0, le=1.0)
    instruments: List[str] = Field(
        default_factory=lambda: ["protective_put", "put_spread", "collar"]
    )
    min_open_interest: int = Field(default=0, ge=0)
    max_relative_spread: float = Field(default=1.0, gt=0.0)
    rate: float = Field(default=0.0, ge=0.0, le=0.25)
    div_yield: float = Field(default=0.0, ge=0.0, le=0.25)
    start: Optional[str] = None
    end: Optional[str] = None
    #: Drives the IV dimension. A single name's own IV moves more than the
    #: index in a name-specific shock, so ^VIX makes option protection
    #: conservative rather than flattering — the response says so.
    vol_symbol: Optional[str] = "^VIX"
    #: Set to simulate one contract off the chain rather than the menu.
    contract: Optional[HedgeContract] = None


class HedgeRecordCreate(BaseModel):
    """Commit to a hedge candidate. Displaying one is not a decision."""

    kind: str = Field(min_length=1, max_length=32)
    underlying: str = Field(min_length=1, max_length=32)
    state: str = Field(default="proposed", pattern="^(proposed|accepted|executed)$")
    quantity: Optional[int] = Field(default=None, ge=1)
    notional: Optional[float] = Field(default=None, gt=0.0)
    legs: List[Dict[str, Any]] = Field(default_factory=list)
    quote_snapshot: Dict[str, Any] = Field(default_factory=dict)
    assumptions: Dict[str, Any] = Field(default_factory=dict)
    estimator_version: Optional[str] = Field(default=None, max_length=32)
    target_exposure: Dict[str, Any] = Field(default_factory=dict)
    expected_cvar_reduction: Optional[float] = None
    expected_cvar_reduction_low: Optional[float] = None
    expected_cvar_reduction_high: Optional[float] = None
    cost_bps: Optional[float] = None
    protection_bps: Optional[float] = None
    portfolio_value_at_entry: Optional[float] = None
    entry_cost: Optional[float] = None
    note: Optional[str] = None


class HedgeRecordUpdate(BaseModel):
    """Advance a hedge's state or stamp what it actually returned."""

    state: Optional[str] = Field(
        default=None, pattern="^(proposed|accepted|executed|rolled|closed|expired)$"
    )
    quantity: Optional[int] = Field(default=None, ge=1)
    notional: Optional[float] = Field(default=None, gt=0.0)
    entry_cost: Optional[float] = None
    exit_value: Optional[float] = None
    realised_hedge_pnl: Optional[float] = None
    realised_book_pnl: Optional[float] = None
    note: Optional[str] = None


class HedgeRecordOut(BaseModel):
    id: int
    portfolio_id: int
    state: str
    kind: str
    underlying: str
    quantity: Optional[int] = None
    notional: Optional[float] = None
    legs: List[Dict[str, Any]] = []
    quote_snapshot: Dict[str, Any] = {}
    assumptions: Dict[str, Any] = {}
    estimator_version: Optional[str] = None
    target_exposure: Dict[str, Any] = {}
    expected_cvar_reduction: Optional[float] = None
    expected_cvar_reduction_low: Optional[float] = None
    expected_cvar_reduction_high: Optional[float] = None
    cost_bps: Optional[float] = None
    protection_bps: Optional[float] = None
    portfolio_value_at_entry: Optional[float] = None
    entry_cost: Optional[float] = None
    exit_value: Optional[float] = None
    realised_hedge_pnl: Optional[float] = None
    realised_book_pnl: Optional[float] = None
    note: Optional[str] = None
    proposed_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ORM


# --------------------------------------------------------------------------- #
# Valuation models (Modeling tab)
# --------------------------------------------------------------------------- #
class DCFAssumptions(BaseModel):
    """Every input the operator controls. Rates are decimals: 0.08 is 8%.

    The per-year fields take either one number (held flat across the forecast)
    or one number per year, which is what lets a model fade growth or step a
    margin without needing a different shape of request.
    """

    revenue_base: float = Field(gt=0)
    shares_diluted: float = Field(gt=0)
    years: int = Field(default=5, ge=1, le=20)

    revenue_growth: Union[float, List[float]] = 0.05
    operating_margin: Union[float, List[float]] = 0.20
    tax_rate: float = Field(default=0.21, ge=0, le=0.99)
    depreciation_pct_revenue: Union[float, List[float]] = 0.04
    capex_pct_revenue: Union[float, List[float]] = 0.05
    nwc_pct_revenue_change: Union[float, List[float]] = 0.10

    #: Set this to drive the model directly, or leave it null and set the weights.
    discount_rate: Optional[float] = Field(default=None, gt=0, le=1)
    equity_weight: float = Field(default=1.0, ge=0, le=1)
    cost_of_equity: float = Field(default=0.09, gt=0, le=1)
    cost_of_debt: float = Field(default=0.05, ge=0, le=1)

    terminal_method: Literal["perpetuity", "exit_multiple"] = "perpetuity"
    terminal_growth: float = Field(default=0.025, ge=-0.05, le=0.10)
    exit_multiple: float = Field(default=12.0, gt=0, le=100)

    net_debt: float = 0.0
    mid_year: bool = True


class ValuationRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    assumptions: DCFAssumptions
    #: Adds the discount-rate x terminal grid alongside the single answer.
    sensitivity: bool = True


class ValuationModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)
    assumptions: DCFAssumptions
    note: Optional[str] = None


class ValuationModelUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    assumptions: Optional[DCFAssumptions] = None
    note: Optional[str] = None


class ValuationModelOut(BaseModel):
    id: int
    name: str
    symbol: str
    kind: str = "dcf"
    assumptions: Dict[str, Any] = Field(default_factory=dict)
    value_per_share: Optional[float] = None
    price_at_save: Optional[float] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ORM


class ValuationModelFull(ValuationModelOut):
    valuation: Dict[str, Any] = Field(default_factory=dict)
