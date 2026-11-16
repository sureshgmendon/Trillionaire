"""
Trillionaire Sentinel Package
Pre-market risk radar, economic calendar blackout guards, and news triage orchestrator.
"""

from sentinel.calendar_guard.event_calendar import EventCalendar, evaluate_calendar_guard
from sentinel.news_triage.llm_classifier import PreMarketNewsClassifier
from sentinel.news_triage.news_fetcher import AsyncNewsFetcher, NewsItem
from sentinel.orchestrator import (
    DAILY_TAGS_PATH,
    SENTINEL_ARTIFACTS_DIR,
    SentinelOrchestrator,
    get_effective_risk_multiplier,
    run_sentinel_triage,
)

__all__ = [
    "EventCalendar",
    "evaluate_calendar_guard",
    "PreMarketNewsClassifier",
    "AsyncNewsFetcher",
    "NewsItem",
    "SentinelOrchestrator",
    "get_effective_risk_multiplier",
    "run_sentinel_triage",
    "run_sentinel_triage_async",
    "DAILY_TAGS_PATH",
    "SENTINEL_ARTIFACTS_DIR",
]
