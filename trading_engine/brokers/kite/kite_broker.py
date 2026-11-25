"""
Trillionaire Kite Broker - Live KiteTicker Stream & KiteConnect Order Engine
Provides live production broker integration with Zerodha Kite Connect:
1. WebSocket Market Data (KiteTicker): Single WS connection across all 100+ watchlist tokens
   with exponential backoff reconnect and explicit symbol-to-token anti-bleed verification.
2. Live Order Execution (KiteConnect): Supports MIS LIMIT & SL_LIMIT orders with tick-size rounding,
   rate-limited REST calls (<=8 req/sec token bucket), and idempotent unique client order tags.
3. Structured Kite Exception Guards: Catches TokenException, OrderException, MarginException,
   InputException, NetworkException, and GeneralException. Immediately halts polling on TokenException.
4. 5-Second State Reconciler & Live OCO Orderbook Sync.
"""

import asyncio
from datetime import datetime
import json
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional
import uuid
from loguru import logger

from research_lab.profiler.indian_charges import ChargesBreakdown, calculate_mis_net_pnl
from shared.constants import IST_TZ
from shared.enums import OrderStatus, OrderType, Side, TradingMode
from shared.exceptions import (
    BrokerConnectionError,
    KiteTokenExpiredError,
    OrderExecutionError,
)
from shared.schemas import Order, Position, Tick
from trading_engine.brokers.kite.instruments import InstrumentSync
from trading_engine.core.event_bus import (
    EventBus,
    OrderUpdateEvent,
    RiskAlertEvent,
    TickEvent,
    bus,
)


class TokenBucketRateLimiter:
    """Threadsafe token-bucket limiter capping KiteConnect REST calls at <= max_per_sec (default 8/sec)."""

    def __init__(self, rate_per_sec: float = 8.0, capacity: float = 8.0):
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            wait_time = 0.0
            with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                if elapsed > 0:
                    self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                    self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait_time = (1.0 - self.tokens) / self.rate
            time.sleep(min(wait_time, 0.25))


