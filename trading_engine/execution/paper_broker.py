"""
Trillionaire Trading Engine - Simulated Paper Broker
Simulates Zerodha Kite Connect order matching with:
1. Level-1 tick-by-tick execution for limit and marketable limit orders.
2. Realistic 2 bps slippage model for immediate marketable executions.
3. Accurate Indian MIS statutory charges and transaction taxes via calculate_mis_net_pnl().
4. Dispatches OrderUpdateEvent onto EventBus for position reconciliation.
"""

from datetime import datetime
from typing import Dict, List, Optional
import uuid
from loguru import logger

from research_lab.profiler.indian_charges import ChargesBreakdown, calculate_mis_net_pnl
from shared.constants import IST_TZ
from shared.enums import OrderStatus, OrderType, Side
from shared.schemas import Order, Tick
from trading_engine.core.event_bus import EventBus, OrderUpdateEvent, TickEvent, bus

DEFAULT_SLIPPAGE_BPS = 0.0002  # 2 basis points (0.02%)


class PaperBroker:
    """Virtual broker simulating execution, queue priority, fills, and Zerodha charges."""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        slippage_rate: float = DEFAULT_SLIPPAGE_BPS,
    ):
        self.bus = event_bus or bus
        self.slippage_rate = slippage_rate

        # order_id -> Order
        self.orders: Dict[str, Order] = {}
        # order_id -> Order (for resting non-marketable orders)
        self.open_orders: Dict[str, Order] = {}
        # symbol -> latest Tick
        self.latest_ticks: Dict[str, Tick] = {}
        # Completed trades charges breakdown history
        self.trade_charges_history: List[ChargesBreakdown] = []

        # Wire event listener
        self.bus.subscribe(TickEvent, self.on_tick)

    def set_latest_tick(self, tick: Tick) -> None:
        """Manually records latest tick for immediate simulated matching."""
        self.latest_ticks[tick.tradingsymbol] = tick

    async def submit_order(self, order: Order) -> Order:
        """
        Submits an order to the simulated exchange.
        Marketable limit orders are executed immediately against current tick with slippage.
        Resting limit orders are queued into open_orders.
        """
        order.broker_order_id = f"SIM_{uuid.uuid4().hex[:10].upper()}"
        order.status = OrderStatus.OPEN
        self.orders[order.order_id] = order

        tick = self.latest_ticks.get(order.tradingsymbol)
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            if tick is not None:
                # Check marketability against latest tick
                if order.side == Side.BUY and order.limit_price >= tick.ltp:
                    sim_price = round(tick.ltp * (1.0 + self.slippage_rate), 2)
                    fill_price = min(order.limit_price, sim_price)
                    return await self._execute_fill(order, fill_price, tick.timestamp_ist)

                elif order.side == Side.SELL and order.limit_price <= tick.ltp:
                    sim_price = round(tick.ltp * (1.0 - self.slippage_rate), 2)
                    fill_price = max(order.limit_price, sim_price)
                    return await self._execute_fill(order, fill_price, tick.timestamp_ist)
            else:
                # No tick cached yet: execute marketable limit order immediately at limit price
                fill_dt = order.created_at_ist or datetime.now(IST_TZ)
                return await self._execute_fill(order, order.limit_price, fill_dt)

        # Resting limit order (not immediately marketable)
        self.open_orders[order.order_id] = order
        logger.info(
            f"[PAPER BROKER] Resting order queued: {order.tradingsymbol} {order.side.value} x {order.quantity} "
            f"@ Limit {order.limit_price} (Order ID: {order.order_id})"
        )
        await self.bus.publish(OrderUpdateEvent(order=order))
        return order

    async def on_tick(self, event: TickEvent) -> None:
        """Processes incoming tick and evaluates execution for resting limit orders."""
        await self.process_tick(event.tick)

    async def process_tick(self, tick: Tick) -> None:
        """Evaluates resting limit orders against incoming tick."""
        sym = tick.tradingsymbol
        self.latest_ticks[sym] = tick

        matching_orders = [o for o in self.open_orders.values() if o.tradingsymbol == sym]
        for order in matching_orders:
            if order.order_type == OrderType.LIMIT and order.limit_price is not None:
                if order.side == Side.BUY and tick.ltp <= order.limit_price:
                    # Resting Buy filled at limit price or better
                    fill_price = min(order.limit_price, tick.ltp)
                    self.open_orders.pop(order.order_id, None)
                    await self._execute_fill(order, fill_price, tick.timestamp_ist)

                elif order.side == Side.SELL and tick.ltp >= order.limit_price:
                    # Resting Sell filled at limit price or better
                    fill_price = max(order.limit_price, tick.ltp)
                    self.open_orders.pop(order.order_id, None)
                    await self._execute_fill(order, fill_price, tick.timestamp_ist)

    async def cancel_order(self, order_id: str) -> Optional[Order]:
        """Cancels a pending resting limit order."""
        order = self.open_orders.pop(order_id, None)
        if order is not None:
            order.status = OrderStatus.CANCELLED
            logger.info(f"[PAPER BROKER] Order cancelled: {order_id} ({order.tradingsymbol})")
            await self.bus.publish(OrderUpdateEvent(order=order))
            return order
        return self.orders.get(order_id)

    async def _execute_fill(self, order: Order, fill_price: float, fill_time: datetime) -> Order:
        """Executes fill, transitions state, and broadcasts OrderUpdateEvent."""
        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.average_fill_price = fill_price

        logger.info(
            f"[PAPER BROKER FILL] {order.tradingsymbol} {order.side.value} x {order.quantity} "
            f"FILLED @ Rs.{fill_price:.2f} (Order: {order.order_id}, BrokerID: {order.broker_order_id})"
        )

        await self.bus.publish(OrderUpdateEvent(order=order))
        return order

    def record_completed_trade(
        self,
        entry_price: float,
        exit_price: float,
        quantity: int,
        side: Side,
    ) -> ChargesBreakdown:
        """Computes and records statutory Zerodha MIS charges for an executed round-trip."""
        breakdown = calculate_mis_net_pnl(
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            side=side,
        )
        self.trade_charges_history.append(breakdown)
        logger.info(
            f"[CHARGES AUDIT] {breakdown.side.value} x {breakdown.quantity} | Gross: Rs.{breakdown.gross_pnl:.2f} | "
            f"Total Charges: Rs.{breakdown.total_charges:.2f} (Brokerage: Rs.{breakdown.brokerage:.2f}, STT: Rs.{breakdown.stt:.2f}, "
            f"Exchange: Rs.{breakdown.exchange_txn:.2f}, GST: Rs.{breakdown.gst:.2f}) | Net PnL: Rs.{breakdown.net_pnl:.2f}"
        )
        return breakdown
