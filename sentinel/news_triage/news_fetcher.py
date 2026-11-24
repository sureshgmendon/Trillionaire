"""
Trillionaire Sentinel - Pre-Market News & Headline Aggregator
Asynchronously fetches overnight global market cues, US tech indicators,
and domestic Indian equity corporate announcements via HTTP adapters and mock providers.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
import httpx
from loguru import logger

from config.settings import settings
from shared.constants import IST_TZ


@dataclass
class NewsItem:
    """Standardized pre-market news item."""
    headline: str
    summary: str = ""
    source: str = "AGGREGATOR"
    published_at: datetime = field(default_factory=lambda: datetime.now(IST_TZ))
    category: str = "GLOBAL_MACRO"  # GLOBAL_MACRO, US_MARKETS, DOMESTIC_CORPORATE, REGULATORY
    symbols_mentioned: List[str] = field(default_factory=list)
    sectors_mentioned: List[str] = field(default_factory=list)


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
        Token-free news fetcher querying Google News RSS feed for Indian equity markets.
        Parses XML items into NewsItem objects without incurring LLM token costs.
        """
        if self._mock_news is not None:
            return self._mock_news

        rss_url = "https://news.google.com/rss/search?q=NSE+stocks+OR+Nifty+50+when:1d&hl=en-IN&gl=IN&ceid=IN:en"
        items: List[NewsItem] = []
        try:
            import xml.etree.ElementTree as ET
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(rss_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                if resp.status_code == 200:
                    root = ET.fromstring(resp.content)
                    for elem in root.findall(".//item")[:15]:
                        title = elem.findtext("title", "").strip()
                        source_elem = elem.find("source")
                        source = source_elem.text if source_elem is not None else "Google News"
                        if title:
                            items.append(
                                NewsItem(
                                    headline=title,
                                    summary=title,
                                    source=source or "Financial Wire",
                                    published_at=datetime.now(IST_TZ),
                                    category="DOMESTIC_CORPORATE",
                                )
                            )
        except Exception as e:
            logger.debug(f"[RSS NEWS FETCHER] RSS fetch bypassed ({e}), using default overnight cues.")

        if not items:
            return self._get_default_overnight_cues()
        return items

    def _get_default_overnight_cues(self) -> List[NewsItem]:
        """Returns baseline neutral overnight market summary."""
        now = datetime.now(IST_TZ)
        return [
            NewsItem(
                headline="US Markets Close Mixed: S&P 500 flat, NASDAQ up 0.1% as bond yields steady",
                summary="Major US indexes ended little changed with modest gains in megacap technology.",
                source="Global Markets Desk",
                published_at=now,
                category="US_MARKETS",
                sectors_mentioned=["NIFTY 50"],
            ),
            NewsItem(
                headline="Asian Markets Open Steady: Nikkei and Hang Seng trade with mild gains",
                summary="Asian equities opened higher tracking stable global cues and currency stability.",
                source="Asia Equities Wire",
                published_at=now,
                category="GLOBAL_MACRO",
                sectors_mentioned=["NIFTY 50"],
            ),
        ]
