"""Open RSS/Atom news providers.

Public financial newswires all publish free RSS. Aggregating a broad set of
them gives a world-news tape without an API key, and Google News' RSS search
gives per-company coverage beyond what Yahoo attaches to a ticker.

Wires that killed their public RSS (Reuters, Bloomberg, Barron's) are read
through Google News' RSS search with the ``source:`` operator instead — same
headlines, still keyless, just titled "… - Reuters" and linked via Google.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.caching import TTL_INTRADAY, cached
from ..core.errors import EmptyDataError, ProviderError
from ..core.http import get_xml, strip_ns

NAME = "rss"


def _gnews_source(publisher: str) -> str:
    """Google News RSS pinned to one publisher's last 24 hours."""
    q = urllib.parse.quote_plus("source:{} when:1d".format(publisher))
    return "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en".format(q)


FEEDS: Dict[str, str] = {
    # Market wires & business desks
    "yahoo": "https://finance.yahoo.com/news/rssindex",
    "wsj_markets": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "wsj_business": "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
    "wsj_tech": "https://feeds.a.dj.com/rss/RSSWSJD.xml",
    "ft_markets": "https://www.ft.com/markets?format=rss",
    "nyt_business": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "nyt_economy": "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml",
    "nyt_dealbook": "https://rss.nytimes.com/services/xml/rss/nyt/Dealbook.xml",
    "economist_finance": "https://www.economist.com/finance-and-economics/rss.xml",
    "economist_business": "https://www.economist.com/business/rss.xml",
    "marketwatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "marketwatch_pulse": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    "cnbc_top": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "cnbc_markets": "https://www.cnbc.com/id/15839069/device/rss/rss.html",
    "cnbc_economy": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "cnbc_earnings": "https://www.cnbc.com/id/15839135/device/rss/rss.html",
    "cnbc_finance": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    "cnbc_tech": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "fortune": "https://fortune.com/feed/",
    # BI's "custom/all" feed mixes personal essays and lifestyle pieces in with
    # the markets desk; the scorer reads those as real signal ("I lost most of
    # my vision…" scores bearish). Use the markets-only feed.
    "business_insider": "https://markets.businessinsider.com/rss/news",
    "benzinga": "https://www.benzinga.com/feed",
    "thestreet": "https://www.thestreet.com/.rss/full/",
    "nasdaq": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
    "investing": "https://www.investing.com/rss/news.rss",
    "seeking_alpha": "https://seekingalpha.com/market_currents.xml",
    # RSS-less wires via Google News (see module docstring)
    "reuters": _gnews_source("reuters"),
    "bloomberg": _gnews_source("bloomberg"),
    "barrons": _gnews_source("barron's"),
    # Policy & macro
    "federal_reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
    "fed_monetary": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "sec": "https://www.sec.gov/news/pressreleases.rss",
    "ecb": "https://www.ecb.europa.eu/rss/press.html",
    "bank_of_england": "https://www.bankofengland.co.uk/rss/news",
    "bbc_business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "guardian_business": "https://www.theguardian.com/uk/business/rss",
    "npr_business": "https://feeds.npr.org/1006/rss.xml",
    # Specialist desks
    "oilprice": "https://oilprice.com/rss/main",
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
}

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    return html.unescape(_TAG_RE.sub("", text)).strip()


