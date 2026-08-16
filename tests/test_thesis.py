"""Thesis spine: falsifier evaluation, offline.

A stub command is registered directly in the registry so no network is
involved; it is removed again on teardown so no other test sees it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.core.registry import REGISTRY, command
from backend.models import Thesis, ThesisCheck
from backend.thesis import spine
from backend.core.models import Result

STUB = "/teststub/series"


@pytest.fixture()
def stub_command():
    payload = {"rows": [{"close": 10.0}, {"close": 12.5}, {"close": None}]}

    @command(STUB, providers=("test",), summary="stub series for spine tests")
    def _stub() -> Result:  # pragma: no cover - exercised via execute()
        return Result(list(payload["rows"]), provider="test")

    yield payload
    REGISTRY.pop(STUB, None)


def make_check(**kw) -> ThesisCheck:
    base = dict(name="c", command_path=STUB, parameters={}, field="close",
                comparator="lt", threshold=5.0, status="holding")
    base.update(kw)
    return ThesisCheck(**base)


def test_extract_field_skips_trailing_nulls_and_non_numeric():
    rows = [{"close": 1.0}, {"close": "2.5"}, {"close": None}, {"other": 9}]
    assert spine.extract_field(rows, "close") == 2.5
    assert spine.extract_field(rows, "missing") is None
    assert spine.extract_field({"close": 3}, "close") == 3.0


def test_check_holds_then_breaks(stub_command):
    check = make_check(comparator="lt", threshold=5.0)
    value, status = spine.evaluate_check(check)
    assert (value, status) == (12.5, "holding")

    # Fresher data crosses the line: the falsifier fires.
    stub_command["rows"].append({"close": 4.0})
    value, status = spine.evaluate_check(check)
    assert (value, status) == (4.0, "broken")
    assert check.breached_at is not None

    # Broken is terminal even if the value recovers.
    stub_command["rows"].append({"close": 100.0})
    _, status = spine.evaluate_check(check)
    assert status == "broken"


def test_provider_error_is_not_a_breach():
    check = make_check(command_path="/no/such/command")
    _, status = spine.evaluate_check(check)
    assert status == "error"
    assert check.last_error


def test_thesis_status_derivation(stub_command):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=30)

    holding = make_check(comparator="lt", threshold=5.0)
    thesis = Thesis(title="t", claim="c", status="open",
                    review_by=future, checks=[holding])
    assert spine.evaluate_thesis(thesis) == "open"

    # Past its review date with every falsifier held -> supported.
    thesis.review_by = past
    assert spine.evaluate_thesis(thesis) == "supported"
    assert thesis.closed_at is not None

    # One breached falsifier breaks the thesis.
    broken = Thesis(title="t2", claim="c", status="open", review_by=future,
                    checks=[make_check(comparator="gt", threshold=1.0)])
    assert spine.evaluate_thesis(broken) == "broken"

    # Hand-closed theses are left alone.
    closed = Thesis(title="t3", claim="c", status="closed", checks=[])
    assert spine.evaluate_thesis(closed) == "closed"


FORM4_XML = b"""\n<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-07-01</periodOfReport>
  <issuer>
    <issuerCik>0000789570</issuerCik>
    <issuerName>MGM Resorts International</issuerName>
    <issuerTradingSymbol>MGM</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001460260</rptOwnerCik>
      <rptOwnerName>Fritz Gary M</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer>
      <officerTitle>President, Interactive</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>0</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-07-01</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>32.17</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>50000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-07-01</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>32.20</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>I</value></directOrIndirectOwnership>
        <natureOfOwnership><value>By 401(k) plan</value></natureOfOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


def test_form4_parser_offline():
    from backend.thesis.fresh import parse_ownership_xml

    # Leading whitespace before the XML declaration must not break parsing —
    # the <XML> wrapper in EDGAR full submissions always leaves a newline.
    rows = parse_ownership_xml(FORM4_XML, "0000789570-26-000067", "2026-07-02")
    assert len(rows) == 2

    real, auto = rows
    assert real["symbol"] == "MGM"
    assert real["owner_name"] == "Fritz Gary M"
    assert real["is_officer"] and not real["is_ten_pct"]
    assert real["code"] == "P" and real["acq_disp"] == "A"
    assert real["value_usd"] == pytest.approx(321700.0)
    assert real["aff10b5one"] is False
    assert real["ownership_form"] == "D"
    assert real["auto_vehicle"] is False

    # The 401(k) leg is flagged so the scanner's gate can drop it.
    assert auto["ownership_form"] == "I"
    assert auto["auto_vehicle"] is True


