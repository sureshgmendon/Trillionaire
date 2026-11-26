"""
Trillionaire Trading Engine - SEBI-Compliant Order Manager & State Machine
Coordinates order execution, rate limiting, OCO exit brackets, immutable closed-trade ledger,
and real-time position monitoring:
1. SEBI Retail Rate Limiter: Enforces statutory ceiling of <= 10 orders per second.
2. SEBI-Compliant Marketable Limit Orders: Disallows pure MARKET orders, applying protective buffers.
3. Price Sanity Guard: Rejects any order/fill deviating >5% from live symbol tick LTP.
4. Strict OCO Exit Management: Tracks paired SL & Target exit legs; cancels opposite leg immediately
   when one fills or when FLATTEN ALL runs.
5. Immutable Closed-Trades Ledger: Captures complete round-trip snapshots (entry/SL/target/exit/PnL/reason)
   BEFORE deleting open positions, syncing daily realized PnL cleanly.
6. Live 1R Breakeven Trailing Stop Manager & Daily Session Persistence (Redis + Disk).
"""

from collections import deque
from datetime import datetime
import json
import time
from typing import Any, Callable, Dict, List, Optional
import uuid
from loguru import logger

from shared.constants import IST_TZ
from shared.enums import OrderStatus, OrderType, Side, Timeframe
from shared.exceptions import RateLimitExceededException
from shared.schemas import Candle, ClosedTrade, Order, Position, Signal, Tick
from trading_engine.core.event_bus import (
    CandleCloseEvent,
    EventBus,
    OrderUpdateEvent,
    RiskAlertEvent,
    TickEvent,
    bus,
)
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper

SEBI_MAX_ORDERS_PER_SEC = 10
DEFAULT_MARKETABLE_BUFFER_PCT = 0.001  # 0.1% buffer above ask / below bid
MAX_PRICE_DEVIATION_PCT = 0.05  # 5% sanity ceiling vs live tick LTP


def normalize_exit_reason(raw_reason: Optional[str], breakeven_advanced: bool = False) -> str:
    """Normalizes internal exit reasons to canonical codes: TARGET_HIT, SL_HIT, PARTIAL_BOOK_1R, FLATTEN_ALL, MANUAL_EXIT, EOD_SQUARE_OFF."""
    if not raw_reason:
        return "MANUAL_EXIT"
    upper = raw_reason.upper()
    if "PARTIAL" in upper:
        return "PARTIAL_BOOK_1R"
    if "TARGET" in upper or upper == "TP_HIT":
        return "TARGET_HIT"
    if "FLATTEN" in upper or "KILL" in upper or "SHUTDOWN" in upper or "LOSS" in upper or "FLOOR" in upper:
        return "FLATTEN_ALL"
    if "1510" in upper or "EOD" in upper or "SQUARE_OFF" in upper or "TIME" in upper:
        return "EOD_SQUARE_OFF"
    if "SL" in upper or "STOP_LOSS" in upper or "BREAKEVEN" in upper:
        return "SL_HIT"
    if "MANUAL" in upper:
        return "MANUAL_EXIT"
    return upper


