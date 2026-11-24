"""
Trillionaire Sentinel - Unified Pre-Market Risk Orchestrator
Coordinates 08:30 AM pre-market triage combining:
1. Macro & Corporate Earnings Calendar Blackouts (event_calendar.py)
2. Overnight Global News & Sector Contagion Triage (llm_classifier.py & news_fetcher.py)
Applies strict conservative risk integration (min multiplier) and persists daily manifest.
"""

from datetime import date, datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

from config.settings import settings
from sentinel.calendar_guard.event_calendar import EventCalendar
from sentinel.news_triage.llm_classifier import PreMarketNewsClassifier
from sentinel.news_triage.news_fetcher import AsyncNewsFetcher, NewsItem
from shared.constants import IST_TZ, KEY_SENTINEL_TAGS
from shared.enums import SentinelStatus
from shared.schemas import NewsSentinelTag

SENTINEL_ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"
SENTINEL_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
DAILY_TAGS_PATH = SENTINEL_ARTIFACTS_DIR / "daily_sentinel_tags.json"


class SentinelOrchestrator:
    """Pre-market risk guardian protecting capital from overnight shocks and event blackouts."""

    def __init__(
        self,
        event_calendar: Optional[EventCalendar] = None,
        news_classifier: Optional[PreMarketNewsClassifier] = None,
        news_fetcher: Optional[AsyncNewsFetcher] = None,
        artifacts_path: Path = DAILY_TAGS_PATH,
        enable_redis: bool = False,
    ):
        self.calendar = event_calendar or EventCalendar()
        self.classifier = news_classifier or PreMarketNewsClassifier()
        self.fetcher = news_fetcher or AsyncNewsFetcher()
        self.artifacts_path = artifacts_path
        self.enable_redis = enable_redis
        self._cached_tags: Dict[str, NewsSentinelTag] = {}
        self.latest_headlines: List[Dict[str, Any]] = []
        self._last_news_fetch_ist: Optional[datetime] = None
        self.gemini_brief: str = ""
        # Try loading existing manifest if available
        self.load_latest_manifest()

    def run_pre_market_triage(
        self,
        news_items: Optional[List[NewsItem]] = None,
        current_date_ist: Optional[date] = None,
    ) -> Dict[str, NewsSentinelTag]:
        """
        Executes complete pre-market risk evaluation across all 25 universe stocks synchronously.
        """
        if current_date_ist is None:
            current_date_ist = datetime.now(IST_TZ).date()

        now_dt = datetime.now(IST_TZ)

        # 1. News items
        if news_items is None:
            news_items = self.fetcher.get_default_overnight_cues()

        self.latest_headlines = [
            {
                "title": item.headline,
                "summary": item.summary,
                "source": item.source,
                "time": item.published_at.strftime("%H:%M IST"),
                "category": item.category,
                "url": getattr(item, "url", None),
            }
            for item in news_items
        ]
        self._last_news_fetch_ist = now_dt

        # 2. Rule-based classification
        news_results = self.classifier.classify_rule_based(news_items)

        return self._merge_and_save_tags(news_results, current_date_ist, now_dt)

    async def run_pre_market_triage_async(
        self,
        news_items: Optional[List[NewsItem]] = None,
        current_date_ist: Optional[date] = None,
    ) -> Dict[str, NewsSentinelTag]:
        """
        Asynchronously fetches live news / runs Gemini LLM scoring then merges risk tags.
        """
        if current_date_ist is None:
            current_date_ist = datetime.now(IST_TZ).date()

        now_dt = datetime.now(IST_TZ)

        if news_items is None:
            news_items = await self.fetcher.fetch_pre_market_news()

        self.latest_headlines = [
            {
                "title": item.headline,
                "summary": item.summary,
                "source": item.source,
                "time": item.published_at.strftime("%H:%M IST"),
                "category": item.category,
                "url": getattr(item, "url", None),
            }
            for item in news_items
        ]
        self._last_news_fetch_ist = now_dt

        news_results = await self.classifier.classify_news(news_items, is_premarket=True)

        return self._merge_and_save_tags(news_results, current_date_ist, now_dt)

    async def refresh_intraday_news(self) -> Dict[str, Any]:
        """
        Fetches fresh RSS headlines every 15 minutes token-free (Google News / NSE RSS),
        updates marquee telemetry, and dynamically applies risk tags if high-severity events occur.
        """
        items = await self.fetcher.fetch_rss_headlines()
        now_dt = datetime.now(IST_TZ)
        self.latest_headlines = [
            {
                "title": item.headline,
                "summary": item.summary,
                "source": item.source,
                "time": item.published_at.strftime("%H:%M IST"),
                "category": item.category,
                "url": getattr(item, "url", None),
            }
            for item in items
        ]
        self._last_news_fetch_ist = now_dt
        news_results = await self.classifier.classify_news(items, is_premarket=False)
        self._merge_and_save_tags(news_results, now_dt.date(), now_dt)
        return self.get_sentinel_summary()

    async def get_sentinel_summary_async(self) -> Dict[str, Any]:
        """
        Returns structured sentinel summary, refreshing RSS headlines asynchronously
        if headlines are empty or older than 15 minutes.
        """
        now = datetime.now(IST_TZ)
        needs_refresh = (
            len(self.latest_headlines) <= 1
            or self._last_news_fetch_ist is None
            or (now - self._last_news_fetch_ist).total_seconds() > 900
        )
        if needs_refresh:
            try:
                await self.refresh_intraday_news()
            except Exception as e:
                logger.warning(f"Error during async sentinel news refresh: {e}")
        return self.get_sentinel_summary()

    def get_sentinel_summary(self) -> Dict[str, Any]:
        """Returns structured summary of headlines, Gemini briefing, and tagged high-risk stocks."""
        now = datetime.now(IST_TZ)
        high_risk = []
        for sym, tag in self._cached_tags.items():
            if tag.risk_multiplier < 1.0 or tag.status != SentinelStatus.NORMAL:
                high_risk.append({
                    "symbol": sym,
                    "status": tag.status.value,
                    "risk_multiplier": tag.risk_multiplier,
                    "reason": tag.reason,
                })

        headlines = self.latest_headlines
        if not headlines:
            default_cues = self.fetcher.get_default_overnight_cues()
            headlines = [
                {
                    "title": item.headline,
                    "summary": item.summary,
                    "source": item.source,
                    "time": item.published_at.strftime("%H:%M IST"),
                    "category": item.category,
                    "url": getattr(item, "url", None),
                }
                for item in default_cues
            ]

        brief = self.gemini_brief
        if not brief:
            if high_risk:
                brief = f"Sentinel Alert: {len(high_risk)} instruments flagged with macro/corporate volatility guards. Position sizes capped."
            else:
                brief = "Global and domestic overnight cues remain balanced. No severe regulatory, fraud, or earnings blackouts detected across NIFTY 50 universe."

        return {
            "updated_at_ist": now.isoformat(),
            "gemini_brief": brief,
            "headlines": headlines,
            "high_risk_count": len(high_risk),
            "high_risk_stocks": high_risk,
        }

    def _merge_and_save_tags(
        self,
        news_results: Dict[str, NewsSentinelTag],
        current_date_ist: date,
        now_dt: datetime,
    ) -> Dict[str, NewsSentinelTag]:
        """Merges calendar and news risk tags using conservative min(multiplier) rule."""
        merged_tags: Dict[str, NewsSentinelTag] = {}
        sector_data = settings.load_sector_map()
        stocks = [s["tradingsymbol"] for s in sector_data.get("stocks", [])]

        logger.info(
            f"Orchestrating Sentinel pre-market triage for {len(stocks)} stocks on {current_date_ist}..."
        )

        for sym in stocks:
            # Calendar check
            cal_status, cal_mult, cal_reason = self.calendar.evaluate_calendar_guard(
                sym, current_date_ist=current_date_ist
            )

            # News check
            news_tag = news_results.get(sym)
            if news_tag is None:
                news_status = SentinelStatus.NORMAL
                news_mult = 1.0
                news_reason = "No adverse news"
            else:
                news_status = news_tag.status
                news_mult = news_tag.risk_multiplier
                news_reason = news_tag.reason

            # 3. Conservative Merge: min(calendar_mult, news_mult)
            effective_mult = min(cal_mult, news_mult)

            if effective_mult == 0.0:
                effective_status = SentinelStatus.DO_NOT_TRADE
            elif effective_mult <= 0.5:
                effective_status = SentinelStatus.CAUTION_HALF_SIZE
            else:
                effective_status = SentinelStatus.NORMAL

            # Build descriptive audit reason
            reasons = []
            if cal_mult < 1.0:
                reasons.append(f"[CALENDAR] {cal_reason}")
            if news_mult < 1.0:
                reasons.append(f"[NEWS] {news_reason}")
            if not reasons:
                effective_reason = "NORMAL: No scheduled event blackout and neutral pre-market news."
            else:
                effective_reason = " | ".join(reasons)

            tag = NewsSentinelTag(
                tradingsymbol=sym,
                status=effective_status,
                reason=effective_reason,
                risk_multiplier=round(effective_mult, 2),
                updated_at_ist=now_dt,
            )
            merged_tags[sym] = tag

        self._cached_tags = merged_tags

        # Save JSON artifact
        self._save_manifest(merged_tags, current_date_ist)

        # Cache in Redis if enabled
        self._cache_in_redis(merged_tags)

        return merged_tags

    def _save_manifest(self, tags: Dict[str, NewsSentinelTag], current_date: date) -> None:
        """Saves daily tags manifest to disk."""
        data = {
            "date": current_date.isoformat(),
            "updated_at_ist": datetime.now(IST_TZ).isoformat(),
            "summary": {
                "total_monitored": len(tags),
                "normal_count": sum(1 for t in tags.values() if t.status == SentinelStatus.NORMAL),
                "caution_count": sum(1 for t in tags.values() if t.status == SentinelStatus.CAUTION_HALF_SIZE),
                "do_not_trade_count": sum(1 for t in tags.values() if t.status == SentinelStatus.DO_NOT_TRADE),
            },
            "tags": {sym: t.model_dump(mode="json") for sym, t in tags.items()},
        }
        with open(self.artifacts_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Persisted Sentinel daily manifest -> {self.artifacts_path}")

    def _cache_in_redis(self, tags: Dict[str, NewsSentinelTag]) -> None:
        """Attempts to cache serialized tags into Redis."""
        if not self.enable_redis:
            return

        try:
            import redis
            r = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )
            mapping = {sym: t.model_dump_json() for sym, t in tags.items()}
            r.hset(KEY_SENTINEL_TAGS, mapping=mapping)
            logger.debug(f"Cached {len(mapping)} sentinel tags in Redis under {KEY_SENTINEL_TAGS}")
        except Exception as e:
            logger.debug(f"Redis cache update bypassed (Redis unavailable or local offline mode): {e}")

    def get_effective_risk_multiplier(self, tradingsymbol: str) -> NewsSentinelTag:
        """
        Retrieves active Sentinel tag for an instrument.
        Returns from in-memory cache, falls back to disk manifest or creates safe default.
        """
        if tradingsymbol in self._cached_tags:
            return self._cached_tags[tradingsymbol]

        # Check disk
        if self.load_latest_manifest():
            if tradingsymbol in self._cached_tags:
                return self._cached_tags[tradingsymbol]

        # Default fallback
        return NewsSentinelTag(
            tradingsymbol=tradingsymbol,
            status=SentinelStatus.NORMAL,
            reason="Sentinel cache initialized default",
            risk_multiplier=1.0,
            updated_at_ist=datetime.now(IST_TZ),
        )

    def load_latest_manifest(self) -> bool:
        """Loads sentinel tags from daily_sentinel_tags.json if present."""
        if not self.artifacts_path.exists():
            return False
        try:
            with open(self.artifacts_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tags_raw = data.get("tags", {})
            self._cached_tags = {
                sym: NewsSentinelTag.model_validate(raw) for sym, raw in tags_raw.items()
            }
            return True
        except Exception as e:
            logger.warning(f"Failed loading sentinel manifest: {e}")
            return False


# Singleton orchestrator
_default_orchestrator = SentinelOrchestrator()


def run_sentinel_triage(
    news_items: Optional[List[NewsItem]] = None,
    current_date_ist: Optional[date] = None,
) -> Dict[str, NewsSentinelTag]:
    """Module-level helper to trigger pre-market sentinel triage."""
    return _default_orchestrator.run_pre_market_triage(news_items, current_date_ist)


async def run_sentinel_triage_async(
    news_items: Optional[List[NewsItem]] = None,
    current_date_ist: Optional[date] = None,
) -> Dict[str, NewsSentinelTag]:
    """Module-level async helper to trigger pre-market sentinel triage with live feeds."""
    return await _default_orchestrator.run_pre_market_triage_async(news_items, current_date_ist)


def get_effective_risk_multiplier(tradingsymbol: str) -> NewsSentinelTag:
    """Module-level helper for trading engine position sizing."""
    return _default_orchestrator.get_effective_risk_multiplier(tradingsymbol)