def test_board_link_join(monkeypatch):
    """The IAC/MGM shape: entity CEO sits on the issuer's board."""
    import pandas as pd
    from backend.thesis import families

    rel = pd.DataFrame([
        # person 42 runs entity 100 ...
        {"owner_cik": "42", "owner_name": "LEVIN JOSEPH", "issuer_cik": "100",
         "is_officer": True, "is_director": True},
        # ... and sits on issuer 200's board
        {"owner_cik": "42", "owner_name": "LEVIN JOSEPH", "issuer_cik": "200",
         "is_officer": False, "is_director": True},
        # noise: an unrelated director at the issuer
        {"owner_cik": "7", "owner_name": "OTHER PERSON", "issuer_cik": "200",
         "is_officer": False, "is_director": True},
    ])
    monkeypatch.setattr(families, "relations", lambda max_quarters=12: rel)

    linked, names = families.board_link("100", "200")
    assert linked and names == ["LEVIN JOSEPH"]

    # No link the other way around: issuer 100 has no director from entity 200.
    linked, names = families.board_link("200", "100")
    assert linked and names == ["LEVIN JOSEPH"]  # 42 is officer+director at 100 too

    # An entity with no people on the board resolves clean.
    linked, names = families.board_link("999", "200")
    assert not linked and names == []


def test_board_link_uses_fresh_rows(monkeypatch):
    """A directorship filed after the newest bulk quarter still counts."""
    import pandas as pd
    from backend.thesis import families

    rel = pd.DataFrame([
        {"owner_cik": "42", "owner_name": "LEVIN JOSEPH", "issuer_cik": "100",
         "is_officer": True, "is_director": False},
    ])
    monkeypatch.setattr(families, "relations", lambda max_quarters=12: rel)

    # Bulk alone finds nothing at issuer 200...
    linked, _ = families.board_link("100", "200")
    assert not linked

    # ...but the issuer's fresh Form 4 rows carry the new directorship.
    fresh_rows = pd.DataFrame([
        {"owner_cik": "0000000042", "is_director": True, "issuer_cik": "200"},
    ])
    linked, names = families.board_link("100", "200", extra_relations=fresh_rows)
    assert linked and names == ["LEVIN JOSEPH"]


INFOTABLE_XML = b"""\n<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>MGM RESORTS INTL</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>552953101</cusip>
    <value>197928666</value>
    <shrsOrPrnAmt><sshPrnamt>5300000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>UBER TECHNOLOGIES INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>90353T100</cusip>
    <value>5239420114</value>
    <shrsOrPrnAmt><sshPrnamt>72840541</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>Put</putCall>
  </infoTable>
</informationTable>"""


def test_infotable_parser_offline():
    from backend.thesis.holders import parse_information_table

    rows = parse_information_table(INFOTABLE_XML)
    assert len(rows) == 2
    mgm, uber = rows
    assert mgm["issuer"] == "MGM RESORTS INTL"
    assert mgm["value_usd"] == pytest.approx(197928666.0)
    assert mgm["shares"] == pytest.approx(5300000.0)
    assert mgm["put_call"] is None
    assert uber["put_call"] == "Put"

    # A non-infotable XML block (the 13F cover page) parses to nothing.
    assert parse_information_table(b"<edgarSubmission><x/></edgarSubmission>") == []


def test_13f_name_match():
    from backend.thesis.holders import name_match

    # Register spelling vs info-table abbreviation.
    assert name_match("MGM Resorts International", "MGM RESORTS INTL")
    assert name_match("Uber Technologies, Inc.", "UBER TECHNOLOGIES INC")
    # Shared first token is not enough: distinctive tokens must all line up.
    assert not name_match("MGM Resorts International", "MGM Growth Properties")
    # Boilerplate-only names never match anything.
    assert not name_match("Inc", "CORP")


class _FakeBlock:
    type = "tool_use"

    def __init__(self, payload):
        self.input = payload


class _FakeMessages:
    def __init__(self, payload):
        self._payload = payload
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs

        class _Msg:
            content = [_FakeBlock(self._payload)]
        _Msg.content = [_FakeBlock(self._payload)]
        return _Msg()


