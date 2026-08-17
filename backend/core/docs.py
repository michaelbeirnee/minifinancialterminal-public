"""Human documentation for the command registry.

Three layers, all served through ``/api/v1/_registry`` so the web UI, the CLI
and the OpenAPI docs stay in sync:

* ``MENU_GUIDES``   — what each top-level menu covers and how to approach it.
* ``PARAM_DOCS``    — a glossary of parameter names. Parameters repeat heavily
                      across the 245 commands (``symbol``, ``start_date``,
                      ``window``…), so one good sentence each covers nearly
                      every input in the platform.
* ``example_for()`` — a runnable example per command (REST URL, CLI line and
                      Python call), built from sensible sample values.
"""
from __future__ import annotations

import inspect
from typing import Any, Dict, List, Optional

from .registry import CommandSpec

# --------------------------------------------------------------------------- #
# Menu guides
# --------------------------------------------------------------------------- #
MENU_GUIDES: Dict[str, str] = {
    "overview": (
        "The daily brief: one command that composes index levels, rates, credit, "
        "breadth, movers and headlines into a single risk-regime read. Run it with "
        "no parameters — it is the same brief shown on the Markets tab."
    ),
    "equity": (
        "Everything about individual stocks. price/* pulls quotes and history, "
        "fundamental/* reads income statements, balance sheets, ratios and the "
        "revenue split by segment, geography and product straight "
        "from SEC filings (or Yahoo), compare/* builds a peer group and puts it "
        "side by side, estimates/* covers analyst targets, "
        "ownership/* shows who holds the stock, calendar/* lists earnings and IPO "
        "dates, discovery/* runs pre-built screens like day gainers, and shorts/* "
        "tracks short volume and fails-to-deliver. Most commands take a symbol — "
        "many accept a comma-separated list."
    ),
    "etf": (
        "Funds instead of single stocks: search ETFs, read their profile and fees, "
        "list holdings, sector and asset-class weights, and reverse-lookup which "
        "big ETFs hold a given stock (equity_exposure)."
    ),
    "crypto": (
        "Digital assets. Prices come from Yahoo pair tickers (BTC-USD) or "
        "CoinGecko coin ids (bitcoin). market ranks coins by market cap; global "
        "gives total market cap and BTC dominance."
    ),
    "currency": (
        "Foreign exchange. Yahoo uses pair tickers like EURUSD=X; the ECB/"
        "Frankfurter providers give official euro reference rates. snapshots shows "
        "one base currency against many at once."
    ),
    "derivatives": (
        "Options and futures. options/chains pulls a full chain (Yahoo per expiry, "
        "Cboe all expiries with greeks), unusual ranks contracts by volume vs open "
        "interest, surface builds the implied-vol surface. futures/curve quotes "
        "each listed contract month to draw the term structure — see "
        "futures/roots for supported roots like CL (oil) and GC (gold)."
    ),
    "index": (
        "Whole-market indexes: current membership of the S&P 500 and 15 other "
        "indices (from Wikipedia), index price history, regional snapshots, and "
        "century-long S&P 500 valuation series (CAPE, P/E, dividend yield) from "
        "multpl."
    ),
    "news": (
        "Headlines from free public RSS: company pulls stories for a ticker, "
        "world merges the newswire feeds (CNBC, MarketWatch, Fed, SEC…), search "
        "queries Google News for any topic."
    ),
    "sentiment": (
        "How positive or negative the news reads, on a -1 to +1 scale. Every "
        "command fetches fresh headlines and scores them against a "
        "finance-tuned word list — market reads the whole newswire, symbol "
        "summarises the mood per ticker, sectors scores all 11 GICS sectors, "
        "headlines shows story-by-story scores, and history rebuilds past "
        "mood week by week from the Google News archive (sector ETF tickers "
        "like XLE map to their sector's news). No model, no API key: each "
        "row lists the exact terms that moved its score, so every number can "
        "be checked by eye."
    ),
    "economy": (
        "Macro data. US series come from FRED (cpi, gdp, unemployment, "
        "money_measures…) — fred_series fetches any FRED id directly. "
        "Cross-country data comes from the World Bank and IMF (indicators, "
        "gdp_forecast, country_profile). transform=pc1 turns a level series into "
        "year-over-year percent change."
    ),
    "fixedincome": (
        "Rates and credit. government/* covers Treasury yield curves and "
        "auctions, corporate/* covers credit spreads (ICE BofA OAS, Moody's), "
        "rate/* covers policy and reference rates like SOFR and EFFR. spreads "
        "returns the classic 10Y-2Y / 10Y-3M / high-yield set in one call."
    ),
    "commodity": (
        "Oil, gold, gas, grains. price/spot uses key-free FRED benchmarks (WTI, "
        "Brent, Henry Hub); price/futures uses Yahoo continuous contracts; cot "
        "shows CFTC futures positioning. The EIA reports need a free API key."
    ),
    "regulators": (
        "Raw regulatory data. sec/* resolves tickers to CIKs, searches the full "
        "text of EDGAR filings since 2001, and lists SIC codes; cftc/* pulls the "
        "Commitments of Traders reports."
    ),
    "technical": (
        "35+ indicators computed on fresh price history — moving averages, RSI, "
        "MACD, Bollinger bands, ADX, Ichimoku, VWAP, volatility cones and more. "
        "Every command takes a symbol plus the usual date window; tune the "
        "indicator with length/fast/slow-style parameters. Results include the "
        "close price so charts can overlay."
    ),
    "screener": (
        "Rank the members of a whole index on computed metrics. run filters by "
        "market cap, trailing move over a chosen window (up or down), annualised "
        "volatility, and CAPM beta/alpha against the index's benchmark ETF; "
        "indexes lists the universes. The first run on an index downloads a year "
        "of prices for every member, so expect up to a minute cold and instant "
        "responses for the following hour."
    ),
    "quantitative": (
        "Statistics on return series: summary moments, normality and unit-root "
        "test batteries, rolling statistics, CAPM alpha/beta, and a full "
        "performance report (Sharpe, Sortino, max drawdown, VaR). Pass several "
        "symbols comma-separated to compare."
    ),
    "econometrics": (
        "Model building: OLS with diagnostics, Granger causality, cointegration, "
        "autocorrelation tests and panel regressions. Commands accept either "
        "symbol= (daily returns are built for you) or a POSTed data table of "
        "rows; panel models require the data table with entity and time columns."
    ),
    "charting": (
        "Server-rendered charts as Plotly JSON — candlesticks, comparisons, "
        "drawdown, correlation heatmaps, the yield curve. charting/command can "
        "chart the output of any other command. Mostly useful over the raw API; "
        "the web UI draws its own charts."
    ),
}

