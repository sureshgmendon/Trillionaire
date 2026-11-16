"""
Trillionaire Trading Engine - Institutional Risk Gatekeeper & Position Sizer
Implements capital tier risk controls, dynamic position sizing, and intraday circuit breakers:
1. Dynamic Position Sizing: Risk-based sizing scaled by Sentinel multiplier and leverage caps.
2. Hard Daily MTM Kill-Switch: Immediate portfolio liquidation and lockout at -1.5% drawdown.
3. Profit-Locking Trailing Floor: Arms at +1.2% daily profit; locks and liquidates if MTM retraces to +0.6%.
4. Mandatory 15:10 IST Auto-Square-Off: Closes all open MIS positions ahead of Zerodha broker cut-off.
"""

import asyncio
from datetime import date, datetime, time
from typing import Any, Callable, Coroutine, Dict, List, Optional
from loguru import logger

from config.settings import settings
from shared.constants import (
    ENTRY_WINDOW_END,
    ENTRY_WINDOW_START,
    FORCE_SQUARE_OFF_TRIGGER,
    IST_TZ,
)
from shared.enums import Side
from shared.schemas import Position, Signal, Tick
from trading_engine.core.event_bus import EventBus, RiskAlertEvent, TickEvent, bus

FlattenCallback = Callable[[str], Coroutine[Any, Any, Any]] | Callable[[str], Any]


