"""
Unit and integration tests for the Six Production Upgrades:
1. Windows Task Scheduler scripts configuration & logging.
2. Quiet Pre-Login Reconciler & Safe Pause/Resume Trading Toggle.
3. Daily Orders and Realized PnL Redis Persistence & Rehydration.
4. NSE Market Holiday Awareness (2026) & 5-Milestone Engine Readiness.
5. Token-Free News Fetching & Sentinel Summary endpoint.
6. Broad Market (NIFTY 50) Trend Filter on breakout signals.
"""

from datetime import date, datetime
from pathlib import Path
import re
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from api_bridge.main import app
from config.settings import settings
from sentinel.calendar_guard.event_calendar import get_market_holiday_name
from sentinel.news_triage.news_fetcher import AsyncNewsFetcher, NewsItem
from shared.constants import IST_TZ
from shared.enums import ClusterRegime, Side, Timeframe, TradingMode
from shared.schemas import Candle, Order, Position, Signal, StockProfile, Tick
from trading_engine.brokers.kite.kite_broker import KiteBroker
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import CandleCloseEvent, EventBus, SignalEvent
from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper
from trading_engine.strategy.live_runner import LiveStrategyRunner


# -----------------------------------------------------------------------------
# 1. NSE Market Holiday Guard & 2026 Calendar
# -----------------------------------------------------------------------------

def test_nse_holiday_calendar_2026():
    """Verifies that NSE 2026 trading holidays and weekends are correctly identified."""
    # Guru Nanak Jayanti
    assert get_market_holiday_name(date(2026, 11, 24)) == "Guru Nanak Jayanti"
    # Christmas
    assert get_market_holiday_name(date(2026, 12, 25)) == "Christmas"
    # Republic Day
    assert get_market_holiday_name(date(2026, 1, 26)) == "Republic Day"
    # Normal Tuesday trading day (March 10, 2026)
    assert get_market_holiday_name(date(2026, 3, 10)) is None
    # Saturday / Sunday
    assert get_market_holiday_name(date(2026, 3, 14)) == "Weekend (Saturday)"
    assert get_market_holiday_name(date(2026, 3, 15)) == "Weekend (Sunday)"


# -----------------------------------------------------------------------------
# 2. Quiet Pre-Login Reconciler
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quiet_prelogin_reconciler_skips_when_token_unauthorized():
    """Verifies that KiteBroker.reconcile_positions does not throw or spam when token is unauthorized."""
    mock_kite = MagicMock()
    mock_kite.positions.side_effect = Exception("Incorrect api_key or access_token")

    bus = EventBus()
    om = OrderManager(event_bus=bus, broker=PaperBroker(event_bus=bus))
    broker = KiteBroker(kite_client=mock_kite, event_bus=bus, order_manager=om)

    res = await broker.reconcile_positions()
    assert res["status"] == "skipped"
    assert res["reason"] == "Token unauthorized"


# -----------------------------------------------------------------------------
# 3. Safe Pause / Resume Trading Toggle
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_safe_pause_resume_trading():
    """Verifies that pause_trading halts signal routing while keeping ticker alive, and resume re-arms."""
    bus = EventBus()
    mock_auth = MagicMock()
    mock_auth.get_valid_access_token.return_value = "tok"
    mock_auth.get_kite_client.return_value = MagicMock()

    engine = TrillionaireEngine(
        trading_mode=TradingMode.PAPER,
        event_bus=bus,
        auth_manager=mock_auth,
    )
    await engine.boot_premarket(trade_date=date(2026, 3, 10))
    await engine.start()

    # 1. Initially active
    assert engine.trading_paused is False
    assert engine.strategy_runner.is_paused is False

    # 2. Pause trading
    p_res = engine.pause_trading()
    assert p_res["status"] == "paused"
    assert engine.trading_paused is True
    assert engine.strategy_runner.is_paused is True

    # 3. Emit signal while paused - order manager should receive 0 orders
    sig = Signal(
        signal_id="sig-pause-test",
        tradingsymbol="RELIANCE",
        parent_index="NIFTY ENERGY",
        side=Side.BUY,
        entry_price=2500.0,
        stop_loss=2480.0,
        target_price=2550.0,
        confidence=0.9,
        strategy_name="TrendMomentumStrategy",
        generated_at_ist=datetime.now(IST_TZ),
    )
    await bus.publish(SignalEvent(signal=sig))
    assert len(engine.order_manager.open_positions) == 0

    # 4. Resume trading
    r_res = engine.resume_trading()
    assert r_res["status"] == "resumed"
    assert engine.trading_paused is False
    assert engine.strategy_runner.is_paused is False

    await engine.stop()


