"""Sentiment scorer and command tests.

The scorer is pure Python, so most of these run offline; the command test
monkeypatches the newswire provider rather than hitting live RSS.
"""
import pandas as pd
import pytest

from backend.core.registry import REGISTRY, execute
from backend.extensions.sentiment import _aggregate, score_text


# --------------------------------------------------------------------------- #
# score_text
# --------------------------------------------------------------------------- #
def test_positive_headline_reads_bullish():
    s = score_text("Nvidia surges after blowout results beat estimates")
    assert s["score"] > 0
    assert s["label"] == "bullish"
    assert s["positive"] >= 2 and s["negative"] == 0


def test_negative_headline_reads_bearish():
    s = score_text("Bank shares plunge as lawsuit and layoffs mount")
    assert s["score"] < 0
    assert s["label"] == "bearish"


def test_negation_flips_polarity():
    plain = score_text("Shares rose this quarter")
    negated = score_text("Shares did not rise this quarter")
    assert plain["label"] == "bullish"
    assert negated["score"] < 0


def test_phrase_beats_unigrams():
    # "profit warning" must score as one negative hit, not +profit -warning.
    s = score_text("Retailer issues profit warning")
    assert s["label"] == "bearish"
    assert "profit warning" in s["neg_terms"]


def test_rate_cut_is_consumed_not_scored():
    s = score_text("Fed announces rate cut")
    assert s["score"] == 0
    assert s["positive"] == 0 and s["negative"] == 0


def test_no_directional_language_is_neutral():
    s = score_text("Company holds annual shareholder meeting on Tuesday")
    assert s["score"] == 0
    assert s["label"] == "neutral"
    assert not s["pos_terms"] and not s["neg_terms"]


def test_contraction_stem_only_negates_before_t():
    # "won" is the past tense of win unless it is the "won't" stem.
    assert score_text("Company won a major contract")["label"] == "bullish"
    assert score_text("Margins won t improve this year")["score"] < 0


# --------------------------------------------------------------------------- #
# _aggregate
# --------------------------------------------------------------------------- #
def _record(score, label, terms=("x", ""), date=None):
    return {"score": score, "label": label, "pos_terms": terms[0],
            "neg_terms": terms[1], "date": date}


def test_aggregate_ignores_no_signal_articles():
    now = pd.Timestamp.now(tz="UTC")
    rows = [
        _record(0.5, "bullish", ("surge", ""), now),
        _record(0.0, "neutral", ("", ""), now),  # no terms — must not vote
    ]
    agg = _aggregate(rows)
    assert agg["scored"] == 1
    assert agg["no_signal"] == 1
    assert agg["score"] == 0.5
    assert agg["label"] == "bullish"


def test_aggregate_weights_recent_stories_harder():
    now = pd.Timestamp.now(tz="UTC")
    rows = [
        _record(0.8, "bullish", ("surge", ""), now),
        _record(-0.8, "bearish", ("", "plunge"), now - pd.offsets.Day(7)),
    ]
    agg = _aggregate(rows)
    assert agg["score"] > 0  # the week-old bearish story has decayed


def test_aggregate_empty_is_neutral():
    agg = _aggregate([])
    assert agg["label"] == "neutral"
    assert agg["articles"] == 0


def test_aggregate_caps_a_flooding_source():
    # 12 fresh bearish stories from one outlet vs 6 bullish spread across six
    # outlets: uncapped, the flood wins 12-to-6; capped at ~3 stories' weight
    # per source, the six independent voices outvote the single loud one.
    now = pd.Timestamp.now(tz="UTC")
    flood = [dict(_record(-0.8, "bearish", ("", "plunge"), now), source="loudwire")
             for _ in range(12)]
    spread = [dict(_record(0.8, "bullish", ("surge", ""), now), source="src{}".format(i))
              for i in range(6)]
    agg = _aggregate(flood + spread)
    assert agg["score"] > 0
    assert agg["bearish"] == 12  # counts stay raw — only the weight is capped


def test_aggregate_single_source_score_unchanged_by_cap():
    # With every story from one source the rescale is uniform, so it must
    # cancel out of the weighted mean (per-ticker Google News tapes).
    now = pd.Timestamp.now(tz="UTC")
    rows = [dict(_record(s, l, t, now), source="google_news") for s, l, t in [
        (0.5, "bullish", ("surge", "")),
        (0.5, "bullish", ("rally", "")),
        (0.5, "bullish", ("beats", "")),
        (0.5, "bullish", ("soars", "")),
        (-0.3, "bearish", ("", "drop")),
    ]]
    agg = _aggregate(rows)
    expected = round((4 * 0.5 - 0.3) / 5, 3)  # plain mean — equal fresh weights
    assert agg["score"] == expected


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def test_sentiment_commands_registered():
    for path in ("/sentiment/market", "/sentiment/symbol", "/sentiment/headlines"):
        assert path in REGISTRY