class PortfolioRiskGatekeeper:
    """Institutional portfolio risk gatekeeper enforcing statutory limits and capital protection."""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        tier_name: Optional[str] = None,
        allocated_capital: Optional[float] = None,
        max_daily_loss_pct: Optional[float] = None,
        risk_per_trade_pct: Optional[float] = None,
        profit_lock_trigger_pct: Optional[float] = None,
        profit_lock_floor_pct: Optional[float] = None,
        max_open_positions: Optional[int] = None,
        max_mis_leverage: Optional[float] = None,
        flatten_handler: Optional[FlattenCallback] = None,
    ):
        self.bus = event_bus or bus
        self.flatten_handler = flatten_handler

        # Load tier configuration
        tier_config = self._load_tier_config(tier_name)

        self.tier_name = tier_name or settings.active_capital_tier
        self.allocated_capital: float = (
            allocated_capital if allocated_capital is not None else float(tier_config.get("allocated_capital", 100000.0))
        )
        self.max_daily_loss_pct: float = (
            max_daily_loss_pct if max_daily_loss_pct is not None else float(tier_config.get("max_daily_loss_pct", 1.5))
        )
        self.risk_per_trade_pct: float = (
            risk_per_trade_pct if risk_per_trade_pct is not None else float(tier_config.get("risk_per_trade_pct", 0.5))
        )
        self.profit_lock_trigger_pct: float = (
            profit_lock_trigger_pct
            if profit_lock_trigger_pct is not None
            else float(tier_config.get("profit_lock_trigger_pct", 1.2))
        )
        self.profit_lock_floor_pct: float = (
            profit_lock_floor_pct
            if profit_lock_floor_pct is not None
            else float(tier_config.get("profit_lock_floor_pct", 0.6))
        )
        self.max_open_positions: int = (
            max_open_positions if max_open_positions is not None else int(tier_config.get("max_open_positions", 3))
        )
        self.max_mis_leverage: float = (
            max_mis_leverage if max_mis_leverage is not None else float(tier_config.get("max_mis_leverage", 3.0))
        )

        # Active portfolio state
        self.realized_daily_pnl: float = 0.0
        self.open_positions: Dict[str, Position] = {}
        self.kill_switch_active: bool = False
        self.profit_lock_armed: bool = False
        self.profit_lock_floor_hit: bool = False
        self.force_square_off_done: bool = False
        self.current_trade_date: Optional[date] = None
        self.circuit_breaker_reason: Optional[str] = None

        # Track risk alert history
        self.risk_alerts: List[RiskAlertEvent] = []

        # Wire event listener
        self.bus.subscribe(TickEvent, self.on_tick)

        logger.info(
            f"PortfolioRiskGatekeeper initialized [{self.tier_name}]: Capital=Rs.{self.allocated_capital:,.2f}, "
            f"MaxDailyLoss={self.max_daily_loss_pct}%, RiskPerTrade={self.risk_per_trade_pct}%, "
            f"MaxPositions={self.max_open_positions}, MaxLeverage={self.max_mis_leverage}x"
        )

    def _load_tier_config(self, tier_name: Optional[str] = None) -> Dict[str, Any]:
        """Loads tier parameters from settings/capital_tiers.yaml."""
        try:
            tiers = settings.load_capital_tiers()
            key = tier_name or settings.active_capital_tier
            if key in tiers:
                return tiers[key]
            return settings.get_active_capital_tier_config()
        except Exception as e:
            logger.warning(f"Could not load capital tier config from file ({e}), using default fallback.")
            return {
                "allocated_capital": 100000.0,
                "max_daily_loss_pct": 1.5,
                "risk_per_trade_pct": 0.5,
                "profit_lock_trigger_pct": 1.2,
                "profit_lock_floor_pct": 0.6,
                "max_open_positions": 3,
                "max_mis_leverage": 3.0,
            }

    def set_flatten_handler(self, handler: FlattenCallback) -> None:
        """Sets the liquidation callback invoked when circuit breakers trip."""
        self.flatten_handler = handler

    def reset_daily_state(self, trade_date: Optional[date] = None) -> None:
        """Resets PnL and circuit breaker flags at market open (09:15 IST)."""
        self.realized_daily_pnl = 0.0
        self.open_positions.clear()
        self.kill_switch_active = False
        self.profit_lock_armed = False
        self.profit_lock_floor_hit = False
        self.force_square_off_done = False
        self.circuit_breaker_reason = None
        self.current_trade_date = trade_date or datetime.now(IST_TZ).date()
        logger.info(f"PortfolioRiskGatekeeper reset for trade date: {self.current_trade_date}")

    def register_position(self, position: Position) -> None:
        """Registers a newly opened or filled position."""
        self.open_positions[position.tradingsymbol] = position
        logger.info(
            f"[POSITION REGISTERED] {position.tradingsymbol} {position.side.value} x {position.quantity} "
            f"@ {position.average_entry_price:.2f}"
        )

    def close_position(self, symbol: str, realized_pnl: float) -> Optional[Position]:
        """Removes an active position and updates realized daily PnL."""
        pos = self.open_positions.pop(symbol, None)
        if pos is not None:
            self.realized_daily_pnl = round(self.realized_daily_pnl + realized_pnl, 2)
            logger.info(
                f"[POSITION CLOSED] {symbol} realized PnL: Rs.{realized_pnl:,.2f} | "
                f"Cumulative Realized: Rs.{self.realized_daily_pnl:,.2f}"
            )
        return pos

    def get_total_daily_mtm(self) -> float:
        """Calculates total mark-to-market daily PnL (realized + unrealized)."""
        unrealized = sum(pos.unrealized_pnl for pos in self.open_positions.values())
        return round(self.realized_daily_pnl + unrealized, 2)

    def calculate_position_size(
        self,
        signal: Signal,
        sentinel_multiplier: float = 1.0,
        current_time: Optional[datetime] = None,
    ) -> int:
        """
        Dynamically computes share quantity for an entry signal.
        Enforces:
        - Circuit breaker lockouts (kill-switch or profit floor)
        - Session entry window: 09:25:00 <= time <= 14:30:00 IST
        - Max concurrent open positions
        - Risk per trade percentage scaled by Sentinel multiplier
        - Gross portfolio leverage constraint (allocated_capital * max_mis_leverage)
        """
        # 1. Circuit Breaker Checks
        if self.kill_switch_active:
            logger.warning(
                f"[RISK REJECTED] Cannot size {signal.tradingsymbol}: Daily kill switch is ACTIVE ({self.circuit_breaker_reason})"
            )
            return 0

        if self.profit_lock_floor_hit:
            logger.warning(
                f"[RISK REJECTED] Cannot size {signal.tradingsymbol}: Profit lock floor hit ({self.circuit_breaker_reason})"
            )
            return 0

        if sentinel_multiplier <= 0.0:
            logger.warning(
                f"[RISK REJECTED] Cannot size {signal.tradingsymbol}: Sentinel risk multiplier is 0.0 (Lockout)"
            )
            return 0

        # 2. Timing Window Check (09:25 <= time <= 14:30 IST)
        eval_dt = current_time or signal.generated_at_ist
        if eval_dt.tzinfo is None:
            eval_dt = eval_dt.replace(tzinfo=IST_TZ)
        t = eval_dt.time()
        if not (ENTRY_WINDOW_START <= t <= ENTRY_WINDOW_END):
            logger.warning(
                f"[RISK REJECTED] Cannot size {signal.tradingsymbol}: Current time {t.strftime('%H:%M:%S')} is outside entry window (09:25 - 14:30 IST)"
            )
            return 0

        # 3. Maximum Open Positions Constraint
        if signal.tradingsymbol not in self.open_positions and len(self.open_positions) >= self.max_open_positions:
            logger.warning(
                f"[RISK REJECTED] Cannot size {signal.tradingsymbol}: Max positions ({self.max_open_positions}) reached"
            )
            return 0

        # 4. Sizing by Risk Budget
        risk_per_share = abs(signal.entry_price - signal.stop_loss)
        if risk_per_share <= 0.01:
            logger.warning(
                f"[RISK REJECTED] Cannot size {signal.tradingsymbol}: Risk per share ({risk_per_share}) is negligible or zero"
            )
            return 0

        capital_at_risk = self.allocated_capital * (self.risk_per_trade_pct / 100.0) * sentinel_multiplier
        target_shares = int(capital_at_risk // risk_per_share)
        if target_shares <= 0:
            logger.info(
                f"[RISK SIZING] {signal.tradingsymbol}: Target shares calculated as 0 (RiskBudget={capital_at_risk:.2f}, Risk/Share={risk_per_share:.2f})"
            )
            return 0

        # 5. Sizing by Maximum Gross MIS Leverage
        current_exposure = sum(pos.quantity * pos.ltp for pos in self.open_positions.values())
        max_allowed_exposure = self.allocated_capital * self.max_mis_leverage
        available_exposure = max(0.0, max_allowed_exposure - current_exposure)

        if available_exposure <= 0.0:
            logger.warning(
                f"[RISK REJECTED] Cannot size {signal.tradingsymbol}: Maximum gross leverage ({self.max_mis_leverage}x) fully utilized"
            )
            return 0

        max_shares_by_leverage = int(available_exposure // signal.entry_price)
        final_shares = min(target_shares, max_shares_by_leverage)

        if final_shares <= 0:
            logger.warning(
                f"[RISK REJECTED] {signal.tradingsymbol}: Available leverage allows 0 shares"
            )
            return 0

        logger.info(
            f"[RISK APPROVED] {signal.tradingsymbol} sized to {final_shares} shares "
            f"(TargetRiskShares={target_shares}, MaxLeverageShares={max_shares_by_leverage}, Mult={sentinel_multiplier:.2f})"
        )
        return final_shares

    async def on_tick(self, event: TickEvent) -> None:
        """
        Consumes live market ticks, recalculates open position MTM, and guards portfolio:
        - -1.5% Daily MTM Kill Switch
        - +1.2% / +0.6% Profit Lock
        - 15:10 IST Mandatory Square-Off
        """
        await self.process_tick(event.tick)

    async def process_tick(self, tick: Tick) -> None:
        """Processes tick update and evaluates risk circuit breakers."""
        sym = tick.tradingsymbol
        now_dt = tick.timestamp_ist
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=IST_TZ)

        # 1. Update MTM for matching open position
        if sym in self.open_positions:
            pos = self.open_positions[sym]
            pos.ltp = tick.ltp
            if pos.side == Side.BUY:
                pos.unrealized_pnl = round((tick.ltp - pos.average_entry_price) * pos.quantity, 2)
            else:
                pos.unrealized_pnl = round((pos.average_entry_price - tick.ltp) * pos.quantity, 2)

        total_daily_mtm = self.get_total_daily_mtm()

        # 2. Hard Daily MTM Kill-Switch (-1.5% of allocated capital)
        max_daily_loss_inr = -1.0 * (self.allocated_capital * (self.max_daily_loss_pct / 100.0))
        if not self.kill_switch_active and total_daily_mtm <= max_daily_loss_inr:
            self.kill_switch_active = True
            self.circuit_breaker_reason = (
                f"Daily MTM drawdown Rs.{total_daily_mtm:,.2f} breached -{self.max_daily_loss_pct}% limit (Rs.{max_daily_loss_inr:,.2f})"
            )
            logger.critical(f"[CIRCUIT BREAKER] {self.circuit_breaker_reason}")
            alert = RiskAlertEvent(
                symbol="ALL",
                level="CIRCUIT_BREAKER",
                message=self.circuit_breaker_reason,
                timestamp_ist=now_dt,
            )
            self.risk_alerts.append(alert)
            await self.bus.publish(alert)
            await self._trigger_flatten("DAILY_LOSS_KILL_SWITCH_TRIPPED")
            return

        # 3. Profit-Locking Trailing Floor (+1.2% trigger, +0.6% floor)
        trigger_inr = self.allocated_capital * (self.profit_lock_trigger_pct / 100.0)
        floor_inr = self.allocated_capital * (self.profit_lock_floor_pct / 100.0)

        if not self.profit_lock_armed and total_daily_mtm >= trigger_inr:
            self.profit_lock_armed = True
            msg = f"Profit lock floor armed at +0.6% (MTM reached Rs.{total_daily_mtm:,.2f} >= Rs.{trigger_inr:,.2f})"
            logger.info(f"[PROFIT LOCK] {msg}")
            alert = RiskAlertEvent(
                symbol="ALL",
                level="INFO",
                message=msg,
                timestamp_ist=now_dt,
            )
            self.risk_alerts.append(alert)
            await self.bus.publish(alert)

        if self.profit_lock_armed and not self.profit_lock_floor_hit and total_daily_mtm <= floor_inr:
            self.profit_lock_floor_hit = True
            self.circuit_breaker_reason = (
                f"Daily profit retraced to floor: MTM Rs.{total_daily_mtm:,.2f} <= +{self.profit_lock_floor_pct}% (Rs.{floor_inr:,.2f})"
            )
            logger.warning(f"[PROFIT LOCK FLOOR] {self.circuit_breaker_reason}")
            alert = RiskAlertEvent(
                symbol="ALL",
                level="CIRCUIT_BREAKER",
                message=self.circuit_breaker_reason,
                timestamp_ist=now_dt,
            )
            self.risk_alerts.append(alert)
            await self.bus.publish(alert)
            await self._trigger_flatten("PROFIT_LOCK_FLOOR_REACHED")
            return

        # 4. Mandatory 15:10 IST Auto-Square-Off
        t = now_dt.time()
        if t >= FORCE_SQUARE_OFF_TRIGGER and not self.force_square_off_done:
            if len(self.open_positions) > 0:
                self.force_square_off_done = True
                msg = f"Mandatory 15:10 IST MIS Square-Off Triggered ({len(self.open_positions)} open positions)"
                logger.warning(f"[SQUARE OFF 15:10] {msg}")
                alert = RiskAlertEvent(
                    symbol="ALL",
                    level="CRITICAL",
                    message=msg,
                    timestamp_ist=now_dt,
                )
                self.risk_alerts.append(alert)
                await self.bus.publish(alert)
                await self._trigger_flatten("MANDATORY_1510_SQUARE_OFF")

    async def _trigger_flatten(self, reason: str) -> None:
        """Invokes the external order flattening handler if configured."""
        if self.flatten_handler is not None:
            try:
                res = self.flatten_handler(reason)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error(f"Error during emergency position flattening: {e}")