class OrderManager:
    """Manages order submission, rate limiting, OCO exit legs, state tracking, and immutable trade ledger."""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        broker: Optional[Any] = None,
        risk_gatekeeper: Optional[PortfolioRiskGatekeeper] = None,
        max_orders_per_sec: int = SEBI_MAX_ORDERS_PER_SEC,
        marketable_buffer_pct: float = DEFAULT_MARKETABLE_BUFFER_PCT,
        enable_oco_brackets: bool = True,
        enable_partial_booking: Optional[bool] = None,
        on_state_change: Optional[Callable[[], None]] = None,
    ):
        self.bus = event_bus or bus
        self.broker = broker or PaperBroker(event_bus=self.bus)
        self.risk_gatekeeper = risk_gatekeeper
        self.max_orders_per_sec = max_orders_per_sec
        self.marketable_buffer_pct = marketable_buffer_pct
        self.enable_oco_brackets = enable_oco_brackets
        self._explicit_enable_partial_booking = enable_partial_booking
        self.on_state_change = on_state_change

        # Rate Limiting: timestamps of sent orders
        self._order_timestamps: deque[float] = deque()

        # State tracking: order_id -> Order
        self.orders: Dict[str, Order] = {}
        # Active positions: symbol -> Position
        self.open_positions: Dict[str, Position] = {}
        # Immutable closed round-trip trades ledger for active IST day
        self.closed_trades: List[ClosedTrade] = []
        # Symbols currently pending exit order execution (to prevent duplicate exits)
        self._exiting_symbols: set[str] = set()
        # Live tick cache for price sanity validation
        self._latest_ticks: Dict[str, Tick] = {}
        # Active OCO exit legs: symbol -> {"sl_order_id": ..., "tp_order_id": ...}
        self._oco_legs: Dict[str, Dict[str, str]] = {}

        # Bracket tracking: symbol -> initial_sl, initial_tp, target, initial_risk, entry_time, entry_order_id
        self._bracket_sl: Dict[str, float] = {}
        self._bracket_tp: Dict[str, float] = {}
        self._initial_sl: Dict[str, float] = {}
        self._initial_tp: Dict[str, float] = {}
        self._initial_risk: Dict[str, float] = {}
        self._breakeven_advanced: Dict[str, bool] = {}
        self._tp_sl_lock_level: Dict[str, float] = {}
        self._entry_times: Dict[str, datetime] = {}
        self._entry_order_ids: Dict[str, str] = {}

        # Wire up risk gatekeeper flatten handler
        if self.risk_gatekeeper is not None:
            self.risk_gatekeeper.set_flatten_handler(self.flatten_all_positions)

        # Wire up event subscribers
        self._register_subscribers()

    def _notify_state_change(self) -> None:
        if self.on_state_change is not None:
            try:
                self.on_state_change()
            except Exception as e:
                logger.debug(f"[ORDER MANAGER] State persist callback skipped: {e}")

    def _persist_order_to_redis(self, order: Order) -> None:
        """Mirrors order state to Redis hash trillionaire:orders:{YYYY-MM-DD} with 48h TTL."""
        try:
            import redis
            from config.settings import settings
            r = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )
            today_iso = datetime.now(IST_TZ).date().isoformat()
            key = f"trillionaire:orders:{today_iso}"
            order_data = order.model_dump(mode="json")
            r.hset(key, order.order_id, json.dumps(order_data))
            r.expire(key, 172800)  # 48h TTL
        except Exception as e:
            logger.debug(f"[REDIS ORDER PERSIST] Bypassed: {e}")

    def _persist_closed_trade_to_redis(self, trade: ClosedTrade) -> None:
        """Mirrors immutable closed trade record to Redis list/hash trillionaire:closed_trades:{YYYY-MM-DD}."""
        try:
            import redis
            from config.settings import settings
            r = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )
            today_iso = datetime.now(IST_TZ).date().isoformat()
            key = f"trillionaire:closed_trades:{today_iso}"
            r.hset(key, trade.order_id, trade.model_dump_json())
            r.expire(key, 172800)
        except Exception as e:
            logger.debug(f"[REDIS CLOSED TRADE PERSIST] Bypassed: {e}")

    def _persist_realized_pnl_to_redis(self, realized_pnl: float) -> None:
        """Mirrors today's cumulative realized PnL to Redis under trillionaire:realized_pnl:{YYYY-MM-DD} with 48h TTL."""
        try:
            import redis
            from config.settings import settings
            r = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )
            today_iso = datetime.now(IST_TZ).date().isoformat()
            key = f"trillionaire:realized_pnl:{today_iso}"
            r.set(key, str(round(realized_pnl, 2)), ex=172800)
        except Exception as e:
            logger.debug(f"[REDIS PNL PERSIST] Bypassed: {e}")

    def _register_subscribers(self) -> None:
        """Subscribes to EventBus topics."""
        self.bus.subscribe(TickEvent, self.on_tick)
        self.bus.subscribe(CandleCloseEvent, self.on_candle_close)
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

    def _get_live_tick_ltp(self, symbol: str) -> Optional[float]:
        """Returns latest cached tick LTP from OrderManager or attached broker."""
        tick = self._latest_ticks.get(symbol)
        if tick is None and hasattr(self.broker, "latest_ticks"):
            tick = self.broker.latest_ticks.get(symbol)
        if tick is not None and getattr(tick, "ltp", 0.0) > 0:
            return float(tick.ltp)
        return None

    def _validate_price_sanity(self, symbol: str, price: float) -> Optional[str]:
        """Validates execution/order price is within +/-5% of live symbol LTP when live tick exists."""
        live_ltp = self._get_live_tick_ltp(symbol)
        if live_ltp is None or price <= 0:
            return None
        dev = abs(price - live_ltp) / live_ltp
        if dev > MAX_PRICE_DEVIATION_PCT:
            return (
                f"Price sanity guard triggered for {symbol}: execution price Rs.{price:.2f} "
                f"deviates {dev * 100:.2f}% (>5.0%) from live tick LTP Rs.{live_ltp:.2f}"
            )
        return None

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

    async def _record_rejection(self, order: Order, reason: str) -> Order:
        """Records rejected order in orders ledger, Redis, disk, EventBus, and RiskAlert."""
        order.status = OrderStatus.REJECTED
        order.rejection_reason = reason
        self.orders[order.order_id] = order
        self._persist_order_to_redis(order)
        self._notify_state_change()
        logger.error(f"[ORDER REJECTED] {order.tradingsymbol}: {reason}")
        await self.bus.publish(OrderUpdateEvent(order=order))
        await self.bus.publish(
            RiskAlertEvent(symbol=order.tradingsymbol, level="CRITICAL", message=reason)
        )
        return order

    async def submit_signal_order(self, signal: Signal, quantity: int) -> Optional[Order]:
        """
        Converts an approved strategy signal into an actionable marketable limit Order.
        Initializes bracket stop loss and take profit targets, enforces rate limit and price sanity guard.
        """
        now_ist = datetime.now(IST_TZ)
        if quantity <= 0:
            logger.warning(f"Order rejected for {signal.tradingsymbol}: Quantity <= 0 ({quantity})")
            return None

        # 1. Rate limiter check
        self._check_rate_limit()

        # 2. Marketable limit price determination
        limit_price = self.calculate_marketable_limit_price(signal.side, signal.entry_price)
        order_id = str(uuid.uuid4())
        client_tag = f"tril_{order_id[:12]}"

        order = Order(
            order_id=order_id,
            signal_id=signal.signal_id,
            tradingsymbol=signal.tradingsymbol,
            side=signal.side,
            product="MIS",
            order_type=OrderType.LIMIT,
            quantity=quantity,
            limit_price=limit_price,
            sl_price=signal.stop_loss,
            target_price=signal.target_price,
            trade_group_id=order_id,
            client_order_tag=client_tag,
            status=OrderStatus.PENDING_NEW,
            created_at_ist=now_ist,
        )

        # 3. Price Sanity Check (>5% deviation vs live symbol tick LTP)
        sanity_err = self._validate_price_sanity(signal.tradingsymbol, limit_price)
        if sanity_err:
            return await self._record_rejection(order, sanity_err)

        # 4. Cache bracket parameters
        sym = signal.tradingsymbol
        self._bracket_sl[sym] = signal.stop_loss
        self._bracket_tp[sym] = signal.target_price
        self._initial_sl[sym] = signal.stop_loss
        self._initial_tp[sym] = signal.target_price
        self._initial_risk[sym] = abs(signal.entry_price - signal.stop_loss)
        self._breakeven_advanced[sym] = False
        self._tp_sl_lock_level.pop(sym, None)

        self.orders[order.order_id] = order
        self._persist_order_to_redis(order)
        self._notify_state_change()
        logger.info(
            f"[ORDER SUBMITTED] {order.tradingsymbol} {order.side.value} x {order.quantity} "
            f"@ Marketable Limit Rs.{order.limit_price:.2f} (SL: {signal.stop_loss}, TP: {signal.target_price})"
        )

        # 5. Dispatch to broker
        try:
            executed_order = await self.broker.submit_order(order)
            return executed_order
        except Exception as e:
            if order.status != OrderStatus.REJECTED:
                await self._record_rejection(order, str(e))
            return order

    async def place_oco_exit_legs(self, symbol: str) -> Dict[str, Optional[Order]]:
        """
        Places paired SL (SL_LIMIT -> TRIGGER PENDING) and Target (LIMIT -> OPEN) exit orders
        for an active position in LIVE mode or OCO simulation.
        """
        pos = self.open_positions.get(symbol)
        if pos is None:
            return {"sl_order": None, "tp_order": None}

        exit_side = Side.SELL if pos.side == Side.BUY else Side.BUY
        now_ist = datetime.now(IST_TZ)

        # 1. Stop Loss leg (SL_LIMIT -> TRIGGER PENDING)
        sl_lim = self.calculate_marketable_limit_price(exit_side, pos.stop_loss)
        sl_id = str(uuid.uuid4())
        sl_order = Order(
            order_id=sl_id,
            tradingsymbol=symbol,
            side=exit_side,
            product="MIS",
            order_type=OrderType.SL_LIMIT,
            quantity=pos.quantity,
            trigger_price=pos.stop_loss,
            limit_price=sl_lim,
            sl_price=pos.stop_loss,
            target_price=pos.target_price,
            is_exit=True,
            parent_order_id=pos.entry_order_id,
            trade_group_id=pos.entry_order_id,
            exit_reason="SL_HIT",
            client_order_tag=f"sl_{sl_id[:12]}",
            status=OrderStatus.PENDING_NEW,
            created_at_ist=now_ist,
        )

        # 2. Target leg (LIMIT -> OPEN / LIMIT PENDING)
        tp_id = str(uuid.uuid4())
        tp_order = Order(
            order_id=tp_id,
            tradingsymbol=symbol,
            side=exit_side,
            product="MIS",
            order_type=OrderType.LIMIT,
            quantity=pos.quantity,
            limit_price=pos.target_price,
            sl_price=pos.stop_loss,
            target_price=pos.target_price,
            is_exit=True,
            parent_order_id=pos.entry_order_id,
            trade_group_id=pos.entry_order_id,
            exit_reason="TARGET_HIT",
            client_order_tag=f"tp_{tp_id[:12]}",
            status=OrderStatus.PENDING_NEW,
            created_at_ist=now_ist,
        )

        self.orders[sl_order.order_id] = sl_order
        self.orders[tp_order.order_id] = tp_order
        self._oco_legs[symbol] = {"sl_order_id": sl_order.order_id, "tp_order_id": tp_order.order_id}
        pos.sl_order_id = sl_order.order_id
        pos.tp_order_id = tp_order.order_id

        is_live_broker = not isinstance(self.broker, PaperBroker)
        placed_sl: Optional[Order] = None
        placed_tp: Optional[Order] = None
        try:
            if is_live_broker:
                self._check_rate_limit()
            placed_sl = await self.broker.submit_order(sl_order)
        except Exception as e:
            logger.error(f"[OCO] Failed to place SL exit leg for {symbol}: {e}")

        try:
            if is_live_broker:
                self._check_rate_limit()
            placed_tp = await self.broker.submit_order(tp_order)
        except Exception as e:
            logger.error(f"[OCO] Failed to place Target exit leg for {symbol}: {e}")

        self._notify_state_change()
        return {"sl_order": placed_sl, "tp_order": placed_tp}

    async def sync_oco_exit_legs(self, symbol: str) -> None:
        """Synchronizes resting SL and TP OCO child orders when position SL, TP, or quantity changes."""
        pos = self.open_positions.get(symbol)
        if pos is None:
            return
        legs = self._oco_legs.get(symbol, {})
        exit_side = Side.SELL if pos.side == Side.BUY else Side.BUY
        sl_oid = legs.get("sl_order_id") or pos.sl_order_id
        tp_oid = legs.get("tp_order_id") or pos.tp_order_id

        if sl_oid and sl_oid in self.orders:
            sl_ord = self.orders[sl_oid]
            if sl_ord.status in (OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING, OrderStatus.PENDING_NEW):
                new_sl_lim = self.calculate_marketable_limit_price(exit_side, pos.stop_loss)
                if hasattr(self.broker, "modify_order"):
                    await self.broker.modify_order(
                        sl_oid,
                        trigger_price=pos.stop_loss,
                        limit_price=new_sl_lim,
                        quantity=pos.quantity,
                    )
                else:
                    sl_ord.trigger_price = round(pos.stop_loss, 2)
                    sl_ord.sl_price = round(pos.stop_loss, 2)
                    sl_ord.limit_price = new_sl_lim
                    sl_ord.quantity = pos.quantity
                    await self.bus.publish(OrderUpdateEvent(order=sl_ord))
                self._persist_order_to_redis(sl_ord)

        if tp_oid and tp_oid in self.orders:
            tp_ord = self.orders[tp_oid]
            if tp_ord.status in (OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING, OrderStatus.PENDING_NEW):
                if hasattr(self.broker, "modify_order"):
                    await self.broker.modify_order(
                        tp_oid,
                        limit_price=pos.target_price,
                        quantity=pos.quantity,
                    )
                else:
                    tp_ord.limit_price = round(pos.target_price, 2)
                    tp_ord.target_price = round(pos.target_price, 2)
                    tp_ord.quantity = pos.quantity
                    await self.bus.publish(OrderUpdateEvent(order=tp_ord))
                self._persist_order_to_redis(tp_ord)

        self._notify_state_change()

    async def modify_order(
        self,
        order_id: str,
        trigger_price: Optional[float] = None,
        limit_price: Optional[float] = None,
    ) -> Optional[Order]:
        """Modifies a pending SL/TP/LIMIT order price and keeps the attached Position bracket in sync."""
        ord_obj = self.orders.get(order_id)
        if ord_obj is None:
            return None
        if ord_obj.status not in (OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING, OrderStatus.PENDING_NEW):
            return ord_obj

        sym = ord_obj.tradingsymbol
        new_trig = round(float(trigger_price), 2) if trigger_price and trigger_price > 0 else ord_obj.trigger_price
        new_lim = round(float(limit_price), 2) if limit_price and limit_price > 0 else ord_obj.limit_price

        if hasattr(self.broker, "modify_order"):
            updated = await self.broker.modify_order(order_id, trigger_price=new_trig, limit_price=new_lim)
            if updated is not None:
                ord_obj = updated
        else:
            kite = getattr(self.broker, "kite", None)
            if kite is not None and ord_obj.broker_order_id:
                try:
                    kite.modify_order(
                        variety="regular",
                        order_id=ord_obj.broker_order_id,
                        trigger_price=new_trig,
                        price=new_lim,
                    )
                except Exception as exc:
                    logger.warning(f"[MODIFY ORDER] Kite modify warning for {order_id}: {exc}")
            ord_obj.trigger_price = new_trig
            ord_obj.limit_price = new_lim
            await self.bus.publish(OrderUpdateEvent(order=ord_obj))

        pos = self.open_positions.get(sym)
        if pos is not None:
            is_sl = ord_obj.order_type == OrderType.SL_LIMIT or (ord_obj.client_order_tag or "").startswith("sl_")
            if is_sl and new_trig is not None:
                pos.stop_loss = new_trig
                self._bracket_sl[sym] = new_trig
                ord_obj.sl_price = new_trig
            elif new_lim is not None and (ord_obj.is_exit or (ord_obj.client_order_tag or "").startswith("tp_")):
                pos.target_price = new_lim
                self._bracket_tp[sym] = new_lim
                ord_obj.target_price = new_lim

        self.orders[order_id] = ord_obj
        self._persist_order_to_redis(ord_obj)
        self._notify_state_change()
        return ord_obj

    async def cancel_pending_exit_orders(
        self,
        symbol: str,
        exclude_order_id: Optional[str] = None,
    ) -> List[Order]:
        """
        Strict OCO cleanup: Cancels all pending OPEN / TRIGGER_PENDING / PENDING_NEW
        exit orders for `symbol` (excluding `exclude_order_id`) so zero orphan legs remain.
        """
        cancelled: List[Order] = []
        pending_statuses = {OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING, OrderStatus.PENDING_NEW}

        # Collect candidate order IDs from tracked OCO legs + full orders book
        candidate_ids: set[str] = set()
        legs = self._oco_legs.get(symbol, {})
        for leg_id in legs.values():
            if leg_id and leg_id != exclude_order_id:
                candidate_ids.add(leg_id)

        for oid, ord_obj in list(self.orders.items()):
            if (
                ord_obj.tradingsymbol == symbol
                and oid != exclude_order_id
                and ord_obj.status in pending_statuses
                and (ord_obj.is_exit or ord_obj.order_type == OrderType.SL_LIMIT or oid in candidate_ids)
            ):
                candidate_ids.add(oid)

        for oid in candidate_ids:
            ord_obj = self.orders.get(oid)
            if ord_obj is None or ord_obj.status not in pending_statuses:
                continue
            try:
                res = await self.broker.cancel_order(oid)
                if res is not None:
                    ord_obj.status = OrderStatus.CANCELLED
                    self._persist_order_to_redis(ord_obj)
                    cancelled.append(ord_obj)
                else:
                    ord_obj.status = OrderStatus.CANCELLED
                    self._persist_order_to_redis(ord_obj)
                    cancelled.append(ord_obj)
                logger.info(f"[OCO CANCELLED] Paired exit leg {oid} cancelled for {symbol}")
            except Exception as e:
                logger.error(f"[OCO CANCEL ERROR] Failed cancelling exit leg {oid} for {symbol}: {e}")

        if symbol in self._oco_legs and exclude_order_id is not None:
            self._oco_legs.pop(symbol, None)
        elif exclude_order_id is None:
            self._oco_legs.pop(symbol, None)

        if cancelled:
            self._notify_state_change()
        return cancelled

    async def exit_position(
        self,
        symbol: str,
        reason: str,
        tick: Optional[Tick] = None,
    ) -> Optional[Order]:
        """
        Cancels any pending OCO exit legs first and submits an immediate marketable
        limit order to close an open position. Prevents duplicate exit orders.
        """
        if symbol not in self.open_positions:
            logger.debug(f"Exit skipped for {symbol}: Position not open")
            return None

        if symbol in self._exiting_symbols:
            logger.debug(f"Exit already in progress for {symbol}")
            return None

        self._exiting_symbols.add(symbol)
        pos = self.open_positions[symbol]

        # 1. Cancel any paired pending OCO SL/Target legs before squaring off
        await self.cancel_pending_exit_orders(symbol)

        # Determine exit side
        exit_side = Side.SELL if pos.side == Side.BUY else Side.BUY

        # Rate limiter check
        self._check_rate_limit()

        ref_price = tick.ltp if tick is not None else pos.ltp
        limit_price = self.calculate_marketable_limit_price(exit_side, ref_price)

        exit_code = normalize_exit_reason(
            reason, breakeven_advanced=self._breakeven_advanced.get(symbol, False)
        )

        now_ist = datetime.now(IST_TZ)
        exit_oid = str(uuid.uuid4())
        exit_order = Order(
            order_id=exit_oid,
            signal_id=None,
            tradingsymbol=symbol,
            side=exit_side,
            product="MIS",
            order_type=OrderType.LIMIT,
            quantity=pos.quantity,
            limit_price=limit_price,
            sl_price=pos.stop_loss,
            target_price=pos.target_price,
            is_exit=True,
            parent_order_id=pos.entry_order_id,
            trade_group_id=pos.entry_order_id,
            client_order_tag=f"ex_{exit_oid[:12]}",
            status=OrderStatus.PENDING_NEW,
            exit_reason=exit_code,
            created_at_ist=now_ist,
        )

        self.orders[exit_order.order_id] = exit_order
        self._persist_order_to_redis(exit_order)
        self._notify_state_change()
        logger.info(
            f"[EXIT ORDER PLACED] {symbol} {exit_side.value} x {exit_order.quantity} @ Rs.{limit_price:.2f} "
            f"(Reason: {exit_code})"
        )

        try:
            executed_order = await self.broker.submit_order(exit_order)
            if executed_order is not None and getattr(executed_order, "exit_reason", None) is None:
                executed_order.exit_reason = exit_code
            return executed_order
        except Exception as e:
            self._exiting_symbols.discard(symbol)
            if exit_order.status != OrderStatus.REJECTED:
                await self._record_rejection(exit_order, str(e))
            return exit_order

    async def flatten_all_positions(self, reason: str = "FLATTEN_ALL") -> List[Order]:
        """
        Liquidates all active open positions immediately (emergency circuit breaker, FLATTEN ALL, or 15:10 IST):
        1. Cancels all pending OPEN / TRIGGER PENDING exit orders across all symbols first.
        2. Squares off all active positions at current LTP with normalized FLATTEN_ALL / EOD_SQUARE_OFF reason.
        """
        logger.warning(f"[FLATTEN ALL] Triggered: {reason} on {len(self.open_positions)} active positions")
        flatten_orders: List[Order] = []

        # 1. Cancel ALL open / trigger pending orders first so zero orphan legs remain
        pending_statuses = {OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING, OrderStatus.PENDING_NEW}
        all_pending_syms = {
            ord_obj.tradingsymbol
            for ord_obj in self.orders.values()
            if ord_obj.status in pending_statuses
        }
        for sym in all_pending_syms | set(self._oco_legs.keys()):
            await self.cancel_pending_exit_orders(sym)

        # Also cancel any remaining non-exit pending orders
        for oid, ord_obj in list(self.orders.items()):
            if ord_obj.status in pending_statuses:
                try:
                    await self.broker.cancel_order(oid)
                    ord_obj.status = OrderStatus.CANCELLED
                    self._persist_order_to_redis(ord_obj)
                except Exception as e:
                    logger.debug(f"[FLATTEN ALL] Cancel pending order {oid}: {e}")

        # 2. Square off all active positions at current LTP
        norm_reason = normalize_exit_reason(reason) if ("1510" in reason.upper() or "EOD" in reason.upper() or "SQUARE_OFF" in reason.upper()) else "FLATTEN_ALL"
        symbols_to_flatten = list(self.open_positions.keys())
        for sym in symbols_to_flatten:
            try:
                tick = self._latest_ticks.get(sym)
                order = await self.exit_position(sym, reason=norm_reason, tick=tick)
                if order is not None:
                    flatten_orders.append(order)
            except Exception as e:
                logger.error(f"Failed to flatten position for {sym}: {e}")

        return flatten_orders

    async def on_order_update(self, event: OrderUpdateEvent) -> None:
        """
        Lifecycle state machine listener for OrderUpdateEvent.
        Handles fill transitions, price sanity checks, OCO opposite-leg cancellation,
        immutable ClosedTrade recording BEFORE deleting open position, and PnL sync.
        """
        order = event.order
        broker_orders = getattr(self.broker, "orders", None)
        if (
            order.order_id not in self.orders
            and isinstance(broker_orders, dict)
            and order.order_id not in broker_orders
        ):
            # Ignore OrderUpdateEvents emitted by another broker/engine sharing the same EventBus
            return

        self.orders[order.order_id] = order
        self._persist_order_to_redis(order)
        sym = order.tradingsymbol

        if order.status == OrderStatus.FILLED:
            fill_price = float(order.average_fill_price or order.limit_price or 0.0)

            # Enforce fill price sanity guard (>5% deviation check)
            if fill_price > 0:
                sanity_err = self._validate_price_sanity(sym, fill_price)
                if sanity_err:
                    await self._record_rejection(order, sanity_err)
                    self._exiting_symbols.discard(sym)
                    return

            # 1. Entry order fill: Create Position (ignore exit orders when position already closed)
            if sym not in self.open_positions and not order.is_exit:
                sl = order.sl_price or self._bracket_sl.get(sym, round(fill_price * 0.99, 2))
                init_sl = order.sl_price or self._initial_sl.get(sym) or sl
                self._initial_sl[sym] = init_sl
                tp = order.target_price or self._bracket_tp.get(sym, round(fill_price * 1.02, 2))
                init_tp = order.target_price or self._initial_tp.get(sym) or tp
                self._initial_tp[sym] = init_tp
                entry_ts = order.created_at_ist or datetime.now(IST_TZ)

                self._entry_times[sym] = entry_ts
                self._entry_order_ids[sym] = order.order_id
                if order.trade_group_id is None:
                    order.trade_group_id = order.order_id

                pos = Position(
                    tradingsymbol=sym,
                    side=order.side,
                    quantity=order.filled_quantity,
                    average_entry_price=fill_price,
                    ltp=fill_price,
                    unrealized_pnl=0.0,
                    realized_pnl=0.0,
                    stop_loss=sl,
                    initial_sl_price=init_sl,
                    target_price=tp,
                    original_target_price=init_tp,
                    entry_time=entry_ts,
                    entry_order_id=order.order_id,
                )
                self.open_positions[sym] = pos
                if self.risk_gatekeeper is not None:
                    self.risk_gatekeeper.register_position(pos)

                logger.info(
                    f"[STATE MACHINE] Position opened: {sym} {pos.side.value} x {pos.quantity} "
                    f"@ Rs.{pos.average_entry_price:.2f} (SL={sl:.2f}, TP={tp:.2f})"
                )

                # In both PAPER and LIVE modes (or explicit OCO mode), place paired SL + Target resting exit legs
                is_live_broker = not isinstance(self.broker, PaperBroker)
                if is_live_broker or self.enable_oco_brackets:
                    await self.place_oco_exit_legs(sym)

                self._notify_state_change()

            # 2. Exit order fill: Capture immutable ClosedTrade BEFORE modifying or closing Position
            elif sym in self.open_positions:
                pos = self.open_positions[sym]
                is_exit = (pos.side == Side.BUY and order.side == Side.SELL) or (
                    pos.side == Side.SELL and order.side == Side.BUY
                )
                if is_exit:
                    exit_price = float(order.average_fill_price or order.limit_price or pos.ltp)
                    filled_qty = int(order.filled_quantity or pos.quantity)
                    is_partial_leg = (
                        order.exit_reason == "PARTIAL_BOOK_1R" and filled_qty < pos.quantity
                    )

                    # Deduct exact Zerodha statutory charges
                    if hasattr(self.broker, "record_completed_trade"):
                        breakdown = self.broker.record_completed_trade(
                            entry_price=pos.average_entry_price,
                            exit_price=exit_price,
                            quantity=filled_qty,
                            side=pos.side,
                        )
                        realized_pnl = breakdown.net_pnl
                    else:
                        gross = (
                            (exit_price - pos.average_entry_price) * filled_qty
                            if pos.side == Side.BUY
                            else (pos.average_entry_price - exit_price) * filled_qty
                        )
                        realized_pnl = round(gross, 2)

                    pnl_pct = 0.0
                    if pos.average_entry_price > 0:
                        direction = 1.0 if pos.side == Side.BUY else -1.0
                        pnl_pct = round(
                            ((exit_price - pos.average_entry_price) / pos.average_entry_price) * 100.0 * direction,
                            2,
                        )

                    norm_exit_reason = normalize_exit_reason(
                        order.exit_reason,
                        breakeven_advanced=self._breakeven_advanced.get(sym, False),
                    )
                    order.exit_reason = norm_exit_reason
                    if order.parent_order_id is None and pos.entry_order_id:
                        order.parent_order_id = pos.entry_order_id
                    if order.trade_group_id is None and pos.entry_order_id:
                        order.trade_group_id = pos.entry_order_id

                    exit_ts = datetime.now(IST_TZ)
                    entry_ts = pos.entry_time or self._entry_times.get(sym) or order.created_at_ist or exit_ts

                    # Capture IMMUTABLE snapshot BEFORE modifying/deleting open position (preserving original initial_sl_price)
                    original_sl = pos.initial_sl_price or self._initial_sl.get(sym) or pos.stop_loss
                    closed_record = ClosedTrade(
                        order_id=order.order_id,
                        symbol=sym,
                        product=order.product or "MIS",
                        side=pos.side,
                        filled_qty=filled_qty,
                        entry_price=round(pos.average_entry_price, 2),
                        sl_price=round(original_sl, 2),
                        target_price=round(pos.target_price, 2),
                        actual_exit_price=round(exit_price, 2),
                        realized_pnl=round(realized_pnl, 2),
                        pnl_pct=pnl_pct,
                        exit_reason=norm_exit_reason,
                        is_partial=is_partial_leg,
                        entry_time=entry_ts,
                        exit_time=exit_ts,
                    )
                    self.closed_trades.append(closed_record)
                    self._persist_closed_trade_to_redis(closed_record)

                    if is_partial_leg:
                        # 50% Partial Booking at +1R: reduce remaining qty, move runner SL to breakeven, sync OCO legs
                        pos.quantity = pos.quantity - filled_qty
                        pos.realized_pnl = round(pos.realized_pnl + realized_pnl, 2)
                        pos.partial_booked = True
                        pos.stop_loss = pos.average_entry_price
                        self._breakeven_advanced[sym] = True
                        self._bracket_sl[sym] = pos.stop_loss
                        dir_sign = 1.0 if pos.side == Side.BUY else -1.0
                        pos.unrealized_pnl = round(
                            (pos.ltp - pos.average_entry_price) * pos.quantity * dir_sign, 2
                        )
                        if self.risk_gatekeeper is not None:
                            self.risk_gatekeeper.realized_daily_pnl = round(
                                sum(t.realized_pnl for t in self.closed_trades), 2
                            )
                            self._persist_realized_pnl_to_redis(self.risk_gatekeeper.realized_daily_pnl)
                        await self.sync_oco_exit_legs(sym)
                        self._notify_state_change()
                        logger.info(
                            f"[PARTIAL BOOKING 50%] {sym} booked {filled_qty} shares @ Rs.{exit_price:.2f} "
                            f"(Realized: Rs.{realized_pnl:,.2f}); remaining {pos.quantity} shares SL -> Breakeven Rs.{pos.stop_loss:.2f}"
                        )
                    else:
                        # Full Exit: Immediately cancel opposite paired OCO leg (if any) so zero orphan orders remain
                        await self.cancel_pending_exit_orders(sym, exclude_order_id=order.order_id)

                        pos.realized_pnl = round(pos.realized_pnl + realized_pnl, 2)
                        del self.open_positions[sym]
                        self._exiting_symbols.discard(sym)
                        self._entry_times.pop(sym, None)
                        self._entry_order_ids.pop(sym, None)
                        self._tp_sl_lock_level.pop(sym, None)
                        self._initial_tp.pop(sym, None)

                        # Notify risk gatekeeper and sync cumulative realized PnL
                        if self.risk_gatekeeper is not None:
                            self.risk_gatekeeper.close_position(sym, realized_pnl)
                            self.risk_gatekeeper.realized_daily_pnl = round(
                                sum(t.realized_pnl for t in self.closed_trades), 2
                            )
                            self._persist_realized_pnl_to_redis(self.risk_gatekeeper.realized_daily_pnl)

                        self._notify_state_change()

                        logger.info(
                            f"[STATE MACHINE] Position closed [{norm_exit_reason}]: {sym} | Entry: {pos.average_entry_price:.2f} | "
                            f"Exit: {exit_price:.2f} | Net Realized PnL: Rs.{realized_pnl:,.2f}"
                        )

        elif order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self._exiting_symbols.discard(sym)
            self._notify_state_change()
            logger.warning(f"[STATE MACHINE] Order {order.order_id} ({sym}) reached {order.status.value}")

    async def execute_partial_booking(
        self,
        symbol: str,
        qty_to_book: int,
        tick: Optional[Tick] = None,
    ) -> Optional[Order]:
        """Books partial 50% profits at +1R without closing remaining runner or cancelling OCO brackets."""
        pos = self.open_positions.get(symbol)
        if pos is None or qty_to_book <= 0 or qty_to_book >= pos.quantity or pos.partial_booked:
            return None

        pos.partial_booked = True
        exit_side = Side.SELL if pos.side == Side.BUY else Side.BUY
        self._check_rate_limit()

        ref_price = tick.ltp if tick is not None else pos.ltp
        limit_price = self.calculate_marketable_limit_price(exit_side, ref_price)
        now_ist = datetime.now(IST_TZ)
        oid = str(uuid.uuid4())
        partial_order = Order(
            order_id=oid,
            signal_id=None,
            tradingsymbol=symbol,
            side=exit_side,
            product="MIS",
            order_type=OrderType.LIMIT,
            quantity=qty_to_book,
            limit_price=limit_price,
            sl_price=pos.stop_loss,
            target_price=pos.target_price,
            is_exit=True,
            parent_order_id=pos.entry_order_id,
            trade_group_id=pos.entry_order_id,
            client_order_tag=f"pb_{oid[:12]}",
            status=OrderStatus.PENDING_NEW,
            exit_reason="PARTIAL_BOOK_1R",
            created_at_ist=now_ist,
        )

        self.orders[oid] = partial_order
        self._persist_order_to_redis(partial_order)
        try:
            executed = await self.broker.submit_order(partial_order)
            return executed
        except Exception as exc:
            pos.partial_booked = False
            logger.error(f"[PARTIAL BOOKING ERROR] {symbol}: {exc}")
            return None

    async def on_candle_close(self, event: CandleCloseEvent) -> None:
        """Evaluates Profit-Only Active Trade Manager on every 5-minute candle close."""
        candle = event.candle
        if candle.timeframe != Timeframe.M5:
            return
        await self.evaluate_profit_trade_manager(candle)

    async def evaluate_profit_trade_manager(self, candle: Candle) -> Optional[str]:
        """
        Profit-Only Active Trade Manager evaluated on M5 candle closes.
        STRICT RULE: Never alters losing or breakeven open positions.
        1. Unrealized gain >= 50% of distance to TP: steps SL 50% closer to Entry Price.
        2. Price approaches within 15% of TP with RVOL >= 1.5: extends TP by +1.0R and locks SL at original TP.
        """
        from config.settings import settings

        if not getattr(settings, "enable_profit_trade_manager", True):
            return None

        sym = candle.tradingsymbol
        pos = self.open_positions.get(sym)
        if pos is None:
            return None

        entry = float(pos.average_entry_price)
        orig_sl = float(pos.initial_sl_price or self._initial_sl.get(sym) or pos.stop_loss)
        orig_tp = float(pos.original_target_price or self._initial_tp.get(sym) or pos.target_price)
        risk_r = float(self._initial_risk.get(sym) or abs(entry - orig_sl))
        if risk_r <= 0.0:
            risk_r = max(0.5, abs(entry - orig_sl))

        is_long = pos.side == Side.BUY
        price = float(candle.close)
        gain = (price - entry) if is_long else (entry - price)
        tp_dist = (orig_tp - entry) if is_long else (entry - orig_tp)

        # STRICT PROFIT-ONLY GUARD: never alter losing or breakeven positions
        if gain <= 0 or tp_dist <= 0:
            return None

        progress = gain / tp_dist
        action_taken: Optional[str] = None
        legs_changed = False

        # Rule 1: >= 50% of distance to TP -> step SL 50% closer to Entry Price
        if progress >= 0.50 and not pos.halfway_sl_stepped:
            halfway_sl = round((orig_sl + entry) / 2.0, 2)
            improved = (halfway_sl > pos.stop_loss) if is_long else (halfway_sl < pos.stop_loss)
            if improved:
                pos.stop_loss = halfway_sl
                self._bracket_sl[sym] = halfway_sl
                legs_changed = True
                action_taken = "HALFWAY_SL_STEPPED"
                logger.info(
                    f"[PROFIT MANAGER] {sym} reached {progress * 100:.0f}% to TP; "
                    f"SL stepped 50% closer to Entry -> Rs.{pos.stop_loss:.2f}"
                )
            pos.halfway_sl_stepped = True

        # Rule 2: Within 15% of TP (progress >= 0.85) with strong RVOL >= 1.5 -> extend TP +1.0R & lock SL at orig_tp
        if progress >= 0.85 and float(candle.rvol or 0.0) >= 1.5 and not pos.profit_locked_at_tp:
            extended_tp = round(orig_tp + risk_r, 2) if is_long else round(orig_tp - risk_r, 2)
            pos.target_price = extended_tp
            self._bracket_tp[sym] = extended_tp

            # Guard against instant self-stop-out when close is 85%-99% of orig_tp:
            # lock at orig_tp immediately if price >= orig_tp, otherwise lock at 75% gain now & snap to orig_tp once crossed.
            at_or_past_orig_tp = (price >= orig_tp) if is_long else (price <= orig_tp)
            if at_or_past_orig_tp:
                safe_sl = round(orig_tp, 2)
            else:
                safe_sl = round(entry + 0.75 * tp_dist, 2) if is_long else round(entry - 0.75 * tp_dist, 2)
                self._tp_sl_lock_level[sym] = round(orig_tp, 2)

            if (safe_sl > pos.stop_loss) if is_long else (safe_sl < pos.stop_loss):
                pos.stop_loss = safe_sl
                self._bracket_sl[sym] = safe_sl
            pos.profit_locked_at_tp = True
            legs_changed = True
            action_taken = "TP_EXTENDED_SL_LOCKED"
            logger.info(
                f"[PROFIT MANAGER RUNNER] {sym} within 15% of TP with RVOL={candle.rvol:.2f} -> "
                f"TP extended +1.0R to Rs.{pos.target_price:.2f}, SL locked at Rs.{pos.stop_loss:.2f}"
            )

        if legs_changed:
            await self.sync_oco_exit_legs(sym)
        return action_taken

    async def on_tick(self, event: TickEvent) -> None:
        """
        Consumes live ticks to monitor bracket orders, 1R breakeven advance,
        50% partial profit booking, and hard SL / TP exits.
        """
        from config.settings import settings

        tick = event.tick
        sym = tick.tradingsymbol
        self._latest_ticks[sym] = tick

        if sym not in self.open_positions:
            return

        pos = self.open_positions[sym]
        pos.ltp = tick.ltp
        is_long = pos.side == Side.BUY
        dir_sign = 1.0 if is_long else -1.0
        pos.unrealized_pnl = round((tick.ltp - pos.average_entry_price) * pos.quantity * dir_sign, 2)

        init_sl = self._initial_sl.get(sym, pos.stop_loss)
        initial_r = self._initial_risk.get(sym, max(0.5, abs(pos.average_entry_price - init_sl)))
        sl_changed = False

        # Snap SL to original TP once extended runner crosses original TP
        pending_lock = self._tp_sl_lock_level.get(sym)
        if pending_lock is not None:
            crossed = (tick.ltp >= pending_lock) if is_long else (tick.ltp <= pending_lock)
            if crossed and ((pending_lock > pos.stop_loss) if is_long else (pending_lock < pos.stop_loss)):
                pos.stop_loss = pending_lock
                self._bracket_sl[sym] = pending_lock
                self._tp_sl_lock_level.pop(sym, None)
                sl_changed = True

        # 1. Check +1R Profit Milestone -> 50% Partial Booking & Breakeven Advance
        hit_1r = (
            tick.ltp >= (pos.average_entry_price + initial_r)
            if is_long
            else tick.ltp <= (pos.average_entry_price - initial_r)
        )
        if hit_1r:
            if not self._breakeven_advanced.get(sym, False):
                if (pos.stop_loss < pos.average_entry_price) if is_long else (pos.stop_loss > pos.average_entry_price):
                    pos.stop_loss = pos.average_entry_price
                    self._breakeven_advanced[sym] = True
                    self._bracket_sl[sym] = pos.stop_loss
                    sl_changed = True
                    logger.info(
                        f"[1R TRAILING STOP ADVANCED] {sym} {pos.side.value} hit +1R (LTP={tick.ltp:.2f}). "
                        f"Stop loss advanced to BREAKEVEN: Rs.{pos.stop_loss:.2f}"
                    )

            if (
                (
                    self._explicit_enable_partial_booking
                    if self._explicit_enable_partial_booking is not None
                    else getattr(settings, "enable_partial_booking", True)
                )
                and not pos.partial_booked
                and pos.quantity >= 2
            ):
                if sym not in self._exiting_symbols:
                    qty_to_book = pos.quantity // 2
                    await self.execute_partial_booking(sym, qty_to_book, tick=tick)
                    if sym not in self.open_positions:
                        return
                    sl_changed = False  # Already synced on partial fill

        if sl_changed:
            await self.sync_oco_exit_legs(sym)

        # 2. Check Hard Stop Loss Trigger
        hit_sl = (tick.ltp <= pos.stop_loss) if is_long else (tick.ltp >= pos.stop_loss)
        if hit_sl:
            logger.warning(
                f"[STOP LOSS TRIGGERED] {sym} {pos.side.value} hit SL: LTP Rs.{tick.ltp:.2f} vs SL Rs.{pos.stop_loss:.2f}"
            )
            await self.exit_position(sym, reason="SL_HIT", tick=tick)
            return

        # 3. Check Take Profit Target Trigger
        hit_tp = (tick.ltp >= pos.target_price) if is_long else (tick.ltp <= pos.target_price)
        if hit_tp:
            logger.info(
                f"[TARGET TRIGGERED] {sym} {pos.side.value} hit Target: LTP Rs.{tick.ltp:.2f} vs TP Rs.{pos.target_price:.2f}"
            )
            await self.exit_position(sym, reason="TARGET_HIT", tick=tick)
            return
