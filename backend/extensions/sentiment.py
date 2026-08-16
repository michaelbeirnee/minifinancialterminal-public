"""Sentiment menu: dictionary-scored news sentiment for tickers and the tape.

The scorer is a compact Loughran-McDonald-style lexicon tuned for market
headlines rather than general English: only words that carry a directional
read on a story ("surges", "downgrade", "beats") score, because ordinary
finance vocabulary ("liability", "depreciation") says nothing about tone.

How a score is built, per article (title + summary):

* multi-word phrases match first and consume their tokens — "profit warning"
  is one negative hit, not a positive plus a negative, and "rate cut" scores
  nothing at all rather than letting "cut" read as bad news;
* a negator within the three preceding tokens flips a match ("did not rise");
* score = (positive - negative) / (positive + negative + 1), bounded to
  (-1, 1) — the +1 keeps a one-word headline from reading as maximum
  conviction.

Aggregates weight each article by recency (36-hour half-life) and only over
articles that used directional language, so a tape full of neutral headlines
reads as "no signal" instead of being dragged to zero. Each news source's
total weight is additionally capped (at the equivalent of a few fresh
stories), so one prolific outlet cannot single-handedly set the mood of a
tape merged from many feeds.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..core.caching import TTL_REFERENCE, cached
from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import jsonable, norm_symbols, to_records
from ..providers import newsfeeds
from .news import news_company

import re

# --------------------------------------------------------------------------- #
# Lexicon
# --------------------------------------------------------------------------- #
_POSITIVE = frozenset("""
accelerate accelerated accelerates accelerating advance advanced advances
advancing beat beats blockbuster bolster bolstered bolsters boom booming boost
boosted boosting boosts breakout breakthrough bullish buoyant buyback buybacks
climb climbed climbing climbs comeback confident exceed exceeded exceeds expand
expanded expands expansion favorable favourable gain gained gaining gains grew
grow growing grows growth improve improved improvement improves improving jump
jumped jumping jumps leap milestone optimism optimistic outperform outperformed
outperforms positive profit profitable profits progress rally rallied rallies
rallying rebound rebounded rebounds recover recovered recovering recovery
resilience resilient rise risen rises rising robust rose soar soared soaring
soars stabilise stabilised stabilize stabilized standout stellar strength
strengthen strengthened strengthens strong stronger strongest surge surged
surges surging surpass surpassed surpasses thrive thriving topped tops upbeat
upgrade upgraded upgrades upside win winner winners winning wins won
""".split())

_NEGATIVE = frozenset("""
alarm alarming bankrupt bankruptcy bearish collapse collapsed collapses
collapsing concern concerned concerns contraction correction crash crashed
crashes crisis cut cutback cutbacks cuts cutting decline declined declines
declining deficit disappoint disappointed disappointing disappointment
disappoints dive dived dives downbeat downgrade downgraded downgrades downside
downturn drop dropped dropping drops fail failed failing fails failure fall
fallen falling falls fear fears fell fined fraud investigation lawsuit lawsuits
layoff layoffs lose loses losing loss losses lost lower lowered lowers miss
missed misses negative panic penalties penalty pessimism pessimistic plummet
plummeted plummets plunge plunged plunges plunging probe probes recall recalled
recalls recession selloff shortfall shrank shrink shrinking shrinks sink
sinking sinks sank slash slashed slashes slid slide slides slip slipped slips
slowdown slump slumped slumps struggle struggles struggling sue sued sues
tanked tumble tumbled tumbles turmoil uncertain uncertainty underperform
underperformed underperforms warn warned warning warnings weak weaken weakened
weaker weakness worried worries worry worse worsen worsened worsening worst
writedown
""".split())

# Phrases match before single words and consume their tokens. Polarity 0 means
# "recognise and score nothing" — a rate cut is policy news, not bad news.
_PHRASES: Dict[Tuple[str, ...], int] = {
    ("record", "high"): 1, ("record", "highs"): 1,
    ("all", "time", "high"): 1, ("all", "time", "highs"): 1,
    ("record", "low"): -1, ("record", "lows"): -1,
    ("all", "time", "low"): -1, ("all", "time", "lows"): -1,
    ("better", "than", "expected"): 1, ("worse", "than", "expected"): -1,
    ("raises", "guidance"): 1, ("raised", "guidance"): 1,
    ("raises", "outlook"): 1, ("raised", "outlook"): 1,
    ("raises", "forecast"): 1, ("raised", "forecast"): 1,
    ("raises", "dividend"): 1, ("raised", "dividend"): 1,
    ("dividend", "hike"): 1, ("hikes", "dividend"): 1,
    ("bull", "market"): 1,
    ("profit", "warning"): -1, ("under", "pressure"): -1,
    ("sell", "off"): -1, ("write", "down"): -1,
    ("bear", "market"): -1,
    ("rate", "cut"): 0, ("rate", "cuts"): 0,
    ("rate", "hike"): 0, ("rate", "hikes"): 0,
}
_MAX_PHRASE = max(len(k) for k in _PHRASES)

_NEGATORS = frozenset(
    ["not", "no", "never", "neither", "without", "hardly", "barely", "cannot",
     "fail", "fails", "failed", "failing"]
)
# "won't" tokenises to ("won", "t") — the stem only negates when "t" follows,
# otherwise "won" stays the past tense of win.
_CONTRACTION_STEMS = frozenset(
    ["isn", "aren", "wasn", "weren", "don", "doesn", "didn", "won", "can",
     "couldn", "wouldn", "shouldn", "hasn", "haven", "hadn"]
)

_TOKEN_RE = re.compile(r"[a-z]+")
_NEGATION_WINDOW = 3
_ARTICLE_THRESHOLD = 0.2   # per-article bullish/bearish cut-off
_AGGREGATE_THRESHOLD = 0.1  # aggregates shrink toward zero, so the bar is lower
_HALF_LIFE_HOURS = 36.0
_UNDATED_WEIGHT = 0.25
# One source can never outvote the equivalent of three fresh stories. This is
# a cap on a source's share relative to OTHER sources — when every article
# comes from a single source (Google News per-ticker searches), the uniform
# rescale cancels out of the weighted mean and the score is unchanged.
_MAX_SOURCE_WEIGHT = 3.0


def _is_negator(tokens: List[str], i: int) -> bool:
    tok = tokens[i]
    if tok in _NEGATORS:
        return True
    return tok in _CONTRACTION_STEMS and i + 1 < len(tokens) and tokens[i + 1] == "t"


def _label(score: float, threshold: float) -> str:
    if score >= threshold:
        return "bullish"
    if score <= -threshold:
        return "bearish"
    return "neutral"


def score_text(text: Optional[str]) -> Dict[str, Any]:
    """Score one piece of text. Returns score, label, hit counts and terms."""
    tokens = _TOKEN_RE.findall((text or "").lower())
    pos: List[str] = []
    neg: List[str] = []
    i = 0
    while i < len(tokens):
        matched = False
        for size in range(min(_MAX_PHRASE, len(tokens) - i), 1, -1):
            gram = tuple(tokens[i:i + size])
            if gram in _PHRASES:
                polarity = _PHRASES[gram]
                if polarity > 0:
                    pos.append(" ".join(gram))
                elif polarity < 0:
                    neg.append(" ".join(gram))
                i += size
                matched = True
                break
        if matched:
            continue
        tok = tokens[i]
        if tok in _CONTRACTION_STEMS and i + 1 < len(tokens) and tokens[i + 1] == "t":
            i += 2  # "won't" stem — a negator, never the verb "won"
            continue
        polarity = 1 if tok in _POSITIVE else -1 if tok in _NEGATIVE else 0
        if polarity:
            negated = any(
                _is_negator(tokens, j)
                for j in range(max(0, i - _NEGATION_WINDOW), i)
            )
            if negated:
                polarity = -polarity
                tok = "not " + tok
            (pos if polarity > 0 else neg).append(tok)
        i += 1
    score = (len(pos) - len(neg)) / (len(pos) + len(neg) + 1.0)
    return {
        "score": round(score, 3),
        "label": _label(score, _ARTICLE_THRESHOLD),
        "positive": len(pos),
        "negative": len(neg),
        "pos_terms": ", ".join(dict.fromkeys(pos)),
        "neg_terms": ", ".join(dict.fromkeys(neg)),
    }


# --------------------------------------------------------------------------- #
# Frame scoring + aggregation
# --------------------------------------------------------------------------- #
_KEEP_COLS = ("date", "symbol", "title", "source", "score", "label",
              "pos_terms", "neg_terms", "url", "summary")


def _score_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Score every row of a news frame on its title + summary."""
    df = df.copy().reset_index(drop=True)
    scored = [
        score_text(" . ".join(
            str(row.get(col)) for col in ("title", "summary")
            if isinstance(row.get(col), str)
        ))
        for row in df.to_dict("records")
    ]
    for col in ("score", "label", "pos_terms", "neg_terms"):
        df[col] = [s[col] for s in scored]
    return df[[c for c in _KEEP_COLS if c in df.columns]]


