"""Open RSS/Atom news providers.

Public financial newswires all publish free RSS. Aggregating a broad set of
them gives a world-news tape without an API key, and Google News' RSS search
gives per-company coverage beyond what Yahoo attaches to a ticker.

The catalogue is grouped by *desk* (markets, policy, energy, healthcare …).
A desk name is accepted anywhere a feed name is, so ``sources="energy"`` reads
the whole energy desk and ``sources="policy,cnbc_top"`` mixes a desk with one
feed. Nothing asked for means :data:`DEFAULT_CATEGORIES` — the wires and
desks that make up "the financial newswire" — rather than every specialist
feed, so the tape and the market-sentiment gauge keep their meaning as the
catalogue grows. ``all`` reads everything.

Wires that killed their public RSS (Reuters, WSJ, Barron's, Nikkei, Kitco)
are read through Google News' RSS search with the ``source:`` operator
instead — same headlines, still keyless, just titled "… - Reuters" and linked
via Google. Every feed here was verified to parse *and* to be current when it
was added; several publishers (WSJ, MarketWatch's pulse feed) still serve XML
years after they stopped updating it, which is why "it fetches" is not the
test.
"""
from __future__ import annotations

import html
import re
import time
import urllib.parse
import warnings
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from ..core.caching import TTL_INTRADAY, cached
from ..core.errors import EmptyDataError, ProviderError
from ..core.http import get_xml, strip_ns

NAME = "rss"


def _gnews_source(publisher: str) -> str:
    """Google News RSS pinned to one publisher's last 24 hours."""
    q = urllib.parse.quote_plus("source:{} when:1d".format(publisher))
    return "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en".format(q)


def _gnews_site(domain: str) -> str:
    """Google News RSS pinned to one domain's last 24 hours.

    ``source:`` matches publisher *names* loosely — ``source:nikkei`` also
    returns any outlet whose headline mentions the Nikkei index — so
    publishers whose name is also a thing get pinned by domain instead.
    """
    q = urllib.parse.quote_plus("site:{} when:1d".format(domain))
    return "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en".format(q)


def _gnews_topic(topic_id: str) -> str:
    """One of Google News' curated topic tapes (Business, World, Technology)."""
    return "https://news.google.com/rss/topics/{}?hl=en-US&gl=US&ceid=US:en".format(topic_id)


def _cnbc(section_id: int) -> str:
    return "https://www.cnbc.com/id/{}/device/rss/rss.html".format(section_id)


def _dive(site: str) -> str:
    """Industry Dive's trade titles all share one feed layout."""
    return "https://www.{}.com/feeds/news/".format(site)


