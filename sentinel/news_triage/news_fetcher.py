"""
Trillionaire Sentinel - Pre-Market News & Headline Aggregator
Asynchronously fetches domestic Indian equity stock corporate announcements
and market headlines via verified Economic Times Stocks RSS feeds.
Zero LLM tokens used for news ingestion.
"""

from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
import asyncio
import re
from typing import List, Optional, Union
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
    source: str = "Economic Times"
    published_at: datetime = field(default_factory=lambda: datetime.now(IST_TZ))
    category: str = "DOMESTIC_CORPORATE"  # DOMESTIC_CORPORATE, REGULATORY, MARKET_NOTICE, GLOBAL_MACRO
    symbols_mentioned: List[str] = field(default_factory=list)
    sectors_mentioned: List[str] = field(default_factory=list)
    url: Optional[str] = None


def _parse_rfc822_date(date_str: Optional[str]) -> datetime:
    """Parses standard RSS pubDate string into timezone-aware IST datetime."""
    if not date_str:
        return datetime.now(IST_TZ)
    try:
        dt = parsedate_to_datetime(date_str.strip())
        if dt.tzinfo is None:
            return dt.replace(tzinfo=IST_TZ)
        return dt.astimezone(IST_TZ)
    except Exception:
        return datetime.now(IST_TZ)


class AsyncNewsFetcher:
    """Fetches real Indian stock headlines from Economic Times RSS feeds."""

    # Primary and secondary verified Economic Times RSS endpoints
    PRIMARY_STOCKS_RSS: str = "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"
    SECONDARY_MARKETS_RSS: str = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"

    def __init__(self, mock_news: Optional[List[NewsItem]] = None):
        self._mock_news = mock_news

    @staticmethod
    def parse_rss_items(
        xml_content: Union[str, bytes],
        source_name: str = "Economic Times",
    ) -> List[NewsItem]:
        """
        Parses XML content (handles single-line XML and CDATA blocks cleanly)
        into standardized NewsItem objects.
        """
        if not xml_content:
            return []
        if isinstance(xml_content, str):
            xml_content = xml_content.encode("utf-8")

        parsed_items: List[NewsItem] = []
        try:
            root = ET.fromstring(xml_content)
            for elem in root.findall(".//item"):
                title_elem = elem.find("title")
                link_elem = elem.find("link")
                pub_elem = elem.find("pubDate")
                desc_elem = elem.find("description")

                title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
                link = link_elem.text.strip() if link_elem is not None and link_elem.text else ""
                pub_str = pub_elem.text.strip() if pub_elem is not None and pub_elem.text else ""
                desc = desc_elem.text.strip() if desc_elem is not None and desc_elem.text else ""

                if not title:
                    continue

                clean_title = re.sub(r"<[^>]+>", "", title).strip()
                clean_desc = re.sub(r"<[^>]+>", "", desc).strip()
                published_at = _parse_rfc822_date(pub_str)

                parsed_items.append(
                    NewsItem(
                        headline=clean_title,
                        summary=clean_desc or clean_title,
                        source=source_name,
                        published_at=published_at,
                        category="DOMESTIC_CORPORATE",
                        url=link or None,
                    )
                )
        except Exception as e:
            logger.warning(f"[RSS NEWS FETCHER] Error parsing RSS XML: {e}")
        return parsed_items

    async def fetch_pre_market_news(self) -> List[NewsItem]:
        """
        Fetches headlines available prior to 08:30 AM IST.
        Uses injected mock news if provided; otherwise queries external API if configured,
        or fetches live Economic Times Stocks RSS headlines.
        """
        if self._mock_news is not None:
            logger.info(f"Using {len(self._mock_news)} injected mock headlines for sentinel triage.")
            return self._mock_news

        if settings.news_api_key:
            try:
                return await self._fetch_live_news()
            except Exception as e:
                logger.warning(f"Live news API fetch failed: {e}. Falling back to Economic Times RSS.")

        return await self.fetch_rss_headlines()

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
            for art in data.get("articles", [])[:20]:
                items.append(
                    NewsItem(
                        headline=art.get("title", ""),
                        summary=art.get("description", "") or "",
                        source=art.get("source", {}).get("name", "NewsAPI"),
                        published_at=datetime.now(IST_TZ),
                        category="DOMESTIC_CORPORATE",
                        url=art.get("url") or None,
                    )
                )
            return items

    def get_default_overnight_cues(self) -> List[NewsItem]:
        """Synchronously returns injected mock items or holiday notice (no fake placeholders)."""
        if self._mock_news is not None:
            return self._mock_news
        today_ist = datetime.now(IST_TZ).date()
        holiday_name = get_market_holiday_name(today_ist)
        if holiday_name:
            return [
                NewsItem(
                    headline=f"NSE MARKET NOTICE: Indian Equity Markets Closed Today for {holiday_name}",
                    summary=f"NSE Equity Cash & Derivatives segments remain closed today in observance of {holiday_name}.",
                    source="NSE Official",
                    published_at=datetime.now(IST_TZ),
                    category="MARKET_NOTICE",
                    url=None,
                )
            ]
        return []

    async def fetch_rss_headlines(self) -> List[NewsItem]:
        """
        Token-free news fetcher querying Economic Times Stocks RSS feed.
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
                    url=None,
                )
            )

        rss_urls = [
            self.PRIMARY_STOCKS_RSS,
            self.SECONDARY_MARKETS_RSS,
        ]

        headers = {"User-Agent": "Mozilla/5.0"}

        async def _fetch_url(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return resp.content
            except Exception as ex:
                logger.debug(f"[RSS NEWS FETCHER] Error fetching {url}: {ex}")
            return None

        raw_contents: List[Optional[bytes]] = []
        try:
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=10.0) as client:
                tasks = [_fetch_url(client, u) for u in rss_urls]
                raw_contents = await asyncio.gather(*tasks, return_exceptions=False)
        except Exception as e:
            logger.warning(f"[RSS NEWS FETCHER] Async HTTP client failed: {e}")

        seen_keys = set()
        news_count = 0
        for content in raw_contents:
            if not content:
                continue
            parsed = self.parse_rss_items(content, source_name="Economic Times")
            for item in parsed:
                norm_key = re.sub(r"[^a-zA-Z0-9]", "", item.headline.lower())[:45]
                if norm_key in seen_keys:
                    continue
                seen_keys.add(norm_key)
                items.append(item)
                news_count += 1
                if news_count >= 20:
                    break
            if news_count >= 20:
                break

        return items
