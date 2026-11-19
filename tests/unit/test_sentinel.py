"""
Trillionaire Sentinel - Unit Test Suite
Validates:
1. Calendar blackout rules: Corporate earnings (DO_NOT_TRADE / 0.0x) and macro events (CAUTION_HALF_SIZE / 0.5x).
2. Sector contagion: Overnight US tech/NVIDIA shock cascading to NIFTY IT constituents (TCS, INFY).
3. Idiosyncratic regulatory/fraud alert setting DO_NOT_TRADE on target stock while leaving others NORMAL.
4. Orchestrator conservative merge logic (min multiplier rule) and daily manifest persistence.
"""

from datetime import date, datetime
from pathlib import Path
import pytest

from sentinel.calendar_guard.event_calendar import EventCalendar, evaluate_calendar_guard
from sentinel.news_triage.llm_classifier import PreMarketNewsClassifier
from sentinel.news_triage.news_fetcher import NewsItem
from sentinel.orchestrator import DAILY_TAGS_PATH, SentinelOrchestrator
from shared.constants import IST_TZ
from shared.enums import SentinelStatus
from shared.schemas import NewsSentinelTag


# ==============================================================================
# 1. CALENDAR GUARD TESTS
# ==============================================================================

def test_calendar_guard_earnings_and_macro_events():
    calendar = EventCalendar()

    # 1. TCS has scheduled earnings on 2026-11-16
    tcs_status, tcs_mult, tcs_reason = calendar.evaluate_calendar_guard(
        "TCS", current_date_ist=date(2026, 11, 16)
    )
    assert tcs_status == SentinelStatus.DO_NOT_TRADE
    assert tcs_mult == 0.0
    assert "earnings" in tcs_reason.lower()

    # 2. RBI MPC policy day on 2026-11-18 affects all stocks with CAUTION_HALF_SIZE
    rbi_status, rbi_mult, rbi_reason = calendar.evaluate_calendar_guard(
        "SBIN", current_date_ist=date(2026, 11, 18)
    )
    assert rbi_status == SentinelStatus.CAUTION_HALF_SIZE
    assert rbi_mult == 0.5
    assert "rbi mpc" in rbi_reason.lower()

    # 3. Regular day on 2026-11-17 with no events
    normal_status, normal_mult, normal_reason = calendar.evaluate_calendar_guard(
        "SBIN", current_date_ist=date(2026, 11, 17)
    )
    assert normal_status == SentinelStatus.NORMAL
    assert normal_mult == 1.0
    assert "no scheduled event" in normal_reason.lower()


# ==============================================================================
# 2. SECTOR CONTAGION CLASSIFIER TESTS
# ==============================================================================

def test_us_tech_shock_sector_contagion():
    classifier = PreMarketNewsClassifier()
    shock_news = [
        NewsItem(
            headline="Nvidia plunges 8% as US tech selloff triggers Nasdaq rout",
            summary="Semiconductor and software megacaps tumbled in overnight trading amid export restrictions.",
            source="Bloomberg Global Tech",
            category="US_MARKETS",
        )
    ]

    tags = classifier.classify_rule_based(shock_news)

    # NIFTY IT constituents must be flagged CAUTION_HALF_SIZE
    for it_stock in ["TCS", "INFY", "HCLTECH", "WIPRO"]:
        assert it_stock in tags
        assert tags[it_stock].status == SentinelStatus.CAUTION_HALF_SIZE
        assert tags[it_stock].risk_multiplier == 0.5
        assert "tech" in tags[it_stock].reason.lower() or "semiconductor" in tags[it_stock].reason.lower()

    # Unrelated sectors (e.g. HDFCBANK in NIFTY BANK, SUNPHARMA in NIFTY PHARMA) must remain NORMAL
    assert tags["HDFCBANK"].status == SentinelStatus.NORMAL
    assert tags["HDFCBANK"].risk_multiplier == 1.0
    assert tags["SUNPHARMA"].status == SentinelStatus.NORMAL
    assert tags["SUNPHARMA"].risk_multiplier == 1.0