def parse_feed(url: str, source: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    """Parse an RSS 2.0 or Atom feed into plain records."""
    root = get_xml(url, ttl=TTL_INTRADAY)
    items: List[Dict[str, Any]] = []
    for node in root.iter():
        tag = strip_ns(node.tag)
        if tag not in ("item", "entry"):
            continue
        record: Dict[str, Any] = {"source": source or url}
        for child in node:
            ctag = strip_ns(child.tag)
            if ctag == "title":
                record["title"] = _clean(child.text)
            elif ctag in ("description", "summary", "content"):
                record.setdefault("summary", _clean(child.text))
            elif ctag == "link":
                record["url"] = child.attrib.get("href") or _clean(child.text)
            elif ctag in ("pubDate", "published", "updated", "date"):
                record.setdefault("published", _clean(child.text))
            elif ctag == "creator" or ctag == "author":
                record["author"] = _clean(child.text) or record.get("author")
            elif ctag == "category":
                record.setdefault("tags", []).append(child.attrib.get("term") or _clean(child.text))
        if record.get("title"):
            items.append(record)
        if len(items) >= limit:
            break
    return items


@cached("rss.world", ttl=TTL_INTRADAY)
def world_news(sources: Optional[str] = None, limit: int = 50) -> pd.DataFrame:
    """Merged newswire tape across the configured public feeds.

    Each feed contributes at most ``max(5, limit // n_feeds)`` stories so the
    high-frequency publishers cannot crowd the weeklies and central banks off
    a recency-sorted tape. Feeds are fetched concurrently — with ~40 of them,
    a cold cache would otherwise take the better part of a minute.
    """
    wanted = [s.strip().lower() for s in (sources or "").split(",") if s.strip()] or list(FEEDS)
    unknown = [s for s in wanted if s not in FEEDS]
    if unknown:
        raise ValueError(
            "Unknown source(s) {}. Available: {}".format(", ".join(unknown), ", ".join(sorted(FEEDS)))
        )
    per_feed = max(5, limit // len(wanted))
    rows: List[Dict[str, Any]] = []
    errors: List[str] = []

    def pull(name: str) -> List[Dict[str, Any]]:
        return parse_feed(FEEDS[name], source=name, limit=per_feed)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [(name, pool.submit(pull, name)) for name in wanted]
        for name, fut in futures:
            try:
                rows.extend(fut.result())
            except Exception as exc:  # noqa: BLE001 - one dead feed must not kill the tape
                errors.append("{}: {}".format(name, exc))
    if not rows:
        raise ProviderError("Every news feed failed. {}".format("; ".join(errors)))
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df.get("published"), errors="coerce", utc=True)
    df = df.sort_values("date", ascending=False, na_position="last")
    keep = [c for c in ("date", "title", "source", "summary", "url", "author") if c in df.columns]
    out = df[keep].head(limit).reset_index(drop=True)
    out.attrs["errors"] = errors
    return out


@cached("rss.google", ttl=TTL_INTRADAY)
def google_news(query: str, limit: int = 30, language: str = "en-US", country: str = "US") -> pd.DataFrame:
    """Google News RSS search — broad coverage for a company or topic."""
    url = "https://news.google.com/rss/search?q={}&hl={}&gl={}&ceid={}:{}".format(
        urllib.parse.quote_plus(query), language, country, country, language.split("-")[0]
    )
    rows = parse_feed(url, source="google_news", limit=limit)
    if not rows:
        raise EmptyDataError("Google News returned nothing for {!r}".format(query))
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df.get("published"), errors="coerce", utc=True)
    df["query"] = query
    keep = [c for c in ("date", "title", "summary", "url", "source", "query") if c in df.columns]
    return df[keep].sort_values("date", ascending=False).head(limit).reset_index(drop=True)


def google_news_window(query: str, after: str, before: str, limit: int = 40,
                       language: str = "en-US", country: str = "US") -> pd.DataFrame:
    """Google News RSS restricted to a historical date window.

    ``after``/``before`` are ISO dates and behave as Google's search operators:
    stories dated ``after <= date < before``. Not cached here — callers cache
    the *scored* window instead, so past weeks are fetched exactly once.
    An empty window returns an empty frame rather than raising: quiet weeks
    are real data for a time series.
    """
    q = "{} after:{} before:{}".format(query, after, before)
    url = "https://news.google.com/rss/search?q={}&hl={}&gl={}&ceid={}:{}".format(
        urllib.parse.quote_plus(q), language, country, country, language.split("-")[0]
    )
    rows = parse_feed(url, source="google_news", limit=limit)
    if not rows:
        return pd.DataFrame(columns=["date", "title", "summary", "url", "source"])
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df.get("published"), errors="coerce", utc=True)
    keep = [c for c in ("date", "title", "summary", "url", "source") if c in df.columns]
    return df[keep].sort_values("date", ascending=False).head(limit).reset_index(drop=True)


def available_sources() -> pd.DataFrame:
    return pd.DataFrame([{"source": k, "feed_url": v} for k, v in sorted(FEEDS.items())])
