"""Black-Scholes-Merton pricing, Greeks and implied volatility.

Pure math on numpy arrays — no data access, no command logic. The
``/derivatives/options/*`` commands feed it market inputs (spot, the Treasury
curve, Yahoo's dividend yield) and it hands back prices and sensitivities.

Model and conventions
---------------------
European exercise with a continuous dividend yield ``q`` (Merton's extension).
US equity options are American, so for calls on low-dividend names the error
is negligible, for puts and high-dividend calls the model *understates* the
price by the early-exercise premium — a known bias, stated in the docs rather
than half-corrected with a binomial tree here.

Units: volatility and rates are decimals per year (0.25 = 25 %), time is in
years (ACT/365), and Greeks follow the trader's conventions —

* ``delta``  per $1 of spot
* ``gamma``  per $1 of spot, per $1 of spot
* ``theta``  per **calendar day** (annual / 365), almost always negative
* ``vega``   per **vol point** (a 1 percentage-point move in sigma)
* ``rho``    per **1 percentage-point** move in the rate

Implied volatility is solved by bisection on [1e-4, 10]: slower than Newton
in the happy case, but immune to vega vanishing deep in/out of the money, and
it either brackets a root or honestly returns ``nan`` (a quote below the
option's arbitrage floor has no BS volatility at all).
"""
from __future__ import annotations

from typing import Dict, Union

import numpy as np
from scipy.stats import norm

ArrayLike = Union[float, np.ndarray]

#: Solver bounds — 0.01 % to 1000 % annualised. Anything outside is noise.
IV_LO, IV_HI = 1e-4, 10.0
_DAYS = 365.0


def _prep(option_type: ArrayLike) -> np.ndarray:
    """``"call"``/``"put"`` (any case, singular or plural) -> +1 / -1."""
    arr = np.atleast_1d(np.asarray(option_type))
    if arr.dtype.kind in ("U", "S", "O"):
        flags = np.char.startswith(np.char.lower(arr.astype(str)), "c")
        out = np.where(flags, 1.0, -1.0)
        bad = ~np.isin(np.char.lower(arr.astype(str)).astype("U1"), ("c", "p"))
        if bad.any():
            raise ValueError("option_type must be 'call' or 'put'")
        return out
    return np.where(arr >= 0, 1.0, -1.0)


def bs_price(
    s: ArrayLike, k: ArrayLike, t: ArrayLike, sigma: ArrayLike,
    r: ArrayLike = 0.0, q: ArrayLike = 0.0, option_type: ArrayLike = "call",
) -> np.ndarray:
    """Black-Scholes-Merton price. At ``t<=0`` returns intrinsic value."""
    s, k, t, sigma, r, q = np.broadcast_arrays(
        *(np.atleast_1d(np.asarray(x, dtype=float)) for x in (s, k, t, sigma, r, q)))
    cp = np.broadcast_to(_prep(option_type), s.shape)
    price = np.maximum(cp * (s - k), 0.0)  # expired -> intrinsic
    live = (t > 0) & (sigma > 0) & (s > 0) & (k > 0)
    if live.any():
        sl, kl, tl, vl, rl, ql, cpl = (a[live] for a in (s, k, t, sigma, r, q, cp))
        sq = vl * np.sqrt(tl)
        d1 = (np.log(sl / kl) + (rl - ql + 0.5 * vl * vl) * tl) / sq
        d2 = d1 - sq
        price = price.copy()
        price[live] = cpl * (
            sl * np.exp(-ql * tl) * norm.cdf(cpl * d1)
            - kl * np.exp(-rl * tl) * norm.cdf(cpl * d2)
        )
    return price


def bs_greeks(
    s: ArrayLike, k: ArrayLike, t: ArrayLike, sigma: ArrayLike,
    r: ArrayLike = 0.0, q: ArrayLike = 0.0, option_type: ArrayLike = "call",
) -> Dict[str, np.ndarray]:
    """Price and the five Greeks, in the units documented at module top."""
    s, k, t, sigma, r, q = np.broadcast_arrays(
        *(np.atleast_1d(np.asarray(x, dtype=float)) for x in (s, k, t, sigma, r, q)))
    cp = np.broadcast_to(_prep(option_type), s.shape).copy()
    out = {name: np.full(s.shape, np.nan) for name in
           ("price", "delta", "gamma", "theta", "vega", "rho")}
    out["price"] = bs_price(s, k, t, sigma, r, q, cp)

    # Expired options: delta is the indicator of moneyness, everything else 0.
    dead = t <= 0
    if dead.any():
        for name in ("gamma", "theta", "vega", "rho"):
            out[name][dead] = 0.0
        out["delta"][dead] = np.where(cp[dead] * (s[dead] - k[dead]) > 0, cp[dead], 0.0)

    live = (t > 0) & (sigma > 0) & (s > 0) & (k > 0)
    if live.any():
        sl, kl, tl, vl, rl, ql, cpl = (a[live] for a in (s, k, t, sigma, r, q, cp))
        rt = np.sqrt(tl)
        sq = vl * rt
        d1 = (np.log(sl / kl) + (rl - ql + 0.5 * vl * vl) * tl) / sq
        d2 = d1 - sq
        eq, er = np.exp(-ql * tl), np.exp(-rl * tl)
        pdf = norm.pdf(d1)
        out["delta"][live] = cpl * eq * norm.cdf(cpl * d1)
        out["gamma"][live] = eq * pdf / (sl * sq)
        theta_year = (
            -sl * eq * pdf * vl / (2 * rt)
            - cpl * rl * kl * er * norm.cdf(cpl * d2)
            + cpl * ql * sl * eq * norm.cdf(cpl * d1)
        )
        out["theta"][live] = theta_year / _DAYS
        out["vega"][live] = sl * eq * pdf * rt / 100.0
        out["rho"][live] = cpl * kl * tl * er * norm.cdf(cpl * d2) / 100.0
    return out


def implied_vol(
    price: ArrayLike, s: ArrayLike, k: ArrayLike, t: ArrayLike,
    r: ArrayLike = 0.0, q: ArrayLike = 0.0, option_type: ArrayLike = "call",
    tol: float = 1e-6, max_iter: int = 100,
) -> np.ndarray:
    """The sigma at which BS reproduces ``price``; ``nan`` where none exists.

    Vectorised bisection. A price at or below the option's arbitrage floor
    (discounted intrinsic) or above its ceiling has no solution and comes
    back ``nan`` rather than pinned to a bound.
    """
    price, s, k, t, r, q = np.broadcast_arrays(
        *(np.atleast_1d(np.asarray(x, dtype=float)) for x in (price, s, k, t, r, q)))
    cp = np.broadcast_to(_prep(option_type), s.shape)
    lo = np.full(s.shape, IV_LO)
    hi = np.full(s.shape, IV_HI)
    floor = bs_price(s, k, t, lo, r, q, cp)      # ~ the no-vol (intrinsic) value
    ceil = bs_price(s, k, t, hi, r, q, cp)
    ok = (t > 0) & (price > floor + 1e-12) & (price < ceil - 1e-12) & np.isfinite(price)
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        val = bs_price(s, k, t, mid, r, q, cp)
        high = val > price
        hi = np.where(ok & high, mid, hi)
        lo = np.where(ok & ~high, mid, lo)
        if float(np.max(hi - lo)) < tol:
            break
    out = 0.5 * (lo + hi)
    return np.where(ok, out, np.nan)
