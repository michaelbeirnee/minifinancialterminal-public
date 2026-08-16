"""Data-platform mechanics: registry, REST generation, Python interface.

The structural tests here need no network; the few that hit live providers are
grouped at the end and marked so they can be deselected with ``-k "not live"``.
"""
import inspect
import typing

import pandas as pd
import pytest

from backend.core.api import _typed_signature, describe_all
from backend.core.errors import UnknownCommandError, UnknownProviderError
from backend.core.interface import mft
from backend.core.models import MFTObject, Result, build_object
from backend.core.registry import REGISTRY, children, coverage, execute, get_spec, resolve_provider
from backend.core.utils import date_window, jsonable, norm_symbols, to_records
from backend.extensions import MODULES, load_all


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_every_extension_module_registers_commands():
    assert load_all() == len(REGISTRY)
    assert len(REGISTRY) > 200
    tags = {spec.tag for spec in REGISTRY.values()}
    assert {"equity", "etf", "crypto", "currency", "derivatives", "economy",
            "fixedincome", "index", "news", "regulators", "commodity",
            "technical", "quantitative", "econometrics", "charting"} <= tags


def test_command_paths_are_absolute_and_unique():
    for path, spec in REGISTRY.items():
        assert path.startswith("/"), path
        assert path == spec.path
        assert not path.endswith("/"), path
    assert len(set(REGISTRY)) == len(REGISTRY)


def test_every_command_has_resolvable_type_hints():
    """FastAPI resolves these at import time, so a bad annotation is fatal."""
    for spec in REGISTRY.values():
        hints = typing.get_type_hints(spec.func)
        signature = _typed_signature(spec.func)
        for name, param in signature.parameters.items():
            assert not isinstance(param.annotation, str), (spec.path, name)
            if name in hints:
                assert param.annotation is hints[name]


def test_every_command_documents_itself():
    for spec in REGISTRY.values():
        assert spec.description, spec.path
        assert spec.providers, spec.path


def test_declared_providers_are_real():
    from backend.providers import PROVIDERS

    known = set(PROVIDERS) | {"google", "mft"}
    for spec in REGISTRY.values():
        assert set(spec.providers) <= known, (spec.path, spec.providers)


def test_menu_tree_navigation():
    submenus, cmds = children("")
    assert "equity" in submenus
    submenus, cmds = children("equity/price")
    assert {c.name for c in cmds} == {"historical", "quote", "performance"}


def test_coverage_report_adds_up():
    report = coverage()
    assert report["total_commands"] == len(REGISTRY)
    assert sum(report["by_menu"].values()) == len(REGISTRY)


# --------------------------------------------------------------------------- #
# Execution plumbing
# --------------------------------------------------------------------------- #
def test_unknown_command_raises():
    with pytest.raises(UnknownCommandError):
        execute("/equity/does/not/exist")


def test_unknown_parameter_is_rejected_before_the_provider_is_called():
    with pytest.raises(TypeError) as exc:
        execute("/equity/price/quote", symbol="AAPL", nonsense=1)
    assert "nonsense" in str(exc.value)


def test_unknown_provider_is_rejected():
    with pytest.raises(UnknownProviderError):
        resolve_provider("bloomberg", ("yahoo", "sec"))
    assert resolve_provider(None, ("yahoo", "sec")) == "yahoo"
    assert resolve_provider(None, ("yahoo", "sec"), default="sec") == "sec"


def test_dotted_path_lookup_matches_slash_path():
    assert get_spec("equity.price.historical") is get_spec("/equity/price/historical")


def test_build_object_normalises_frames_and_results():
    df = pd.DataFrame({"close": [1.0, 2.0]},
                      index=pd.to_datetime(["2024-01-02", "2024-01-03"]))
    df.index.name = "date"
    obj = build_object(Result(df, provider="yahoo", warnings=["w"]), command="/x")
    assert isinstance(obj, MFTObject)
    assert obj.provider == "yahoo" and obj.warnings == ["w"]
    assert obj.results == [{"date": "2024-01-02", "close": 1.0},
                           {"date": "2024-01-03", "close": 2.0}]
    assert list(obj.to_df().columns) == ["close"]


