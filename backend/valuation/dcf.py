"""Discounted cash flow: the arithmetic, and nothing else.

Pure functions over plain numbers. Nothing here fetches, caches or persists, so
a model can be re-run a few hundred times for a sensitivity grid without going
near the network, and every number on screen can be traced to one line of this
file.

The model is a standard unlevered DCF:

    revenue_t   = revenue_{t-1} × (1 + growth_t)
    EBIT_t      = revenue_t × margin_t
    NOPAT_t     = EBIT_t × (1 − tax)
    FCF_t       = NOPAT_t + D&A_t − capex_t − Δ working capital_t
    EV          = Σ FCF_t / (1+r)^t  +  terminal value / (1+r)^N
    equity      = EV − net debt
    per share   = equity / diluted shares

Two things are worth stating because they are where DCFs quietly go wrong:

* **Working capital is driven by the *change* in revenue, not its level.** A
  business growing 5% ties up a fraction of that 5%; charging a percentage of
  total revenue every year instead would bleed cash forever and is the single
  most common way a spreadsheet under-values a stable company.
* **The terminal value usually *is* the valuation.** It routinely carries 60–80%
  of enterprise value, so the result reports what share of the answer came from
  it. A model where that number is 95% is not a forecast, it is a perpetuity
  with some noise in front of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence

# Growing into a perpetuity only converges while r > g, and gets numerically
# silly well before they meet: at a 0.5% spread a rounding error moves the
# answer by a third.
MIN_SPREAD = 0.005

# How far terminal-year capex may sit above depreciation before the perpetuity
# stops being a steady state. A growing business reinvests somewhat above D&A;
# a multiple of it is a capital programme, and perpetuities do not have those.
TERMINAL_REINVESTMENT = 1.5


class ModelError(ValueError):
    """An assumption set that cannot produce a number."""


def _year_series(value: Any, years: int, name: str) -> List[float]:
    """Accept one number for every year, or one per year."""
    if isinstance(value, (int, float)):
        return [float(value)] * years
    series = [float(v) for v in (value or [])]
    if not series:
        raise ModelError("{} needs a value".format(name))
    if len(series) < years:              # hold the last year flat
        series = series + [series[-1]] * (years - len(series))
    return series[:years]


@dataclass
class Assumptions:
    """Everything the operator controls. Rates are decimals: 0.08 is 8%."""

    revenue_base: float
    shares_diluted: float

    years: int = 5
    revenue_growth: Any = 0.05
    operating_margin: Any = 0.20
    tax_rate: float = 0.21
    depreciation_pct_revenue: Any = 0.04
    capex_pct_revenue: Any = 0.05
    # As a share of the *increase* in revenue — see the module docstring.
    nwc_pct_revenue_change: Any = 0.10

    # Discount rate: either stated outright, or built from the weights below.
    discount_rate: Optional[float] = None
    equity_weight: float = 1.0
    cost_of_equity: float = 0.09
    cost_of_debt: float = 0.05

    terminal_method: str = "perpetuity"      # or "exit_multiple"
    terminal_growth: float = 0.025
    exit_multiple: float = 12.0              # EV / terminal-year EBITDA

    net_debt: float = 0.0
    # Cash flows arrive across the year, not on its last day. Mid-year
    # discounting is the convention; leaving it off is the conservative choice.
    mid_year: bool = True

    def wacc(self) -> float:
        """The discount rate actually used, and where it came from."""
        if self.discount_rate is not None:
            return float(self.discount_rate)
        equity = min(max(float(self.equity_weight), 0.0), 1.0)
        debt = 1.0 - equity
        return equity * float(self.cost_of_equity) + debt * float(self.cost_of_debt) * (
            1.0 - float(self.tax_rate))


@dataclass
class Projection:
    year: int
    revenue: float
    ebit: float
    nopat: float
    depreciation: float
    capex: float
    nwc_change: float
    free_cash_flow: float
    discount_factor: float
    present_value: float


@dataclass
class Valuation:
    projections: List[Projection] = field(default_factory=list)
    discount_rate: float = 0.0
    pv_explicit: float = 0.0
    terminal_value: float = 0.0
    pv_terminal: float = 0.0
    enterprise_value: float = 0.0
    net_debt: float = 0.0
    equity_value: float = 0.0
    value_per_share: float = 0.0
    terminal_share: float = 0.0
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "projections": [vars(p) for p in self.projections],
            "discount_rate": self.discount_rate,
            "pv_explicit": self.pv_explicit,
            "terminal_value": self.terminal_value,
            "pv_terminal": self.pv_terminal,
            "enterprise_value": self.enterprise_value,
            "net_debt": self.net_debt,
            "equity_value": self.equity_value,
            "value_per_share": self.value_per_share,
            "terminal_share": self.terminal_share,
            "warnings": self.warnings,
        }


def value(assumptions: Assumptions) -> Valuation:
    """Run one DCF."""
    a = assumptions
    years = int(a.years)
    if not 1 <= years <= 20:
        raise ModelError("Project between 1 and 20 years, not {}".format(years))
    if a.revenue_base is None or a.revenue_base <= 0:
        raise ModelError("Base revenue must be positive")
    if a.shares_diluted is None or a.shares_diluted <= 0:
        raise ModelError("Diluted share count must be positive")

    growth = _year_series(a.revenue_growth, years, "Revenue growth")
    margin = _year_series(a.operating_margin, years, "Operating margin")
    dep = _year_series(a.depreciation_pct_revenue, years, "Depreciation")
    capex = _year_series(a.capex_pct_revenue, years, "Capital expenditure")
    nwc = _year_series(a.nwc_pct_revenue_change, years, "Working capital")

    rate = a.wacc()
    if rate <= 0:
        raise ModelError("The discount rate must be positive")
    tax = min(max(float(a.tax_rate), 0.0), 0.99)

    out = Valuation(discount_rate=rate, net_debt=float(a.net_debt))
    revenue = float(a.revenue_base)
    for index in range(years):
        prior_revenue = revenue
        revenue = prior_revenue * (1.0 + growth[index])
        ebit = revenue * margin[index]
        nopat = ebit * (1.0 - tax)
        depreciation = revenue * dep[index]
        capital_spend = revenue * capex[index]
        working_capital = (revenue - prior_revenue) * nwc[index]
        fcf = nopat + depreciation - capital_spend - working_capital

        period = (index + 1) - (0.5 if a.mid_year else 0.0)
        factor = 1.0 / ((1.0 + rate) ** period)
        out.projections.append(Projection(
            year=index + 1, revenue=revenue, ebit=ebit, nopat=nopat,
            depreciation=depreciation, capex=capital_spend, nwc_change=working_capital,
            free_cash_flow=fcf, discount_factor=factor, present_value=fcf * factor,
        ))

    out.pv_explicit = sum(p.present_value for p in out.projections)
    final = out.projections[-1]

    if a.terminal_method == "exit_multiple":
        multiple = float(a.exit_multiple)
        if multiple <= 0:
            raise ModelError("The exit multiple must be positive")
        out.terminal_value = (final.ebit + final.depreciation) * multiple
    else:
        g = float(a.terminal_growth)
        if rate - g < MIN_SPREAD:
            raise ModelError(
                "Terminal growth of {:.1%} against a {:.1%} discount rate leaves no room "
                "for the perpetuity to converge — keep them at least {:.1%} apart, or "
                "value the terminal year on an exit multiple instead."
                .format(g, rate, MIN_SPREAD)
            )
        out.terminal_value = final.free_cash_flow * (1.0 + g) / (rate - g)
        if g > rate * 0.6:
            out.warnings.append(
                "Terminal growth of {:.1%} is high against a {:.1%} discount rate; most "
                "of the answer is the perpetuity.".format(g, rate))
        # A perpetuity is a *steady state*. A company growing at g needs capex a
        # little above depreciation to keep its asset base growing — not a
        # multiple of it. Carrying a build-out year's capex into the perpetuity
        # is the commonest way a company mid-investment-cycle values at a
        # fraction of its price, and it is invisible in the output otherwise.
        if final.depreciation > 0 and final.capex > final.depreciation * TERMINAL_REINVESTMENT:
            out.warnings.append(
                "Terminal-year capex is {:.1f}x depreciation, which is a build-out, not a "
                "steady state — the perpetuity assumes it continues forever. Fade capex "
                "toward D&A by the final year if the investment cycle is meant to end."
                .format(final.capex / final.depreciation))

    # The terminal value is a stock at the end of year N, so it discounts over
    # the full N years — never the mid-year period the flows use.
    out.pv_terminal = out.terminal_value / ((1.0 + rate) ** years)
    out.enterprise_value = out.pv_explicit + out.pv_terminal
    out.equity_value = out.enterprise_value - float(a.net_debt)
    out.value_per_share = out.equity_value / float(a.shares_diluted)
    out.terminal_share = (out.pv_terminal / out.enterprise_value
                          if out.enterprise_value else 0.0)

    if out.terminal_share > 0.85:
        out.warnings.append(
            "{:.0%} of the enterprise value is the terminal value — the explicit "
            "forecast is barely doing any work.".format(out.terminal_share))
    if out.equity_value < 0:
        out.warnings.append("Net debt exceeds the enterprise value, so the equity is worthless "
                            "on these assumptions.")
    if any(p.free_cash_flow < 0 for p in out.projections):
        out.warnings.append("Free cash flow is negative in at least one projected year.")
    return out


def sensitivity(assumptions: Assumptions, rates: Sequence[float],
                seconds: Sequence[float]) -> Dict[str, Any]:
    """Per-share value across a grid of discount rate × terminal assumption.

    The second axis is terminal growth under the perpetuity method and the exit
    multiple otherwise, so the grid always varies the two inputs the answer is
    most sensitive to. Cells that cannot be valued come back ``None`` rather
    than failing the whole grid — an r ≤ g corner is a normal thing to ask for
    and a useful thing to see blank.
    """
    grid: List[List[Optional[float]]] = []
    for rate in rates:
        row: List[Optional[float]] = []
        for second in seconds:
            changes: Dict[str, Any] = {"discount_rate": float(rate)}
            if assumptions.terminal_method == "exit_multiple":
                changes["exit_multiple"] = float(second)
            else:
                changes["terminal_growth"] = float(second)
            try:
                row.append(value(replace(assumptions, **changes)).value_per_share)
            except ModelError:
                row.append(None)
        grid.append(row)
    return {
        "discount_rates": [float(r) for r in rates],
        "terminal_axis": "exit_multiple" if assumptions.terminal_method == "exit_multiple"
                         else "terminal_growth",
        "terminal_values": [float(s) for s in seconds],
        "grid": grid,
    }
