"""
Unit tests for WeekdayMarketScheduler
Validates:
1. Lifecycle start and stop of background monitoring task.
2. 08:45 AM IST morning pre-market boot & live streaming startup.
3. 03:15 PM IST mandatory intraday square-off via order_manager.flatten_all_positions.
4. 03:35 PM IST post-market live engine shutdown with scheduler remaining armed.
5. Weekend blackout safety (Saturday/Sunday no-ops).
6. Deterministic test triggers: trigger_morning_boot(), trigger_eod_squareoff(), trigger_post_market_stop().
"""

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock
import pytest

from shared.constants import IST_TZ
from trading_engine.core.engine import TrillionaireEngine
from trading_engine.core.event_bus import EventBus
from trading_engine.core.scheduler import WeekdayMarketScheduler
from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper
from trading_engine.strategy.live_runner import LiveStrategyRunner


@pytest.fixture
def mock_engine():
    """Sets up an engine with mocked lifecycle methods for scheduler verification."""
    bus = EventBus()
    risk_guard = PortfolioRiskGatekeeper(event_bus=bus, allocated_capital=100000.0)
    broker = PaperBroker(event_bus=bus)
    om = OrderManager(event_bus=bus, broker=broker, risk_gatekeeper=risk_guard)
    strategy_runner = LiveStrategyRunner(event_bus=bus)

    engine = TrillionaireEngine(
        event_bus=bus,
        risk_gatekeeper=risk_guard,
        order_manager=om,
        broker=broker,
        strategy_runner=strategy_runner,
    )

    # Attach AsyncMocks to lifecycle calls
    engine.boot_premarket = AsyncMock(return_value={"status": "booted"})
    engine.start = AsyncMock()
    engine.stop = AsyncMock()
    om.flatten_all_positions = AsyncMock(return_value=[])

    return engine


@pytest.mark.asyncio
async def test_scheduler_lifecycle(mock_engine):
    """Validates starting and stopping the scheduler background loop."""
    scheduler = WeekdayMarketScheduler(engine=mock_engine, poll_interval_seconds=0.05)
    assert scheduler.is_running is False
    assert scheduler._task is None

    scheduler.start()
    assert scheduler.is_running is True
    assert scheduler._task is not None

    await asyncio.sleep(0.1)

    await scheduler.stop()
    assert scheduler.is_running is False
    assert scheduler._task is None


@pytest.mark.asyncio
async def test_scheduler_morning_boot_0845_ist(mock_engine):
    """
    Validates that at 08:45 AM IST on a weekday (Monday),
    the scheduler stops any stale session, runs boot_premarket, and starts engine streaming.
    """
    scheduler = WeekdayMarketScheduler(engine=mock_engine)

    # Monday at 08:45:00 IST
    monday_morning = datetime(2026, 11, 16, 8, 45, 0, tzinfo=IST_TZ)
    assert monday_morning.weekday() == 0  # Monday

    await scheduler.evaluate_schedule(monday_morning)

    mock_engine.boot_premarket.assert_called_once()
    mock_engine.start.assert_called_once()
    assert scheduler._last_boot_date == monday_morning.date()

    # Second evaluation on same day must not re-trigger
    await scheduler.evaluate_schedule(monday_morning)
    assert mock_engine.boot_premarket.call_count == 1
    assert mock_engine.start.call_count == 1


@pytest.mark.asyncio
async def test_scheduler_eod_squareoff_1515_ist(mock_engine):
    """
    Validates that at 03:15 PM IST (15:15) on a weekday,
    the scheduler flattens all open positions with reason 'EOD_AUTO_CLOSE'.
    """
    scheduler = WeekdayMarketScheduler(engine=mock_engine)

    # Monday at 15:15:00 IST
    monday_eod = datetime(2026, 11, 16, 15, 15, 0, tzinfo=IST_TZ)

    await scheduler.evaluate_schedule(monday_eod)

    mock_engine.order_manager.flatten_all_positions.assert_called_once_with(reason="EOD_AUTO_CLOSE")
    assert scheduler._last_squareoff_date == monday_eod.date()

    # Second evaluation must not re-trigger
    await scheduler.evaluate_schedule(monday_eod)
    assert mock_engine.order_manager.flatten_all_positions.call_count == 1


@pytest.mark.asyncio
async def test_scheduler_post_market_stop_1535_ist(mock_engine):
    """
    Validates that at 03:35 PM IST (15:35) on a weekday,
    the scheduler gracefully halts the engine while keeping scheduler armed.
    """
    mock_engine.is_running = True
    scheduler = WeekdayMarketScheduler(engine=mock_engine)

    # Monday at 15:35:00 IST
    monday_post_market = datetime(2026, 11, 16, 15, 35, 0, tzinfo=IST_TZ)

    await scheduler.evaluate_schedule(monday_post_market)

    mock_engine.stop.assert_called_once()
    assert scheduler._last_stop_date == monday_post_market.date()

    # Second evaluation must not re-trigger
    await scheduler.evaluate_schedule(monday_post_market)
    assert mock_engine.stop.call_count == 1


@pytest.mark.asyncio
async def test_scheduler_weekend_skipped(mock_engine):
    """
    Validates that on Saturday and Sunday, all scheduler triggers are skipped.
    """
    scheduler = WeekdayMarketScheduler(engine=mock_engine)

    # Saturday at 08:45:00 IST
    saturday_morning = datetime(2026, 11, 21, 8, 45, 0, tzinfo=IST_TZ)
    assert saturday_morning.weekday() == 5  # Saturday

    await scheduler.evaluate_schedule(saturday_morning)
    mock_engine.boot_premarket.assert_not_called()
    mock_engine.start.assert_not_called()

    # Sunday at 15:15:00 IST
    sunday_eod = datetime(2026, 11, 22, 15, 15, 0, tzinfo=IST_TZ)
    assert sunday_eod.weekday() == 6  # Sunday

    await scheduler.evaluate_schedule(sunday_eod)
    mock_engine.order_manager.flatten_all_positions.assert_not_called()


@pytest.mark.asyncio
async def test_scheduler_deterministic_manual_triggers(mock_engine):
    """
    Validates direct manual test hooks for deterministic invocation.
    """
    scheduler = WeekdayMarketScheduler(engine=mock_engine)

    test_date = date(2026, 11, 19)

    # 1. Trigger morning boot
    res = await scheduler.trigger_morning_boot(trade_date=test_date)
    assert res["status"] == "booted"
    mock_engine.boot_premarket.assert_called_once_with(trade_date=test_date)
    mock_engine.start.assert_called_once()
    assert scheduler._last_boot_date == test_date

    # 2. Trigger EOD square-off
    orders = await scheduler.trigger_eod_squareoff()
    assert isinstance(orders, list)
    mock_engine.order_manager.flatten_all_positions.assert_called_once_with(reason="EOD_AUTO_CLOSE")

    # 3. Trigger post-market stop
    mock_engine.is_running = True
    await scheduler.trigger_post_market_stop()
    mock_engine.stop.assert_called_once()
