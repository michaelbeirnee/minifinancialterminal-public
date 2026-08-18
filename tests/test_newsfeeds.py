"""Newswire catalogue and parser tests.

Everything here runs offline: the catalogue checks are pure data, the parser
is fed hand-written XML, and the tape merge monkeypatches ``parse_feed``. The
live check that every feed still answers lives in the probe scripts, not
here — feeds rot, and a test suite that fails whenever a publisher moves a
URL trains people to ignore it.
"""
import xml.etree.ElementTree as ET

import pandas as pd
import pytest

from backend.core.registry import execute
from backend.providers import newsfeeds


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #
def test_every_feed_belongs_to_exactly_one_desk():
    seen = {}
    for desk, group in newsfeeds.CATALOGUE.items():
        for name in group:
            assert name not in seen, "{} listed under {} and {}".format(name, seen[name], desk)
            seen[name] = desk
    assert set(seen) == set(newsfeeds.FEEDS)
    assert all(newsfeeds.CATEGORY_OF[n] == d for n, d in seen.items())


def test_default_desks_exist_and_are_the_newswire_not_the_sector_desks():
    for desk in newsfeeds.DEFAULT_CATEGORIES:
        assert desk in newsfeeds.CATALOGUE
    # Sector desks and commentary are opt-in so the market-mood reading stays
    # a reading of the market.
    for desk in ("healthcare", "crypto", "opinion", "energy"):
        assert desk not in newsfeeds.DEFAULT_CATEGORIES


def test_aliases_point_at_live_feeds_and_never_shadow_one():
    for old, new in newsfeeds.ALIASES.items():
        assert new in newsfeeds.FEEDS
        assert old not in newsfeeds.FEEDS


def test_feed_urls_are_absolute_and_unique():
    urls = list(newsfeeds.FEEDS.values())
    assert all(u.startswith(("http://", "https://")) for u in urls)
    assert len(set(urls)) == len(urls), "two names read the same URL"


# --------------------------------------------------------------------------- #
# resolve_sources
# --------------------------------------------------------------------------- #
def test_nothing_means_the_default_desks_in_catalogue_order():
    names = newsfeeds.resolve_sources(None)
    expected = [n for d in newsfeeds.DEFAULT_CATEGORIES for n in newsfeeds.CATALOGUE[d]]
    assert names == expected
    assert newsfeeds.resolve_sources("") == expected
    assert newsfeeds.resolve_sources("default") == expected


def test_a_desk_name_expands_to_its_feeds():
    assert newsfeeds.resolve_sources("fx") == list(newsfeeds.CATALOGUE["fx"])


def test_feeds_desks_and_aliases_mix_without_repeats():
    names = newsfeeds.resolve_sources("cnbc_top, FX, cnbc_top, wsj_markets, bloomberg")
    assert names == ["cnbc_top"] + list(newsfeeds.CATALOGUE["fx"]) + ["wsj", "bloomberg_markets"]


def test_all_reads_the_whole_catalogue():
    assert newsfeeds.resolve_sources("all") == list(newsfeeds.FEEDS)


def test_unknown_source_names_the_desks_in_the_error():
    with pytest.raises(ValueError) as exc:
        newsfeeds.resolve_sources("energy,nope")
    assert "nope" in str(exc.value)
    assert "energy" in str(exc.value)  # the desk list is part of the message


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw, expected", [
    ("Mon, 17 Aug 2026 10:00:00 EDT", "2026-08-17 14:00:00+00:00"),   # obsolete US zone name
    ("Mon, 17 Aug 2026 21:37:12 GMT", "2026-08-17 21:37:12+00:00"),
    ("Sun, 16 Aug 2026 14:00:00 +0000", "2026-08-16 14:00:00+00:00"),
    ("2026-08-18T01:04:42Z", "2026-08-18 01:04:42+00:00"),             # Atom
    ("2026-08-17T20:00:00-04:00", "2026-08-18 00:00:00+00:00"),
    ("Aug 17, 2026 1:55pm", "2026-08-17 13:55:00+00:00"),              # Fierce's house style
])
def test_entry_stamp_reads_every_house_style_as_utc(raw, expected):
    assert str(newsfeeds.entry_stamp(raw)) == expected
    assert newsfeeds.entry_date(raw) == expected[:10]


@pytest.mark.parametrize("raw", ["", None, "garbage", "32/13/2026"])
def test_unreadable_dates_are_nat_not_errors(raw):
    assert pd.isna(newsfeeds.entry_stamp(raw))
    assert newsfeeds.entry_date(raw) is None