def _weight(ts: Any, now: pd.Timestamp) -> float:
    if ts is None or pd.isna(ts):
        return _UNDATED_WEIGHT
    if not isinstance(ts, pd.Timestamp):
        return _UNDATED_WEIGHT
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    age_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
    return 0.5 ** (age_hours / _HALF_LIFE_HOURS)


def _reading(label: str, bullish: int, bearish: int, scored: int) -> str:
    if not scored:
        return "Stories found, but none used strongly directional language."
    if label == "bullish":
        return "{} of {} scored stories lean positive — the coverage reads upbeat.".format(bullish, scored)
    if label == "bearish":
        return "{} of {} scored stories lean negative — the coverage reads worried.".format(bearish, scored)
    return "Positive and negative stories roughly balance — no strong tilt either way."


def _aggregate(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Recency-weighted mood across scored article records, with each source's
    total weight capped at ``_MAX_SOURCE_WEIGHT`` fresh-story equivalents."""
    now = pd.Timestamp.now(tz="UTC")
    votes: List[Tuple[float, float, str]] = []  # (score, weight, source)
    source_weight: Dict[str, float] = {}
    bullish = bearish = neutral = 0
    for r in records:
        if not (r.get("pos_terms") or r.get("neg_terms")):
            continue  # no directional language — no vote
        if r["label"] == "bullish":
            bullish += 1
        elif r["label"] == "bearish":
            bearish += 1
        else:
            neutral += 1
        w = _weight(r.get("date"), now)
        src = str(r.get("source") or "")
        source_weight[src] = source_weight.get(src, 0.0) + w
        votes.append((r["score"], w, src))
    scale = {
        s: _MAX_SOURCE_WEIGHT / t if t > _MAX_SOURCE_WEIGHT else 1.0
        for s, t in source_weight.items()
    }
    num = den = 0.0
    for score_, w, src in votes:
        w *= scale[src]
        num += w * score_
        den += w
    scored = bullish + bearish + neutral
    score = round(num / den, 3) if den else 0.0
    label = _label(score, _AGGREGATE_THRESHOLD) if scored else "neutral"
    return {
        "score": score,
        "label": label,
        "articles": len(records),
        "scored": scored,
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "no_signal": len(records) - scored,
        "reading": _reading(label, bullish, bearish, scored),
        "as_of": now,
    }


def _drivers(records: Sequence[Dict[str, Any]], top: int = 3) -> Dict[str, Any]:
    """The most strongly-worded stories on each side of the tape."""
    scored = [r for r in records if r.get("pos_terms") or r.get("neg_terms")]
    slim = lambda r: {k: r.get(k) for k in ("title", "source", "url", "date", "score")}  # noqa: E731
    ups = sorted((r for r in scored if r["score"] > 0), key=lambda r: -r["score"])
    downs = sorted((r for r in scored if r["score"] < 0), key=lambda r: r["score"])
    return {
        "bullish": [slim(r) for r in ups[:top]],
        "bearish": [slim(r) for r in downs[:top]],
    }


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@command("/sentiment/market", providers=("rss", "google"),
         summary="Mood of the financial newswire")
def sentiment_market(sources: Optional[str] = None, query: Optional[str] = None,
                     limit: int = 120, provider: Optional[str] = None) -> Result:
    """Pulls the merged world newswire (or a Google News search when ``query``
    is given), scores every story, and attaches the aggregate mood, a
    per-source breakdown and the strongest stories each way under ``extra``.
    The aggregate caps each source's weight so no single outlet, however
    prolific, can dominate a tape merged from ~40 feeds."""
    src = resolve_provider(provider, ("rss", "google"))
    warnings: List[str] = []
    if src == "google" or query:
        frame = newsfeeds.google_news(query or "financial markets", limit)
        src = "google"
    else:
        frame = newsfeeds.world_news(sources, limit)
        warnings = list(frame.attrs.get("errors", []))
    df = _score_frame(frame)
    records = df.to_dict("records")
    by_source: List[Dict[str, Any]] = []
    for name, group in df.groupby("source", sort=False):
        hits = group[(group["pos_terms"] != "") | (group["neg_terms"] != "")]
        if hits.empty:
            continue
        by_source.append({
            "source": name,
            "articles": int(len(group)),
            "scored": int(len(hits)),
            "score": round(float(hits["score"].mean()), 3),
        })
    by_source.sort(key=lambda r: -r["score"])
    extra = jsonable({
        "aggregate": _aggregate(records),
        "by_source": by_source,
        "drivers": _drivers(records),
    })
    return Result(df, provider=src, warnings=warnings, extra=extra)


@command("/sentiment/symbol", providers=("yahoo", "google"),
         summary="Overall news sentiment per ticker")
def sentiment_symbol(symbol: str, limit: int = 25,
                     provider: Optional[str] = None) -> Result:
    """One row per ticker: recency-weighted score, bullish/bearish/neutral
    story counts and a plain-English reading. The scored articles behind each
    summary ride along under ``extra["articles"]``."""
    src = resolve_provider(provider, ("yahoo", "google"))
    warnings: List[str] = []
    rows: List[Dict[str, Any]] = []
    articles: Dict[str, Any] = {}
    for sym in norm_symbols(symbol, limit=8):
        try:
            inner = news_company(symbol=sym, limit=limit, provider=src)
            df = _score_frame(inner.data)
            warnings.extend(inner.warnings)
        except Exception as exc:  # noqa: BLE001 - one dead ticker must not kill the batch
            warnings.append("{}: {}".format(sym, exc))
            df = pd.DataFrame()
        records = df.to_dict("records")
        agg = _aggregate(records)
        rows.append({
            "symbol": sym,
            "score": agg["score"],
            "label": agg["label"],
            "articles": agg["articles"],
            "bullish": agg["bullish"],
            "bearish": agg["bearish"],
            "neutral": agg["neutral"],
            "no_signal": agg["no_signal"],
            "reading": agg["reading"] if records else "No recent stories found.",
        })
        if records:
            articles[sym] = to_records(df)
    return Result(pd.DataFrame(rows), provider=src, warnings=warnings,
                  extra={"articles": articles})


@command("/sentiment/headlines", providers=("yahoo", "google"),
         summary="Sentiment-scored headlines for a ticker")
def sentiment_headlines(symbol: str, limit: int = 25,
                        provider: Optional[str] = None) -> Result:
    """The story-by-story view: every headline with its score, label and the
    exact terms that drove it."""
    src = resolve_provider(provider, ("yahoo", "google"))
    inner = news_company(symbol=symbol, limit=limit, provider=src)
    return Result(_score_frame(inner.data), provider=src, warnings=inner.warnings)


# --------------------------------------------------------------------------- #
# Sector sentiment
# --------------------------------------------------------------------------- #
# Each GICS sector gets a tuned Google News query and its SPDR ETF proxy. The
# ETF matters twice: it prices the signal-quality benchmark, and it lets the
# backtest wizard trade sector sentiment by just picking XLE or XLK.
_SECTORS: Dict[str, Dict[str, str]] = {
    "technology": {"name": "Technology", "etf": "XLK",
                   "query": "technology sector tech stocks"},
    "healthcare": {"name": "Health Care", "etf": "XLV",
                   "query": "healthcare pharma biotech stocks"},
    "financials": {"name": "Financials", "etf": "XLF",
                   "query": "bank stocks financial sector"},
    "energy": {"name": "Energy", "etf": "XLE",
               "query": "oil gas energy stocks"},
    "consumer_discretionary": {"name": "Cons Discretionary", "etf": "XLY",
                               "query": "retail consumer discretionary stocks"},
    "consumer_staples": {"name": "Cons Staples", "etf": "XLP",
                         "query": "consumer staples food beverage stocks"},
    "industrials": {"name": "Industrials", "etf": "XLI",
                    "query": "industrials manufacturing defense stocks"},
    "materials": {"name": "Materials", "etf": "XLB",
                  "query": "mining chemicals materials stocks"},
    "utilities": {"name": "Utilities", "etf": "XLU",
                  "query": "utility stocks electricity power"},
    "real_estate": {"name": "Real Estate", "etf": "XLRE",
                    "query": "real estate REIT housing market stocks"},
    "communication": {"name": "Communication", "etf": "XLC",
                      "query": "media telecom streaming stocks"},
}
_SECTOR_BY_ETF = {v["etf"]: k for k, v in _SECTORS.items()}


def _norm_sector(sector: str) -> str:
    """Accept a sector key, display name or SPDR ETF ticker."""
    raw = str(sector).strip()
    key = raw.lower().replace(" ", "_").replace("-", "_")
    if key in _SECTORS:
        return key
    if raw.upper() in _SECTOR_BY_ETF:
        return _SECTOR_BY_ETF[raw.upper()]
    for k, meta in _SECTORS.items():
        if meta["name"].lower() == raw.lower():
            return k
    raise ValueError(
        "Unknown sector {!r}. Available: {}".format(sector, ", ".join(sorted(_SECTORS)))
    )


def _query_for_symbol(symbol: str) -> str:
    """Sector ETFs get their sector's news query; anything else is a ticker."""
    key = _SECTOR_BY_ETF.get(symbol.upper())
    return _SECTORS[key]["query"] if key else "{} stock".format(symbol.upper())


@command("/sentiment/sectors", providers=("google",),
         summary="News mood across the 11 market sectors")
def sentiment_sectors(sector: Optional[str] = None, limit: int = 25,
                      provider: Optional[str] = None) -> Result:
    """One row per GICS sector: recency-weighted news mood, story counts and a
    plain-English reading, sorted most-bullish first. Pass ``sector`` (key,
    name or SPDR ticker) for just one. The scored stories behind each row ride
    along under ``extra["articles"]``."""
    resolve_provider(provider, ("google",))
    keys = [_norm_sector(sector)] if sector else list(_SECTORS)
    rows: List[Dict[str, Any]] = []
    articles: Dict[str, Any] = {}
    warnings: List[str] = []
    for key in keys:
        meta = _SECTORS[key]
        try:
            df = _score_frame(newsfeeds.google_news(meta["query"], limit))
        except Exception as exc:  # noqa: BLE001 - one quiet sector must not kill the board
            warnings.append("{}: {}".format(key, exc))
            continue
        records = df.to_dict("records")
        agg = _aggregate(records)
        rows.append({
            "sector": meta["name"],
            "key": key,
            "etf": meta["etf"],
            "score": agg["score"],
            "label": agg["label"],
            "articles": agg["articles"],
            "bullish": agg["bullish"],
            "bearish": agg["bearish"],
            "neutral": agg["neutral"],
            "reading": agg["reading"],
        })
        articles[key] = to_records(df)
    if not rows:
        raise EmptyDataError("No sector news could be fetched. {}".format("; ".join(warnings)))
    rows.sort(key=lambda r: r["score"], reverse=True)
    return Result(pd.DataFrame(rows), provider="google", warnings=warnings,
                  extra={"articles": articles})


# --------------------------------------------------------------------------- #
# Historical sentiment
# --------------------------------------------------------------------------- #
# Live RSS only reaches back a few days, but Google News search accepts
# after:/before: date operators, so past sentiment can be rebuilt window by
# window. Completed windows never change, so each one is scored once and
# cached for a week — the first build of a year is slow, reruns are instant.
_MAX_BUCKETS = 80


def _bucket_pairs(start: pd.Timestamp, end: pd.Timestamp,
                  freq: str) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    """Consecutive [start, end) windows covering the range — completed only."""
    if freq == "auto":
        freq = "W" if (end - start).days <= 400 else "M"
    if freq not in ("W", "M"):
        raise ValueError("freq must be 'auto', 'W' (weekly) or 'M' (monthly)")
    today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    pairs: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    cur = start.normalize()
    end = end.normalize()
    while cur < end:
        nxt = min((cur + pd.offsets.MonthBegin(1)) if freq == "M"
                  else cur + pd.offsets.Day(7), end)
        if nxt > today:
            break  # the current partial window belongs to the live commands
        pairs.append((cur, nxt))
        cur = nxt
    if len(pairs) > _MAX_BUCKETS:
        raise ValueError(
            "Range spans {} windows (max {}). Use freq='M' or a shorter range.".format(
                len(pairs), _MAX_BUCKETS)
        )
    return pairs


@cached("sentiment.window", ttl=TTL_REFERENCE)
def _window_mood(query: str, after: str, before: str, limit: int = 30) -> Dict[str, Any]:
    """Fetch and score one completed archive window. Cached hard — the past
    does not change."""
    frame = newsfeeds.google_news_window(query, after, before, limit)
    if frame.empty:
        return {"score": None, "articles": 0, "scored": 0,
                "bullish": 0, "bearish": 0, "neutral": 0}
    df = _score_frame(frame)
    hits = df[(df["pos_terms"] != "") | (df["neg_terms"] != "")]
    labels = hits["label"].value_counts()
    return {
        # Plain mean inside a window — recency weighting is for the live tape.
        "score": round(float(hits["score"].mean()), 3) if len(hits) else None,
        "articles": int(len(df)),
        "scored": int(len(hits)),
        "bullish": int(labels.get("bullish", 0)),
        "bearish": int(labels.get("bearish", 0)),
        "neutral": int(labels.get("neutral", 0)),
    }


def history_frame(query: str, start: str, end: Optional[str] = None,
                  freq: str = "auto", limit: int = 30) -> pd.DataFrame:
    """Bucketed sentiment history. Rows are stamped with the window END date —
    a week's mood is only knowable once the week is over, which is what keeps
    the backtest free of look-ahead."""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) if end else pd.Timestamp.now(tz="UTC").tz_localize(None)
    rows: List[Dict[str, Any]] = []
    for a, b in _bucket_pairs(start_ts, end_ts, freq):
        mood = _window_mood(query, a.date().isoformat(), b.date().isoformat(), limit)
        rows.append(dict({"date": b}, **mood))
    if not rows:
        raise EmptyDataError(
            "No completed windows in range — widen the dates or wait for the week to close."
        )
    return pd.DataFrame(rows)


