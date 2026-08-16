"""News menu: company headlines and a merged world newswire tape."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import norm_symbols
from ..providers import newsfeeds, sec, yahoo


@command("/news/company", providers=("yahoo", "google", "sec"), summary="Headlines for a ticker")
def news_company(symbol: str, limit: int = 25, provider: Optional[str] = None) -> Result:
    """Yahoo attaches curated stories to a ticker; Google News RSS casts wider;
    ``sec`` returns the company's filing stream as a news feed."""
    src = resolve_provider(provider, ("yahoo", "google", "sec"))
    symbols = norm_symbols(symbol, limit=10)
    rows: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for sym in symbols:
        try:
            if src == "google":
                frame = newsfeeds.google_news("{} stock".format(sym), limit)
                frame["symbol"] = sym
                rows.extend(frame.to_dict("records"))
            elif src == "sec":
                frame = sec.filings(sym, limit=limit)
                rows.extend(
                    {
                        "date": r["filing_date"], "symbol": sym,
                        "title": "{} — {}".format(r.get("form"), r.get("company")),
                        "summary": r.get("primary_document"), "url": r.get("url"), "source": "sec",
                    }
                    for r in frame.to_dict("records")
                )
            else:
                for item in yahoo.news(sym, limit):
                    content = item.get("content") or item
                    provider_info = content.get("provider") or {}
                    click = (content.get("clickThroughUrl") or content.get("canonicalUrl") or {})
                    rows.append(
                        {
                            "date": content.get("pubDate") or content.get("displayTime"),
                            "symbol": sym,
                            "title": content.get("title"),
                            "summary": content.get("summary") or content.get("description"),
                            "url": click.get("url") if isinstance(click, dict) else click,
                            "source": provider_info.get("displayName") if isinstance(provider_info, dict) else None,
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(sym, exc))

    if not rows:
        raise EmptyDataError("No company news found. {}".format("; ".join(warnings)))
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=True, format="mixed")
    df = df.sort_values("date", ascending=False, na_position="last").head(limit)
    return Result(df.reset_index(drop=True), provider=src, warnings=warnings)


@command("/news/world", providers=("rss", "google"), summary="Merged financial newswire tape")
def news_world(sources: Optional[str] = None, query: Optional[str] = None, limit: int = 50,
               provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("rss", "google"))
    if src == "google" or query:
        return Result(newsfeeds.google_news(query or "financial markets", limit), provider="google")
    df = newsfeeds.world_news(sources, limit)
    return Result(df, provider=src, warnings=df.attrs.get("errors", []))


@command("/news/sources", providers=("rss",), summary="Configured news feeds")
def news_sources() -> Result:
    return Result(newsfeeds.available_sources(), provider="rss")


@command("/news/search", providers=("google",), summary="Search the news for any topic")
def news_search(query: str, limit: int = 30, language: str = "en-US", country: str = "US",
                provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("google",))
    return Result(newsfeeds.google_news(query, limit, language, country), provider=src)
