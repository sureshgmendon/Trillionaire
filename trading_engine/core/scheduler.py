"""
Trillionaire Trading Engine - Weekday IST Automated Scheduler
Coordinates hands-free execution across the daily Indian equity market cycle:
1. 08:45 AM IST (Premarket Boot): Flushes stale states, syncs instruments, runs Sentinel triage, warms indicators, and starts data streaming.
2. 03:15 PM IST (EOD Force Square-off): Flattens all active intraday MIS positions via order_manager.
3. 03:35 PM IST (Post-Market Teardown): Gracefully halts live ticker feeds and streaming while keeping scheduler armed.
"""

import asyncio
from datetime import date, datetime, timedelta
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

        # Morning boot retry tracking to prevent rapid retry storms
        self._boot_retries_today: int = 0
        self._boot_retry_after: Optional[datetime] = None
        self._current_tracked_date: Optional[date] = None

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
        elif now_ist.hour == 15 and now_ist.minute >= 15:
            self._last_boot_date = today
            self._last_squareoff_date = today
        elif getattr(self.engine, "is_booted", False) and (
            getattr(self.engine, "current_trade_date", None) == today or getattr(self.engine, "is_running", False)
        ):
            self._last_boot_date = today

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

    async def trigger_morning_boot(
        self,
        trade_date: Optional[date] = None,
        current_time_ist: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Executes pre-market startup:
        1. Unconditionally resets risk gatekeeper daily state and circuit breakers.
        2. Gracefully stops any stale or lingering live session.
        3. Executes pre-market boot (sync instruments, run Sentinel, warm indicators).
        4. Starts live trading engine.
        """
        now = current_time_ist or datetime.now(IST_TZ)
        target_date = trade_date or now.date()
        logger.info(f"[SCHEDULER 08:45 AM] Executing morning pre-market boot for {target_date}...")

        # 1. Unconditionally reset daily risk gatekeeper state
        if hasattr(self.engine, "risk_gatekeeper") and self.engine.risk_gatekeeper is not None:
            self.engine.risk_gatekeeper.reset_daily_state(trade_date=target_date)

        try:
            # 2. Stop any stale engine session
            if self.engine.is_running:
                logger.info("[SCHEDULER] Halting existing engine session prior to morning boot.")
                await self.engine.stop()

            # 3. Boot pre-market routine
            boot_res = await self.engine.boot_premarket(trade_date=target_date)

            # 4. Start engine streaming
            await self.engine.start()

            self._last_boot_date = target_date
            self._boot_retries_today = 0
            self._boot_retry_after = None
            logger.info(f"[SCHEDULER 08:45 AM] Morning startup completed successfully: {boot_res.get('status')}")
            return boot_res
        except Exception as e:
            self._boot_retries_today += 1
            self._boot_retry_after = now + timedelta(minutes=5)
            logger.error(
                f"[SCHEDULER 08:45 AM] Morning boot attempt {self._boot_retries_today} failed: {e}. "
                f"Cooldown for 5 minutes (retry after {self._boot_retry_after.strftime('%H:%M:%S')})."
            )
            if self._boot_retries_today >= 3:
                self._last_boot_date = target_date
                logger.critical(
                    f"[SCHEDULER 08:45 AM] Morning boot reached max retries ({self._boot_retries_today}) for {target_date}. "
                    f"Marking boot as completed to prevent retry storms."
                )
            raise

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

    async def generate_daily_trade_autopsy(self, trade_date: Optional[date] = None) -> Optional[Dict[str, Any]]:
        """Generates the 03:35 PM IST Gemini post-market Trade Autopsy report."""
        from sentinel.news_triage.trade_autopsy import generate_daily_trade_autopsy

        target_date = trade_date or datetime.now(IST_TZ).date()
        try:
            om = getattr(self.engine, "order_manager", None)
            closed = list(getattr(om, "closed_trades", [])) if om else []
            rg = getattr(self.engine, "risk_gatekeeper", None)
            realized = (
                round(sum(t.realized_pnl for t in closed), 2)
                if closed
                else float(getattr(rg, "realized_daily_pnl", 0.0))
            )
            daily_mtm = float(rg.get_total_daily_mtm()) if rg else realized
            allocated = float(getattr(rg, "allocated_capital", 100000.0)) if rg else 100000.0
            funnel = dict(getattr(self.engine, "funnel_metrics", {}))
            report = await generate_daily_trade_autopsy(
                closed_trades=closed,
                funnel_metrics=funnel,
                daily_mtm=daily_mtm,
                realized_pnl=realized,
                allocated_capital=allocated,
                trade_date=target_date,
                send_telegram=True,
            )
            self.last_autopsy_report = report
            return report
        except Exception as exc:
            logger.error(f"[SCHEDULER 03:35 PM] Trade Autopsy generation error: {exc}")
            return None

    async def trigger_post_market_stop(self, trade_date: Optional[date] = None) -> None:
        """
        Generates 03:35 PM IST Gemini Trade Autopsy coaching report and halts real-time streaming
        while keeping scheduler armed for tomorrow.
        """
        target_date = trade_date or datetime.now(IST_TZ).date()
        logger.info(f"[SCHEDULER 03:35 PM] Executing post-market autopsy & engine shutdown for {target_date}...")

        await self.generate_daily_trade_autopsy(trade_date=target_date)

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
        if self._current_tracked_date != today:
            self._current_tracked_date = today
            self._boot_retries_today = 0
            self._boot_retry_after = None

        hour = now_ist.hour
        minute = now_ist.minute

        # 1. 08:45 AM IST: Morning pre-market boot & start
        # Triggers anytime between 08:45 and 15:14 if not yet booted today
        if (hour == 8 and minute >= 45) or (9 <= hour < 15) or (hour == 15 and minute < 15):
            if self._last_boot_date != today:
                if getattr(self.engine, "is_booted", False) and getattr(self.engine, "current_trade_date", None) == today:
                    self._last_boot_date = today
                elif self._boot_retries_today < 3 and (
                    self._boot_retry_after is None or now_ist >= self._boot_retry_after
                ):
                    try:
                        await self.trigger_morning_boot(trade_date=today, current_time_ist=now_ist)
                    except Exception as boot_err:
                        logger.error(f"[SCHEDULER] evaluate_schedule caught morning boot error: {boot_err}")

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