# ==============================================================================
# 3. IDIOSYNCRATIC FRAUD & REGULATORY HEADLINE TESTS
# ==============================================================================

def test_company_fraud_alert_lockout():
    classifier = PreMarketNewsClassifier()
    fraud_news = [
        NewsItem(
            headline="SEBI orders forensic audit after whistleblower alleges accounting fraud at Tata Motors",
            summary="Regulatory inspection ordered into commercial vehicles division accounting practices.",
            source="Financial Express",
            category="REGULATORY",
        )
    ]

    tags = classifier.classify_rule_based(fraud_news)

    # TMPV / TATAMOTORS must be locked out (DO_NOT_TRADE / 0.0x)
    sym = "TMPV" if "TMPV" in tags else "TATAMOTORS"
    assert tags[sym].status == SentinelStatus.DO_NOT_TRADE
    assert tags[sym].risk_multiplier == 0.0
    assert "fraud" in tags[sym].reason.lower() or "regulatory" in tags[sym].reason.lower()

    # Other Auto peers (e.g. MARUTI, M&M) should remain NORMAL
    assert tags["MARUTI"].status == SentinelStatus.NORMAL
    assert tags["MARUTI"].risk_multiplier == 1.0
    assert tags["M&M"].status == SentinelStatus.NORMAL
    assert tags["M&M"].risk_multiplier == 1.0


# ==============================================================================
# 4. ORCHESTRATOR CONSERVATIVE MERGE & PERSISTENCE TESTS
# ==============================================================================

def test_sentinel_orchestrator_conservative_merge(tmp_path: Path):
    manifest_path = tmp_path / "daily_sentinel_tags.json"
    orchestrator = SentinelOrchestrator(artifacts_path=manifest_path, enable_redis=False)

    # Inject news: Tech shock (NIFTY IT -> 0.5x)
    mock_news = [
        NewsItem(
            headline="US tech selloff: Nasdaq slump hits global software equities",
            summary="Overnight tech declines expected to pressure Asian IT sector.",
            source="Reuters",
            category="US_MARKETS",
        )
    ]

    # Target date: 2026-11-16
    # On 2026-11-16:
    # - TCS has EARNINGS in calendar (0.0x) AND tech shock in news (0.5x)
    #   -> Conservative merge: min(0.0, 0.5) = 0.0x (DO_NOT_TRADE)
    # - INFY has NO calendar event (1.0x) AND tech shock in news (0.5x)
    #   -> Conservative merge: min(1.0, 0.5) = 0.5x (CAUTION_HALF_SIZE)
    # - RELIANCE has NO calendar event (1.0x) and neutral news (1.0x)
    #   -> Conservative merge: min(1.0, 1.0) = 1.0x (NORMAL)

    tags = orchestrator.run_pre_market_triage(
        news_items=mock_news,
        current_date_ist=date(2026, 11, 16),
    )

    assert len(tags) == 100

    # TCS: min(0.0, 0.5) = 0.0 -> DO_NOT_TRADE
    tcs_tag = tags["TCS"]
    assert tcs_tag.status == SentinelStatus.DO_NOT_TRADE
    assert tcs_tag.risk_multiplier == 0.0
    assert "[CALENDAR]" in tcs_tag.reason
    assert "[NEWS]" in tcs_tag.reason

    # INFY: min(1.0, 0.5) = 0.5 -> CAUTION_HALF_SIZE
    infy_tag = tags["INFY"]
    assert infy_tag.status == SentinelStatus.CAUTION_HALF_SIZE
    assert infy_tag.risk_multiplier == 0.5
    assert "[NEWS]" in infy_tag.reason

    # RELIANCE: min(1.0, 1.0) = 1.0 -> NORMAL
    rel_tag = tags["RELIANCE"]
    assert rel_tag.status == SentinelStatus.NORMAL
    assert rel_tag.risk_multiplier == 1.0

    # Verify manifest file persistence on disk
    assert manifest_path.exists()
    retrieved_tag = orchestrator.get_effective_risk_multiplier("TCS")
    assert retrieved_tag.status == SentinelStatus.DO_NOT_TRADE
    assert retrieved_tag.risk_multiplier == 0.0