# -----------------------------------------------------------------------------
# 4. Redis Order & Realized PnL Persistence & Rehydration
# -----------------------------------------------------------------------------

def test_rehydrate_from_redis_mocked():
    """Verifies that rehydrate_from_redis populates OrderManager orders and risk gatekeeper realized PnL."""
    import sys
    bus = EventBus()
    mock_auth = MagicMock()
    mock_auth.get_valid_access_token.return_value = "tok"
    mock_auth.get_kite_client.return_value = MagicMock()

    engine = TrillionaireEngine(trading_mode=TradingMode.PAPER, event_bus=bus, auth_manager=mock_auth)

    fake_orders = {
        "ord-1": '{"order_id": "ord-1", "tradingsymbol": "INFY", "side": "BUY", "product": "MIS", "quantity": 10, "status": "FILLED", "created_at_ist": "2026-03-10T09:30:00+05:30"}',
        "ord-2": '{"order_id": "ord-2", "tradingsymbol": "INFY", "side": "SELL", "product": "MIS", "quantity": 10, "status": "FILLED", "exit_reason": "TP_HIT", "created_at_ist": "2026-03-10T10:15:00+05:30"}'
    }

    mock_redis = MagicMock()
    mock_redis.hgetall.return_value = fake_orders
    mock_redis.get.return_value = "1540.50"

    mock_redis_module = MagicMock()
    mock_redis_module.from_url.return_value = mock_redis
    orig_redis = sys.modules.get("redis")
    sys.modules["redis"] = mock_redis_module

    try:
        res = engine.rehydrate_from_redis(trade_date=date(2026, 3, 10))
        assert res["orders_rehydrated"] == 2
        assert res["realized_pnl_rehydrated"] == 1540.50
        assert "ord-1" in engine.order_manager.orders
        assert "ord-2" in engine.order_manager.orders
        assert engine.risk_gatekeeper.realized_pnl == 1540.50
    finally:
        if orig_redis is not None:
            sys.modules["redis"] = orig_redis
        else:
            sys.modules.pop("redis", None)


# -----------------------------------------------------------------------------
# 5. 5-Milestone Engine Readiness Status
# -----------------------------------------------------------------------------

def test_5_milestones_structure():
    """Verifies get_lifecycle_milestones returns all 5 required milestones with valid schema."""
    bus = EventBus()
    mock_auth = MagicMock()
    mock_auth.get_valid_access_token.return_value = "tok"
    mock_auth.get_kite_client.return_value = MagicMock()

    engine = TrillionaireEngine(trading_mode=TradingMode.PAPER, event_bus=bus, auth_manager=mock_auth)
    milestones = engine.get_lifecycle_milestones()

    assert "summary_label" in milestones
    assert "milestones" in milestones
    assert milestones["total_count"] == 5

    milestone_ids = [m["id"] for m in milestones["milestones"]]
    assert milestone_ids == [
        "kite_auth",
        "universe_warmed",
        "ticker_connected",
        "ticks_flowing",
        "strategy_armed",
    ]


