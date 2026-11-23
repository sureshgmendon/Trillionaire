"""
Trillionaire Trading Engine - SEBI-Compliant Order Manager & State Machine
Coordinates order execution, rate limiting, and real-time position bracket monitoring:
1. SEBI Retail Rate Limiter: Enforces statutory ceiling of <= 10 orders per second.
2. SEBI-Compliant Marketable Limit Orders: Disallows pure MARKET orders, applying protective buffers.
3. Order Lifecycle State Machine: Manages transitions (PENDING_NEW -> OPEN -> FILLED/CANCELLED/REJECTED).
4. Live 1R Breakeven Trailing Stop Manager: Monitors incoming ticks; when profit reaches 1R, advances stop loss
   to entry price (breakeven). Triggers immediate marketable exits on Stop Loss or Target Profit hits.
5. Emergency Liquidation: Supports rapid position flattening on risk circuit breakers or 15:10 IST square-off.
"""

from collections import deque
from datetime import datetime
import time
from typing import Any, Dict, List, Optional
import uuid
from loguru import logger

from shared.constants import IST_TZ
from shared.enums import OrderStatus, OrderType, Side
from shared.exceptions import RateLimitExceededException
from shared.schemas import Order, Position, Signal, Tick
from trading_engine.core.event_bus import (
    EventBus,
    OrderUpdateEvent,
    TickEvent,
    bus,
)
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper

SEBI_MAX_ORDERS_PER_SEC = 10
DEFAULT_MARKETABLE_BUFFER_PCT = 0.001  # 0.1% buffer above ask / below bid