def test_market_command_scores_and_aggregates(monkeypatch):
    now = pd.Timestamp.now(tz="UTC")
    fake = pd.DataFrame([
        {"date": now, "title": "Stocks rally to record high", "source": "cnbc_top",
         "summary": "", "url": "http://x/1"},
        {"date": now, "title": "Tech shares tumble on weak outlook", "source": "cnbc_top",
         "summary": "", "url": "http://x/2"},
        {"date": now, "title": "Treasury schedules quarterly refunding", "source": "sec",
         "summary": "", "url": "http://x/3"},
    ])
    from backend.providers import newsfeeds
    monkeypatch.setattr(newsfeeds, "world_news", lambda sources, limit: fake)

    out = execute("/sentiment/market")
    assert len(out.results) == 3
    labels = {r["title"]: r["label"] for r in out.results}
    assert labels["Stocks rally to record high"] == "bullish"
    assert labels["Tech shares tumble on weak outlook"] == "bearish"
    agg = out.extra["aggregate"]
    assert agg["scored"] == 2 and agg["no_signal"] == 1
    assert {s["source"] for s in out.extra["by_source"]} == {"cnbc_top"}
    assert out.extra["drivers"]["bullish"][0]["title"] == "Stocks rally to record high"


# --------------------------------------------------------------------------- #
# Sector sentiment
# --------------------------------------------------------------------------- #
def test_norm_sector_accepts_key_name_and_etf():
    from backend.extensions.sentiment import _norm_sector
    assert _norm_sector("energy") == "energy"
    assert _norm_sector("Health Care") == "healthcare"
    assert _norm_sector("XLE") == "energy"
    assert _norm_sector("real estate") == "real_estate"
    with pytest.raises(ValueError):
        _norm_sector("crypto")


def test_query_for_symbol_maps_sector_etfs():
    from backend.extensions.sentiment import _query_for_symbol
    assert "oil" in _query_for_symbol("XLE")
    assert _query_for_symbol("AAPL") == "AAPL stock"


def test_sectors_command_scores_every_sector(monkeypatch):
    now = pd.Timestamp.now(tz="UTC")

    def fake_news(query, limit):
        # Energy news reads grim, everything else upbeat.
        word = "collapse" if "oil" in query else "rally"
        return pd.DataFrame([
            {"date": now, "title": "Sector stocks {} on outlook".format(word),
             "summary": "", "url": "http://x/1", "source": "google_news"},
        ])

    from backend.providers import newsfeeds
    monkeypatch.setattr(newsfeeds, "google_news", fake_news)

    out = execute("/sentiment/sectors")
    assert len(out.results) == 11
    by_key = {r["key"]: r for r in out.results}
    assert by_key["energy"]["label"] == "bearish"
    assert by_key["technology"]["label"] == "bullish"
    # sorted most-bullish first -> energy sits last
    assert out.results[-1]["key"] == "energy"
    assert set(out.extra["articles"]) == set(by_key)


def test_sectors_command_single_sector_by_etf(monkeypatch):
    now = pd.Timestamp.now(tz="UTC")
    from backend.providers import newsfeeds
    monkeypatch.setattr(newsfeeds, "google_news", lambda q, l: pd.DataFrame([
        {"date": now, "title": "Banks surge after strong results", "summary": "",
         "url": "http://x/1", "source": "google_news"}]))
    out = execute("/sentiment/sectors", sector="XLF")
    assert len(out.results) == 1
    assert out.results[0]["key"] == "financials"


def test_history_series_uses_sector_query_for_etf(monkeypatch):
    import backend.extensions.sentiment as senti
    seen = {}

    def fake_frame(query, start, end=None, freq="auto", limit=30):
        seen["query"] = query
        return pd.DataFrame({"date": [pd.Timestamp("2024-01-08")], "score": [0.2]})

    monkeypatch.setattr(senti, "history_frame", fake_frame)
    senti.history_series("XLK", "2024-01-01")
    assert seen["query"] == senti._SECTORS["technology"]["query"]


# --------------------------------------------------------------------------- #
# Historical sentiment + backtest strategy
# --------------------------------------------------------------------------- #
def test_bucket_pairs_weekly_completed_only():
    from backend.extensions.sentiment import _bucket_pairs
    today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    pairs = _bucket_pairs(today - pd.offsets.Day(28), today + pd.offsets.Day(14), "W")
    assert pairs, "should produce completed windows"
    assert all(b <= today for _, b in pairs)          # nothing in the future
    assert all((b - a).days == 7 for a, b in pairs[:-1])
    # consecutive and non-overlapping
    assert all(pairs[i][1] == pairs[i + 1][0] for i in range(len(pairs) - 1))