# -----------------------------------------------------------------------------
# 6. Broad Market (NIFTY 50) Trend Filter
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nifty_50_broad_market_regime_filter():
    """Verifies that LiveStrategyRunner blocks BUY when NIFTY 50 M5 candle is below VWAP."""
    bus = EventBus()
    runner = LiveStrategyRunner(event_bus=bus)

    profile = StockProfile(
        tradingsymbol="TCS",
        instrument_token=2953217,
        parent_index="NIFTY IT",
        index_token=257289,
        cluster_regime=ClusterRegime.TREND_MOMENTUM,
        beta_5y=1.0,
        adr_pct=2.0,
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=3.0,
        min_rvol_threshold=1.5,
        oos_sharpe=1.6,
        oos_profit_factor=1.8,
        is_active=True,
    )
    runner._set_profiles([profile])

    now = datetime(2026, 3, 10, 10, 0, tzinfo=IST_TZ)

    # Seed Bearish NIFTY 50 (close < vwap)
    runner._latest_candles[("NIFTY 50", Timeframe.M5)] = Candle(
        tradingsymbol="NIFTY 50",
        timeframe=Timeframe.M5,
        timestamp_ist=now,
        open=24000.0,
        high=24010.0,
        low=23950.0,
        close=23960.0,  # Below VWAP
        volume=50000,
        vwap=24000.0,
        ema_200=23900.0,
        atr_14=40.0,
        rvol=1.5,
    )
    runner._index_ema_20["NIFTY 50"] = 24000.0

    # Seed Parent Sector Index NIFTY IT (Bullish)
    runner._latest_candles[("NIFTY IT", Timeframe.M5)] = Candle(
        tradingsymbol="NIFTY IT",
        timeframe=Timeframe.M5,
        timestamp_ist=now,
        open=38000.0,
        high=38200.0,
        low=37950.0,
        close=38150.0,
        volume=20000,
        vwap=38050.0,
        ema_200=37800.0,
        atr_14=80.0,
        rvol=1.8,
    )
    runner._index_ema_20["NIFTY IT"] = 38050.0

    # Setup Opening Range & CPR for TCS
    runner._opening_ranges["TCS"] = (3900.0, 3850.0)
    runner._cpr_levels["TCS"] = (3860.0, 3890.0)

    # TCS Bullish breakout candle (c > or_high, c > vwap, c > ema200)
    tcs_candle = Candle(
        tradingsymbol="TCS",
        timeframe=Timeframe.M5,
        timestamp_ist=now,
        open=3880.0,
        high=3920.0,
        low=3875.0,
        close=3915.0,  # > 3900 or_high
        volume=100000,
        vwap=3890.0,
        ema_200=3800.0,
        atr_14=15.0,
        rvol=2.0,
    )

    # Evaluate candle — should be blocked by Gate 4 (NIFTY 50 market trend filter)
    sig = await runner.on_candle_close(CandleCloseEvent(candle=tcs_candle))
    assert sig is None
    assert runner.funnel_metrics.get("blocked_by_market_filter", 0) >= 1


# -----------------------------------------------------------------------------
# 7. API Bridge Endpoints (Pause, Resume, Milestones, Sentinel Summary)
# -----------------------------------------------------------------------------

def test_api_endpoints():
    """Tests /api/v1/control/engine/pause, resume, milestones, and sentinel/summary endpoints."""
    client = TestClient(app)
    admin_hdr = {"X-API-Key": settings.trillionaire_admin_key}

    # 1. Pause
    pause_res = client.post("/api/v1/control/engine/pause", headers=admin_hdr)
    assert pause_res.status_code == 200
    assert pause_res.json()["trading_paused"] is True

    # 2. Resume
    resume_res = client.post("/api/v1/control/engine/resume", headers=admin_hdr)
    assert resume_res.status_code == 200
    assert resume_res.json()["trading_paused"] is False

    # 3. Milestones
    m_res = client.get("/api/v1/engine/milestones")
    assert m_res.status_code == 200
    assert "milestones" in m_res.json()

    # 4. Sentinel Summary
    s_res = client.get("/api/v1/sentinel/summary")
    assert s_res.status_code == 200
    data = s_res.json()
    assert "headlines" in data
    assert "gemini_brief" in data
    assert len(data["headlines"]) > 0
    # Every headline must have a valid url field
    for h in data["headlines"]:
        assert "url" in h


# -----------------------------------------------------------------------------
# 8. Economic Times Stocks RSS Single-Line CDATA Fixture & Live Fetcher Tests
# -----------------------------------------------------------------------------