def test_jsonable_handles_numpy_and_missing_values():
    import numpy as np

    assert jsonable(np.int64(3)) == 3
    assert jsonable(np.float64("nan")) is None
    assert jsonable(pd.NaT) is None
    assert jsonable(pd.Timestamp("2024-05-06")) == "2024-05-06"
    assert jsonable({"a": np.array([1, 2])}) == {"a": [1, 2]}


def test_to_records_promotes_a_named_index():
    series = pd.Series([1, 2], index=pd.Index(["a", "b"], name="k"), name="v")
    assert to_records(series) == [{"k": "a", "v": 1}, {"k": "b", "v": 2}]
    frame = pd.DataFrame({"close": [1.0]}, index=pd.DatetimeIndex(["2024-01-02"], name="date"))
    assert to_records(frame) == [{"date": "2024-01-02", "close": 1.0}]


def test_to_records_keeps_duplicate_columns_instead_of_dropping_them():
    """pandas' to_dict("records") silently discards all but one on a collision."""
    frame = pd.DataFrame([[1, 2, 3]], columns=["a", "a", "b"])
    assert to_records(frame) == [{"a": 1, "a_1": 2, "b": 3}]


def test_to_records_survives_an_index_name_that_collides_with_a_column():
    """reset_index() raises outright here, so the promotion must not use it."""
    frame = pd.DataFrame({"x": [1]}, index=pd.Index(["A"], name="x"))
    assert to_records(frame) == [{"x": "A", "x_1": 1}]


def test_symbol_and_date_helpers():
    assert norm_symbols(" aapl , msft;aapl ") == ["AAPL", "MSFT"]
    start, end = date_window("2024-01-01", "2024-03-01")
    assert str(start) == "2024-01-01" and str(end) == "2024-03-01"
    start, end = date_window(None, "2024-03-01", default_days=30)
    assert (end - start).days == 30


# --------------------------------------------------------------------------- #
# REST surface
# --------------------------------------------------------------------------- #
def test_every_command_becomes_a_route(client):
    """Assert against the OpenAPI schema rather than walking ``app.routes``.

    How a framework stores its route tree is an implementation detail — Starlette
    1.3 stopped flattening included routers into the parent — but the generated
    schema is the public contract, and it is what clients actually read.
    """
    paths = set(client.app.openapi()["paths"])
    for spec in REGISTRY.values():
        assert "/api/v1" + spec.path in paths, spec.path
    assert "/api/auth/login" in paths


def test_openapi_schema_builds_for_the_whole_registry(client):
    schema = client.app.openapi()
    endpoint = schema["paths"]["/api/v1/equity/price/historical"]["get"]
    assert {p["name"] for p in endpoint["parameters"]} == {
        "symbol", "start_date", "end_date", "interval", "provider"}


def test_platform_routes_require_authentication(client):
    assert client.get("/api/v1/equity/price/quote?symbol=AAPL").status_code == 401


def test_registry_endpoint_describes_commands(auth_client):
    body = auth_client.get("/api/v1/_registry?menu=technical").json()
    assert body["count"] > 30
    assert all(r["path"].startswith("/technical") for r in body["results"])
    assert describe_all()[0]["parameters"] is not None


def test_registry_carries_documentation_and_examples(auth_client):
    """Every command ships parameter help; nearly all ship a runnable example."""
    body = auth_client.get("/api/v1/_registry").json()
    assert set(body["guides"]) >= {"equity", "economy", "technical", "news"}

    rsi = next(r for r in body["results"] if r["path"] == "/technical/rsi")
    by_name = {p["name"]: p for p in rsi["parameters"]}
    assert "look-back" in by_name["length"]["description"]
    assert rsi["example"]["params"] == {"symbol": "AAPL"}
    assert rsi["example"]["python"] == 'mft.technical.rsi(symbol="AAPL")'
    assert rsi["example"]["url"].startswith("/api/v1/technical/rsi?")

    # Common parameters are documented across the whole surface.
    documented = sum(
        1 for r in body["results"] for p in r["parameters"] if p.get("description")
    )
    total = sum(len(r["parameters"]) for r in body["results"])
    assert documented / total > 0.9

    with_examples = sum(1 for r in body["results"] if r["example"])
    assert with_examples / len(body["results"]) > 0.9


