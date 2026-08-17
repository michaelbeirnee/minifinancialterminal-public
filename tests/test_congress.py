"""Congressional disclosure parsing and the cluster gate, offline.

The PTR fixture below is the shape Senate EFD actually serves, including the
two details that break a naive parse: a ticker cell that renders the filer's
blank ("--") *and* a resolved quote link side by side, and an open-ended top
amount bracket with no upper bound.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.providers import congress

PTR = """
<html><body>
<table class="table">
<thead><tr><th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th>
<th>Asset Name</th><th>Asset Type</th><th>Type</th><th>Amount</th><th>Comment</th></tr></thead>
<tbody>
<tr>
  <td>1</td><td>07/07/2026</td><td>Self</td>
  <td>
     --
     <br/>
     <a href="https://finance.yahoo.com/quote/AMCR" target="_blank">AMCR</a>
  </td>
  <td>Amcor plc</td><td>Stock</td><td>Purchase</td>
  <td>$50,001 - $100,000</td><td>--</td>
</tr>
<tr>
  <td>2</td><td>07/02/2026</td><td>Spouse</td>
  <td><a href="https://finance.yahoo.com/quote/MSFT" target="_blank">MSFT</a></td>
  <td>Microsoft Corp</td><td>Stock</td><td>Sale (Partial)</td>
  <td>$1,000,001 - $5,000,000</td><td>managed account</td>
</tr>
<tr>
  <td>3</td><td>07/01/2026</td><td>Self</td><td>--</td>
  <td>Manassas VA GO Public Improvement Bonds</td><td>Municipal Security</td>
  <td>Purchase</td><td>Over $50,000,000</td><td>--</td>
</tr>
</tbody></table>
</body></html>
"""


def test_amount_brackets_are_ranges_not_sizes():
    assert congress.parse_amount("$50,001 - $100,000") == (50001.0, 100000.0)
    assert congress.parse_amount("$1,001 - $15,000") == (1001.0, 15000.0)
    # An open-ended top bracket has no ceiling, and inventing one would be
    # inventing data.
    assert congress.parse_amount("Over $50,000,000") == (50000000.0, None)
    assert congress.parse_amount("") == (None, None)


def test_ticker_cell_prefers_the_resolved_link_over_the_filers_blank():
    """EFD renders "--" and a quote link in the same cell; flattening gives
    "-- AMCR", which is not a symbol anybody can look up."""
    rows = congress.transactions_in(PTR)
    assert [r["symbol"] for r in rows] == ["AMCR", "MSFT", None]


def test_a_municipal_bond_is_not_given_a_ticker():
    """Third row names no security with a symbol, so the column stays empty
    rather than carrying a placeholder onto a chart."""
    muni = congress.transactions_in(PTR)[2]
    assert muni["symbol"] is None
    assert muni["asset_type"] == "Municipal Security"
    assert muni["amount_low"] == 50000000.0 and muni["amount_high"] is None


def test_transaction_rows_carry_side_owner_and_dates():
    first, second, _ = congress.transactions_in(PTR)
    assert (first["side"], first["owner"]) == ("buy", "Self")
    assert first["transaction_date"] == "2026-07-07"
    # "Sale (Partial)" and "Sale (Full)" are both sells; the distinction is
    # about position size, which the disclosure does not give either way.
    assert (second["side"], second["owner"]) == ("sell", "Spouse")
    assert second["comment"] == "managed account"


def test_header_and_layout_rows_are_not_transactions():
    assert congress.transactions_in("<table><tr><td>x</td></tr></table>") == []
    assert congress.transactions_in("") == []


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def _feed(rows):
    """Stand in for a live sweep with a fixed frame."""
    frame = pd.DataFrame(rows)
    frame.attrs["reports_read"] = len(frame)
    frame.attrs["reports_available"] = len(frame)
    return frame


def _trade(member, symbol, side, filed, traded, owner="Self", low=15001.0):
    return {"member": member, "symbol": symbol, "side": side, "filing_date": filed,
            "transaction_date": traded, "owner": owner, "amount_low": low,
            "amount_high": 50000.0, "asset": symbol + " Inc", "asset_type": "Stock",
            "amended": False, "filing_url": "https://efdsearch.senate.gov/x/",
            "comment": None}


def test_one_member_is_not_a_cluster(monkeypatch):
    """Two disclosures from the same person are one person's decision."""
    from backend.extensions import congress as ext
    from backend.core.errors import EmptyDataError

    monkeypatch.setattr(congress, "recent", lambda **kw: _feed([
        _trade("Doe, Jane (Senator)", "ACME", "buy", "2026-03-02", "2026-02-20"),
        _trade("Doe, Jane (Senator)", "ACME", "buy", "2026-03-05", "2026-02-25"),
    ]))
    with pytest.raises(EmptyDataError):
        ext.congress_clusters(min_members=2)