class _FakeClient:
    def __init__(self, payload):
        self.messages = _FakeMessages(payload)


def test_triage_card_contains_only_funnel_numbers():
    from backend.thesis.triage import build_card

    row = {"symbol": "MGM", "issuer": "MGM Resorts International",
           "family": "board_backed_strategic", "officer_buyers": 0,
           "officer_value": 0, "board_backed_value": 39884552,
           "board_backed_via": "LEVIN JOSEPH", "buyers": "IAC Inc.",
           "total_buyers": 1, "has_ceo_cfo": False, "last_filing": "2025-12-09"}
    card = build_card(row, {"one_month": -0.05, "three_month": 0.15, "one_year": 0.25},
                      {"one_year": 0.22})
    assert "39,884,552" in card and "LEVIN JOSEPH" in card
    assert "+25.0%" in card and "SPY 1y +22.0%" in card
    # No price context degrades explicitly, never silently.
    assert "price: unavailable" in build_card(row)


def test_triage_run_and_validation(monkeypatch, stub_command):
    from backend.thesis import triage

    monkeypatch.setattr(triage, "availability",
                        lambda: {"enabled": True, "reason": None})
    payload = {"candidates": [
        {"symbol": "MGM", "promote": True, "confidence": "medium",
         "reason": "board-backed accumulation",
         "legs": [
             {"claim": "Osaka opening not priced in", "source": "world_knowledge",
              "verify_with": [{"path": "regulators/sec/filing_search",
                               "params": {"query": "Osaka"}, "expect": "disclosed"},
                              {"path": "/made/up/command", "expect": "x"}],
              "if_absent": "catalyst is press speculation; kill the leg"},
             {"claim": "no if_absent given", "source": "world_knowledge",
              "verify_with": []},
         ]},
        {"symbol": "ZZZZ", "promote": True, "confidence": "high",
         "reason": "invented out of thin air"},
    ]}
    client = _FakeClient(payload)
    out = triage.run(["card1"], ["MGM"], client=client)

    # The invented symbol is dropped and reported.
    assert [c["symbol"] for c in out["candidates"]] == ["MGM"]
    assert out["dropped_invented_symbols"] == ["ZZZZ"]

    legs = out["candidates"][0]["legs"]
    # Paths are normalised; a registered command passes, a made-up one is flagged.
    osaka = legs[0]["verify_with"]
    assert osaka[0]["path"] == "/regulators/sec/filing_search"
    assert "unknown_command" not in osaka[0]
    assert osaka[1]["unknown_command"] is True
    # A world-knowledge leg without if_absent is rejected as unfalsifiable.
    assert legs[1]["rejected"].startswith("world_knowledge leg")

    # The call was a forced tool call with the anti-slop system prompt.
    kwargs = client.messages.last_kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": "triage_result"}
    assert "Never state a number" in kwargs["system"]


