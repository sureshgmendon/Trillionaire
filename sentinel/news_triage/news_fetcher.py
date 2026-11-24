"""
Trillionaire Sentinel - Pre-Market News & Headline Aggregator
Asynchronously fetches overnight global market cues, US tech indicators,
and domestic Indian equity corporate announcements via HTTP adapters and mock providers.
"""

from dataclasses import dataclass, field
from datetime import datetime
import asyncio
import re
from typing import List, Optional
import xml.etree.ElementTree as ET
import httpx
from loguru import logger

from config.settings import settings
from sentinel.calendar_guard.event_calendar import get_market_holiday_name
from shared.constants import IST_TZ


@dataclass
class NewsItem:
    """Standardized pre-market news item."""
    headline: str
    summary: str = ""
    source: str = "AGGREGATOR"
    published_at: datetime = field(default_factory=lambda: datetime.now(IST_TZ))
    category: str = "GLOBAL_MACRO"  # GLOBAL_MACRO, US_MARKETS, DOMESTIC_CORPORATE, REGULATORY, MARKET_NOTICE
    symbols_mentioned: List[str] = field(default_factory=list)
    sectors_mentioned: List[str] = field(default_factory=list)
    url: str = ""


class AsyncNewsFetcher:
    """Fetches overnight news headlines from external APIs or deterministic mock providers."""

    def __init__(self, mock_news: Optional[List[NewsItem]] = None):
        self._mock_news = mock_news

    async def fetch_pre_market_news(self) -> List[NewsItem]:
        """
        Fetches headlines available prior to 08:30 AM IST.
        Uses injected mock news if provided; otherwise queries external API if configured,
        or falls back to canonical overnight market indicators.
        """
        if self._mock_news is not None:
            logger.info(f"Using {len(self._mock_news)} injected mock headlines for sentinel triage.")
            return self._mock_news

        # If live News API key is provided, attempt live fetch
        if settings.news_api_key:
            try:
                return await self._fetch_live_news()
            except Exception as e:
                logger.warning(f"Live news API fetch failed: {e}. Falling back to default cues.")

        # Default clean overnight market indicators
        return self._get_default_overnight_cues()

    async def _fetch_live_news(self) -> List[NewsItem]:
        """Queries external financial news provider via httpx."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://newsapi.org/v2/top-headlines",
                params={
                    "country": "in",
                    "category": "business",
                    "apiKey": settings.news_api_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            items: List[NewsItem] = []
            for art in data.get("articles", [])[:15]:
                items.append(
                    NewsItem(
                        headline=art.get("title", ""),
                        summary=art.get("description", "") or "",
                        source=art.get("source", {}).get("name", "NewsAPI"),
                        published_at=datetime.now(IST_TZ),
                        category="DOMESTIC_CORPORATE",
                    )
                )
            return items

    def get_default_overnight_cues(self) -> List[NewsItem]:
        """Synchronously returns baseline overnight news items or injected mock items."""
        if self._mock_news is not None:
            return self._mock_news
        return self._get_default_overnight_cues()

    async def fetch_rss_headlines(self) -> List[NewsItem]:
        """
        Token-free news fetcher querying Google News India RSS & Economic Times Markets RSS.
        Parses XML items into NewsItem objects with direct article URLs, deduplicating headlines.
        Prepends an NSE Market Notice if today is an exchange trading holiday.
        """
        if self._mock_news is not None:
            return self._mock_news

        items: List[NewsItem] = []
        now = datetime.now(IST_TZ)
        today_ist = now.date()
        holiday_name = get_market_holiday_name(today_ist)
        if holiday_name:
            items.append(
                NewsItem(
                    headline=f"NSE MARKET NOTICE: Indian Equity Markets Closed Today for {holiday_name}",
                    summary=f"NSE Equity Cash & Derivatives segments remain closed today in observance of {holiday_name}.",
                    source="NSE Official",
                    published_at=now,
                    category="MARKET_NOTICE",
                    url="https://www.nseindia.com/resources/exchange-communication-holidays",
                )
            )

        rss_feeds = [
            (
                "https://news.google.com/rss/search?q=(NSE+OR+Nifty+50+OR+Sensex+OR+RBI+OR+SEBI+OR+Reliance+OR+HDFC+Bank+OR+Infosys+OR+TCS)&hl=en-IN&gl=IN&ceid=IN:en",
                "Google News",
            ),
            (
                "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
                "Economic Times",
            ),
        ]

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        async def _fetch_feed(client: httpx.AsyncClient, url: str, default_source: str):
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return resp.content, default_source
            except Exception as ex:
                logger.debug(f"[RSS NEWS FETCHER] Error fetching {url}: {ex}")
            return None, default_source

        raw_feed_results = []
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                tasks = [_fetch_feed(client, url, src) for url, src in rss_feeds]
                raw_feed_results = await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.warning(f"[RSS NEWS FETCHER] Async HTTP client failed: {e}")

        seen_keys = set()
        for res in raw_feed_results:
            if isinstance(res, tuple) and res[0] is not None:
                content, default_source = res
                try:
                    root = ET.fromstring(content)
                    for elem in root.findall(".//item"):
                        raw_title = elem.findtext("title", "").strip()
                        raw_link = elem.findtext("link", "").strip()
                        raw_desc = elem.findtext("description", "").strip()

                        # Strip HTML tags from description if present
                        clean_desc = re.sub(r"<[^>]+>", "", raw_desc).strip()

                        source_elem = elem.find("source")
                        source_name = (
                            source_elem.text.strip()
                            if source_elem is not None and source_elem.text
                            else default_source
                        )

                        if not raw_title:
                            continue

                        # Clean title: Google news titles often end with " - Source"
                        clean_title = raw_title
                        if default_source == "Google News" and " - " in clean_title:
                            parts = clean_title.rsplit(" - ", 1)
                            clean_title = parts[0].strip()
                            if not source_name or source_name == "Google News":
                                source_name = parts[1].strip()

                        # Deduplication key
                        norm_key = re.sub(r"[^a-zA-Z0-9]", "", clean_title.lower())[:45]
                        if norm_key in seen_keys:
                            continue
                        seen_keys.add(norm_key)

                        items.append(
                            NewsItem(
                                headline=clean_title,
                                summary=clean_desc or clean_title,
                                source=source_name or "Financial Wire",
                                published_at=now,
                                category="DOMESTIC_CORPORATE",
                                url=raw_link or "https://www.nseindia.com",
                            )
                        )
                        if len(items) >= 20:
                            break
                except Exception as ex:
                    logger.debug(f"[RSS NEWS FETCHER] XML parse error: {ex}")

            if len(items) >= 20:
                break

        if not items:
            return self._get_default_overnight_cues()
        if holiday_name and len(items) == 1:
            # Only holiday notice present, append default cues with URLs
            return items + self._get_default_overnight_cues()
        return items

    def _get_default_overnight_cues(self) -> List[NewsItem]:
        """Returns baseline neutral overnight market summary with URLs."""
        now = datetime.now(IST_TZ)
        return [
            NewsItem(
                headline="US Markets Close Mixed: S&P 500 flat, NASDAQ up 0.1% as bond yields steady",
                summary="Major US indexes ended little changed with modest gains in megacap technology.",
                source="Global Markets Desk",
                published_at=now,
                category="US_MARKETS",
                sectors_mentioned=["NIFTY 50"],
                url="https://www.bloomberg.com/markets",
            ),
            NewsItem(
                headline="Asian Markets Open Steady: Nikkei and Hang Seng trade with mild gains",
                summary="Asian equities opened higher tracking stable global cues and currency stability.",
                source="Asia Equities Wire",
                published_at=now,
                category="GLOBAL_MACRO",
                sectors_mentioned=["NIFTY 50"],
                url="https://www.reuters.com/markets/asia/",
            ),
            NewsItem(
                headline="NSE Market Ticker: Live telemetry & risk gatekeepers active",
                summary="Domestic equity monitoring and volatility sentinels streaming.",
                source="NSE Live",
                published_at=now,
                category="DOMESTIC_CORPORATE",
                sectors_mentioned=["NIFTY 50"],
                url="https://www.nseindia.com",
            ),
        ]