def test_a_mixed_format_column_keeps_every_date():
    # pandas infers one format from the first row and coerces the rest to NaT;
    # a merged tape has many formats, so rows are parsed one at a time.
    df = pd.DataFrame({"published": [
        "Sun, 16 Aug 2026 14:00:00 +0000", "Mon, 17 Aug 2026 21:37:12 GMT",
        "2026-08-18T01:04:42Z", "Aug 17, 2026 1:55pm", None,
    ]})
    stamps = newsfeeds._stamp_column(df)
    assert stamps.isna().sum() == 1
    assert str(stamps.dt.tz) == "UTC"
    assert stamps.iloc[2] > stamps.iloc[1] > stamps.iloc[0]


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw, expected", [
    ("<p>Shares <b>rose</b> 3%</p>", "Shares rose 3%"),
    ("&lt;p&gt;Escaped &amp;amp; markup&lt;/p&gt;", "Escaped & markup"),          # escaped HTML
    ("<style>@media (max-width: 768px) { .x { width: 100% } }</style><p>Q2 call</p>", "Q2 call"),
    ("<script>track()</script>Real text", "Real text"),
    ("@media (max-width: 768px) {\n .image-container { width: 100% !important; }\n }\n\nImage source: The Fool", "Image source: The Fool"),
    ("Yields <5% and >3% still", "Yields <5% and >3% still"),                     # prose, not tags
    ("  two\n\n  lines ", "two lines"),
    (None, ""), ("", ""),
])
def test_clean_strips_markup_but_keeps_prose(raw, expected):
    assert newsfeeds._clean(raw) == expected


RSS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Wire</title>
<item><title><a href="/x" hreflang="en">Regeneron ends eye disease trial</a></title>
      <link>https://example.com/x</link><description>Phase 1/2a halted.</description>
      <pubDate>Aug 17, 2026 1:55pm</pubDate><dc:creator><a href="/p">Darren</a></dc:creator></item>
<item><title>Plain &amp; simple</title><link>https://example.com/y</link>
      <pubDate>Mon, 17 Aug 2026 10:00:00 GMT</pubDate><category>Markets</category></item>
<item><description>no title, dropped</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title type="html">Fed &lt;b&gt;holds&lt;/b&gt;</title>
       <link rel="alternate" href="https://example.com/a"/><updated>2026-08-18T01:04:42Z</updated>
       <summary>Rates unchanged.</summary><author><name>Board</name></author></entry>
</feed>"""

RDF = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="x"><title>DW</title><items><rdf:Seq><rdf:li resource="https://example.com/r"/></rdf:Seq></items></channel>
<item rdf:about="https://example.com/r"><title>Norway fund warns of bubble</title>
      <link>https://example.com/r</link><dc:date>2026-08-17T14:00:00Z</dc:date></item>
</rdf:RDF>"""


def _serve(monkeypatch, body):
    monkeypatch.setattr(newsfeeds, "get_xml", lambda url, **kw: ET.fromstring(body))


def test_parser_reads_titles_that_nest_markup(monkeypatch):
    _serve(monkeypatch, RSS)
    items = newsfeeds.parse_feed("http://x", source="fierce")
    assert [i["title"] for i in items] == ["Regeneron ends eye disease trial", "Plain & simple"]
    assert items[0]["author"] == "Darren"
    assert items[0]["url"] == "https://example.com/x"
    assert items[0]["summary"] == "Phase 1/2a halted."
    assert items[1]["tags"] == ["Markets"]
    assert all(i["source"] == "fierce" for i in items)


def test_parser_reads_atom_and_strips_escaped_markup(monkeypatch):
    _serve(monkeypatch, ATOM)
    (item,) = newsfeeds.parse_feed("http://x")
    assert item["title"] == "Fed holds"
    assert item["url"] == "https://example.com/a"
    assert item["published"] == "2026-08-18T01:04:42Z"
    assert item["summary"] == "Rates unchanged."


def test_parser_reads_rss_1_0_rdf_with_dc_date(monkeypatch):
    _serve(monkeypatch, RDF)
    (item,) = newsfeeds.parse_feed("http://x")
    assert item["title"] == "Norway fund warns of bubble"
    assert newsfeeds.entry_date(item["published"]) == "2026-08-17"


def test_parser_honours_limit(monkeypatch):
    _serve(monkeypatch, RSS)
    assert len(newsfeeds.parse_feed("http://x", limit=1)) == 1


# --------------------------------------------------------------------------- #
# world_news
# --------------------------------------------------------------------------- #
def _fake_parse(url, source="", limit=50):
    if source == "dead_feed":
        raise RuntimeError("HTTP 503")
    stamps = {"cnbc_top": "Mon, 17 Aug 2026 22:00:00 GMT",
              "fxstreet": "2026-08-18T01:00:00Z",
              "yahoo": "Sun, 16 Aug 2026 14:00:00 +0000"}
    return [{"source": source, "title": "{} story {}".format(source, i),
             "url": "https://example.com/{}/{}".format(source, i),
             "published": stamps.get(source, "Mon, 17 Aug 2026 12:00:00 GMT")}
            for i in range(limit)]


