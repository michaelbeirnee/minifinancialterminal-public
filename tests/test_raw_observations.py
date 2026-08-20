from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.backtest import multisource_research as msr
from backend.models import Base, ProductionSignalVintage, RawObservation, ResearchFeatureSnapshot
from backend.trading import production
from backend.trading.production import DEFAULT_CAPTURE_UNIVERSE, resolve_capture_universe


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def fake_yahoo(monkeypatch):
    """A complete synthetic Yahoo surface so capture never touches the network."""

    today = pd.Timestamp.today().normalize()
    near = (today + timedelta(days=30)).date().isoformat()
    far = (today + timedelta(days=80)).date().isoformat()

    def estimates(symbol, kind="earnings"):
        idx = ["0q", "+1q"]
        if kind == "eps_revisions":
            return pd.DataFrame({"upLast30days": [5, 2], "downLast30days": [2, 1]}, index=idx)
        if kind == "eps_trend":
            return pd.DataFrame({"current": [2.5, 2.7], "30daysAgo": [2.0, 2.6]}, index=idx)
        if kind == "earnings":
            return pd.DataFrame({"avg": [2.5, 2.7], "low": [2.0, 2.2], "high": [3.0, 3.1]}, index=idx)
        return pd.DataFrame({"estimate": [1.0, 2.0]}, index=idx)

    chain = pd.DataFrame({
        "strike": [90.0, 100.0, 110.0, 90.0, 100.0, 110.0],
        "option_type": ["call"] * 3 + ["put"] * 3,
        "implied_volatility": [0.32, 0.30, 0.31, 0.36, 0.33, 0.30],
        "open_interest": [100, 220, 90, 300, 180, 60],
        "bid": [10.0, 4.0, 1.0, 0.8, 3.5, 9.0],
        "ask": [10.5, 4.3, 1.2, 1.0, 3.8, 9.5],
        "contract_ignored": ["x"] * 6,  # not in the whitelist; must be trimmed
    })
    closes = pd.Series(
        100.0 * np.cumprod(1 + np.random.default_rng(3).normal(0, 0.01, 60)),
        index=pd.bdate_range(end=today, periods=60),
    )

    info = {f"field_{i}": float(i) for i in range(150)}
    info.update({
        "sector": "Technology", "industry": "Semiconductors",
        "sharesShortPercentOfFloat": 0.04, "sharesShort": 4e7,
        "sharesShortPriorMonth": 5e7, "floatShares": 1e9, "shortRatio": 2.5,
        "targetMeanPrice": 120.0, "recommendationMean": 2.1,
        "asof": pd.Timestamp("2026-08-20"), "weird": np.nan,
    })

    patches = {
        "estimates": estimates,
        "price_targets": lambda symbol: {"mean": 120.0, "high": 150.0, "low": 90.0},
        "quote": lambda symbol: {"symbol": symbol, "last_price": 100.0},
        "recommendations": lambda symbol: pd.DataFrame(
            {"strongBuy": [10], "buy": [20], "hold": [8]}, index=["0m"]
        ),
        "option_expirations": lambda symbol: [near, far],
        "option_chain": lambda symbol, expiry=None: chain,
        "history": lambda symbol, start=None, end=None, **kw: pd.DataFrame({"close": closes}),
        "info": lambda symbol: info,
        "upgrades_downgrades": lambda symbol: pd.DataFrame(
            {"action": ["up", "down"], "firm": ["A", "B"]},
            index=pd.to_datetime([today - timedelta(days=3), today - timedelta(days=9)]),
        ),
        "earnings_dates": lambda symbol, limit=12: pd.DataFrame(
            {"surprise(%)": [4.0]}, index=pd.to_datetime([today - timedelta(days=20)]),
        ),
    }
    for name, fn in patches.items():
        monkeypatch.setattr(msr.yahoo, name, fn)
    return patches


def test_capture_appends_raw_payloads_and_derives_features(db, fake_yahoo):
    result = msr.archive_current_snapshots(["TEST"], db)
    assert result["raw_rows"] > 0

    raw = db.query(RawObservation).all()
    sources = {r.source for r in raw}
    assert {"yahoo.info", "yahoo.estimates.eps_revisions", "yahoo.price_targets",
            "yahoo.option_chain.near", "yahoo.option_expirations"} <= sources
    # The full info payload is banked, not just the extracted fields.
    info_row = next(r for r in raw if r.source == "yahoo.info")
    assert len(info_row.payload) > 100
    assert info_row.payload["weird"] is None            # NaN became null
    assert info_row.payload["asof"] == "2026-08-20T00:00:00"  # timestamp became ISO
    chain_row = next(r for r in raw if r.source == "yahoo.option_chain.near")
    assert "contract_ignored" not in chain_row.payload["contracts"][0]
    assert info_row.as_of_date == date.today().isoformat()

    # Derived features still computed exactly as before, from the same fetch.
    feats = {f.family: f.features for f in db.query(ResearchFeatureSnapshot).all()}
    assert feats["estimates"]["eps_revision_breadth"] == pytest.approx((5 - 2) / (5 + 2 + 1))
    assert "short_percent_float" in feats["crowding"]
    assert "put_call_oi_log" in feats["options"]


def test_second_capture_same_day_appends_raw_but_upserts_features(db, fake_yahoo):
    first = msr.archive_current_snapshots(["TEST"], db)
    raw_after_first = db.query(RawObservation).count()
    feat_after_first = db.query(ResearchFeatureSnapshot).count()

    second = msr.archive_current_snapshots(["TEST"], db)
    assert db.query(RawObservation).count() == 2 * raw_after_first  # append-only
    assert db.query(ResearchFeatureSnapshot).count() == feat_after_first  # per-day upsert
    assert first["raw_rows"] == second["raw_rows"]


def test_capture_without_raw_still_writes_features_only(db, fake_yahoo):
    result = msr.archive_current_snapshots(["TEST"], db, include_raw=False)
    assert result["raw_rows"] == 0
    assert db.query(RawObservation).count() == 0
    assert db.query(ResearchFeatureSnapshot).count() > 0


def test_capture_universe_resolution_order(db, monkeypatch):
    # Explicit list wins.
    assert resolve_capture_universe(db, ["aapl", "msft", "aapl"]) == ["AAPL", "MSFT"]
    # Then the latest approved vintage.
    db.add(ProductionSignalVintage(
        status="approved", as_of="2026-08-01", symbols="NVDA,AMD",
        params={}, blend=[{"signal": "x", "weight": 1.0}], sleeves=[], evidence=[], config={},
    ))
    db.commit()
    assert resolve_capture_universe(db) == ["NVDA", "AMD"]
    # Then the configured universe, once no vintage is approved.
    db.query(ProductionSignalVintage).update({"status": "retired"})
    db.commit()
    monkeypatch.setattr(production.settings, "capture_universe", "spy, qqq")
    assert resolve_capture_universe(db) == ["SPY", "QQQ"]
    # Finally the built-in default.
    monkeypatch.setattr(production.settings, "capture_universe", "")
    resolved = resolve_capture_universe(db)
    assert resolved == list(DEFAULT_CAPTURE_UNIVERSE)
    assert len(resolved) >= 100
