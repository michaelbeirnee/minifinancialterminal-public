"""Research controls for cross-sectional signal discovery.

These helpers are intentionally independent from any one signal family.  They
answer three questions that become important once a research library grows:

1. Does the edge survive after broad sector/industry effects are removed?
2. Is the apparent significance still credible after testing many ideas?
3. Are several "different" signals really the same predictor in disguise?

The controls are conservative by design.  Group-neutral evidence is an
*additional* hurdle and never replaces a failed raw test.  Redundancy filtering
can remove capital allocation candidates but cannot promote a weak signal.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping

import numpy as np
import pandas as pd
from scipy.stats import t as student_t


def normalize_groups(
    columns: pd.Index,
    groups: Mapping[str, Any] | pd.Series | None,
) -> pd.Series:
    """Return a cleaned symbol -> group series aligned to ``columns``.

    Missing classifications stay missing.  They are not pooled into a fake
    ``unknown`` sector because doing so would neutralize unrelated companies
    against each other.
    """

    out = pd.Series(index=columns, dtype="object")
    if groups is None:
        return out
    source = groups if isinstance(groups, pd.Series) else pd.Series(dict(groups), dtype="object")
    for symbol in columns:
        value = source.get(symbol)
        if value is None or (isinstance(value, float) and np.isnan(value)):
            continue
        text = str(value).strip()
        if text:
            out.loc[symbol] = text
    return out


def group_coverage(columns: pd.Index, groups: Mapping[str, Any] | pd.Series | None) -> float:
    normalized = normalize_groups(columns, groups)
    if len(columns) == 0:
        return 0.0
    return float(normalized.notna().sum() / len(columns))


def _eligible_groups(
    columns: pd.Index,
    groups: Mapping[str, Any] | pd.Series | None,
    min_group_names: int,
) -> list[list[str]]:
    normalized = normalize_groups(columns, groups)
    buckets: dict[str, list[str]] = defaultdict(list)
    for symbol, group in normalized.dropna().items():
        buckets[str(group)].append(str(symbol))
    return [members for members in buckets.values() if len(members) >= int(min_group_names)]


def group_neutral_ic(
    signal: pd.DataFrame,
    future_returns: pd.DataFrame,
    groups: Mapping[str, Any] | pd.Series,
    min_names: int = 3,
    min_group_names: int = 2,
) -> pd.Series:
    """Daily within-group rank IC.

    Signal and forward-return ranks are computed *inside* each group first.
    Concatenating those ranks before the daily correlation removes a common
    group move while retaining stock-level ordering.  Groups without at least
    ``min_group_names`` valid names on a date contribute nothing on that date.
    """

    min_names = max(3, int(min_names))
    min_group_names = max(2, int(min_group_names))
    common_index = signal.index.intersection(future_returns.index)
    common_columns = signal.columns.intersection(future_returns.columns)
    s = signal.loc[common_index, common_columns]
    r = future_returns.loc[common_index, common_columns]

    s_rank = pd.DataFrame(np.nan, index=common_index, columns=common_columns, dtype=float)
    r_rank = pd.DataFrame(np.nan, index=common_index, columns=common_columns, dtype=float)
    eligible_count = pd.Series(0, index=common_index, dtype=int)

    for members in _eligible_groups(common_columns, groups, min_group_names):
        cols = [c for c in members if c in common_columns]
        if len(cols) < min_group_names:
            continue
        valid = s[cols].notna() & r[cols].notna() & np.isfinite(s[cols]) & np.isfinite(r[cols])
        count = valid.sum(axis=1)
        enough = count >= min_group_names
        sr = s[cols].where(valid).rank(axis=1, method="average", pct=True).where(enough, axis=0)
        rr = r[cols].where(valid).rank(axis=1, method="average", pct=True).where(enough, axis=0)
        s_rank.loc[:, cols] = sr
        r_rank.loc[:, cols] = rr
        eligible_count = eligible_count.add(count.where(enough, 0).astype(int), fill_value=0).astype(int)

    ic = s_rank.corrwith(r_rank, axis=1)
    return ic.where(eligible_count >= min_names)


def group_neutral_spread(
    signal: pd.DataFrame,
    future_returns: pd.DataFrame,
    groups: Mapping[str, Any] | pd.Series,
    min_names: int = 5,
    min_group_names: int = 2,
    quantile: float = 0.2,
) -> pd.Series:
    """Equal-group average of within-group top-minus-bottom return spreads."""

    if not 0 < quantile < 0.5:
        raise ValueError("quantile must be between 0 and 0.5")
    min_names = max(3, int(min_names))
    min_group_names = max(2, int(min_group_names))
    common_index = signal.index.intersection(future_returns.index)
    common_columns = signal.columns.intersection(future_returns.columns)
    s = signal.loc[common_index, common_columns]
    r = future_returns.loc[common_index, common_columns]

    spreads: list[pd.Series] = []
    eligible_count = pd.Series(0, index=common_index, dtype=int)
    for members in _eligible_groups(common_columns, groups, min_group_names):
        cols = [c for c in members if c in common_columns]
        if len(cols) < min_group_names:
            continue
        valid = s[cols].notna() & r[cols].notna() & np.isfinite(s[cols]) & np.isfinite(r[cols])
        count = valid.sum(axis=1)
        enough = count >= min_group_names
        order = s[cols].where(valid).rank(axis=1, method="first", ascending=True)
        bucket = np.ceil(count * quantile).clip(lower=1)
        bottom_mask = order.le(bucket, axis=0)
        top_mask = order.gt(count - bucket, axis=0)
        group_spread = r[cols].where(top_mask).mean(axis=1) - r[cols].where(bottom_mask).mean(axis=1)
        spreads.append(group_spread.where(enough))
        eligible_count = eligible_count.add(count.where(enough, 0).astype(int), fill_value=0).astype(int)

    if not spreads:
        return pd.Series(np.nan, index=common_index, dtype=float)
    spread = pd.concat(spreads, axis=1).mean(axis=1, skipna=True)
    return spread.where(eligible_count >= min_names)


def one_sided_positive_p_value(t_stat: float | None, observations: int) -> float | None:
    """P-value for H1: mean IC > 0 using a Student-t reference distribution."""

    if t_stat is None or observations < 2 or not np.isfinite(float(t_stat)):
        return None
    return float(student_t.sf(float(t_stat), df=max(1, int(observations) - 1)))


def benjamini_hochberg(p_values: Mapping[str, float | None]) -> dict[str, float | None]:
    """Benjamini-Hochberg adjusted q-values, preserving missing hypotheses."""

    valid = [(name, float(p)) for name, p in p_values.items() if p is not None and np.isfinite(float(p))]
    out: dict[str, float | None] = {name: None for name in p_values}
    if not valid:
        return out
    valid.sort(key=lambda item: item[1])
    m = len(valid)
    adjusted = [0.0] * m
    running = 1.0
    for i in range(m - 1, -1, -1):
        rank = i + 1
        raw = valid[i][1] * m / rank
        running = min(running, raw)
        adjusted[i] = min(max(running, 0.0), 1.0)
    for (name, _), q in zip(valid, adjusted):
        out[name] = float(q)
    return out


def signal_correlation_matrix(
    components: Mapping[str, pd.DataFrame],
    min_overlap: int = 100,
) -> pd.DataFrame:
    """Correlation of cross-sectional signal scores over date-symbol observations.

    Every component is centered within date before flattening.  That strips any
    accidental daily level shift and makes the matrix describe whether two
    predictors rank *the same stocks* similarly.
    """

    names = list(components)
    if not names:
        return pd.DataFrame(dtype=float)
    series: dict[str, pd.Series] = {}
    for name, frame in components.items():
        centered = frame.sub(frame.mean(axis=1), axis=0)
        # Build the date-symbol index explicitly instead of DataFrame.stack so
        # this remains warning-free across pandas 2.x's old/new stack engines.
        stacked = pd.Series(
            centered.to_numpy().reshape(-1),
            index=pd.MultiIndex.from_product(
                [centered.index, centered.columns], names=["date", "symbol"]
            ),
            dtype=float,
        ).dropna()
        series[name] = stacked
    joined = pd.concat(series, axis=1)
    return joined.corr(min_periods=max(2, int(min_overlap)))


def correlation_clusters(
    correlation: pd.DataFrame,
    threshold: float = 0.8,
) -> list[list[str]]:
    """Connected components under an absolute-correlation redundancy threshold."""

    threshold = float(threshold)
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("redundancy threshold must be between 0 and 1")
    names = list(correlation.columns)
    parent = {name: name for name in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            value = correlation.loc[left, right]
            if pd.notna(value) and abs(float(value)) >= threshold:
                union(left, right)

    buckets: dict[str, list[str]] = defaultdict(list)
    for name in names:
        buckets[find(name)].append(name)
    # Stable output helps API diffs and tests.
    return sorted((sorted(members) for members in buckets.values()), key=lambda members: members[0])


def choose_cluster_representatives(
    clusters: list[list[str]],
    score_by_signal: Mapping[str, float],
    q_by_signal: Mapping[str, float | None],
    eligible_by_signal: Mapping[str, bool],
) -> dict[str, str]:
    """Map every signal to the strongest eligible representative in its cluster."""

    representative: dict[str, str] = {}
    for members in clusters:
        eligible = [name for name in members if bool(eligible_by_signal.get(name, False))]
        pool = eligible or list(members)

        def key(name: str) -> tuple[float, float, str]:
            q = q_by_signal.get(name)
            q_score = -(float(q) if q is not None else 1.0)
            return (float(score_by_signal.get(name, 0.0)), q_score, name)

        rep = max(pool, key=key)
        for name in members:
            representative[name] = rep
    return representative