class _ScriptedClient:
    """Plays a fixed sequence of responses; records every request."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.requests.append(kwargs)
                return outer._responses.pop(0)
        self.messages = _Messages()


def _tool_use(name, payload, call_id="c1"):
    block = _FakeBlock(payload)
    block.name = name
    block.id = call_id
    return block


def _msg(*blocks):
    class _M:
        content = list(blocks)
    return _M()


def test_deepdive_loop_runs_tools_then_returns_dossier(monkeypatch, stub_command):
    from backend.thesis import deepdive, triage

    monkeypatch.setattr(triage, "availability",
                        lambda: {"enabled": True, "reason": None})

    dossier = {
        "proceed": True, "confidence": "medium",
        "claim": "stub series stays above 5", "summary": "held on live data",
        "review_by_days": 90,
        "legs": [{
            "claim": "series is elevated", "verdict": "verified",
            "evidence": [{"path": "teststub/series", "params": {}},
                         {"path": "/no/such/thing", "params": {}}],
            "falsifiers": [
                {"name": "floor", "path": "teststub/series", "params": {},
                 "field": "close", "comparator": "lt", "threshold": 5.0,
                 "by_date_days": 60},
                {"name": "bad", "path": "/fake/cmd", "field": "x",
                 "comparator": "lt", "threshold": 1.0},
            ],
        }],
    }
    client = _ScriptedClient([
        _msg(_tool_use("run_command", {"path": STUB}, "t1")),
        _msg(_tool_use("deepdive_result", dossier, "t2")),
    ])
    out = deepdive.run({"symbol": "TEST", "legs": []}, client=client, max_rounds=3)

    # Round 1's tool result was fed back as a tool_result user message.
    second_request = client.requests[1]
    roles = [m["role"] for m in second_request["messages"]]
    assert roles == ["user", "assistant", "user"]
    tool_results = second_request["messages"][2]["content"]
    assert tool_results[0]["type"] == "tool_result" and not tool_results[0]["is_error"]

    # Validation normalised and flagged paths.
    leg = out["legs"][0]
    assert leg["evidence"][0]["path"] == STUB
    assert "unknown_command" not in leg["evidence"][0]
    assert leg["evidence"][1]["unknown_command"] is True
    assert "unknown_command" not in leg["falsifiers"][0]
    assert leg["falsifiers"][1]["unknown_command"] is True


def test_deepdive_forces_dossier_on_final_round(monkeypatch, stub_command):
    from backend.thesis import deepdive, triage

    monkeypatch.setattr(triage, "availability",
                        lambda: {"enabled": True, "reason": None})
    dossier = {"proceed": False, "confidence": "low", "claim": "n/a",
               "summary": "could not verify", "legs": []}
    client = _ScriptedClient([
        _msg(_tool_use("run_command", {"path": STUB}, "t1")),
        _msg(_tool_use("deepdive_result", dossier, "t2")),
    ])
    out = deepdive.run({"symbol": "TEST", "legs": []}, client=client, max_rounds=1)
    assert out["proceed"] is False
    # The final round carried the forced tool_choice.
    assert client.requests[-1]["tool_choice"] == {"type": "tool", "name": "deepdive_result"}


@pytest.fixture()
def memory_db(tmp_path, monkeypatch):
    """Point the memory layer at a throwaway SQLite file."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.database import Base
    from backend.thesis import memory

    engine = create_engine("sqlite:///{}".format(tmp_path / "mem.db"))
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr(memory, "SessionLocal", maker)
    return maker


def test_signal_events_upsert_idempotently(memory_db):
    from backend.models import SignalEvent, SignalRun
    from backend.thesis import memory

    rows = [{"symbol": "MGM", "known_on": "2025-12-09", "score": 2.76,
             "payload": {"family": "board_backed_strategic"}}]
    assert memory.record_events("insider_cluster", rows, kind="test") == 1
    # Same scan again: refreshed, not duplicated.
    rows[0]["score"] = 3.0
    assert memory.record_events("insider_cluster", rows, kind="test") == 0

    session = memory_db()
    events = session.query(SignalEvent).all()
    assert len(events) == 1
    assert events[0].score == 3.0
    assert events[0].fwd_1m is None  # outcomes start unknown
    runs = session.query(SignalRun).all()
    assert [r.events_new for r in runs] == [1, 0]
    session.close()


def test_grading_stamps_only_elapsed_horizons(memory_db, monkeypatch):
    import numpy as np
    import pandas as pd

    from backend.models import SignalEvent
    from backend.thesis import memory

    # An event 100 days old: 1m and 3m are gradeable, 6m/12m are not.
    known = (datetime.now(timezone.utc) - timedelta(days=100)).replace(tzinfo=None)
    memory.record_events("insider_cluster",
                         [{"symbol": "TSTA", "known_on": known.date().isoformat()}],
                         kind="test")

    # Fake panel: TSTA compounds +0.1%/session, SPY is flat.
    dates = pd.bdate_range(known - timedelta(days=5), periods=320)
    panel = pd.DataFrame({
        "TSTA": 100 * (1.001 ** np.arange(len(dates))),
        "SPY": np.full(len(dates), 100.0),
    }, index=dates)
    monkeypatch.setattr(memory, "_price_panel", lambda symbols, start: panel)

    out = memory.grade_pending()
    assert out["graded"] == 1

    session = memory_db()
    event = session.query(SignalEvent).one()
    assert event.fwd_1m == pytest.approx(1.001 ** 21 - 1, rel=1e-6)
    assert event.fwd_3m == pytest.approx(1.001 ** 63 - 1, rel=1e-6)
    assert event.fwd_6m is None and event.fwd_12m is None  # not elapsed yet
    session.close()

    report = memory.report()
    assert report[0]["family"] == "insider_cluster"
    assert report[0]["graded_1m"] == 1 and report[0]["graded_6m"] == 0
    assert report[0]["hit_rate_1m"] == 1.0