def test_world_news_merges_desks_sorts_by_recency_and_labels_the_desk(monkeypatch):
    monkeypatch.setattr(newsfeeds, "parse_feed", _fake_parse)
    monkeypatch.setitem(newsfeeds.FEEDS, "dead_feed", "http://dead")
    monkeypatch.setitem(newsfeeds.CATEGORY_OF, "dead_feed", "markets")
    df = newsfeeds.world_news.__wrapped__("fxstreet,cnbc_top,yahoo,dead_feed", limit=12)
    assert list(df.columns[:4]) == ["date", "title", "source", "category"]
    assert len(df) == 12
    assert df["date"].is_monotonic_decreasing
    assert df.iloc[0]["source"] == "fxstreet" and df.iloc[0]["category"] == "fx"
    assert set(df["source"]) == {"fxstreet", "cnbc_top", "yahoo"}
    assert df.attrs["errors"] == ["dead_feed: HTTP 503"]


def test_world_news_caps_each_feed_so_no_publisher_crowds_the_tape(monkeypatch):
    seen = {}

    def counting(url, source="", limit=50):
        seen[source] = limit
        return _fake_parse(url, source, limit)

    monkeypatch.setattr(newsfeeds, "parse_feed", counting)
    newsfeeds.world_news.__wrapped__("fxstreet,cnbc_top", limit=50)
    assert seen == {"fxstreet": 25, "cnbc_top": 25}
    seen.clear()
    newsfeeds.world_news.__wrapped__("fx", limit=8)
    assert set(seen) == set(newsfeeds.CATALOGUE["fx"])
    assert all(v == 5 for v in seen.values())  # never fewer than 5 per feed


def test_world_news_raises_when_every_feed_fails(monkeypatch):
    monkeypatch.setattr(newsfeeds, "parse_feed", _fake_parse)
    monkeypatch.setitem(newsfeeds.FEEDS, "dead_feed", "http://dead")
    with pytest.raises(newsfeeds.ProviderError):
        newsfeeds.world_news.__wrapped__("dead_feed", limit=5)


def test_a_hung_feed_is_reported_not_waited_for(monkeypatch):
    import threading
    release = threading.Event()

    def slow(url, source="", limit=50):
        if source == "yahoo":
            release.wait(5)
        return _fake_parse(url, source, limit)

    monkeypatch.setattr(newsfeeds, "parse_feed", slow)
    monkeypatch.setattr(newsfeeds, "TAPE_DEADLINE_SECONDS", 0.5)
    try:
        df = newsfeeds.world_news.__wrapped__("yahoo,cnbc_top", limit=10)
    finally:
        release.set()
    assert set(df["source"]) == {"cnbc_top"}
    assert len(df.attrs["errors"]) == 1 and df.attrs["errors"][0].startswith("yahoo: no answer within")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def test_news_sources_lists_desk_and_default_flag():
    df = execute("/news/sources").to_df()
    assert set(df.columns) == {"source", "category", "default", "feed_url"}
    assert len(df) == len(newsfeeds.FEEDS)
    row = df[df["source"] == "cnbc_top"].iloc[0]
    assert row["category"] == "markets" and bool(row["default"]) is True
    row = df[df["source"] == "coindesk"].iloc[0]
    assert row["category"] == "crypto" and bool(row["default"]) is False


def test_news_sources_filters_to_one_desk():
    df = execute("/news/sources", category="fx").to_df()
    assert list(df["source"]) == list(newsfeeds.CATALOGUE["fx"])
    with pytest.raises(ValueError):
        execute("/news/sources", category="nope")


def test_news_categories_counts_every_desk():
    df = execute("/news/categories").to_df()
    assert list(df["category"]) == list(newsfeeds.CATALOGUE)
    assert int(df["feeds"].sum()) == len(newsfeeds.FEEDS)
    assert set(df[df["default"]]["category"]) == set(newsfeeds.DEFAULT_CATEGORIES)


def test_news_world_passes_desks_through(monkeypatch):
    calls = {}

    def fake(sources, limit):
        calls["sources"], calls["limit"] = sources, limit
        out = pd.DataFrame([{"date": pd.Timestamp("2026-08-18", tz="UTC"), "title": "t",
                             "source": "fxstreet", "category": "fx", "url": "u"}])
        out.attrs["errors"] = ["x: boom"]
        return out

    monkeypatch.setattr(newsfeeds, "world_news", fake)
    res = execute("/news/world", sources="fx,cnbc_top", limit=7)
    assert calls == {"sources": "fx,cnbc_top", "limit": 7}
    assert res.warnings == ["x: boom"]
