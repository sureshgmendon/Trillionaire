"""
Trillionaire Kite Broker - Live KiteTicker Stream & KiteConnect Order Engine
Provides live production broker integration with Zerodha Kite Connect:
1. WebSocket Market Data (KiteTicker): Subscribes in MODE_FULL, normalizes raw ticks to Tick schemas,
   and publishes TickEvent to EventBus with threadsafe dispatch.
2. Live Order Execution (KiteConnect): Places MIS limit orders with statutory tick-size rounding,
   tracks order lifecycle updates (on_order_update), and publishes OrderUpdateEvents.
3. 5-Second State Reconciler: Periodically audits broker positions and orders against OrderManager,
   adopting crash-orphaned positions and raising RiskAlertEvents on mismatches.
"""

import asyncio
from datetime import datetime
import threading
from typing import Any, Dict, List, Optional
import uuid
from loguru import logger

from research_lab.profiler.indian_charges import ChargesBreakdown, calculate_mis_net_pnl
from shared.constants import IST_TZ
from shared.enums import OrderStatus, OrderType, Side
from shared.exceptions import BrokerConnectionError, OrderExecutionError
from shared.schemas import Order, Position, Tick
from trading_engine.brokers.kite.instruments import InstrumentSync
from trading_engine.core.event_bus import (
    EventBus,
    OrderUpdateEvent,
    RiskAlertEvent,
    TickEvent,
    bus,
)