def _nasdaq(category: str) -> str:
    return "https://www.nasdaq.com/feed/rssoutbound?category={}".format(urllib.parse.quote_plus(category))


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #
#: Feeds grouped by desk. Order matters twice: it is the order the source
#: table lists them in, and the first group a name appears in is its desk.
CATALOGUE: Dict[str, Dict[str, str]] = {
    # ---- The wire: market desks and the big business papers ----------------
    "markets": {
        "yahoo": "https://finance.yahoo.com/news/rssindex",
        "reuters": _gnews_source("reuters"),
        "bloomberg_markets": "https://feeds.bloomberg.com/markets/news.rss",
        "bloomberg_business": "https://feeds.bloomberg.com/business/news.rss",
        "wsj": _gnews_source("wsj"),
        "barrons": _gnews_source("barron's"),
        "ft_markets": "https://www.ft.com/markets?format=rss",
        "ft_companies": "https://www.ft.com/companies?format=rss",
        "marketwatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "marketwatch_bulletins": "https://feeds.content.dowjones.io/public/rss/mw_bulletins",
        "cnbc_top": _cnbc(100003114),
        "cnbc_markets": _cnbc(15839069),
        "cnbc_earnings": _cnbc(15839135),
        "cnbc_finance": _cnbc(10000664),
        "benzinga": "https://www.benzinga.com/feed",
        "thestreet": "https://www.thestreet.com/.rss/full/",
        "nasdaq": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
        "investing": "https://www.investing.com/rss/news.rss",
        "seeking_alpha": "https://seekingalpha.com/market_currents.xml",
        "seeking_alpha_breakfast": "https://seekingalpha.com/tag/wall-st-breakfast.xml",
        # BI's "custom/all" feed mixes personal essays and lifestyle pieces in
        # with the markets desk; the scorer reads those as real signal ("I lost
        # most of my vision…" scores bearish). Use the markets-only feed.
        "business_insider": "https://markets.businessinsider.com/rss/news",
        "kiplinger": "https://www.kiplinger.com/feeds/all",
        "etf_trends": "https://www.etftrends.com/feed/",
        "fox_markets": "https://moxie.foxbusiness.com/google-publisher/markets.xml",
        "cbs_moneywatch": "https://www.cbsnews.com/latest/rss/moneywatch",
        "nbc_business": "https://feeds.nbcnews.com/nbcnews/public/business",
        "abc_money": "https://abcnews.go.com/abcnews/moneyheadlines",
    },
    "business": {
        "google_business": _gnews_topic("CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB"),
        "bloomberg_industries": "https://feeds.bloomberg.com/industries/news.rss",
        "nyt_business": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "nyt_dealbook": "https://rss.nytimes.com/services/xml/rss/nyt/Dealbook.xml",
        "economist_business": "https://www.economist.com/business/rss.xml",
        "fortune": "https://fortune.com/feed/",
        "forbes": "https://www.forbes.com/business/feed/",
        "fox_business": "https://moxie.foxbusiness.com/google-publisher/latest.xml",
        "bbc_business": "https://feeds.bbci.co.uk/news/business/rss.xml",
        "guardian_business": "https://www.theguardian.com/uk/business/rss",
        "npr_business": "https://feeds.npr.org/1006/rss.xml",
        "latimes_business": "https://www.latimes.com/business/rss2.0.xml",
        "time_business": "https://time.com/business/feed/",
        "semafor": "https://www.semafor.com/rss.xml",
        "fast_company": "https://www.fastcompany.com/latest/rss",
        "cfo_dive": _dive("cfodive"),
    },
    "economy": {
        "bloomberg_economics": "https://feeds.bloomberg.com/economics/news.rss",
        "cnbc_economy": _cnbc(20910258),
        "nyt_economy": "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml",
        "economist_finance": "https://www.economist.com/finance-and-economics/rss.xml",
        "guardian_economics": "https://www.theguardian.com/business/economics/rss",
        "npr_economy": "https://feeds.npr.org/1017/rss.xml",
        "fox_economy": "https://moxie.foxbusiness.com/google-publisher/economy.xml",
        "investing_economy": "https://www.investing.com/rss/news_14.rss",
        "fred_blog": "https://fredblog.stlouisfed.org/feed/",
        "liberty_street": "https://libertystreeteconomics.newyorkfed.org/feed/",
        "hiring_lab": "https://www.hiringlab.org/feed/",
        # Statistical agencies: the release itself, the day it lands.
        "bea": "https://apps.bea.gov/rss/rss.xml",
        "census_indicators": "https://www.census.gov/economic-indicators/indicator.xml",
        "ons": "https://www.ons.gov.uk/releasecalendar?rss",
    },
    # ---- Central banks, regulators, governments -----------------------------
    "policy": {
        "federal_reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
        "fed_monetary": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "fed_speeches": "https://www.federalreserve.gov/feeds/speeches.xml",
        "ecb": "https://www.ecb.europa.eu/rss/press.html",
        "bank_of_england": "https://www.bankofengland.co.uk/rss/news",
        "bank_of_japan": "https://www.boj.or.jp/en/rss/whatsnew.xml",
        "bank_of_canada": "https://www.bankofcanada.ca/content_type/press-releases/feed/",
        "rba": "https://www.rba.gov.au/rss/rss-cb-media-releases.xml",
        "rbnz": "https://www.rbnz.govt.nz/feeds/news",
        "riksbank": "https://www.riksbank.se/en-gb/rss/press-releases/",
        "rbi": "https://www.rbi.org.in/pressreleases_rss.xml",
        # Every central banker's speech, worldwide, as the BIS collects them.
        "bis_speeches": "https://www.bis.org/doclist/cbspeeches.rss",
        "sec": "https://www.sec.gov/news/pressreleases.rss",
        "sec_statements": "https://www.sec.gov/news/speeches-statements.rss",
        "cftc": "https://www.cftc.gov/RSS/RSSGP/rssgp.xml",
        "occ": "https://www.occ.gov/rss/occ_news.xml",
        "cfpb": "https://www.consumerfinance.gov/about-us/newsroom/feed/",
        "ftc": "https://www.ftc.gov/feeds/press-release.xml",
        "doj": "https://www.justice.gov/news/rss?type=press_release",
        "federal_register": "https://www.federalregister.gov/api/v1/documents.rss?conditions%5Bsignificant%5D=1",
        "cbo": "https://www.cbo.gov/publications/all/rss.xml",
        "ustr": "https://ustr.gov/rss.xml",
        "hm_treasury": "https://www.gov.uk/government/organisations/hm-treasury.atom",
        "fca": "https://www.fca.org.uk/news/rss.xml",
        "european_commission": "https://ec.europa.eu/commission/presscorner/api/rss?language=en",
        "wto": "https://www.wto.org/library/rss/latest_news_e.xml",
        "bloomberg_politics": "https://feeds.bloomberg.com/politics/news.rss",
        "thehill_business": "https://thehill.com/business/feed/",
    },
    # ---- Business press outside the US --------------------------------------
    "world": {
        "cnbc_world": _cnbc(100727362),
        "cnbc_asia": _cnbc(19832390),
        "cnbc_europe": _cnbc(19794221),
        "economist_china": "https://www.economist.com/china/rss.xml",
        "economist_asia": "https://www.economist.com/asia/rss.xml",
        "economist_europe": "https://www.economist.com/europe/rss.xml",
        "nikkei": _gnews_site("asia.nikkei.com"),
        "scmp_business": "https://www.scmp.com/rss/92/feed",
        "scmp_economy": "https://www.scmp.com/rss/318208/feed",
        "japan_times": "https://www.japantimes.co.jp/business/feed/",
        "straits_times": "https://www.straitstimes.com/news/business/rss.xml",
        "cna_business": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6936",
        "economic_times": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "livemint": "https://www.livemint.com/rss/markets",
        "business_standard": "https://www.business-standard.com/rss/markets-106.rss",
        "financial_post": "https://financialpost.com/feed",
        "smh_business": "https://www.smh.com.au/rss/business.xml",
        "abc_au_business": "https://www.abc.net.au/news/feed/51892/rss.xml",
        "moneyweb": "https://www.moneyweb.co.za/feed/",
        "dw_business": "https://rss.dw.com/rdf/rss-en-bus",
        "france24_business": "https://www.france24.com/en/business/rss",
        "euronews_business": "https://www.euronews.com/rss?level=theme&name=business",
        "sky_business": "https://feeds.skynews.com/feeds/rss/business.xml",
        "independent_business": "https://www.independent.co.uk/news/business/rss",
        "cityam": "https://www.cityam.com/feed/",
        "this_is_money": "https://www.thisismoney.co.uk/money/index.rss",
        "moneyweek": "https://moneyweek.com/feed/all",
        "rte_business": "https://www.rte.ie/feeds/rss/?index=/news/business/",
    },
    "geopolitics": {
        "google_world": _gnews_topic("CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FtVnVHZ0pWVXlnQVAB"),
        "bbc_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "nyt_world": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        "guardian_world": "https://www.theguardian.com/world/rss",
        "aljazeera": "https://www.aljazeera.com/xml/rss/all.xml",
        "politico_eu": "https://www.politico.eu/feed/",
        "foreign_policy": "https://foreignpolicy.com/feed/",
        "foreign_affairs": "https://www.foreignaffairs.com/rss.xml",
        "cfr": "https://feeds.cfr.org/publication/blog",
        "economist_briefing": "https://www.economist.com/briefing/rss.xml",
    },
    # ---- Commentary: columnists, newsletters, blogs, think tanks ------------
    "opinion": {
        "bloomberg_opinion": "https://feeds.bloomberg.com/bview/news.rss",
        "economist_leaders": "https://www.economist.com/leaders/rss.xml",
        "ft_alphaville": "https://www.ft.com/alphaville?format=rss",
        "investing_analysis": "https://www.investing.com/rss/market_overview.rss",
        "project_syndicate": "https://www.project-syndicate.org/rss",
        "bruegel": "https://www.bruegel.org/rss.xml",
        "calculated_risk": "https://calculatedrisk.substack.com/feed",
        "econbrowser": "https://econbrowser.com/feed",
        "marginal_revolution": "https://marginalrevolution.com/feed",
        "naked_capitalism": "https://www.nakedcapitalism.com/feed",
        "wolf_street": "https://wolfstreet.com/feed/",
        "zerohedge": "https://feeds.feedburner.com/zerohedge/feed",
        "mish_talk": "https://mishtalk.com/feed/",
        "big_picture": "https://ritholtz.com/feed/",
        "abnormal_returns": "https://abnormalreturns.com/feed/",
        "wealth_of_common_sense": "https://awealthofcommonsense.com/feed/",
        "of_dollars_and_data": "https://ofdollarsanddata.com/feed/",
        "damodaran": "https://aswathdamodaran.blogspot.com/feeds/posts/default",
        "yardeni": "https://yardeniquicktakes.com/feed/",
        "net_interest": "https://www.netinterest.co/feed",
        "klement": "https://klementoninvesting.substack.com/feed",
        "doomberg": "https://doomberg.substack.com/feed",
        "noahpinion": "https://www.noahpinion.blog/feed",
        "krugman": "https://paulkrugman.substack.com/feed",
        "grumpy_economist": "https://www.grumpy-economist.com/feed",
        "kyla_scanlon": "https://kyla.substack.com/feed",
        "chartbook": "https://adamtooze.substack.com/feed",
        "big_stoller": "https://www.thebignewsletter.com/feed",
    },
    # ---- Sector desks -------------------------------------------------------
    "tech": {
        "cnbc_tech": _cnbc(19854910),
        "nyt_tech": "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
        "bbc_tech": "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "bloomberg_tech": "https://feeds.bloomberg.com/technology/news.rss",
        "google_tech": _gnews_topic("CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FtVnVHZ0pWVXlnQVAB"),
        "the_information": _gnews_source('"the information"'),
        "techcrunch": "https://techcrunch.com/feed/",
        "the_verge": "https://www.theverge.com/rss/index.xml",
        "ars_technica": "https://feeds.arstechnica.com/arstechnica/index",
        "wired_business": "https://www.wired.com/feed/category/business/latest/rss",
        "the_register": "https://www.theregister.com/headlines.atom",
        "venturebeat": "https://venturebeat.com/feed/",
        "mit_tech_review": "https://www.technologyreview.com/feed/",
        "geekwire": "https://www.geekwire.com/feed/",
        "stratechery": "https://stratechery.com/feed/",
        "platformer": "https://www.platformer.news/feed",
        "semianalysis": "https://newsletter.semianalysis.com/feed",
        "ee_times": "https://www.eetimes.com/feed/",
        "semiconductor_engineering": "https://semiengineering.com/feed/",
        "digitimes": "https://www.digitimes.com/rss/daily.xml",
        "light_reading": "https://www.lightreading.com/rss.xml",
        "rcr_wireless": "https://www.rcrwireless.com/feed",
        "cio_dive": _dive("ciodive"),
        "cybersecurity_dive": _dive("cybersecuritydive"),
    },
    "energy": {
        "cnbc_energy": _cnbc(19836768),
        "nyt_energy": "https://rss.nytimes.com/services/xml/rss/nyt/EnergyEnvironment.xml",
        "oilprice": "https://oilprice.com/rss/main",
        "rigzone": "https://www.rigzone.com/news/rss/rigzone_latest.aspx",
        "natural_gas_intel": "https://www.naturalgasintel.com/feed/",
        "offshore_energy": "https://www.offshore-energy.biz/feed/",
        "eia_today": "https://www.eia.gov/rss/todayinenergy.xml",
        "utility_dive": _dive("utilitydive"),
        "power_magazine": "https://www.powermag.com/feed/",
        "world_nuclear_news": "https://www.world-nuclear-news.org/rss",
        "canary_media": "https://www.canarymedia.com/rss",
        "pv_magazine": "https://www.pv-magazine.com/feed/",
        "renewable_energy_world": "https://www.renewableenergyworld.com/feed/",
        "cleantechnica": "https://cleantechnica.com/feed/",
        "carbon_brief": "https://www.carbonbrief.org/feed",
    },
    "commodities": {
        "investing_commodities": "https://www.investing.com/rss/news_11.rss",
        "nasdaq_commodities": _nasdaq("Commodities"),
        "kitco": _gnews_source("kitco"),
        "mining_com": "https://www.mining.com/feed/",
        "northern_miner": "https://www.northernminer.com/feed/",
        "farmdoc_daily": "https://farmdocdaily.illinois.edu/feed",
        "farm_progress": "https://www.farmprogress.com/rss.xml",
        "brownfield_ag": "https://www.brownfieldagnews.com/feed/",
        "agri_pulse": "https://www.agri-pulse.com/rss/articles",
        "world_grain": "https://www.world-grain.com/rss/articles",
    },
    "fx": {
        "fxstreet": "https://www.fxstreet.com/rss/news",
        "forexlive": "https://www.forexlive.com/feed/news",
        "action_forex": "https://www.actionforex.com/feed/",
        "investing_forex": "https://www.investing.com/rss/news_1.rss",
    },
    "crypto": {
        "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "cointelegraph": "https://cointelegraph.com/rss",
        "the_block": "https://www.theblock.co/rss.xml",
        "decrypt": "https://decrypt.co/feed",
        "bitcoin_magazine": "https://bitcoinmagazine.com/.rss/full/",
        "cryptoslate": "https://cryptoslate.com/feed/",
        "protos": "https://protos.com/feed/",
        "investing_crypto": "https://www.investing.com/rss/news_301.rss",
    },
    # Equity-desk deep cuts: earnings, options flow, ETFs, dividends, IPOs.
    "stocks": {
        "nasdaq_earnings": _nasdaq("Earnings"),
        "nasdaq_options": _nasdaq("Options"),
        "nasdaq_etfs": _nasdaq("ETFs"),
        "nasdaq_dividends": _nasdaq("Dividends"),
        "nasdaq_ipos": _nasdaq("IPOs"),
        "nasdaq_stocks": _nasdaq("Stocks"),
        "investing_stocks": "https://www.investing.com/rss/news_25.rss",
        "seeking_alpha_ipo": "https://seekingalpha.com/tag/ipo-analysis.xml",
        "seeking_alpha_dividends": "https://seekingalpha.com/tag/dividend-ideas.xml",
        "motley_fool": "https://www.fool.com/feeds/index.aspx",
        "investorplace": "https://investorplace.com/feed/",
    },
    "finance": {
        "american_banker": "https://www.americanbanker.com/feed?rss=true",
        "banking_dive": _dive("bankingdive"),
        "payments_dive": _dive("paymentsdive"),
        "pymnts": "https://www.pymnts.com/feed/",
        "finextra": "https://www.finextra.com/rss/headlines.aspx",
        "techcrunch_fintech": "https://techcrunch.com/category/fintech/feed/",
        "insurance_journal": "https://www.insurancejournal.com/rss/news/national/",
        "reinsurance_news": "https://www.reinsurancene.ws/feed/",
        "artemis": "https://www.artemis.bm/feed/",
        "hedgeweek": "https://www.hedgeweek.com/feed/",
        "private_equity_international": "https://www.privateequityinternational.com/feed/",
        "crunchbase_news": "https://news.crunchbase.com/feed/",
        "wealth_management": "https://www.wealthmanagement.com/rss.xml",
        "esg_dive": _dive("esgdive"),
    },
    "healthcare": {
        "cnbc_health": _cnbc(10000108),
        "stat_news": "https://www.statnews.com/feed/",
        "endpoints": "https://endpts.com/feed/",
        "fierce_biotech": "https://www.fiercebiotech.com/rss/xml",
        "fierce_pharma": "https://www.fiercepharma.com/rss/xml",
        "fierce_healthcare": "https://www.fiercehealthcare.com/rss/xml",
        "biopharma_dive": _dive("biopharmadive"),
        "healthcare_dive": _dive("healthcaredive"),
        "medtech_dive": _dive("medtechdive"),
        "medcity_news": "https://medcitynews.com/feed/",
    },
    "real_estate": {
        "cnbc_real_estate": _cnbc(10000115),
        "housingwire": "https://www.housingwire.com/feed/",
        "mortgage_news_daily": "http://www.mortgagenewsdaily.com/rss/newsletter",
        "realtor_research": "https://www.realtor.com/research/feed/",
        "redfin_news": "https://www.redfin.com/news/feed/",
        "bisnow": "https://www.bisnow.com/rss",
        "commercial_observer": "https://commercialobserver.com/feed/",
        "inman": "https://www.inman.com/feed/",
    },
    "industrials": {
        "cnbc_autos": _cnbc(10000101),
        "automotive_dive": _dive("automotivedive"),
        "electrek": "https://electrek.co/feed/",
        "insideevs": "https://insideevs.com/rss/articles/all/",
        "manufacturing_dive": _dive("manufacturingdive"),
        "construction_dive": _dive("constructiondive"),
        "flightglobal": "https://www.flightglobal.com/rss",
        "spacenews": "https://spacenews.com/feed/",
        "defense_news": "https://www.defensenews.com/arc/outboundfeeds/rss/",
        "breaking_defense": "https://breakingdefense.com/feed/",
        "defense_one": "https://www.defenseone.com/rss/all/",
    },
    "logistics": {
        "freightwaves": "https://www.freightwaves.com/news/feed",
        "supply_chain_dive": _dive("supplychaindive"),
        "the_loadstar": "https://theloadstar.com/feed/",
        "transport_topics": "https://www.ttnews.com/rss.xml",
        "trucking_dive": _dive("truckingdive"),
        "gcaptain": "https://gcaptain.com/feed/",
        "splash247": "https://splash247.com/feed/",
        "hellenic_shipping": "https://www.hellenicshippingnews.com/feed/",
        "seatrade_maritime": "https://www.seatrade-maritime.com/rss.xml",
        "maritime_executive": "https://www.maritime-executive.com/articles.rss",
        "marinelink": "https://www.marinelink.com/news/rss",
    },
    "consumer": {
        "cnbc_retail": _cnbc(10000116),
        "retail_dive": _dive("retaildive"),
        "modern_retail": "https://www.modernretail.co/feed/",
        "grocery_dive": _dive("grocerydive"),
        "supermarket_news": "https://www.supermarketnews.com/rss.xml",
        "food_dive": _dive("fooddive"),
        "restaurant_dive": _dive("restaurantdive"),
        "hotel_dive": _dive("hoteldive"),
    },
    "media": {
        "cnbc_media": _cnbc(10000110),
        "variety": "https://variety.com/v/biz/feed/",
        "hollywood_reporter": "https://www.hollywoodreporter.com/c/business/feed/",
        "digiday": "https://digiday.com/feed/",
        "marketing_dive": _dive("marketingdive"),
    },
}