def test_search_endpoint(auth_client):
    body = auth_client.get("/api/v1/_search?query=yield").json()
    assert any("yield_curve" in r["path"] for r in body["results"])


def test_coverage_and_provider_endpoints(auth_client):
    assert auth_client.get("/api/v1/_coverage").json()["total_commands"] == len(REGISTRY)
    providers = auth_client.get("/api/system/providers").json()
    assert providers["count"] >= 15
    assert {p["name"] for p in providers["providers"]} >= {"yahoo", "sec", "fred", "treasury"}


def test_ticker_suggest_ranks_sensibly(auth_client):
    """The type-ahead directory: liquid names first, keywords resolve indices."""
    top = lambda q: [r["symbol"] for r in
                     auth_client.get("/api/data/suggest?q={}".format(q)).json()["results"]]
    assert top("sp")[0] == "SPY"
    assert top("apple")[0] == "AAPL"
    assert "^GSPC" in top("sp5")
    assert top("tesla")[0] == "TSLA"
    assert any(s.endswith("=F") for s in top("crude"))


def test_ticker_suggest_requires_auth(client):
    assert client.get("/api/data/suggest?q=sp").status_code == 401


def test_bad_provider_returns_400(auth_client):
    r = auth_client.get("/api/v1/equity/price/quote?symbol=AAPL&provider=bloomberg")
    assert r.status_code == 400
    assert "bloomberg" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Python interface
# --------------------------------------------------------------------------- #
def test_namespaces_are_generated_from_the_registry():
    assert "price" in dir(mft.equity)
    assert "historical" in dir(mft.equity.price)
    assert callable(mft.equity.price.historical)
    assert "Providers" in (mft.equity.price.historical.__doc__ or "")


def test_interface_rejects_unknown_attributes():
    with pytest.raises(AttributeError):
        mft.equity.nonexistent_menu


def test_interface_search_and_coverage():
    assert any("/economy/cpi" in line for line in mft.search("consumer price"))
    assert mft.coverage()["total_commands"] == len(REGISTRY)


# --------------------------------------------------------------------------- #
# Live provider checks (network required)
# --------------------------------------------------------------------------- #
def test_live_equity_history_via_python_interface():
    obj = mft.equity.price.historical(symbol="AAPL", start_date="2024-01-01",
                                      end_date="2024-03-01")
    frame = obj.to_df()
    assert len(frame) > 30
    assert {"open", "high", "low", "close", "volume"} <= set(frame.columns)
    assert (frame["high"] >= frame["low"]).all()
    assert obj.provider == "yahoo"


def test_live_equity_history_via_rest(auth_client):
    r = auth_client.get("/api/v1/equity/price/historical"
                        "?symbol=MSFT&start_date=2024-01-01&end_date=2024-03-01")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "yahoo"
    assert len(body["results"]) > 30
    assert set(body["results"][0]) >= {"date", "open", "high", "low", "close", "volume"}


def test_live_sec_fundamentals_come_from_xbrl():
    obj = mft.equity.fundamental.income(symbol="AAPL", period="annual", limit=4)
    frame = obj.to_df()
    assert obj.provider == "sec"
    assert len(frame) >= 2
    assert frame["revenue"].dropna().gt(0).all()


def test_live_treasury_yield_curve_is_monotonic_in_maturity():
    rows = mft.fixedincome.government.yield_curve().to_records()
    assert len(rows) >= 8
    maturities = [r["maturity_years"] for r in rows]
    assert maturities == sorted(maturities)


def test_live_technical_indicator_round_trip():
    obj = mft.technical.rsi(symbol="SPY", length=14, start_date="2024-01-01")
    values = obj.to_df()["rsi_14"].dropna()
    assert len(values) > 100
    assert values.between(0, 100).all()


def test_live_charting_returns_a_plotly_figure():
    figure = mft.charting.price(symbol="AAPL", start_date="2024-01-01").results
    assert set(figure) == {"data", "layout"}
    assert figure["data"][0]["type"] == "candlestick"
    assert figure["layout"]["title"]["text"].startswith("AAPL")