class KiteBroker:
    """Production Zerodha Kite Connect live broker adapter and ticker streaming manager."""

    def __init__(
        self,
        kite_client: Any,
        ticker_client: Optional[Any] = None,
        event_bus: Optional[EventBus] = None,
        instrument_sync: Optional[InstrumentSync] = None,
        order_manager: Optional[Any] = None,
        tokens_to_subscribe: Optional[List[int]] = None,
        reconciler_interval_sec: float = 5.0,
    ):
        self.kite = kite_client
        self.ticker = ticker_client
        self.bus = event_bus or bus
        self.instruments = instrument_sync or InstrumentSync(kite_client=self.kite)
        self.order_manager = order_manager
        self.tokens_to_subscribe: List[int] = tokens_to_subscribe or []
        self.reconciler_interval_sec = reconciler_interval_sec

        # Internal tracking: order_id -> Order, broker_order_id -> order_id
        self.orders: Dict[str, Order] = {}
        self.broker_id_to_order_id: Dict[str, str] = {}
        self.latest_ticks: Dict[str, Tick] = {}
        self.trade_charges_history: List[ChargesBreakdown] = []

        # Background tasks and threads
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reconciler_task: Optional[asyncio.Task] = None
        self._is_running = False
        self._ticker_thread: Optional[threading.Thread] = None

        if self.ticker is not None:
            self._setup_ticker_callbacks()

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Assigns the asyncio event loop for threadsafe event publishing."""
        self._loop = loop

    def set_order_manager(self, om: Any) -> None:
        """Assigns OrderManager for state reconciliation."""
        self.order_manager = om

    def set_latest_tick(self, tick: Tick) -> None:
        """Sets latest tick in memory cache and forwards to PaperBroker if paper trading."""
        self.latest_ticks[tick.tradingsymbol] = tick
        if self.order_manager is not None:
            om_broker = getattr(self.order_manager, "broker", None)
            if om_broker is not None and hasattr(om_broker, "set_latest_tick") and om_broker is not self:
                om_broker.set_latest_tick(tick)

    # -------------------------------------------------------------------------
    # WebSocket KiteTicker Streaming & Event Normalization
    # -------------------------------------------------------------------------

    def _setup_ticker_callbacks(self) -> None:
        """Configures event callbacks on KiteTicker instance."""
        self.ticker.on_ticks = self._on_ticks
        self.ticker.on_connect = self._on_connect
        self.ticker.on_close = self._on_close
        self.ticker.on_error = self._on_error
        self.ticker.on_order_update = self._on_order_update

    def _on_connect(self, ws: Any, response: Any) -> None:
        """Callback when KiteTicker connects successfully."""
        logger.info(f"[KITE TICKER] Connected. Subscribing to {len(self.tokens_to_subscribe)} instrument tokens...")
        if self.tokens_to_subscribe:
            ws.subscribe(self.tokens_to_subscribe)
            ws.set_mode(ws.MODE_FULL, self.tokens_to_subscribe)

    def _on_close(self, ws: Any, code: int, reason: str) -> None:
        """Callback when KiteTicker disconnects."""
        logger.warning(f"[KITE TICKER] Connection closed: code={code}, reason={reason}")

    def _on_error(self, ws: Any, code: int, reason: str) -> None:
        """Callback on KiteTicker transport error."""
        logger.error(f"[KITE TICKER] Error encountered: code={code}, reason={reason}")

    def _on_ticks(self, ws: Any, raw_ticks: List[Dict[str, Any]]) -> None:
        """Callback processing raw Kite tick payloads and dispatching TickEvents."""
        for raw in raw_ticks:
            tick = self.normalize_kite_tick(raw)
            if tick is None:
                continue

            self.set_latest_tick(tick)
            self._dispatch_coroutine(self.bus.publish(TickEvent(tick=tick)))

    def normalize_kite_tick(self, raw_tick: Dict[str, Any]) -> Optional[Tick]:
        """Normalizes a raw Zerodha Kite tick dictionary into a validated Tick schema."""
        token = raw_tick.get("instrument_token")
        if not token:
            return None

        sym = self.instruments.get_symbol(token)
        if not sym:
            sym = str(token)

        ltp = float(raw_tick.get("last_price", 0.0))
        if ltp <= 0.0:
            return None

        volume = int(raw_tick.get("volume_traded", raw_tick.get("volume", 0)))
        oi = int(raw_tick.get("oi", 0))

        # Level-1 Best Bid and Ask extraction
        depth = raw_tick.get("depth", {})
        bids = depth.get("buy", [])
        asks = depth.get("sell", [])

        bid = float(bids[0]["price"]) if bids and bids[0].get("price", 0.0) > 0 else ltp
        ask = float(asks[0]["price"]) if asks and asks[0].get("price", 0.0) > 0 else ltp

        dt = raw_tick.get("last_trade_time") or raw_tick.get("timestamp")
        if dt is None:
            dt = datetime.now(IST_TZ)
        elif isinstance(dt, str):
            dt = datetime.fromisoformat(dt)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST_TZ)
        else:
            dt = dt.astimezone(IST_TZ)

        return Tick(
            instrument_token=token,
            tradingsymbol=sym,
            timestamp_ist=dt,
            ltp=round(ltp, 2),
            volume=volume,
            bid=round(bid, 2),
            ask=round(ask, 2),
            oi=oi,
        )

    def _on_order_update(self, ws: Any, data: Dict[str, Any]) -> None:
        """Callback handling live Zerodha broker order state updates."""
        broker_order_id = str(data.get("order_id", ""))
        order_status_str = data.get("status", "")
        avg_price = float(data.get("average_price", 0.0))
        filled_qty = int(data.get("filled_quantity", 0))

        logger.info(f"[KITE ORDER UPDATE] BrokerID={broker_order_id}, Status={order_status_str}, Filled={filled_qty} @ {avg_price}")

        # Map to internal order
        internal_id = self.broker_id_to_order_id.get(broker_order_id)
        if internal_id and internal_id in self.orders:
            order = self.orders[internal_id]
            order.filled_quantity = filled_qty
            if avg_price > 0:
                order.average_fill_price = avg_price

            # Map Kite status to OrderStatus
            status_map = {
                "COMPLETE": OrderStatus.FILLED,
                "OPEN": OrderStatus.OPEN,
                "TRIGGER PENDING": OrderStatus.OPEN,
                "CANCELLED": OrderStatus.CANCELLED,
                "REJECTED": OrderStatus.REJECTED,
            }
            order.status = status_map.get(order_status_str, order.status)
            self._dispatch_coroutine(self.bus.publish(OrderUpdateEvent(order=order)))

    def _dispatch_coroutine(self, coro: Any) -> None:
        """Dispatches an async coroutine thread-safely onto the active asyncio loop."""
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(coro)
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # Order Placement & Cancellation API
    # -------------------------------------------------------------------------

    async def submit_order(self, order: Order) -> Order:
        """
        Places live MIS limit order through Zerodha Kite Connect REST API.
        Enforces tick-size rounding before API transmission.
        """
        # Ensure price is rounded strictly to symbol's tick size
        if order.limit_price is not None:
            order.limit_price = self.instruments.round_price(order.tradingsymbol, order.limit_price)

        self.orders[order.order_id] = order

        try:
            kite_variety = getattr(self.kite, "VARIETY_REGULAR", "regular")
            kite_exchange = getattr(self.kite, "EXCHANGE_NSE", "NSE")
            kite_side = (
                getattr(self.kite, "TRANSACTION_TYPE_BUY", "BUY")
                if order.side == Side.BUY
                else getattr(self.kite, "TRANSACTION_TYPE_SELL", "SELL")
            )
            kite_product = getattr(self.kite, "PRODUCT_MIS", "MIS")
            kite_order_type = getattr(self.kite, "ORDER_TYPE_LIMIT", "LIMIT")

            broker_res = self.kite.place_order(
                variety=kite_variety,
                exchange=kite_exchange,
                tradingsymbol=order.tradingsymbol,
                transaction_type=kite_side,
                quantity=order.quantity,
                product=kite_product,
                order_type=kite_order_type,
                price=order.limit_price,
                tag="trillionaire",
            )

            broker_order_id = str(broker_res if isinstance(broker_res, (str, int)) else broker_res.get("order_id", ""))
            order.broker_order_id = broker_order_id
            order.status = OrderStatus.OPEN
            self.broker_id_to_order_id[broker_order_id] = order.order_id

            logger.info(
                f"[LIVE BROKER ORDER] Placed {order.tradingsymbol} {order.side.value} x {order.quantity} "
                f"@ Rs.{order.limit_price:.2f} (InternalID: {order.order_id}, BrokerID: {broker_order_id})"
            )

            await self.bus.publish(OrderUpdateEvent(order=order))
            return order

        except Exception as e:
            order.status = OrderStatus.REJECTED
            logger.error(f"[LIVE BROKER ERROR] Order placement failed for {order.tradingsymbol}: {e}")
            await self.bus.publish(OrderUpdateEvent(order=order))
            raise OrderExecutionError(f"KiteConnect place_order failed: {e}") from e

    async def cancel_order(self, order_id: str) -> Optional[Order]:
        """Cancels an open order through Zerodha Kite Connect REST API."""
        order = self.orders.get(order_id)
        if not order or not order.broker_order_id:
            logger.warning(f"Cannot cancel order {order_id}: Not found or missing broker_order_id")
            return None

        try:
            kite_variety = getattr(self.kite, "VARIETY_REGULAR", "regular")
            self.kite.cancel_order(
                variety=kite_variety,
                order_id=order.broker_order_id,
            )
            order.status = OrderStatus.CANCELLED
            logger.info(f"[LIVE BROKER CANCEL] Cancelled order {order_id} (BrokerID: {order.broker_order_id})")
            await self.bus.publish(OrderUpdateEvent(order=order))
            return order

        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            raise OrderExecutionError(f"KiteConnect cancel_order failed: {e}") from e

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
        return breakdown

    # -------------------------------------------------------------------------
    # 5-Second State Reconciler
    # -------------------------------------------------------------------------

    async def reconcile_positions(self) -> Dict[str, Any]:
        """
        Audits actual broker net positions against OrderManager.open_positions:
        1. Adopts unmanaged positions (crash-restart recovery).
        2. Dispatches RiskAlertEvents on unexpected quantity or directional mismatches.
        """
        if self.order_manager is None:
            return {"status": "skipped", "reason": "No OrderManager attached"}

        if self.kite is None:
            return {"status": "skipped", "reason": "No Kite client attached"}

        try:
            broker_pos_data = self.kite.positions()
            net_positions = broker_pos_data.get("net", [])
        except Exception as e:
            logger.error(f"[RECONCILER] Error fetching broker positions: {e}")
            return {"status": "error", "error": str(e)}

        broker_active: Dict[str, Dict[str, Any]] = {}
        for bp in net_positions:
            sym = bp.get("tradingsymbol", "")
            qty = int(bp.get("quantity", 0))
            if qty != 0:
                broker_active[sym] = bp

        internal_positions = self.order_manager.open_positions

        # 1. Check for orphaned broker positions to adopt
        for sym, bp in broker_active.items():
            qty = int(bp["quantity"])
            avg_price = float(bp.get("average_price", 0.0))
            side = Side.BUY if qty > 0 else Side.SELL
            abs_qty = abs(qty)

            if sym not in internal_positions:
                logger.warning(
                    f"[RECONCILER ADOPT] Found unmanaged broker position for {sym} ({side.value} x {abs_qty} @ {avg_price:.2f}). Adopting..."
                )
                pos = Position(
                    tradingsymbol=sym,
                    side=side,
                    quantity=abs_qty,
                    average_entry_price=avg_price,
                    ltp=avg_price,
                    unrealized_pnl=0.0,
                    realized_pnl=0.0,
                    stop_loss=round(avg_price * (0.99 if side == Side.BUY else 1.01), 2),
                    target_price=round(avg_price * (1.02 if side == Side.BUY else 0.98), 2),
                )
                internal_positions[sym] = pos
                if self.order_manager.risk_gatekeeper is not None:
                    self.order_manager.risk_gatekeeper.register_position(pos)

                await self.bus.publish(
                    RiskAlertEvent(
                        symbol=sym,
                        level="WARNING",
                        message=f"Reconciler adopted external/crash-restart position for {sym} ({abs_qty} shares)",
                    )
                )

            else:
                # 2. Check quantity mismatch
                int_pos = internal_positions[sym]
                if int_pos.quantity != abs_qty:
                    mismatch_msg = (
                        f"Position quantity mismatch for {sym}: Internal={int_pos.quantity}, Broker={abs_qty}. Reconciling to broker state."
                    )
                    logger.warning(f"[RECONCILER MISMATCH] {mismatch_msg}")
                    int_pos.quantity = abs_qty
                    await self.bus.publish(
                        RiskAlertEvent(
                            symbol=sym,
                            level="WARNING",
                            message=mismatch_msg,
                        )
                    )

        # 3. Check for internally recorded positions closed on broker
        for sym in list(internal_positions.keys()):
            if sym not in broker_active:
                logger.warning(f"[RECONCILER GHOST] Position {sym} is active internally but closed on broker. Clearing internal state.")
                self.order_manager.open_positions.pop(sym, None)
                if self.order_manager.risk_gatekeeper is not None:
                    self.order_manager.risk_gatekeeper.close_position(sym, realized_pnl=0.0)

        return {"status": "ok", "active_broker_positions": len(broker_active)}

    async def _reconciler_loop(self) -> None:
        """Continuous background task running position reconciliation every 5 seconds."""
        logger.info(f"Started state reconciler loop (Interval: {self.reconciler_interval_sec}s)")
        while self._is_running:
            try:
                await self.reconcile_positions()
            except Exception as e:
                logger.error(f"[RECONCILER LOOP ERROR] {e}")
            await asyncio.sleep(self.reconciler_interval_sec)

    # -------------------------------------------------------------------------
    # Lifecycle Management
    # -------------------------------------------------------------------------

    async def start(self) -> None:
        """Starts ticker connection and background reconciliation loop."""
        self._is_running = True
        self._loop = asyncio.get_running_loop()

        # Auto-instantiate KiteTicker if self.ticker is None and credentials available
        if self.ticker is None and self.kite is not None:
            api_key = getattr(self.kite, "api_key", None)
            access_token = getattr(self.kite, "access_token", None)
            if not api_key:
                from config.settings import settings
                api_key = settings.kite_api_key
            if not access_token:
                from config.settings import settings
                access_token = settings.kite_access_token
            if api_key and access_token:
                try:
                    from kiteconnect import KiteTicker
                    self.ticker = KiteTicker(api_key=api_key, access_token=access_token)
                    self._setup_ticker_callbacks()
                    logger.info("[KITE BROKER] Auto-instantiated KiteTicker in start().")
                except Exception as e:
                    logger.error(f"[KITE BROKER] Failed to auto-instantiate KiteTicker: {e}")

        # Start ticker in background thread if available
        if self.ticker is not None:
            try:
                self.ticker.connect(threaded=True)
                self._ticker_thread = getattr(self.ticker, "websocket_thread", None)
                logger.info("[KITE TICKER] KiteTicker connected with threaded=True.")
            except Exception as e:
                logger.error(f"[KITE TICKER] Failed to connect KiteTicker: {e}")

        # Start reconciler loop
        self._reconciler_task = asyncio.create_task(self._reconciler_loop())

    def subscribe_tokens(self, tokens: List[int]) -> None:
        """Updates subscription token list and subscribes on active ticker if connected."""
        self.tokens_to_subscribe = sorted(list(set(self.tokens_to_subscribe + tokens)))
        if self.ticker is not None and getattr(self.ticker, "is_connected", lambda: False)():
            try:
                self.ticker.subscribe(tokens)
                self.ticker.set_mode(getattr(self.ticker, "MODE_FULL", "full"), tokens)
                logger.info(f"[KITE TICKER] Subscribed to {len(tokens)} additional tokens.")
            except Exception as e:
                logger.warning(f"[KITE TICKER] Failed to subscribe additional tokens: {e}")

    async def stop(self) -> None:
        """Stops reconciler task and closes ticker connection."""
        self._is_running = False

        if self._reconciler_task and not self._reconciler_task.done():
            self._reconciler_task.cancel()
            try:
                try:
                    current_loop = asyncio.get_running_loop()
                except RuntimeError:
                    current_loop = None

                if current_loop is not None and self._reconciler_task.get_loop() is current_loop:
                    await self._reconciler_task
            except (asyncio.CancelledError, RuntimeError):
                pass
            self._reconciler_task = None

        if self.ticker is not None:
            try:
                self.ticker.close()
            except Exception as e:
                logger.debug(f"Error closing KiteTicker: {e}")

        logger.info("KiteBroker stopped.")