#: What ``world_news()`` reads when nothing is asked for: the newswire proper.
#: Sector desks, commentary and crypto are opt-in so a market-mood reading
#: stays a reading of the market rather than of whichever desk is busiest.
DEFAULT_CATEGORIES: Tuple[str, ...] = ("markets", "business", "economy", "policy", "world")

#: Flat ``name -> url`` view; the first desk to claim a name wins.
FEEDS: Dict[str, str] = {}
#: ``name -> desk``.
CATEGORY_OF: Dict[str, str] = {}
for _desk, _group in CATALOGUE.items():
    for _name, _url in _group.items():
        FEEDS.setdefault(_name, _url)
        CATEGORY_OF.setdefault(_name, _desk)

#: Names that used to exist. WSJ and MarketWatch's pulse feed still serve XML
#: but stopped updating in 2025; Bloomberg moved from a Google News proxy to
#: its own feeds. Old names keep resolving so saved links and scripts survive.
ALIASES: Dict[str, str] = {
    "wsj_markets": "wsj",
    "wsj_business": "wsj",
    "wsj_tech": "wsj",
    "marketwatch_pulse": "marketwatch_bulletins",
    "bloomberg": "bloomberg_markets",
}

_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")  # a letter after "<": "yields <5% and >3%" is prose
_BLOCK_RE = re.compile(r"<(style|script)\b.*?</\1\s*>", re.S | re.I)
# A CSS at-rule with one level of nesting, e.g. "@media (…) { .x { … } }".
# Nasdaq's summaries open with one, bare, outside any <style> tag.
_CSS_RE = re.compile(r"@(?:media|font-face|import|keyframes)\b[^{;]*(?:\{(?:[^{}]|\{[^{}]*\})*\}|;)", re.S)
_WS_RE = re.compile(r"\s+")