ET_STOCKS_RSS_CDATA_FIXTURE = (
    b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
    b'<title>Stocks-Markets-Economic Times</title>'
    b'<link>https://economictimes.indiatimes.com/markets/stocks</link>'
    b'<description><![CDATA[Stock Market News: Latest Stock news and updates on The Economic Times.]]></description>'
    b'<item><title><![CDATA[HDFC Bank, Reliance Industries among 10 stocks with highest DII holding in Q2]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/hdfc-bank-reliance-industries-q2/articleshow/136121838.cms]]></link>'
    b'<description><![CDATA[Domestic institutional investors raised holdings across top private banks.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 11:45:03 +0530</pubDate></item>'
    b'<item><title><![CDATA[TCS bags $1.5 billion multi-year digital transformation deal with European retailer]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/tcs-bags-multi-year-deal/articleshow/136121801.cms]]></link>'
    b'<description><![CDATA[Tata Consultancy Services signed a major enterprise contract.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 11:40:12 +0530</pubDate></item>'
    b'<item><title><![CDATA[Infosys expands AI enterprise suite with next-gen generative agent platform]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/infosys-expands-ai-suite/articleshow/136121802.cms]]></link>'
    b'<description><![CDATA[Infosys rolled out generative AI solutions for global BFSI clients.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 11:35:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[ICICI Bank records steady 18% credit growth across retail and SME portfolios]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/icici-bank-steady-credit-growth/articleshow/136121803.cms]]></link>'
    b'<description><![CDATA[Private sector lender reports resilient asset quality metrics.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 11:30:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[State Bank of India plans $1 billion bond issuance to fund infrastructure loans]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/sbin-plans-bond-issuance/articleshow/136121804.cms]]></link>'
    b'<description><![CDATA[SBI board approves fundraising via green and infrastructure bonds.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 11:25:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Bharti Airtel adds 2.4 million mobile broadband subscribers in September quarter]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/bharti-airtel-subscriber-gain/articleshow/136121805.cms]]></link>'
    b'<description><![CDATA[Telecom major leads industry ARPU expansion.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 11:20:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Larsen & Toubro wins mega offshore order worth over Rs 15,000 crore]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/lt-wins-mega-offshore-order/articleshow/136121806.cms]]></link>'
    b'<description><![CDATA[Hydrocarbon vertical bags substantial international engineering contract.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 11:15:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Kotak Mahindra Bank expands wealth management franchise across tier-2 hubs]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/kotak-expands-wealth-management/articleshow/136121807.cms]]></link>'
    b'<description><![CDATA[Focus on high-net-worth individual asset advisory services.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 11:10:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Tata Motors inaugurates advanced EV manufacturing unit in Tamil Nadu]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/tata-motors-inaugurates-ev-unit/articleshow/136121808.cms]]></link>'
    b'<description><![CDATA[Annual production capacity targeted at 250,000 electric vehicles.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 11:05:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Bajaj Finance expands digital merchant payments to 4,000 new cities]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/bajaj-finance-merchant-payments/articleshow/136121809.cms]]></link>'
    b'<description><![CDATA[NBFC powerhouse drives customer acquisition via omnibus app.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 11:00:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Axis Bank partners with global payments network for cross-border remittances]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/axis-bank-cross-border-deal/articleshow/136121810.cms]]></link>'
    b'<description><![CDATA[Real-time international fund transfers enabled for NRI corridor.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 10:55:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Sun Pharma receives USFDA approval for generic dermatology formulation]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/sun-pharma-usfda-approval/articleshow/136121811.cms]]></link>'
    b'<description><![CDATA[Pharma giant strengthens specialized US generic pipeline.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 10:50:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Maruti Suzuki registers 12% rise in monthly exports led by SUV demand]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/maruti-suzuki-export-growth/articleshow/136121812.cms]]></link>'
    b'<description><![CDATA[Shipments to Africa and Latin America drive volume growth.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 10:45:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Titan Company reports robust festive season jewelry sales growth of 22%]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/titan-festive-sales-growth/articleshow/136121813.cms]]></link>'
    b'<description><![CDATA[Tanishq network additions and wedding collections drive top-line.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 10:40:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[UltraTech Cement commissions 3.2 MTPA brownfield expansion in Rajasthan]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/ultratech-commissions-plant/articleshow/136121814.cms]]></link>'
    b'<description><![CDATA[Consolidated domestic cement capacity scales past 150 MTPA.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 10:35:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[ITC hotel business demerger receives final NCLT and shareholder approvals]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/itc-hotel-demerger-approval/articleshow/136121815.cms]]></link>'
    b'<description><![CDATA[Hospitality arm prepares for standalone listing on NSE and BSE.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 10:30:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Adani Ports cargo volumes surpass 42 million metric tonnes in October]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/adani-ports-cargo-milestone/articleshow/136121816.cms]]></link>'
    b'<description><![CDATA[Container handling volumes grow 14% year-on-year.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 10:25:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Wipro collaborates with Microsoft to deploy sovereign cloud for European clients]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/wipro-microsoft-cloud-deal/articleshow/136121817.cms]]></link>'
    b'<description><![CDATA[Enterprise IT service provider expands secure cloud architecture.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 10:20:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Asian Paints expands decorative waterproofing portfolio with green certified coatings]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/asian-paints-green-coatings/articleshow/136121818.cms]]></link>'
    b'<description><![CDATA[New premium interior emulsions launched ahead of wedding quarter.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 10:15:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[Bajaj Finserv healthcare venture reaches 10 million registered wellness subscribers]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/bajaj-finserv-health-milestone/articleshow/136121819.cms]]></link>'
    b'<description><![CDATA[Digital OPD and preventive health protection sees rapid adoption.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 10:10:00 +0530</pubDate></item>'
    b'<item><title><![CDATA[HDFC Bank, Reliance Industries among 10 stocks with highest DII holding in Q2]]></title>'
    b'<link><![CDATA[https://economictimes.indiatimes.com/markets/stocks/news/hdfc-bank-reliance-industries-q2/articleshow/136121838.cms]]></link>'
    b'<description><![CDATA[Duplicate item testing deduplication filter.]]></description>'
    b'<pubDate>Tue, 24 Nov 2026 11:45:03 +0530</pubDate></item>'
    b'</channel></rss>'
)