def history_series(symbol: str, start: str, end: Optional[str] = None) -> pd.Series:
    """Sentiment scores for a ticker as a date-indexed Series (window ends).
    This is the hook the ``news_sentiment`` backtest strategy trades on.
    Sector ETF tickers (XLE, XLK…) trade their sector's news, not ETF news."""
    df = history_frame(_query_for_symbol(symbol), start, end, freq="auto")
    s = pd.Series(df["score"].values, index=pd.DatetimeIndex(df["date"]), dtype=float)
    return s.dropna().sort_index()


def _history_benchmark(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """Where does the latest window sit against every window before it?"""
    valid = df.dropna(subset=["score"])
    if len(valid) < 3:
        return None
    scores = valid["score"].astype(float)
    latest = float(scores.iloc[-1])
    pct = round(float((scores <= latest).mean() * 100))
    best = valid.loc[scores.idxmax()]
    worst = valid.loc[scores.idxmin()]
    return {
        "latest": latest,
        "latest_date": valid["date"].iloc[-1],
        "percentile": pct,
        "mean": round(float(scores.mean()), 3),
        "stdev": round(float(scores.std()), 3) if len(scores) > 1 else 0.0,
        "best": {"date": best["date"], "score": float(best["score"])},
        "worst": {"date": worst["date"], "score": float(worst["score"])},
        "reading": (
            "The latest window scores {:+.2f} — higher than {}% of the {} windows "
            "in this range.".format(latest, pct, len(valid))
        ),
    }


def _history_signal(df: pd.DataFrame, symbol: str, start: str,
                    end: Optional[str]) -> Optional[Dict[str, Any]]:
    """Did mood predict the next window's return? The one-line answer to
    'was this signal worth anything historically'."""
    valid = df.dropna(subset=["score"]).reset_index(drop=True)
    if len(valid) < 8:
        return None
    try:
        from ..data.provider import get_history
        closes = get_history(symbol, start, end)["close"].sort_index()
    except Exception:  # noqa: BLE001 - benchmark is best-effort garnish
        return None
    px = closes.reindex(pd.DatetimeIndex(valid["date"]), method="ffill")
    nxt_ret = px.shift(-1) / px - 1
    pairs = pd.DataFrame({"score": valid["score"].values, "next": nxt_ret.values}).dropna()
    if len(pairs) < 8:
        return None
    median = pairs["score"].median()
    above = pairs[pairs["score"] > median]["next"]
    below = pairs[pairs["score"] <= median]["next"]
    corr = pairs["score"].corr(pairs["next"])
    return {
        "periods": int(len(pairs)),
        "correlation": round(float(corr), 2) if pd.notna(corr) else None,
        "above_median_next_return": round(float(above.mean()), 4) if len(above) else None,
        "below_median_next_return": round(float(below.mean()), 4) if len(below) else None,
        "reading": (
            "Across {} windows, above-median mood was followed by {:+.1%} on average "
            "the next window, below-median by {:+.1%}.".format(
                len(pairs), float(above.mean() or 0), float(below.mean() or 0))
        ) if len(above) and len(below) else None,
    }


@command("/sentiment/history", providers=("google",),
         summary="Historical news sentiment, bucketed over time")
def sentiment_history(symbol: Optional[str] = None, sector: Optional[str] = None,
                      query: Optional[str] = None,
                      start_date: Optional[str] = None, end_date: Optional[str] = None,
                      freq: str = "auto", limit: int = 30,
                      provider: Optional[str] = None) -> Result:
    """Rebuilds past sentiment from the Google News archive in weekly windows
    (monthly beyond ~13 months, or set ``freq``). Each row is one completed
    window stamped with its END date. Give it a ``symbol``, a ``sector`` (key,
    name or SPDR ticker — priced via the sector ETF) or a free-text ``query``.
    ``extra`` carries two benchmarks: where the latest window ranks against
    the past, and what the stock/ETF did after high-mood vs low-mood windows.
    First build of a long range is slow — every window is a separate archive
    query — then cached for a week."""
    resolve_provider(provider, ("google",))
    signal_symbol: Optional[str] = None
    if sector:
        key = _norm_sector(sector)
        q = _SECTORS[key]["query"]
        signal_symbol = _SECTORS[key]["etf"]
    elif symbol:
        signal_symbol = norm_symbols(symbol, limit=1)[0]
        q = _query_for_symbol(signal_symbol)
    else:
        q = query or "stock market"
    start = start_date or (
        pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.offsets.Day(182)
    ).date().isoformat()
    df = history_frame(q, start, end_date, freq=freq, limit=limit)
    extra: Dict[str, Any] = {"query": q}
    bench = _history_benchmark(df)
    if bench:
        extra["benchmark"] = bench
    if signal_symbol:
        signal = _history_signal(df, signal_symbol, start, end_date)
        if signal:
            extra["signal"] = signal
    quiet = int(df["score"].isna().sum())
    warnings = ["{} of {} windows had no scoreable stories".format(quiet, len(df))] if quiet else []
    return Result(df, provider="google", warnings=warnings, extra=jsonable(extra))