def test_bucket_pairs_caps_window_count():
    from backend.extensions.sentiment import _bucket_pairs
    end = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    with pytest.raises(ValueError):
        _bucket_pairs(end - pd.offsets.Day(365 * 3), end, "W")  # 156 weeks > cap


def _patch_window_mood(monkeypatch, scores):
    """Feed deterministic per-window scores into the history builder."""
    import backend.extensions.sentiment as senti
    calls = iter(scores)

    def fake(query, after, before, limit=30):
        s = next(calls)
        return {"score": s, "articles": 10 if s is not None else 0,
                "scored": 8 if s is not None else 0,
                "bullish": 5, "bearish": 2, "neutral": 1}
    monkeypatch.setattr(senti, "_window_mood", fake)


def test_history_command_benchmarks_latest_vs_past(monkeypatch):
    today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    start = (today - pd.offsets.Day(35)).date().isoformat()
    _patch_window_mood(monkeypatch, [-0.2, 0.0, 0.1, 0.3, 0.4])
    out = execute("/sentiment/history", symbol="AAPL", start_date=start, freq="W")
    assert len(out.results) == 5
    bench = out.extra["benchmark"]
    assert bench["latest"] == 0.4
    assert bench["percentile"] == 100  # best window in the range
    assert out.extra["query"] == "AAPL stock"


def test_history_series_drops_quiet_windows(monkeypatch):
    from backend.extensions.sentiment import history_series
    today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    start = (today - pd.offsets.Day(28)).date().isoformat()
    _patch_window_mood(monkeypatch, [0.2, None, -0.1, 0.05])
    s = history_series("AAPL", start)
    assert len(s) == 3  # the None window vanishes instead of polluting the signal
    assert s.index.is_monotonic_increasing


def test_news_sentiment_strategy_longs_only_bullish_weeks(monkeypatch):
    import backend.backtest.strategies as strategies
    idx = pd.date_range("2024-01-01", periods=40, freq="B")
    prices = pd.DataFrame({"AAPL": 100.0, "MSFT": 50.0}, index=idx)

    def fake_series(symbol, start, end=None):
        if symbol == "MSFT":
            raise ValueError("no history")  # MSFT sits out, AAPL still trades
        return pd.Series([0.3, -0.5, 0.3],
                         index=pd.DatetimeIndex(["2024-01-08", "2024-01-22", "2024-02-05"]))

    import backend.extensions.sentiment as senti
    monkeypatch.setattr(senti, "history_series", fake_series)

    w = strategies.news_sentiment(prices, {"threshold": 0.05, "smooth": 1})
    assert (w["MSFT"] == 0).all()
    assert w.loc["2024-01-02", "AAPL"] == 0.0        # before any signal exists
    assert w.loc["2024-01-10", "AAPL"] == 1.0        # bullish window active
    assert w.loc["2024-01-30", "AAPL"] == 0.0        # bearish window -> flat
    assert w.loc["2024-02-06", "AAPL"] == 1.0        # bullish again


def test_news_sentiment_strategy_errors_when_no_signal(monkeypatch):
    import backend.backtest.strategies as strategies
    import backend.extensions.sentiment as senti
    monkeypatch.setattr(senti, "history_series",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("down")))
    idx = pd.date_range("2024-01-01", periods=10, freq="B")
    prices = pd.DataFrame({"AAPL": 100.0}, index=idx)
    with pytest.raises(ValueError):
        strategies.news_sentiment(prices, {})


def test_strategy_registered():
    from backend.backtest.strategies import REGISTRY as STRATS
    assert "news_sentiment" in STRATS


def test_history_command_registered():
    assert "/sentiment/history" in REGISTRY


def test_symbol_command_summarises_per_ticker(monkeypatch):
    now = pd.Timestamp.now(tz="UTC")

    def fake_company(symbol, limit, provider):
        from backend.core.models import Result
        return Result(pd.DataFrame([
            {"date": now, "symbol": symbol, "title": "{} upgraded after strong growth".format(symbol),
             "summary": "", "url": "http://x/1", "source": "yahoo"},
        ]), provider=provider)

    import backend.extensions.sentiment as senti
    monkeypatch.setattr(senti, "news_company", fake_company)

    out = execute("/sentiment/symbol", symbol="AAPL,MSFT")
    assert [r["symbol"] for r in out.results] == ["AAPL", "MSFT"]
    assert all(r["label"] == "bullish" for r in out.results)
    assert set(out.extra["articles"]) == {"AAPL", "MSFT"}