class OrderManager:
    """Manages order submission, rate limiting, state tracking, and 1R trailing stops."""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        broker: Optional[Any] = None,
        risk_gatekeeper: Optional[PortfolioRiskGatekeeper] = None,
        max_orders_per_sec: int = SEBI_MAX_ORDERS_PER_SEC,
        marketable_buffer_pct: float = DEFAULT_MARKETABLE_BUFFER_PCT,
    ):
        self.bus = event_bus or bus
        self.broker = broker or PaperBroker(event_bus=self.bus)
        self.risk_gatekeeper = risk_gatekeeper
        self.max_orders_per_sec = max_orders_per_sec
        self.marketable_buffer_pct = marketable_buffer_pct

        # Rate Limiting: timestamps of sent orders
        self._order_timestamps: deque[float] = deque()

        # State tracking: order_id -> Order
        self.orders: Dict[str, Order] = {}
        # Active positions: symbol -> Position
        self.open_positions: Dict[str, Position] = {}
        # Symbols currently pending exit order execution (to prevent duplicate exits)
        self._exiting_symbols: set[str] = set()

        # Bracket tracking: symbol -> initial_sl, target, initial_risk
        self._bracket_sl: Dict[str, float] = {}
        self._bracket_tp: Dict[str, float] = {}
        self._initial_sl: Dict[str, float] = {}
        self._initial_risk: Dict[str, float] = {}
        self._breakeven_advanced: Dict[str, bool] = {}

        # Wire up risk gatekeeper flatten handler
        if self.risk_gatekeeper is not None:
            self.risk_gatekeeper.set_flatten_handler(self.flatten_all_positions)

        # Wire up event subscribers
        self._register_subscribers()

    def _register_subscribers(self) -> None:
        """Subscribes to EventBus topics."""
        self.bus.subscribe(TickEvent, self.on_tick)
        self.bus.subscribe(OrderUpdateEvent, self.on_order_update)

    def _check_rate_limit(self) -> None:
        """Enforces SEBI retail rate limiter (<= 10 orders per sliding second)."""
        now = time.monotonic()
        while self._order_timestamps and (now - self._order_timestamps[0] >= 1.0):
            self._order_timestamps.popleft()

        if len(self._order_timestamps) >= self.max_orders_per_sec:
            msg = (
                f"SEBI retail rate limit exceeded: {len(self._order_timestamps)} orders dispatched in last 1.0s "
                f"(Max allowed: {self.max_orders_per_sec}/sec)"
            )
            logger.error(f"[RATE LIMIT] {msg}")
            raise RateLimitExceededException(msg)

        self._order_timestamps.append(now)

    def calculate_marketable_limit_price(self, side: Side, reference_price: float) -> float:
        """
        Calculates SEBI-compliant marketable limit price with protective buffer:
        - BUY: reference_price * (1 + buffer)
        - SELL: reference_price * (1 - buffer)
        """
        if side == Side.BUY:
            return round(reference_price * (1.0 + self.marketable_buffer_pct), 2)
        else:
            return round(reference_price * (1.0 - self.marketable_buffer_pct), 2)

    async def submit_signal_order(self, signal: Signal, quantity: int) -> Optional[Order]:
        """
        Converts an approved strategy signal into an actionable marketable limit Order.
        Initializes bracket stop loss and take profit targets.
        """
        if quantity <= 0:
            logger.warning(f"Order rejected for {signal.tradingsymbol}: Quantity <= 0 ({quantity})")
            return None

        # 1. Rate limiter check
        self._check_rate_limit()

        # 2. Marketable limit price determination
        limit_price = self.calculate_marketable_limit_price(signal.side, signal.entry_price)

        now_ist = datetime.now(IST_TZ)
        order = Order(
            order_id=str(uuid.uuid4()),
            signal_id=signal.signal_id,
            tradingsymbol=signal.tradingsymbol,
            side=signal.side,
            product="MIS",
            order_type=OrderType.LIMIT,
            quantity=quantity,
            limit_price=limit_price,
            status=OrderStatus.PENDING_NEW,
            created_at_ist=now_ist,
        )

        # 3. Cache bracket parameters
        sym = signal.tradingsymbol
        self._bracket_sl[sym] = signal.stop_loss
        self._bracket_tp[sym] = signal.target_price
        self._initial_sl[sym] = signal.stop_loss
        self._initial_risk[sym] = abs(signal.entry_price - signal.stop_loss)
        self._breakeven_advanced[sym] = False

        self.orders[order.order_id] = order
        logger.info(
            f"[ORDER SUBMITTED] {order.tradingsymbol} {order.side.value} x {order.quantity} "
            f"@ Marketable Limit Rs.{order.limit_price:.2f} (SL: {signal.stop_loss}, TP: {signal.target_price})"
        )

        # 4. Dispatch to broker
        executed_order = await self.broker.submit_order(order)
        return executed_order

    async def exit_position(
        self,
        symbol: str,
        reason: str,
        tick: Optional[Tick] = None,
    ) -> Optional[Order]:
        """
        Submits an immediate marketable limit order to close an open position.
        Prevents duplicate exit orders for the same symbol.
        """
        if symbol not in self.open_positions:
            logger.debug(f"Exit skipped for {symbol}: Position not open")
            return None

        if symbol in self._exiting_symbols:
            logger.debug(f"Exit already in progress for {symbol}")
            return None

        self._exiting_symbols.add(symbol)
        pos = self.open_positions[symbol]

        # Determine exit side
        exit_side = Side.SELL if pos.side == Side.BUY else Side.BUY

        # Rate limiter check
        self._check_rate_limit()

        ref_price = tick.ltp if tick is not None else pos.ltp
        limit_price = self.calculate_marketable_limit_price(exit_side, ref_price)

        # Normalize exit reason to standard codes: TP_HIT, SL_HIT, 1R_BREAKEVEN, TIME_EXIT
        clean_reason = reason.upper() if reason else "MANUAL"
        if "TARGET" in clean_reason or "TP" in clean_reason:
            exit_code = "TP_HIT"
        elif "BREAKEVEN" in clean_reason or ("STOP_LOSS" in clean_reason and self._breakeven_advanced.get(symbol, False)):
            exit_code = "1R_BREAKEVEN"
        elif "STOP_LOSS" in clean_reason or "SL" in clean_reason:
            exit_code = "SL_HIT"
        elif "1510" in clean_reason or "TIME" in clean_reason or "SQUARE_OFF" in clean_reason:
            exit_code = "TIME_EXIT"
        else:
            exit_code = clean_reason

        now_ist = datetime.now(IST_TZ)
        exit_order = Order(
            order_id=str(uuid.uuid4()),
            signal_id=None,
            tradingsymbol=symbol,
            side=exit_side,
            product="MIS",
            order_type=OrderType.LIMIT,
            quantity=pos.quantity,
            limit_price=limit_price,
            status=OrderStatus.PENDING_NEW,
            exit_reason=exit_code,
            created_at_ist=now_ist,
        )

        self.orders[exit_order.order_id] = exit_order
        logger.info(
            f"[EXIT ORDER PLACED] {symbol} {exit_side.value} x {exit_order.quantity} @ Rs.{limit_price:.2f} "
            f"(Reason: {exit_code})"
        )

        executed_order = await self.broker.submit_order(exit_order)
        if executed_order is not None and getattr(executed_order, "exit_reason", None) is None:
            executed_order.exit_reason = exit_code
        return executed_order

    async def flatten_all_positions(self, reason: str) -> List[Order]:
        """Liquidates all active open positions immediately (emergency circuit breaker or 15:10 IST)."""
        logger.warning(f"[FLATTEN ALL] Triggered: {reason} on {len(self.open_positions)} active positions")
        flatten_orders: List[Order] = []

        symbols_to_flatten = list(self.open_positions.keys())
        for sym in symbols_to_flatten:
            try:
                order = await self.exit_position(sym, reason=reason)
                if order is not None:
                    flatten_orders.append(order)
            except Exception as e:
                logger.error(f"Failed to flatten position for {sym}: {e}")

        return flatten_orders

    async def on_order_update(self, event: OrderUpdateEvent) -> None:
        """
        Lifecycle state machine listener for OrderUpdateEvent.
        Handles fill transitions, updates open positions, and computes realized net PnL.
        """
        order = event.order
        self.orders[order.order_id] = order
        sym = order.tradingsymbol

        if order.status == OrderStatus.FILLED:
            # 1. Entry order fill: Create Position
            if sym not in self.open_positions:
                fill_price = order.average_fill_price or (order.limit_price or 0.0)
                sl = self._bracket_sl.get(sym, fill_price * 0.99)
                tp = self._bracket_tp.get(sym, fill_price * 1.02)

                pos = Position(
                    tradingsymbol=sym,
                    side=order.side,
                    quantity=order.filled_quantity,
                    average_entry_price=fill_price,
                    ltp=fill_price,
                    unrealized_pnl=0.0,
                    realized_pnl=0.0,
                    stop_loss=sl,
                    target_price=tp,
                )
                self.open_positions[sym] = pos
                if self.risk_gatekeeper is not None:
                    self.risk_gatekeeper.register_position(pos)

                logger.info(
                    f"[STATE MACHINE] Position opened: {sym} {pos.side.value} x {pos.quantity} @ Rs.{pos.average_entry_price:.2f}"
                )

            # 2. Exit order fill: Close Position and compute realized net PnL
            else:
                pos = self.open_positions[sym]
                # Confirm this was an exit order (opposite side)
                is_exit = (pos.side == Side.BUY and order.side == Side.SELL) or (
                    pos.side == Side.SELL and order.side == Side.BUY
                )
                if is_exit:
                    exit_price = order.average_fill_price or (order.limit_price or pos.ltp)
                    # Deduct exact Zerodha statutory charges
                    if hasattr(self.broker, "record_completed_trade"):
                        breakdown = self.broker.record_completed_trade(
                            entry_price=pos.average_entry_price,
                            exit_price=exit_price,
                            quantity=order.filled_quantity,
                            side=pos.side,
                        )
                        realized_pnl = breakdown.net_pnl
                    else:
                        gross = (
                            (exit_price - pos.average_entry_price) * order.filled_quantity
                            if pos.side == Side.BUY
                            else (pos.average_entry_price - exit_price) * order.filled_quantity
                        )
                        realized_pnl = round(gross, 2)

                    pos.realized_pnl = realized_pnl
                    del self.open_positions[sym]
                    self._exiting_symbols.discard(sym)

                    # Notify risk gatekeeper
                    if self.risk_gatekeeper is not None:
                        self.risk_gatekeeper.close_position(sym, realized_pnl)

                    logger.info(
                        f"[STATE MACHINE] Position closed: {sym} | Entry: {pos.average_entry_price:.2f} | "
                        f"Exit: {exit_price:.2f} | Net Realized PnL: Rs.{realized_pnl:,.2f}"
                    )

        elif order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self._exiting_symbols.discard(sym)
            logger.warning(f"[STATE MACHINE] Order {order.order_id} ({sym}) reached {order.status.value}")

    async def on_tick(self, event: TickEvent) -> None:
        """
        Consumes live ticks to monitor bracket orders and manage 1R breakeven trailing stops:
        - Advances stop loss to entry price when position moves +1R in profit.
        - Triggers exit on Stop Loss breach.
        - Triggers exit on Take Profit target breach.
        """
        tick = event.tick
        sym = tick.tradingsymbol

        if sym not in self.open_positions:
            return

        pos = self.open_positions[sym]
        pos.ltp = tick.ltp

        # Evaluate Long Position
        if pos.side == Side.BUY:
            pos.unrealized_pnl = round((tick.ltp - pos.average_entry_price) * pos.quantity, 2)
            init_sl = self._initial_sl.get(sym, pos.stop_loss)
            initial_r = self._initial_risk.get(sym, max(0.5, pos.average_entry_price - init_sl))

            # 1. Check 1R Breakeven Advance
            if not self._breakeven_advanced.get(sym, False):
                if tick.ltp >= (pos.average_entry_price + initial_r):
                    if pos.stop_loss < pos.average_entry_price:
                        pos.stop_loss = pos.average_entry_price
                        self._breakeven_advanced[sym] = True
                        logger.info(
                            f"[1R TRAILING STOP ADVANCED] {sym} LONG hit +1R (LTP={tick.ltp:.2f} >= "
                            f"{pos.average_entry_price + initial_r:.2f}). Stop loss advanced to BREAKEVEN: Rs.{pos.stop_loss:.2f}"
                        )

            # 2. Check Hard Stop Loss Trigger
            if tick.ltp <= pos.stop_loss:
                logger.warning(
                    f"[STOP LOSS TRIGGERED] {sym} LONG hit SL: LTP Rs.{tick.ltp:.2f} <= SL Rs.{pos.stop_loss:.2f}"
                )
                await self.exit_position(sym, reason="STOP_LOSS_HIT", tick=tick)
                return

            # 3. Check Take Profit Target Trigger
            if tick.ltp >= pos.target_price:
                logger.info(
                    f"[TARGET TRIGGERED] {sym} LONG hit Target: LTP Rs.{tick.ltp:.2f} >= TP Rs.{pos.target_price:.2f}"
                )
                await self.exit_position(sym, reason="TARGET_HIT", tick=tick)
                return

        # Evaluate Short Position
        elif pos.side == Side.SELL:
            pos.unrealized_pnl = round((pos.average_entry_price - tick.ltp) * pos.quantity, 2)
            init_sl = self._initial_sl.get(sym, pos.stop_loss)
            initial_r = self._initial_risk.get(sym, max(0.5, init_sl - pos.average_entry_price))

            # 1. Check 1R Breakeven Advance
            if not self._breakeven_advanced.get(sym, False):
                if tick.ltp <= (pos.average_entry_price - initial_r):
                    if pos.stop_loss > pos.average_entry_price:
                        pos.stop_loss = pos.average_entry_price
                        self._breakeven_advanced[sym] = True
                        logger.info(
                            f"[1R TRAILING STOP ADVANCED] {sym} SHORT hit +1R (LTP={tick.ltp:.2f} <= "
                            f"{pos.average_entry_price - initial_r:.2f}). Stop loss advanced to BREAKEVEN: Rs.{pos.stop_loss:.2f}"
                        )

            # 2. Check Hard Stop Loss Trigger
            if tick.ltp >= pos.stop_loss:
                logger.warning(
                    f"[STOP LOSS TRIGGERED] {sym} SHORT hit SL: LTP Rs.{tick.ltp:.2f} >= SL Rs.{pos.stop_loss:.2f}"
                )
                await self.exit_position(sym, reason="STOP_LOSS_HIT", tick=tick)
                return

            # 3. Check Take Profit Target Trigger
            if tick.ltp <= pos.target_price:
                logger.info(
                    f"[TARGET TRIGGERED] {sym} SHORT hit Target: LTP Rs.{tick.ltp:.2f} <= TP Rs.{pos.target_price:.2f}"
                )
                await self.exit_position(sym, reason="TARGET_HIT", tick=tick)
                return