# --------------------------------------------------------------------------- #
# Parameter glossary
# --------------------------------------------------------------------------- #
PARAM_DOCS: Dict[str, str] = {
    "symbol": "Ticker symbol. Most commands accept a comma-separated list (AAPL,MSFT). "
              "Crypto uses pairs like BTC-USD, FX uses EURUSD=X, futures use CL=F.",
    "provider": "Which data source serves the request. Leave blank for the default; "
                "the command's provider list shows the alternatives.",
    "start_date": "Start of the date window, YYYY-MM-DD. Defaults to a sensible look-back.",
    "end_date": "End of the date window, YYYY-MM-DD. Defaults to today.",
    "interval": "Bar size: 1d daily (default), 1wk weekly, 1mo monthly; intraday 1m–1h "
                "works for recent windows only.",
    "limit": "Maximum rows to return.",
    "period": "Reporting period: annual, quarter, or ttm where supported.",
    "transform": "FRED unit transform: pc1 = % change vs a year ago, pch = period % change, "
                 "chg = change, log = natural log, lin = as reported.",
    "frequency": "Resample frequency where supported: d, w, m, q, a.",
    "country": "Country name or ISO code (united_states, DEU, japan). US routes to FRED, "
               "everything else to World Bank/IMF.",
    "indicator": "Indicator alias (gdp_growth, inflation, unemployment…) or a raw "
                 "World Bank / IMF code. See available_indicators.",
    "series_id": "FRED series id like GDP, CPIAUCSL or DGS10 — comma-separate several. "
                 "Find ids with fred_search.",
    "query": "Free-text search term.",
    "window": "Rolling window length in trading days.",
    "length": "Indicator look-back length in bars (e.g. 14 for RSI).",
    "fast": "Fast look-back in bars.",
    "slow": "Slow look-back in bars.",
    "signal": "Signal-line smoothing length in bars.",
    "std": "Band width in standard deviations.",
    "multiplier": "Band width as a multiple of ATR.",
    "benchmark": "Ticker to compare against; SPY approximates the US market.",
    "risk_free_rate": "Annual risk-free rate as a decimal (0.05 = 5%).",
    "rf_annual": "Annual risk-free rate as a decimal (0.05 = 5%).",
    "var_level": "Value-at-Risk tail probability (0.05 = worst 5% of days).",
    "target": "Series to analyse: returns, log_returns, or close prices.",
    "y_column": "Name of the dependent (left-hand side) column.",
    "x_columns": "Comma-separated explanatory column names; defaults to every other "
                 "numeric column.",
    "x_column": "Name of a single explanatory column.",
    "data": "Optional JSON array of row objects to analyse instead of fetching "
            "returns for symbol=. Required for panel models.",
    "entity_column": "Column identifying the panel entity (firm, country…).",
    "time_column": "Column identifying the time period.",
    "model": "Panel estimator: pooled, fixed, between, first_difference or fama_macbeth.",
    "lags": "Number of lags to test.",
    "expiration": "Option expiry date YYYY-MM-DD; defaults to the nearest listed. "
                  "See options/expirations for the list.",
    "option_type": "call or put; blank returns both sides.",
    "min_volume": "Ignore contracts that traded fewer times than this today.",
    "max_expirations": "How many expiries to include when building the surface.",
    "kind": "Which variant of the dataset to return — the command doc lists the options.",
    "form_type": "SEC form filter, comma-separated: 10-K, 10-Q, 8-K, 4, 13F-HR…",
    "forms": "SEC form filter, comma-separated: 10-K, 10-Q, 8-K…",
    "tag": "XBRL concept tag as filed, e.g. Revenues, Assets, NetIncomeLoss.",
    "taxonomy": "XBRL taxonomy: us-gaap for financials, dei for entity facts.",
    "units": "Filter to one XBRL unit, e.g. USD or shares.",
    "unit": "XBRL unit for the cross-section, usually USD.",
    "report": "COT report flavour: legacy, disaggregated, financial, supplemental "
              "(each also has a _combined variant).",
    "market": "For COT: part of the market name, e.g. GOLD or S&P. For market "
              "status/snapshots: US, GB, ASIA, EUROPE, RATES, COMMODITIES, "
              "CURRENCIES or CRYPTOCURRENCIES.",
    "sector": "Yahoo sector key: technology, healthcare, financial-services, energy, "
              "consumer-cyclical, consumer-defensive, industrials, basic-materials, "
              "utilities, real-estate, communication-services. For /screener/run: a "
              "sector name exactly as returned in the screen's sectors list.",
    "symbols": "Comma-separated tickers to screen as a custom universe instead of an "
               "index — a watchlist, say. Benchmarked against SPY.",
    "preset": "Named Yahoo screen — list them with screener_presets.",
    "filters": "Custom screen clauses 'operator,field,value' joined by semicolons, "
               "e.g. gt,intradaymarketcap,10000000000;lt,peratio,15.",
    "exchange": "Exchange filter: nasdaq, nyse or amex.",
    "sort_field": "Field to sort screen results by.",
    "day": "Calendar date YYYY-MM-DD; weekends roll forward to the next session.",
    "days": "Number of trading days of history to fetch (one file per session).",
    "months": "Months of history; each month is another archive download on a cold cache.",
    "summary_type": "FINRA table: ATS_W_SMBL per-symbol dark pool, OTC_W_SMBL non-ATS, "
                    "ATS_W_FIRM per-venue, ATS_W_VOL_STATS market-wide.",
    "vs_currency": "Quote currency for crypto prices, usually usd.",
    "coin_id": "CoinGecko coin id like bitcoin or ethereum — find it with crypto/search.",
    "category": "CoinGecko category slug to filter the market table.",
    "base": "Base currency code, e.g. USD.",
    "counter_currencies": "Comma-separated quote currencies; defaults to the majors.",
    "currencies": "Comma-separated currency codes.",
    "curve": "Treasury curve flavour: nominal, bill, real, long_term, real_long_term.",
    "region": "us for the Treasury curve, eu for the ECB AAA curve.",
    "rate": "Reference rate: sofr, effr, obfr, bgcr, tgcr, iorb, ameribor, sonia, "
            "estr, prime or fed_funds.",
    "security_type": "TreasuryDirect type: Bill, Note, Bond, TIPS, FRN or CMB.",
    "commodity": "Commodity name: wti, brent, natural_gas, gold, corn… — see "
                 "commodity/available.",
    "index": "Index name (sp500, nasdaq100, dowjones…) or a raw ticker like ^GSPC. "
             "index/available lists them.",
    "series": "Which series to return — the command doc lists the options.",
    "group": "Grouping: sector, style, asset_class or country.",
    "sources": "Comma-separated feed names — see news/sources.",
    "language": "Locale for Google News, e.g. en-US.",
    "root": "Futures root symbol, e.g. CL (WTI), GC (gold), ES (S&P e-mini).",
    "anchor": "Reset the VWAP accumulation each D day, W week or M month.",
    "quantile": "Quantile between 0 and 1 (0.05 = 5th percentile).",
    "method": "Correlation method: pearson, kendall or spearman.",
    "annualise": "Multiply by 252 trading days to annualise.",
    "regression": "Deterministic terms: c constant, ct constant+trend, n none.",
    "trading_days": "Trading days per year used to annualise, normally 252.",
    "chart_type": "candlestick or line.",
    "moving_averages": "Comma-separated SMA windows to overlay, e.g. 50,200.",
    "normalise": "Rebase every series to 100 at the window start.",
    "bins": "Number of histogram buckets.",
    "compare_date": "Second date to overlay for comparison.",
    "command_path": "Registry path of the command to run, e.g. /equity/price/quote.",
    "parameters": "JSON object of parameters passed to that command.",
    "y_columns": "Comma-separated numeric columns to plot.",
    "start_year": "First year of data.",
    "end_year": "Last year of data.",
    "dataflow": "OECD dataflow id from data-explorer.oecd.org, e.g. "
                "OECD.SDD.STES,DSD_STES@DF_CLI,.",
    "key": "SDMX series key; 'all' for everything in the dataflow.",
    "cik": "SEC Central Index Key, with or without leading zeros.",
    "as_of": "Point-in-time date YYYY-MM-DD; defaults to the latest available.",
    "measure": "Which measure to return — the command doc lists the options.",
    "universe": "Comma-separated ETF list to scan instead of the built-in one.",
    "prepost": "Include pre-market and after-hours bars.",
    "adjusted": "Adjust prices for splits and dividends.",
    "long": "Long look-back in bars.",
    "short": "Short look-back in bars.",
    "medium": "Medium look-back in bars.",
    "conversion": "Tenkan-sen (conversion line) length.",
    "span_b": "Senkou span B length.",
    "step": "Acceleration factor step.",
    "maximum": "Maximum acceleration factor.",
    "min_lag": "Smallest lag used in the estimate.",
    "max_lag": "Largest lag used in the estimate.",
    "skip": "Most recent bars to skip (momentum convention).",
    "timeframe": "Window the move is measured over: one_day, one_week, one_month, "
                 "three_month, six_month, ytd or one_year.",
    "direction": "Require the move to be up, down, or any.",
    "min_move": "Minimum absolute move over the timeframe, in percent (5 = at least ±5%).",
    "mcap_min": "Smallest market cap to include, in $ billions.",
    "mcap_max": "Largest market cap to include, in $ billions.",
    "vol_min": "Minimum annualised volatility, in percent (30 = 30%).",
    "vol_max": "Maximum annualised volatility, in percent.",
    "beta_min": "Minimum CAPM beta vs the index's benchmark ETF.",
    "beta_max": "Maximum CAPM beta vs the index's benchmark ETF.",
    "alpha_min": "Minimum annualised CAPM alpha vs the benchmark, in percent.",
    "alpha_max": "Maximum annualised CAPM alpha vs the benchmark, in percent.",
    "above_ma50": "true keeps only prices above their 50-day moving average; false only below.",
    "above_ma200": "true keeps only prices above their 200-day moving average; false only below.",
    "rsi_min": "Minimum 14-day RSI (0-100); above 70 is conventionally overbought.",
    "rsi_max": "Maximum 14-day RSI (0-100); below 30 is conventionally oversold.",
    "sort": "Column to order results by — any output column, or 'move' for the "
            "selected timeframe.",
    "ascending": "Sort smallest first instead of largest first.",
    "status_filter": "Filter runs by status: ok or error.",
    "favorites_only": "Return only favourites.",
    "title": "Chart title.",
    "name": "Which named dataset to return — the command doc lists the options.",
}