def test_parse_et_stocks_rss_cdata_fixture():
    """Verifies that single-line CDATA Economic Times RSS XML is cleanly parsed with 20 real stock items."""
    parsed_items = AsyncNewsFetcher.parse_rss_items(ET_STOCKS_RSS_CDATA_FIXTURE, source_name="Economic Times")
    assert len(parsed_items) == 21  # 20 unique + 1 duplicate in fixture

    # Deduplicate with normalized keys
    seen = set()
    unique_items = []
    for it in parsed_items:
        norm = re.sub(r"[^a-zA-Z0-9]", "", it.headline.lower())[:45]
        if norm not in seen:
            seen.add(norm)
            unique_items.append(it)

    assert len(unique_items) == 20

    # Ensure zero fake placeholder strings
    placeholder_indicators = [
        "asia equities wire",
        "global markets desk",
        "us markets close mixed",
        "asian markets open steady",
    ]

    for item in unique_items:
        assert item.source == "Economic Times"
        assert item.url is not None
        assert item.url.startswith("https://economictimes.indiatimes.com/")
        assert item.category == "DOMESTIC_CORPORATE"
        # Zero placeholder check
        for ph in placeholder_indicators:
            assert ph not in item.headline.lower(), f"Placeholder '{ph}' found in headline: {item.headline}"
            assert ph not in item.source.lower(), f"Placeholder '{ph}' found in source: {item.source}"


@pytest.mark.asyncio
async def test_fetch_rss_headlines_with_urls_and_holiday_notice():
    """Verifies that fetch_rss_headlines returns articles with urls and prepends holiday notice on holidays."""
    fetcher = AsyncNewsFetcher()
    items = await fetcher.fetch_rss_headlines()
    assert len(items) >= 20

    # If today is Guru Nanak Jayanti (2026-11-24), first item is the market notice with url=None
    if get_market_holiday_name(datetime.now(IST_TZ).date()) == "Guru Nanak Jayanti":
        assert items[0].category == "MARKET_NOTICE"
        assert "Guru Nanak Jayanti" in items[0].headline
        assert items[0].url is None
        news_items = items[1:]
    else:
        news_items = items

    assert len(news_items) == 20
    for item in news_items:
        assert item.source == "Economic Times"
        assert item.url is not None
        assert item.url.startswith("https://economictimes.indiatimes.com/")
        assert "Asia Equities Wire" not in item.source
        assert "Global Markets Desk" not in item.source