def _clean(text: Optional[str]) -> str:
    """Feed field to plain text.

    Descriptions arrive as raw HTML in CDATA or as *escaped* HTML, so tags are
    stripped both before and after unescaping; entities are unescaped twice
    for the same reason (``&amp;amp;`` is common). ``<style>``/``<script>``
    blocks and stray CSS at-rules go whole rather than leaving their text.
    """
    if not text:
        return ""
    out = _TAG_RE.sub("", _BLOCK_RE.sub("", text))
    out = html.unescape(out)
    out = _TAG_RE.sub("", _BLOCK_RE.sub("", out))
    out = html.unescape(_CSS_RE.sub("", out))
    return _WS_RE.sub(" ", out).strip()


def _text(node: Any) -> str:
    """All text under an element, markup stripped.

    ``node.text`` alone misses feeds that nest markup inside the field —
    Fierce's titles are ``<title><a href=…>Headline</a></title>`` — and would
    silently drop every story from them for want of a title.
    """
    return _clean("".join(node.itertext()))


def entry_stamp(published: Optional[str]) -> pd.Timestamp:
    """A feed's ``pubDate`` as a UTC timestamp, ``NaT`` when unreadable.

    RSS dates are RFC 2822, which allows the obsolete US zone names — the BEA
    stamps its releases ``EDT``, which pandas refuses to parse. The stdlib mail
    parser knows those, so it goes first and pandas is the fallback for the
    ISO-ish spellings Atom feeds use and the odd house style ("Aug 17, 2026
    1:55pm").
    """
    if not published:
        return pd.NaT
    try:
        stamp = pd.Timestamp(parsedate_to_datetime(published))
    except (TypeError, ValueError):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            stamp = pd.to_datetime(published, errors="coerce", utc=True, format="mixed")
        if pd.isna(stamp):
            return pd.NaT
    return stamp.tz_convert("UTC") if stamp.tzinfo else stamp.tz_localize("UTC")


