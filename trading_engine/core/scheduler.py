"""
Trillionaire Trading Engine - Weekday IST Automated Scheduler
Coordinates hands-free execution across the daily Indian equity market cycle:
1. 08:45 AM IST (Premarket Boot): Flushes stale states, syncs instruments, runs Sentinel triage, warms indicators, and starts data streaming.
2. 03:15 PM IST (EOD Force Square-off): Flattens all active intraday MIS positions via order_manager.
3. 03:35 PM IST (Post-Market Teardown): Gracefully halts live ticker feeds and streaming while keeping scheduler armed.
"""

import asyncio
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from loguru import logger

from shared.constants import IST_TZ
from trading_engine.core.engine import TrillionaireEngine


class WeekdayMarketScheduler:
    """
    Automated weekday scheduler operating strictly in Asia/Kolkata (IST).
    Monitors market windows, executing pre-market boot, EOD liquidation, and post-market stop.
    """

    def __init__(
        self,
        engine: TrillionaireEngine,
        poll_interval_seconds: float = 15.0,
    ) -> None:
        self.engine = engine
        self.poll_interval = poll_interval_seconds
        self.is_running: bool = False
        self._task: Optional[asyncio.Task] = None

        # Tracking state to ensure each action executes once per trade date
        self._last_boot_date: Optional[date] = None
        self._last_squareoff_date: Optional[date] = None
        self._last_stop_date: Optional[date] = None

    def start(self) -> None:
        """Starts the scheduler background monitoring task."""
        if self.is_running:
            return

        # If starting post-market for today, record today's events as already passed
        now_ist = datetime.now(IST_TZ)
        today = now_ist.date()
        if (now_ist.hour > 15) or (now_ist.hour == 15 and now_ist.minute >= 35):
            self._last_boot_date = today
            self._last_squareoff_date = today
            self._last_stop_date = today

        self.is_running = True
        self._task = asyncio.create_task(self._monitoring_loop())
        logger.info("[SCHEDULER] WeekdayMarketScheduler started (IST timezone active).")

    async def stop(self) -> None:
        """Gracefully cancels and stops scheduler monitoring."""
        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("[SCHEDULER] WeekdayMarketScheduler stopped.")

    # -------------------------------------------------------------------------
    # Deterministic Trigger Actions (Also callable directly by tests)
    # -------------------------------------------------------------------------

    async def trigger_morning_boot(self, trade_date: Optional[date] = None) -> Dict[str, Any]:
        """
        Executes pre-market startup:
        1. Gracefully stops any stale or lingering live session.
        2. Executes pre-market boot (sync instruments, run Sentinel, warm indicators).
        3. Starts live trading engine.
        """
        target_date = trade_date or datetime.now(IST_TZ).date()
        logger.info(f"[SCHEDULER 08:45 AM] Executing morning pre-market boot for {target_date}...")

        # 1. Stop any stale engine session
        if self.engine.is_running:
            logger.info("[SCHEDULER] Halting existing engine session prior to morning boot.")
            await self.engine.stop()

        # 2. Boot pre-market routine
        boot_res = await self.engine.boot_premarket(trade_date=target_date)

        # 3. Start engine streaming
        await self.engine.start()

        self._last_boot_date = target_date
        logger.info(f"[SCHEDULER 08:45 AM] Morning startup completed successfully: {boot_res.get('status')}")
        return boot_res

    async def trigger_eod_squareoff(self, trade_date: Optional[date] = None) -> List[Any]:
        """
        Executes mandatory EOD intraday square-off:
        Flattens all open MIS positions with reason 'EOD_AUTO_CLOSE'.
        """
        target_date = trade_date or datetime.now(IST_TZ).date()
        logger.warning(f"[SCHEDULER 03:15 PM] Initiating mandatory EOD square-off for {target_date}...")

        flattened_orders = await self.engine.order_manager.flatten_all_positions(reason="EOD_AUTO_CLOSE")
        self._last_squareoff_date = target_date

        logger.warning(
            f"[SCHEDULER 03:15 PM] Mandatory EOD square-off complete. Liquidated {len(flattened_orders)} positions."
        )
        return flattened_orders

    async def trigger_post_market_stop(self, trade_date: Optional[date] = None) -> None:
        """
        Halts real-time streaming operations at 03:35 PM IST while keeping scheduler armed.
        """
        target_date = trade_date or datetime.now(IST_TZ).date()
        logger.info(f"[SCHEDULER 03:35 PM] Executing post-market engine shutdown for {target_date}...")

        if self.engine.is_running:
            await self.engine.stop()

        self._last_stop_date = target_date
        logger.info("[SCHEDULER 03:35 PM] Post-market shutdown complete. Engine stopped, scheduler armed for tomorrow.")

    # -------------------------------------------------------------------------
    # Evaluation Logic
    # -------------------------------------------------------------------------

    async def evaluate_schedule(self, now_ist: datetime) -> None:
        """Evaluates whether current IST time matches any scheduled triggers."""
        # Weekend check: Monday=0 ... Friday=4, Saturday=5, Sunday=6
        if now_ist.weekday() >= 5:
            return

        today = now_ist.date()
        hour = now_ist.hour
        minute = now_ist.minute

        # 1. 08:45 AM IST: Morning pre-market boot & start
        # Triggers anytime between 08:45 and 15:14 if not yet booted today
        if (hour == 8 and minute >= 45) or (9 <= hour < 15) or (hour == 15 and minute < 15):
            if self._last_boot_date != today:
                await self.trigger_morning_boot(today)

        # 2. 03:15 PM IST (15:15): EOD Force Square-off
        # Active between 15:15 and 15:34 IST on weekdays
        if hour == 15 and 15 <= minute < 35:
            if self._last_squareoff_date != today:
                await self.trigger_eod_squareoff(today)

        # 3. 03:35 PM IST (15:35): Post-Market Halt
        # Active between 15:35 and 15:59 IST on weekdays
        if hour == 15 and minute >= 35:
            if self._last_stop_date != today:
                await self.trigger_post_market_stop(today)

    async def _monitoring_loop(self) -> None:
        """Background poll loop checking IST market clock."""
        while self.is_running:
            try:
                now_ist = datetime.now(IST_TZ)
                await self.evaluate_schedule(now_ist)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SCHEDULER LOOP ERROR] {e}")

            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