def test_the_gate_splits_direction_from_whose_account(monkeypatch):
    from backend.extensions import congress as ext

    monkeypatch.setattr(congress, "recent", lambda **kw: _feed([
        _trade("Doe, Jane (Senator)", "ACME", "buy", "2026-03-02", "2026-02-20"),
        _trade("Roe, Rick (Senator)", "ACME", "buy", "2026-03-04", "2026-02-25"),
        # A different symbol, traded by a spouse and a dependent child: still a
        # cluster, but not one the members necessarily made.
        _trade("Doe, Jane (Senator)", "BETA", "sell", "2026-03-02", "2026-02-20",
               owner="Spouse"),
        _trade("Roe, Rick (Senator)", "BETA", "sell", "2026-03-03", "2026-02-21",
               owner="Child"),
    ]))
    rows = {r["symbol"]: r for r in ext.congress_clusters(min_members=2).data}

    assert rows["ACME"]["family"] == "buy_self"
    assert rows["ACME"]["members"] == 2
    assert rows["BETA"]["family"] == "sell_household"
    assert rows["BETA"]["self_directed"] == 0


def test_a_joint_account_is_the_members_own(monkeypatch):
    """Joint is held with a spouse but the member is a party to it; pooling it
    with Spouse would file the member's own trades under somebody else."""
    from backend.extensions import congress as ext

    monkeypatch.setattr(congress, "recent", lambda **kw: _feed([
        _trade("Doe, Jane (Senator)", "ACME", "buy", "2026-03-02", "2026-02-20",
               owner="Joint"),
        _trade("Roe, Rick (Senator)", "ACME", "buy", "2026-03-04", "2026-02-25",
               owner="Joint"),
    ]))
    row = ext.congress_clusters(min_members=2).data[0]
    assert row["self_directed"] == 2
    assert row["family"] == "buy_self"


def test_disclosure_lag_is_measured_per_filing(monkeypatch):
    """The 45-day deadline means clusters form on filing calendars. The median
    per-disclosure lag is what says whether these people acted at the same
    time or merely filed at the same time."""
    from backend.extensions import congress as ext

    monkeypatch.setattr(congress, "recent", lambda **kw: _feed([
        _trade("Doe, Jane (Senator)", "ACME", "buy", "2026-03-11", "2026-03-01"),
        _trade("Roe, Rick (Senator)", "ACME", "buy", "2026-03-12", "2026-01-02"),
    ]))
    row = ext.congress_clusters(min_members=2).data[0]
    assert row["disclosure_lag_days"] == 39  # median of 10 and 69, truncated
    assert row["earliest_trade"] == "2026-01-02"


def test_the_gate_records_what_it_emits(monkeypatch, tmp_path):
    """Every scanner in the engine signs the same contract: emit, and log it
    anchored on the day the market could first know."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.database import Base
    from backend.extensions import congress as ext
    from backend.models import SignalEvent
    from backend.thesis import memory

    engine = create_engine("sqlite:///{}".format(tmp_path / "sig.db"))
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr(memory, "SessionLocal", maker)
    monkeypatch.setattr(congress, "recent", lambda **kw: _feed([
        _trade("Doe, Jane (Senator)", "ACME", "buy", "2026-03-02", "2026-02-20"),
        _trade("Roe, Rick (Senator)", "ACME", "buy", "2026-03-04", "2026-02-25"),
    ]))

    ext.congress_clusters(min_members=2)

    session = maker()
    event = session.query(SignalEvent).one()
    assert event.family == "congress_cluster:buy_self"
    assert event.symbol == "ACME"
    assert event.known_on.date().isoformat() == "2026-03-04"  # filing, not trade
    session.close()


# --------------------------------------------------------------------------- #
# The card line
# --------------------------------------------------------------------------- #
def test_the_card_line_counts_people_not_bracket_money():
    from backend.thesis import triage

    line = triage.describe_congress([
        _trade("Doe, Jane (Senator)", "ACME", "buy", "2026-03-02", "2026-02-20"),
        _trade("Roe, Rick (Senator)", "ACME", "buy", "2026-03-04", "2026-02-25"),
        _trade("Roe, Rick (Senator)", "ACME", "sell", "2026-03-04", "2026-02-26",
               owner="Spouse"),
    ])
    assert line == ("2 member(s), 2 buy, 1 sell (2 in the member's own account) "
                    "· latest filing 2026-03-04")
    # Nobody disclosing is the normal case for 100 of 535 members, so it says
    # nothing rather than saying no.
    assert triage.describe_congress([]) is None
    assert triage.describe_congress(None) is None


def test_the_card_carries_the_congress_line_when_there_is_one():
    from backend.thesis.triage import build_card

    row = {"symbol": "ACME", "issuer": "Acme Inc", "family": "officer_conviction"}
    assert "congress" not in build_card(row)
    assert "congress (Senate STOCK Act): 2 member(s)" in build_card(
        row, congress="2 member(s), 2 buy (2 in the member's own account) · latest filing 2026-03-04")