# Sample values used to build the runnable example for each command.
EXAMPLE_VALUES: Dict[str, Any] = {
    "symbol": "AAPL", "query": "apple", "series_id": "GDP", "indicator": "gdp_growth",
    "country": "united_states", "tag": "Revenues", "coin_id": "bitcoin",
    "command_path": "/equity/price/quote", "cik": "0000320193", "root": "CL",
    "market": "GOLD", "commodity": "gold", "index": "sp500", "sector": "technology",
    "preset": "day_gainers", "rate": "sofr", "group": "sector", "series": "shiller_pe",
    "base": "USD", "dataflow": "OECD.SDD.STES,DSD_STES@DF_CLI,",
    "y_column": "AAPL", "x_columns": "SPY",
}
# Menu-specific symbol samples.
SYMBOL_BY_MENU: Dict[str, str] = {
    "crypto": "BTC-USD", "currency": "EURUSD=X", "etf": "SPY", "derivatives": "AAPL",
    "index": "sp500", "commodity": "GC=F",
}


def describe_param(spec: CommandSpec, name: str) -> str:
    doc = PARAM_DOCS.get(name, "")
    if name == "symbol" and spec.tag in SYMBOL_BY_MENU:
        doc += " Example here: {}.".format(SYMBOL_BY_MENU[spec.tag])
    return doc


