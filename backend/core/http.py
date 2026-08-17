"""Cached HTTP access for the free/open data providers.

Every outbound request goes through here so that the whole platform shares one
connection pool, one retry policy and one on-disk cache. Providers therefore
stay tiny: build a URL, ask for JSON/CSV/XML, shape the frame.
"""
from __future__ import annotations

import io
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Mapping, Optional

import httpx
import pandas as pd

from ..cache import cache
from ..config import settings
from .errors import ProviderError

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
PLAIN_UA = "python-httpx/{}".format(httpx.__version__)

# Most public endpoints want a browser-looking client (Nasdaq, Stooq, Wikipedia
# and several RSS feeds 403 or hang otherwise). A few WAFs do the opposite and
# silently drop connections that claim to be a browser but are not, so those
# hosts get the honest client string instead.
_HOST_UA: Dict[str, str] = {
    "stlouisfed.org": PLAIN_UA,
    "imf.org": PLAIN_UA,
}

_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()

# Endpoints that are polite-rate-limited rather than hard-blocked; we simply
# space consecutive calls out instead of getting throttled.
_THROTTLE: Dict[str, float] = {
    "sec.gov": 0.12,
    "coingecko.com": 1.2,
    "cftc.gov": 0.3,
    # FRED's CSV download endpoint drops concurrent connections rather than
    # returning 429, so space requests out instead of relying on retries.
    "stlouisfed.org": 0.5,
    "nasdaq.com": 0.4,
    # Sentiment-history rebuilds walk the Google News archive window by
    # window — space the calls so a long backfill never reads as abuse.
    "news.google.com": 0.5,
    # A congressional sweep opens one document per filing; the Senate's search
    # service is small and there is no hurry.
    "efdsearch.senate.gov": 0.4,
}
_last_call: Dict[str, float] = {}
_throttle_lock = threading.Lock()


def get_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=httpx.Timeout(settings.http_timeout_seconds),
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_UA, "Accept-Encoding": "gzip, deflate"},
            )
        return _client


def _throttle(url: str) -> None:
    host = next((h for h in _THROTTLE if h in url), None)
    if not host:
        return
    gap = _THROTTLE[host]
    with _throttle_lock:
        wait = gap - (time.monotonic() - _last_call.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()


def fetch(
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    method: str = "GET",
    json_body: Any = None,
    ttl: Optional[int] = None,
    retries: int = 3,
    use_cache: bool = True,
) -> bytes:
    """Return the raw body for a request, served from cache when fresh."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    key = cache.make_key("http", method, url, sorted(params.items()), json_body)

    if use_cache:
        hit = cache.get(key, ttl=ttl)
        if hit is not None:
            return hit

    _throttle(url)
    headers = dict(headers or {})
    if "User-Agent" not in headers:
        override = next((ua for host, ua in _HOST_UA.items() if host in url), None)
        if override:
            headers["User-Agent"] = override
    client = get_client()
    last_error = ""
    for attempt in range(retries):
        try:
            # `params={}` would *replace* a query string already present in the
            # URL, so only pass it through when there is something to add.
            resp = client.request(
                method, url, params=params or None, headers=headers, json=json_body
            )
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code < 400:
                body = resp.content
                if use_cache:
                    cache.set(key, body)
                return body
            last_error = f"HTTP {resp.status_code}"
            # 4xx other than rate-limiting will not fix itself on a retry.
            if resp.status_code < 500 and resp.status_code != 429:
                break
        time.sleep(0.6 * (2**attempt))

    raise ProviderError(f"Request failed ({last_error}): {url}")


def get_json(url: str, **kwargs: Any) -> Any:
    import json

    body = fetch(url, **kwargs)
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ProviderError(f"Malformed JSON from {url}: {exc}")


def get_text(url: str, encoding: str = "utf-8", **kwargs: Any) -> str:
    return fetch(url, **kwargs).decode(encoding, errors="replace")


def get_csv(url: str, *, read_kwargs: Optional[Dict[str, Any]] = None, **kwargs: Any) -> pd.DataFrame:
    body = fetch(url, **kwargs)
    try:
        return pd.read_csv(io.BytesIO(body), **(read_kwargs or {}))
    except Exception as exc:  # noqa: BLE001 - pandas raises a wide range here
        raise ProviderError(f"Could not parse CSV from {url}: {exc}")


def get_xml(url: str, **kwargs: Any) -> ET.Element:
    body = fetch(url, **kwargs)
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise ProviderError(f"Could not parse XML from {url}: {exc}")


def get_html_tables(url: str, *, match: Optional[str] = None, **kwargs: Any) -> List[pd.DataFrame]:
    body = fetch(url, **kwargs)
    try:
        return pd.read_html(io.BytesIO(body), match=match) if match else pd.read_html(io.BytesIO(body))
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"No parsable HTML table at {url}: {exc}")


def strip_ns(tag: str) -> str:
    """``{http://ns}price`` -> ``price`` for the SDMX/XML feeds."""
    return tag.rsplit("}", 1)[-1]