def test_memory_failure_never_breaks_the_scan(memory_db, monkeypatch):
    from backend.thesis import memory

    class _Boom:
        def __call__(self):
            raise RuntimeError("disk full")
    monkeypatch.setattr(memory, "SessionLocal", _Boom())
    # A dead memory layer returns None; it never raises into the scanner.
    assert memory.record_events("x", [{"symbol": "A", "known_on": "2026-01-01"}],
                                kind="test") is None
    memory.record_triage(None, "m", {}, [], {"candidates": []})  # must not raise
    memory.record_deepdive(None, "m", "A", {}, {"proceed": False}, None)


# --------------------------------------------------------------------------- #
# Learn: the step that turns a graded log into base rates
# --------------------------------------------------------------------------- #
def test_qualify_is_idempotent():
    from backend.thesis import memory

    assert memory.qualify("insider_cluster", "both") == "insider_cluster:both"
    assert memory.qualify("insider_cluster", None) == "insider_cluster"
    assert memory.qualify("insider_cluster", "insider_cluster") == "insider_cluster"
    # Requalifying an already-qualified family must not nest it.
    assert memory.qualify("insider_cluster", "insider_cluster:both") == "insider_cluster:both"


def test_report_splits_the_families_it_measures(memory_db):
    """A report that pools families is a single average that says nothing.

    An index fund crossing 10% and a board-backed strategic buyer are the
    distinction the funnel exists to draw; the base rates have to keep it.
    """
    from backend.thesis import memory

    memory.record_events("insider_cluster", [
        {"symbol": "AAA", "known_on": "2026-01-05", "family": "officer_conviction"},
        {"symbol": "BBB", "known_on": "2026-01-06", "family": "board_backed_strategic"},
        {"symbol": "CCC", "known_on": "2026-01-07", "family": "officer_conviction"},
    ], kind="test")

    assert {r["family"]: r["events"] for r in memory.report()} == {
        "insider_cluster:officer_conviction": 2,
        "insider_cluster:board_backed_strategic": 1,
    }


def test_backfill_requalifies_legacy_events_and_keeps_their_grades(memory_db):
    from backend.models import SignalEvent
    from backend.thesis import memory

    session = memory_db()
    session.add(SignalEvent(  # a row as the original scanner wrote it
        event_key=memory.event_key("insider_cluster", "MGM", "2025-12-09"),
        family="insider_cluster", symbol="MGM", known_on=datetime(2025, 12, 9),
        score=2.76, payload={"family": "board_backed_strategic"}, fwd_3m=0.12))
    session.commit()
    session.close()

    assert memory.backfill_families() == {"moved": 1, "merged": 0}

    session = memory_db()
    event = session.query(SignalEvent).one()
    assert event.family == "insider_cluster:board_backed_strategic"
    assert event.event_key == memory.event_key(
        "insider_cluster:board_backed_strategic", "MGM", "2025-12-09")
    assert event.fwd_3m == 0.12  # a stamped horizon cannot be recomputed; keep it
    session.close()

    assert memory.backfill_families() == {"moved": 0, "merged": 0}  # runs every boot


def test_backfill_merges_a_collision_rather_than_dropping_grades(memory_db):
    """The fixed scanner may already have written the row the backfill lands on."""
    from backend.models import SignalEvent
    from backend.thesis import memory

    session = memory_db()
    session.add(SignalEvent(  # legacy row, graded
        event_key=memory.event_key("insider_cluster", "MGM", "2025-12-09"),
        family="insider_cluster", symbol="MGM", known_on=datetime(2025, 12, 9),
        payload={"family": "both"}, fwd_1m=0.05, fwd_3m=0.12))
    session.add(SignalEvent(  # what the fixed scanner wrote afterwards, ungraded
        event_key=memory.event_key("insider_cluster:both", "MGM", "2025-12-09"),
        family="insider_cluster:both", symbol="MGM", known_on=datetime(2025, 12, 9),
        payload={"family": "both"}))
    session.commit()
    session.close()

    assert memory.backfill_families() == {"moved": 0, "merged": 1}

    session = memory_db()
    event = session.query(SignalEvent).one()  # the duplicate is gone
    assert event.family == "insider_cluster:both"
    assert (event.fwd_1m, event.fwd_3m) == (0.05, 0.12)  # grades carried across
    session.close()