def _classify_kite_exception(exc: Exception) -> str:
    """Classifies KiteConnect exception by exact class name or message pattern."""
    cls_name = type(exc).__name__
    msg = str(exc)
    msg_lower = msg.lower()
    if "TokenException" in cls_name or "incorrect api_key or access_token" in msg_lower or "403" in msg_lower or "token" in cls_name.lower():
        return "TokenException"
    if "MarginException" in cls_name or "margin" in msg_lower or "insufficient funds" in msg_lower:
        return "MarginException"
    if "InputException" in cls_name or "invalid" in msg_lower:
        return "InputException"
    if "OrderException" in cls_name:
        return "OrderException"
    if "NetworkException" in cls_name or "429" in msg or "502" in msg or "504" in msg or "timeout" in msg_lower or "connection" in msg_lower:
        return "NetworkException"
    return "GeneralException" if "GeneralException" in cls_name else cls_name


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
        trading_mode: TradingMode = TradingMode.LIVE,
    ):
        self.kite = kite_client
        self.ticker = ticker_client
        self.bus = event_bus or bus
        self.instruments = instrument_sync or InstrumentSync(kite_client=self.kite)
        self.order_manager = order_manager
        self.tokens_to_subscribe: List[int] = tokens_to_subscribe or []
        self.reconciler_interval_sec = reconciler_interval_sec
        self.trading_mode = trading_mode

        # Rate limiter (max 8 req/sec)
        self._rest_limiter = TokenBucketRateLimiter(rate_per_sec=8.0, capacity=8.0)

        # Internal tracking: order_id -> Order, broker_order_id -> order_id
        self.orders: Dict[str, Order] = {}
        self.broker_id_to_order_id: Dict[str, str] = {}
        self.latest_ticks: Dict[str, Tick] = {}
        self.trade_charges_history: List[ChargesBreakdown] = []

        # Connection & token state flags
        self.token_expired: bool = False
        self.token_error_message: Optional[str] = None
        self.ticker_connected: bool = False
        self.ticker_reconnecting: bool = False

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
            if hasattr(self.order_manager, "_latest_ticks"):
                self.order_manager._latest_ticks[tick.tradingsymbol] = tick
            om_broker = getattr(self.order_manager, "broker", None)
            if om_broker is not None and hasattr(om_broker, "set_latest_tick") and om_broker is not self:
                om_broker.set_latest_tick(tick)

    # -------------------------------------------------------------------------
    # Structured Rate-Limited REST Execution Wrapper
    # -------------------------------------------------------------------------

    def _call_kite_rest(
        self,
        fn: Callable[..., Any],
        *args: Any,
        max_network_retries: int = 2,
        **kwargs: Any,
    ) -> Any:
        """
        Executes a synchronous KiteConnect REST API call guarded by:
        - Token-bucket rate limiter (max 8 req/sec)
        - Jittered exponential backoff on NetworkException / 429 / 502 / 504
        - Immediate halt on TokenException
        """
        last_exc: Optional[Exception] = None
        for attempt in range(max_network_retries + 1):
            self._rest_limiter.acquire()
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                kind = _classify_kite_exception(exc)

                if kind == "TokenException":
                    self._handle_token_expiry(str(exc))
                    raise KiteTokenExpiredError(f"Kite TokenException: {exc}") from exc

                if kind == "NetworkException" and attempt < max_network_retries:
                    backoff = (0.35 * (2 ** attempt)) + random.uniform(0.05, 0.25)
                    logger.warning(
                        f"[KITE REST RETRY] Network/429 error on attempt {attempt + 1}: {exc}. Backing off {backoff:.2f}s"
                    )
                    time.sleep(backoff)
                    continue

                raise

        if last_exc is not None:
            raise last_exc

    def _handle_token_expiry(self, detail: str) -> None:
        """Immediately halts REST polling on TokenException and alerts dashboard."""
        already_expired = self.token_expired
        self.token_expired = True
        self.token_error_message = detail
        if not already_expired:
            logger.critical(f"[KITE TOKEN EXPIRED] Halting REST polling loops. Re-login required: {detail}")
            self._dispatch_coroutine(
                self.bus.publish(
                    RiskAlertEvent(
                        symbol="KITE_AUTH",
                        level="CRITICAL",
                        message="Zerodha Kite session expired (TokenException/403). Re-login via Settings & Keys.",
                    )
                )
            )

    def clear_token_expiry(self) -> None:
        """Clears token_expired flag after fresh OAuth / token push."""
        self.token_expired = False
        self.token_error_message = None

    # -------------------------------------------------------------------------
    # WebSocket KiteTicker Streaming & Event Normalization
    # -------------------------------------------------------------------------

    def _setup_ticker_callbacks(self) -> None:
        """Configures event callbacks on KiteTicker instance (single WebSocket connection)."""
        self.ticker.on_ticks = self._on_ticks
        self.ticker.on_connect = self._on_connect
        self.ticker.on_close = self._on_close
        self.ticker.on_error = self._on_error
        self.ticker.on_order_update = self._on_order_update
        if hasattr(self.ticker, "on_reconnect"):
            self.ticker.on_reconnect = self._on_reconnect
        if hasattr(self.ticker, "on_noreconnect"):
            self.ticker.on_noreconnect = self._on_noreconnect

    def _on_connect(self, ws: Any, response: Any) -> None:
        """Callback when KiteTicker connects successfully."""
        self.ticker_connected = True
        self.ticker_reconnecting = False
        logger.info(f"[KITE TICKER] Connected. Subscribing to {len(self.tokens_to_subscribe)} instrument tokens...")
        if self.tokens_to_subscribe:
            ws.subscribe(self.tokens_to_subscribe)
            ws.set_mode(ws.MODE_FULL, self.tokens_to_subscribe)

    def _on_close(self, ws: Any, code: int, reason: str) -> None:
        """Callback when KiteTicker disconnects."""
        self.ticker_connected = False
        if self._is_running:
            self.ticker_reconnecting = True
        logger.warning(f"[KITE TICKER] Connection closed: code={code}, reason={reason}")

    def _on_error(self, ws: Any, code: int, reason: str) -> None:
        """Callback on KiteTicker transport error."""
        logger.error(f"[KITE TICKER] Error encountered: code={code}, reason={reason}")
        if "403" in str(code) or "403" in str(reason) or "token" in str(reason).lower():
            self._handle_token_expiry(f"WebSocket error {code}: {reason}")
        elif self._is_running:
            self.ticker_reconnecting = True

    def _on_reconnect(self, ws: Any, attempts_count: int) -> None:
        """Callback when KiteTicker attempts exponential-backoff reconnect."""
        self.ticker_connected = False
        self.ticker_reconnecting = True
        logger.warning(f"[KITE TICKER] Reconnecting (attempt #{attempts_count})...")

    def _on_noreconnect(self, ws: Any) -> None:
        """Callback when KiteTicker exhausts reconnect retries."""
        self.ticker_connected = False
        self.ticker_reconnecting = False
        logger.error("[KITE TICKER] Maximum reconnect attempts exhausted.")
        self._dispatch_coroutine(
            self.bus.publish(
                RiskAlertEvent(
                    symbol="KITE_WS",
                    level="CRITICAL",
                    message="KiteTicker WebSocket disconnected and max reconnect retries exhausted.",
                )
            )
        )

    def _on_ticks(self, ws: Any, raw_ticks: List[Dict[str, Any]]) -> None:
        """Callback processing raw Kite tick payloads and dispatching TickEvents."""
        for raw in raw_ticks:
            tick = self.normalize_kite_tick(raw)
            if tick is None:
                continue

            self.set_latest_tick(tick)
            self._dispatch_coroutine(self.bus.publish(TickEvent(tick=tick)))

    def normalize_kite_tick(self, raw_tick: Dict[str, Any]) -> Optional[Tick]:
        """
        Normalizes a raw Zerodha Kite tick dictionary into a validated Tick schema.
        Strictly enforces 1-to-1 instrument_token -> tradingsymbol lookup to prevent tick bleed.
        """
        token = raw_tick.get("instrument_token")
        if not token:
            return None
        token_int = int(token)

        sym = self.instruments.get_symbol(token_int)
        if not sym:
            logger.debug(f"[TICK AUDIT] Dropping unmapped instrument_token={token_int}")
            return None

        expected_tok = self.instruments.get_token(sym)
        if expected_tok is not None and expected_tok != token_int:
            logger.error(
                f"[TICK BLEED GUARD] Dropping mismatched tick: token={token_int} mapped to {sym}, "
                f"but canonical token for {sym} is {expected_tok}"
            )
            return None

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
            instrument_token=token_int,
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
        status_message = data.get("status_message") or ""

        logger.info(
            f"[KITE ORDER UPDATE] BrokerID={broker_order_id}, Status={order_status_str}, "
            f"Filled={filled_qty} @ {avg_price}"
        )

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
                "TRIGGER PENDING": OrderStatus.TRIGGER_PENDING,
                "CANCELLED": OrderStatus.CANCELLED,
                "REJECTED": OrderStatus.REJECTED,
            }
            order.status = status_map.get(order_status_str, order.status)
            if order.status == OrderStatus.REJECTED and status_message:
                order.rejection_reason = status_message
            self._dispatch_coroutine(self.bus.publish(OrderUpdateEvent(order=order)))

    def _dispatch_coroutine(self, coro: Any) -> None:
        """Dispatches an async coroutine thread-safely onto the active asyncio loop."""
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)
            return
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                asyncio.create_task(coro)
                return
        except Exception:
            pass
        # Close unscheduled coroutine cleanly so tests without an active loop emit zero warnings
        try:
            coro.close()
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Idempotent Order Placement & Cancellation API
    # -------------------------------------------------------------------------

    def _find_existing_order_by_tag(self, client_tag: str) -> Optional[Dict[str, Any]]:
        """Queries kite.orders() to detect if an idempotent order tag was already placed."""
        if not client_tag or self.kite is None:
            return None
        try:
            all_orders = self._call_kite_rest(self.kite.orders, max_network_retries=1)
            for bo in all_orders or []:
                if str(bo.get("tag", "")) == client_tag:
                    return bo
        except Exception as e:
            logger.debug(f"[KITE IDEMPOTENCY] Orderbook tag lookup skipped ({client_tag}): {e}")
        return None

    async def submit_order(self, order: Order) -> Order:
        """
        Places live MIS LIMIT or SL_LIMIT order through Zerodha Kite Connect REST API.
        Enforces tick-size rounding, rate limiting, structured exception handling, and idempotency.
        """
        if self.token_expired:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "Kite daily token expired — re-login required before placing live orders"
            await self.bus.publish(OrderUpdateEvent(order=order))
            raise KiteTokenExpiredError(order.rejection_reason)

        if order.limit_price is not None:
            order.limit_price = self.instruments.round_price(order.tradingsymbol, order.limit_price)
        if order.trigger_price is not None:
            order.trigger_price = self.instruments.round_price(order.tradingsymbol, order.trigger_price)

        client_tag = order.client_order_tag or f"tril_{order.order_id[:12]}"
        order.client_order_tag = client_tag
        self.orders[order.order_id] = order

        kite_variety = getattr(self.kite, "VARIETY_REGULAR", "regular")
        kite_exchange = getattr(self.kite, "EXCHANGE_NSE", "NSE")
        kite_side = (
            getattr(self.kite, "TRANSACTION_TYPE_BUY", "BUY")
            if order.side == Side.BUY
            else getattr(self.kite, "TRANSACTION_TYPE_SELL", "SELL")
        )
        kite_product = getattr(self.kite, "PRODUCT_MIS", "MIS")

        if order.order_type == OrderType.SL_LIMIT:
            kite_order_type = getattr(self.kite, "ORDER_TYPE_SL", "SL")
        else:
            kite_order_type = getattr(self.kite, "ORDER_TYPE_LIMIT", "LIMIT")

        place_kwargs: Dict[str, Any] = {
            "variety": kite_variety,
            "exchange": kite_exchange,
            "tradingsymbol": order.tradingsymbol,
            "transaction_type": kite_side,
            "quantity": order.quantity,
            "product": kite_product,
            "order_type": kite_order_type,
            "price": order.limit_price,
            "tag": client_tag,
        }
        if order.order_type == OrderType.SL_LIMIT and order.trigger_price is not None:
            place_kwargs["trigger_price"] = order.trigger_price

        try:
            broker_res = None
            try:
                broker_res = self._call_kite_rest(
                    self.kite.place_order,
                    max_network_retries=0,  # Idempotent check handles retry safely
                    **place_kwargs,
                )
            except Exception as first_exc:
                kind = _classify_kite_exception(first_exc)
                if kind == "NetworkException":
                    # Idempotency guard: query kite.orders() by client_tag before retrying!
                    existing = self._find_existing_order_by_tag(client_tag)
                    if existing is not None:
                        broker_res = str(existing.get("order_id", ""))
                        logger.warning(
                            f"[KITE IDEMPOTENCY] Found already-accepted order for tag={client_tag} "
                            f"(BrokerID={broker_res}); avoiding duplicate order!"
                        )
                    else:
                        broker_res = self._call_kite_rest(
                            self.kite.place_order,
                            max_network_retries=1,
                            **place_kwargs,
                        )
                else:
                    raise

            broker_order_id = str(broker_res if isinstance(broker_res, (str, int)) else broker_res.get("order_id", ""))
            order.broker_order_id = broker_order_id
            order.status = (
                OrderStatus.TRIGGER_PENDING if order.order_type == OrderType.SL_LIMIT else OrderStatus.OPEN
            )
            self.broker_id_to_order_id[broker_order_id] = order.order_id

            logger.info(
                f"[LIVE BROKER ORDER] Placed {order.tradingsymbol} {order.side.value} x {order.quantity} "
                f"@ Rs.{(order.limit_price or 0.0):.2f} (Type={order.order_type.value}, BrokerID={broker_order_id})"
            )

            await self.bus.publish(OrderUpdateEvent(order=order))
            return order

        except Exception as e:
            kind = _classify_kite_exception(e)
            err_msg = f"[{kind}] {str(e)}"
            order.status = OrderStatus.REJECTED
            order.rejection_reason = err_msg
            logger.error(f"[LIVE BROKER ERROR] Order placement failed for {order.tradingsymbol}: {err_msg}")
            await self.bus.publish(OrderUpdateEvent(order=order))
            await self.bus.publish(
                RiskAlertEvent(
                    symbol=order.tradingsymbol,
                    level="CRITICAL",
                    message=f"Broker order rejected for {order.tradingsymbol}: {err_msg}",
                )
            )
            raise OrderExecutionError(f"KiteConnect place_order failed: {err_msg}") from e

    async def cancel_order(self, order_id: str) -> Optional[Order]:
        """Cancels an open order through Zerodha Kite Connect REST API."""
        order = self.orders.get(order_id)
        if not order or not order.broker_order_id:
            logger.warning(f"Cannot cancel order {order_id}: Not found or missing broker_order_id")
            return None

        try:
            kite_variety = getattr(self.kite, "VARIETY_REGULAR", "regular")
            self._call_kite_rest(
                self.kite.cancel_order,
                variety=kite_variety,
                order_id=order.broker_order_id,
            )
            order.status = OrderStatus.CANCELLED
            logger.info(f"[LIVE BROKER CANCEL] Cancelled order {order_id} (BrokerID: {order.broker_order_id})")
            await self.bus.publish(OrderUpdateEvent(order=order))
            return order

        except Exception as e:
            kind = _classify_kite_exception(e)
            logger.error(f"Failed to cancel order {order_id} ({kind}): {e}")
            raise OrderExecutionError(f"KiteConnect cancel_order failed [{kind}]: {e}") from e

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
    # 5-Second State Reconciler & Live Orderbook Sync
    # -------------------------------------------------------------------------

    async def _sync_broker_orders(self) -> None:
        """Polls kite.orders() safely to catch any SL/Target fills missed over WS."""
        if self.kite is None or not hasattr(self.kite, "orders") or self.token_expired:
            return
        if not self.broker_id_to_order_id:
            return
        try:
            broker_orders = self._call_kite_rest(self.kite.orders, max_network_retries=0)
            status_map = {
                "COMPLETE": OrderStatus.FILLED,
                "OPEN": OrderStatus.OPEN,
                "TRIGGER PENDING": OrderStatus.TRIGGER_PENDING,
                "CANCELLED": OrderStatus.CANCELLED,
                "REJECTED": OrderStatus.REJECTED,
            }
            for bo in broker_orders or []:
                boid = str(bo.get("order_id", ""))
                internal_id = self.broker_id_to_order_id.get(boid)
                if not internal_id or internal_id not in self.orders:
                    continue
                ord_obj = self.orders[internal_id]
                new_status = status_map.get(bo.get("status", ""), ord_obj.status)
                if new_status != ord_obj.status:
                    ord_obj.status = new_status
                    ord_obj.filled_quantity = int(bo.get("filled_quantity", ord_obj.filled_quantity))
                    avg_px = float(bo.get("average_price", 0.0))
                    if avg_px > 0:
                        ord_obj.average_fill_price = avg_px
                    if new_status == OrderStatus.REJECTED and bo.get("status_message"):
                        ord_obj.rejection_reason = bo["status_message"]
                    await self.bus.publish(OrderUpdateEvent(order=ord_obj))
        except KiteTokenExpiredError:
            pass
        except Exception as e:
            logger.debug(f"[ORDERBOOK SYNC] Skipped: {e}")

    async def reconcile_positions(self) -> Dict[str, Any]:
        """
        Audits actual broker net positions against OrderManager.open_positions:
        1. Syncs broker orders to catch OCO exit leg fills.
        2. Adopts unmanaged positions (crash-restart recovery).
        3. Dispatches RiskAlertEvents on unexpected quantity or directional mismatches.
        """
        if self.order_manager is None:
            return {"status": "skipped", "reason": "No OrderManager attached"}

        mode_val = getattr(self.trading_mode, "value", str(self.trading_mode))
        if mode_val != TradingMode.LIVE.value:
            # Never reconcile live Zerodha positions during PAPER / REPLAY mode (prevents ghost-closing simulated trades)
            return {
                "status": "skipped",
                "mode": mode_val,
                "reason": f"Live Kite position reconciliation disabled in {mode_val} mode",
                "open_paper_positions": len(self.order_manager.open_positions),
            }

        if self.kite is None:
            return {"status": "skipped", "reason": "No Kite client attached"}

        if self.token_expired:
            return {"status": "halted", "reason": "Token expired — re-login required"}

        # Quiet Pre-Login Reconciler: verify Redis key holds today's IST date before querying Kite API
        try:
            import redis
            from config.settings import settings
            from shared.constants import KEY_KITE_TOKEN
            r = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )
            raw = r.get(KEY_KITE_TOKEN)
            today_iso = datetime.now(IST_TZ).date().isoformat()
            if raw:
                tok_data = json.loads(raw)
                if tok_data.get("date") != today_iso:
                    logger.debug(f"[RECONCILER] Token date {tok_data.get('date')} != today {today_iso}. Skipping reconciliation.")
                    return {"status": "skipped", "reason": "Token not authorized for today"}
            else:
                if type(self.kite).__name__ == "KiteConnect":
                    logger.debug(f"[RECONCILER] No token in Redis key {KEY_KITE_TOKEN} for today. Skipping reconciliation.")
                    return {"status": "skipped", "reason": "No token pushed for today"}
        except Exception:
            pass

        # Sync orderbook first to process any live SL/Target fills cleanly
        await self._sync_broker_orders()

        try:
            broker_pos_data = self._call_kite_rest(self.kite.positions, max_network_retries=1)
            net_positions = broker_pos_data.get("net", [])
        except KiteTokenExpiredError:
            return {"status": "skipped", "reason": "Token unauthorized"}
        except Exception as e:
            kind = _classify_kite_exception(e)
            if kind == "TokenException":
                logger.debug(f"[RECONCILER] Kite session not authorized yet: {e}")
                return {"status": "skipped", "reason": "Token unauthorized"}
            logger.error(f"[RECONCILER] Error fetching broker positions ({kind}): {e}")
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
                    entry_time=datetime.now(IST_TZ),
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
        """Continuous background task running position reconciliation every 5 seconds; halts immediately on TokenException."""
        logger.info(f"Started state reconciler loop (Interval: {self.reconciler_interval_sec}s)")
        while self._is_running:
            if self.token_expired:
                logger.warning("[RECONCILER LOOP] Halting reconciler loop due to TokenException.")
                break
            try:
                await self.reconcile_positions()
            except Exception as e:
                logger.error(f"[RECONCILER LOOP ERROR] {e}")
            await asyncio.sleep(self.reconciler_interval_sec)

    # -------------------------------------------------------------------------
    # Lifecycle Management
    # -------------------------------------------------------------------------

    async def start(self) -> None:
        """Starts single KiteTicker connection and background reconciliation loop."""
        self._is_running = True
        self._loop = asyncio.get_running_loop()

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
                    self.ticker = KiteTicker(
                        api_key=api_key,
                        access_token=access_token,
                        reconnect=True,
                        reconnect_max_tries=50,
                        reconnect_max_delay=60,
                    )
                    self._setup_ticker_callbacks()
                    logger.info("[KITE BROKER] Auto-instantiated single-WS KiteTicker with exponential backoff.")
                except Exception as e:
                    logger.error(f"[KITE BROKER] Failed to auto-instantiate KiteTicker: {e}")

        if self.ticker is not None:
            try:
                self.ticker.connect(threaded=True)
                self._ticker_thread = getattr(self.ticker, "websocket_thread", None)
                logger.info("[KITE TICKER] KiteTicker connected with threaded=True.")
            except Exception as e:
                logger.error(f"[KITE TICKER] Failed to connect KiteTicker: {e}")

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
        self.ticker_connected = False
        self.ticker_reconnecting = False

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