def entry_date(published: Optional[str]) -> Optional[str]:
    """A feed's ``pubDate`` as ``YYYY-MM-DD`` (see :func:`entry_stamp`)."""
    stamp = entry_stamp(published)
    return None if pd.isna(stamp) else str(stamp.date())


def _stamp_column(df: pd.DataFrame) -> pd.Series:
    """``published`` parsed row by row.

    Not ``pd.to_datetime`` on the whole column: pandas infers one format from
    the first row and coerces every row that differs to ``NaT``, and a tape
    merged from many publishers has many formats — Yahoo writes ``+0000``, the
    Dow Jones feeds ``GMT``, Atom feeds ISO 8601. One inference pass left most
    of the tape undated and therefore unsorted.
    """
    if "published" not in df.columns:
        return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    return pd.Series(pd.DatetimeIndex([entry_stamp(v) for v in df["published"]], tz="UTC"), index=df.index)


def parse_feed(url: str, source: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    """Parse an RSS 2.0, RSS 1.0 (RDF) or Atom feed into plain records."""
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
                record["title"] = _text(child)
            elif ctag in ("description", "summary", "content"):
                record.setdefault("summary", _text(child))
            elif ctag == "link":
                record["url"] = child.attrib.get("href") or _clean(child.text)
            elif ctag in ("pubDate", "published", "updated", "date"):
                record.setdefault("published", _clean(child.text))
            elif ctag == "creator" or ctag == "author":
                record["author"] = _text(child) or record.get("author")
            elif ctag == "category":
                record.setdefault("tags", []).append(child.attrib.get("term") or _clean(child.text))
        if record.get("title"):
            items.append(record)
        if len(items) >= limit:
            break
    return items


def resolve_sources(sources: Optional[str] = None) -> List[str]:
    """Expand a ``sources`` string into feed names, in order, without repeats.

    Each comma-separated token may be a feed name, a retired alias, a desk
    (``energy``), ``default`` or ``all``. Nothing means :data:`DEFAULT_CATEGORIES`.
    """
    tokens = [s.strip().lower() for s in (sources or "").split(",") if s.strip()] or ["default"]
    out: List[str] = []
    unknown: List[str] = []

    def add(names: Iterable[str]) -> None:
        for n in names:
            if n not in out:
                out.append(n)

    for tok in tokens:
        if tok == "all":
            add(FEEDS)
        elif tok == "default":
            for desk in DEFAULT_CATEGORIES:
                add(CATALOGUE[desk])
        elif tok in CATALOGUE:
            add(CATALOGUE[tok])
        elif tok in FEEDS:
            add([tok])
        elif tok in ALIASES:
            add([ALIASES[tok]])
        else:
            unknown.append(tok)
    if unknown:
        raise ValueError(
            "Unknown source(s) {}. Desks: {}, or all. Feed names are listed by "
            "/news/sources.".format(", ".join(unknown), ", ".join(CATALOGUE))
        )
    return out


#: Wall-clock budget for one tape build. A feed that has not answered by then
#: is reported in ``warnings`` and left to finish in the background (its
#: result still lands in the HTTP cache for the next call), so one hung
#: publisher cannot hold the whole tape hostage.
TAPE_DEADLINE_SECONDS = 25.0


@cached("rss.world", ttl=TTL_INTRADAY)
def world_news(sources: Optional[str] = None, limit: int = 50) -> pd.DataFrame:
    """Merged newswire tape across the configured public feeds.

    ``sources`` takes feed names, desk names, ``default`` or ``all`` (see
    :func:`resolve_sources`). Each feed contributes at most
    ``max(5, limit // n_feeds)`` stories so the high-frequency publishers cannot
    crowd the weeklies and central banks off a recency-sorted tape. Feeds are
    fetched concurrently — the default desks alone are ~100 feeds.
    """
    wanted = resolve_sources(sources)
    per_feed = max(5, limit // len(wanted))
    rows: List[Dict[str, Any]] = []
    errors: List[str] = []

    def pull(name: str) -> List[Dict[str, Any]]:
        return parse_feed(FEEDS[name], source=name, limit=per_feed)

    pool = ThreadPoolExecutor(max_workers=16)
    futures = [(name, pool.submit(pull, name)) for name in wanted]
    deadline = time.monotonic() + TAPE_DEADLINE_SECONDS
    try:
        for name, fut in futures:
            remaining = deadline - time.monotonic()
            try:
                rows.extend(fut.result(timeout=max(0.0, remaining)))
            except FutureTimeout:
                errors.append("{}: no answer within {:.0f}s".format(name, TAPE_DEADLINE_SECONDS))
            except Exception as exc:  # noqa: BLE001 - one dead feed must not kill the tape
                errors.append("{}: {}".format(name, exc))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if not rows:
        raise ProviderError("Every news feed failed. {}".format("; ".join(errors)))
    df = pd.DataFrame(rows)
    df["date"] = _stamp_column(df)
    df["category"] = df["source"].map(CATEGORY_OF)
    df = df.sort_values("date", ascending=False, na_position="last")
    keep = [c for c in ("date", "title", "source", "category", "summary", "url", "author") if c in df.columns]
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
    df["date"] = _stamp_column(df)
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
    df["date"] = _stamp_column(df)
    keep = [c for c in ("date", "title", "summary", "url", "source") if c in df.columns]
    return df[keep].sort_values("date", ascending=False).head(limit).reset_index(drop=True)


def available_sources(category: Optional[str] = None) -> pd.DataFrame:
    """One row per feed: name, desk, whether the default tape reads it, URL."""
    if category is not None:
        cat = category.strip().lower()
        if cat not in CATALOGUE:
            raise ValueError("Unknown category {!r}. Available: {}".format(category, ", ".join(CATALOGUE)))
        desks = [cat]
    else:
        desks = list(CATALOGUE)
    rows = []
    for desk in desks:
        for name in CATALOGUE[desk]:
            if CATEGORY_OF[name] != desk:
                continue  # listed under its first desk only
            rows.append({"source": name, "category": desk,
                         "default": desk in DEFAULT_CATEGORIES, "feed_url": FEEDS[name]})
    return pd.DataFrame(rows)


def available_categories() -> pd.DataFrame:
    """One row per desk: name, feed count, whether it is on the default tape."""
    return pd.DataFrame([
        {"category": desk, "feeds": len(group), "default": desk in DEFAULT_CATEGORIES}
        for desk, group in CATALOGUE.items()
    ])