def test_base_rate_is_withheld_until_it_means_something():
    from backend.thesis import memory

    thin = {"family": "insider_cluster:both", "graded_3m": 9,
            "hit_rate_3m": 1.0, "mean_excess_3m": 0.2}
    assert memory.describe_base_rate(thin) is None  # nine events is not a base rate
    assert memory.describe_base_rate(None) is None

    line = memory.describe_base_rate(
        {**thin, "graded_3m": 40, "hit_rate_3m": 0.42, "mean_excess_3m": -0.018})
    assert "40 events" in line
    assert "42% beat benchmark" in line
    assert "-1.8%" in line


def test_triage_card_carries_the_measured_base_rate():
    from backend.thesis import triage

    row = {"symbol": "MGM", "issuer": "MGM Resorts", "family": "both",
           "officer_buyers": 3, "officer_value": 5e6, "board_backed_value": 0,
           "last_filing": "2026-01-05", "buyers": "A; B", "total_buyers": 4,
           "has_ceo_cfo": True}
    assert "base rate" not in triage.build_card(row)  # unmeasured, so unclaimed
    assert "base rate (3m, measured): 40 events beat" in triage.build_card(
        row, base_rate="40 events beat")


# --------------------------------------------------------------------------- #
# The engine grades its own output too
# --------------------------------------------------------------------------- #
def test_evaluating_a_thesis_enters_it_in_the_signal_log(memory_db, stub_command):
    from backend.models import SignalEvent
    from backend.thesis import spine

    thesis = Thesis(id=7, title="t", claim="c", symbols="MGM, LVS",
                    source="deep_dive", status="open", direction="long", prior=0.6,
                    created_at=datetime(2026, 2, 2),
                    review_by=datetime.now(timezone.utc) + timedelta(days=30),
                    checks=[make_check(comparator="lt", threshold=5.0)])
    assert spine.evaluate_thesis(thesis) == "open"

    session = memory_db()
    events = session.query(SignalEvent).order_by(SignalEvent.symbol).all()
    assert [e.symbol for e in events] == ["LVS", "MGM"]  # a pair is two things to grade
    assert {e.family for e in events} == {"thesis:deep_dive"}
    assert events[0].known_on.date() == date(2026, 2, 2)  # anchored at creation
    assert events[0].payload["status"] == "open"
    session.close()

    # Re-evaluating refreshes the row rather than duplicating it.
    stub_command["rows"].append({"close": 4.0})
    assert spine.evaluate_thesis(thesis) == "broken"

    session = memory_db()
    assert session.query(SignalEvent).count() == 2
    assert session.query(SignalEvent).first().payload["status"] == "broken"
    session.close()


def test_a_thesis_with_no_symbols_is_not_logged(memory_db):
    from backend.models import SignalEvent
    from backend.thesis import spine

    spine.evaluate_thesis(Thesis(title="t", claim="c", symbols="", checks=[]))

    session = memory_db()
    assert session.query(SignalEvent).count() == 0  # nothing to price, nothing to learn
    session.close()


# --------------------------------------------------------------------------- #
# The clock that makes grading actually happen
# --------------------------------------------------------------------------- #
def test_grading_clock_can_be_switched_off(monkeypatch):
    from backend.thesis import scheduler

    monkeypatch.setattr(scheduler.settings, "grading_interval_hours", 0)
    assert scheduler.start() is None


def test_the_clock_keeps_sweeping_after_a_sweep_fails(monkeypatch):
    import asyncio
    import threading

    from backend.thesis import memory, scheduler

    calls, second_sweep = [], threading.Event()

    def _sweep(limit):
        calls.append(limit)
        if len(calls) == 1:
            raise RuntimeError("price API down")
        second_sweep.set()
        return {"graded": 3}

    monkeypatch.setattr(memory, "grade_pending", _sweep)
    monkeypatch.setattr(scheduler, "FIRST_SWEEP_DELAY_SECONDS", 0.0)

    async def drive():
        task = asyncio.create_task(scheduler._sweep_forever(0.01, 7))
        await asyncio.to_thread(second_sweep.wait, 5.0)
        await scheduler.stop(task)

    asyncio.run(drive())
    assert calls[:2] == [7, 7]  # a dead price API does not stop the clock
    assert second_sweep.is_set()