def example_for(spec: CommandSpec) -> Optional[Dict[str, Any]]:
    """A runnable example: params, REST URL, CLI line and Python call."""
    params: Dict[str, Any] = {}
    for p in spec.parameters:
        pname = p["name"]
        if pname == "provider":
            continue
        wanted = p["required"] or pname in ("symbol", "series_id", "indicator")
        if not wanted:
            continue
        if pname == "symbol":
            params[pname] = SYMBOL_BY_MENU.get(spec.tag, EXAMPLE_VALUES["symbol"])
        elif pname in EXAMPLE_VALUES:
            params[pname] = EXAMPLE_VALUES[pname]
        elif p["default"] not in (None, ""):
            params[pname] = p["default"]
        elif p["required"]:
            return None  # required input we cannot sensibly invent (e.g. a data table)
    from urllib.parse import urlencode

    query = urlencode(params)
    url = "/api/v1{}{}".format(spec.path, "?" + query if query else "")
    cli_args = " ".join("--{} {}".format(k, v) for k, v in params.items())
    cli = 'python -m cli.terminal "{}{}"'.format(spec.path, " " + cli_args if cli_args else "")
    python_call = "mft.{}({})".format(
        spec.path.strip("/").replace("/", "."),
        ", ".join('{}="{}"'.format(k, v) for k, v in params.items()),
    )
    return {"params": params, "url": url, "cli": cli, "python": python_call}


def full_doc(spec: CommandSpec) -> str:
    return inspect.getdoc(spec.func) or ""
