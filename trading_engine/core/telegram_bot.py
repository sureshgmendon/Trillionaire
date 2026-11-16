"""
Trillionaire Trading Engine - Telegram Mobile Alerts & Remote Command Controller
Provides push notifications to the operator's mobile device and allows remote kill-switch overrides:
1. Trade Fill Notifications: Sends formatted HTML alerts upon OrderStatus.FILLED events.
2. Risk Alerts: Instant mobile alerts on daily drawdown circuit breakers and flash volatility.
3. Premarket & MTM Milestones: Daily 08:45 AM briefing and intraday PnL milestones.
4. Remote Operator Controls: Processes incoming commands (/status, /positions, /kill),
   strictly authenticating the sender against TELEGRAM_CHAT_ID.
"""

from datetime import datetime
from typing import Any, Dict, Optional
import httpx
from loguru import logger

from config.settings import settings
from shared.constants import IST_TZ
from shared.enums import OrderStatus
from shared.schemas import Order
from trading_engine.core.event_bus import (
    EventBus,
    OrderUpdateEvent,
    RiskAlertEvent,
    bus,
)


class TelegramNotifier:
    """Sends real-time mobile push notifications and processes remote admin commands."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        engine: Optional[Any] = None,
        event_bus: Optional[EventBus] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        self.bot_token = bot_token if bot_token is not None else settings.telegram_bot_token
        self.chat_id = str(chat_id if chat_id is not None else settings.telegram_chat_id)
        self.engine = engine
        self.bus = event_bus or bus
        self._http_client = http_client

        # Wire EventBus listeners
        self._register_subscribers()

    def set_engine(self, engine: Any) -> None:
        """Attaches TrillionaireEngine for command execution and telemetry queries."""
        self.engine = engine

    def _register_subscribers(self) -> None:
        """Subscribes to relevant engine events on EventBus."""
        self.bus.subscribe(OrderUpdateEvent, self.on_order_update)
        self.bus.subscribe(RiskAlertEvent, self.on_risk_alert)

    # -------------------------------------------------------------------------
    # Outbound Alert Messaging
    # -------------------------------------------------------------------------

    async def send_message(self, text: str) -> bool:
        """
        Sends an HTML-formatted message to the authorized Telegram chat.
        Gracefully bypasses if token or chat_id is unconfigured (e.g. dev/test mode).
        """
        if not self.bot_token or not self.chat_id:
            logger.debug(f"[TELEGRAM BYPASS] Bot token or chat_id not set. Message: {text[:50]}...")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        }

        try:
            if self._http_client is not None:
                resp = await self._http_client.post(url, json=payload, timeout=5.0)
            else:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(url, json=payload)

            if resp.status_code == 200:
                logger.debug(f"[TELEGRAM SENT] Successfully delivered message to chat {self.chat_id}")
                return True
            else:
                logger.error(f"[TELEGRAM ERROR] API returned {resp.status_code}: {resp.text}")
                return False
        except Exception as e:
            logger.error(f"[TELEGRAM NETWORK ERROR] Failed to send message: {e}")
            return False

    async def send_premarket_summary(self, summary: Dict[str, Any]) -> bool:
        """Formats and dispatches 08:45 AM pre-market engine briefing."""
        now_str = datetime.now(IST_TZ).strftime("%d-%b-%Y %H:%M IST")
        msg = (
            f"🚀 <b>Trillionaire Algo Engine - Boot Briefing</b>\n"
            f"⏰ <i>{now_str}</i>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 <b>Trading Mode:</b> {summary.get('trading_mode', 'PAPER')}\n"
            f"🔹 <b>Trade Date:</b> {summary.get('trade_date', 'N/A')}\n"
            f"🔹 <b>Monitored Stocks:</b> {summary.get('monitored_stocks', 0)}\n"
            f"🔹 <b>Sentinel Tags:</b> {summary.get('sentinel_tags', 0)}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 <i>Pre-market gates active. Ready for 09:15 market open.</i>"
        )
        return await self.send_message(msg)

    async def send_trade_fill_alert(self, order: Order) -> bool:
        """Formats and sends instant alert on executed trade fills."""
        side_emoji = "🟢 BUY" if order.side.value == "BUY" else "🔴 SELL"
        avg_price = order.average_fill_price or order.limit_price or 0.0
        msg = (
            f"⚡ <b>Trade Filled ({order.product})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Instrument:</b> {order.tradingsymbol}\n"
            f"<b>Direction:</b> {side_emoji}\n"
            f"<b>Quantity:</b> {order.filled_quantity} shares\n"
            f"<b>Fill Price:</b> Rs.{avg_price:.2f}\n"
            f"<b>Order ID:</b> <code>{order.order_id[:8]}</code>\n"
            f"⏰ <i>{datetime.now(IST_TZ).strftime('%H:%M:%S IST')}</i>"
        )
        return await self.send_message(msg)

    async def send_circuit_breaker_alert(self, event: RiskAlertEvent) -> bool:
        """Formats and sends emergency circuit breaker or drawdown alert."""
        msg = (
            f"🚨 <b>RISK CIRCUIT BREAKER TRIGGERED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Level:</b> {event.level}\n"
            f"<b>Symbol:</b> {event.symbol}\n"
            f"<b>Message:</b> {event.message}\n"
            f"⏰ <i>{event.timestamp_ist.strftime('%H:%M:%S IST')}</i>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <i>Check cockpit dashboard or reply /kill to force flatten.</i>"
        )
        return await self.send_message(msg)

    # -------------------------------------------------------------------------
    # EventBus Callbacks
    # -------------------------------------------------------------------------

    async def on_order_update(self, event: OrderUpdateEvent) -> None:
        """Event listener for OrderUpdateEvent: sends push alert on fills."""
        order = event.order
        if order.status == OrderStatus.FILLED and order.filled_quantity > 0:
            await self.send_trade_fill_alert(order)

    async def on_risk_alert(self, event: RiskAlertEvent) -> None:
        """Event listener for RiskAlertEvent: sends alert on critical/circuit breaker events."""
        if event.level in ("CRITICAL", "CIRCUIT_BREAKER"):
            await self.send_circuit_breaker_alert(event)

    # -------------------------------------------------------------------------
    # Inbound Command Handler (/status, /positions, /kill)
    # -------------------------------------------------------------------------

    async def handle_command(self, command_text: str, sender_chat_id: str) -> str:
        """
        Authenticates sender and executes remote operator commands:
        - /status: Live MTM and engine diagnostic state.
        - /positions: Open intraday positions and mark-to-market.
        - /kill: Emergency portfolio liquidation and entry lockout.
        """
        sender_id = str(sender_chat_id).strip()
        auth_id = self.chat_id.strip()

        # Strict Chat ID authorization check
        if not auth_id or sender_id != auth_id:
            logger.warning(
                f"[TELEGRAM SECURITY] Unauthorized command attempt: sender={sender_id}, expected={auth_id}"
            )
            return "⛔ <b>Unauthorized:</b> Your chat ID is not permitted to issue commands."

        cmd = command_text.strip().split()[0].lower() if command_text.strip() else ""

        if cmd == "/status":
            if self.engine is None:
                return "ℹ️ Engine not attached to TelegramNotifier."
            st = self.engine.get_status()
            mtm = st.get("daily_total_mtm", 0.0)
            realized = st.get("daily_realized_pnl", 0.0)
            kill_active = st.get("kill_switch_active", False)
            positions_count = st.get("open_positions_count", 0)

            status_emoji = "🟢 ACTIVE" if st.get("is_running") else "🔴 HALTED"
            kill_str = "🚨 ACTIVE (LOCKED)" if kill_active else "🟢 NORMAL"

            return (
                f"📊 <b>Engine Status Snapshot</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Status:</b> {status_emoji}\n"
                f"<b>Mode:</b> {st.get('trading_mode')}\n"
                f"<b>Capital Tier:</b> {st.get('capital_tier')}\n"
                f"<b>Total MTM:</b> Rs.{mtm:,.2f}\n"
                f"<b>Realized PnL:</b> Rs.{realized:,.2f}\n"
                f"<b>Open Positions:</b> {positions_count}\n"
                f"<b>Kill Switch:</b> {kill_str}\n"
                f"⏰ <i>{datetime.now(IST_TZ).strftime('%H:%M:%S IST')}</i>"
            )

        elif cmd == "/positions":
            if self.engine is None:
                return "ℹ️ Engine not attached to TelegramNotifier."
            open_positions = self.engine.order_manager.open_positions
            if not open_positions:
                return "ℹ️ No open positions currently active."

            lines = ["📋 <b>Active Open Positions:</b>\n━━━━━━━━━━━━━━━━━━━"]
            for sym, pos in open_positions.items():
                side_tag = "BUY" if pos.side.value == "BUY" else "SELL"
                lines.append(
                    f"• <b>{sym}</b> ({side_tag}): {pos.quantity} @ Rs.{pos.average_entry_price:.2f} "
                    f"| LTP: Rs.{pos.ltp:.2f} | PnL: Rs.{pos.unrealized_pnl:,.2f}"
                )
            return "\n".join(lines)

        elif cmd == "/kill":
            if self.engine is None:
                return "❌ Engine not attached; cannot execute emergency kill switch."

            logger.critical(f"[TELEGRAM OVERRIDE] /kill received from authorized operator {sender_id}!")
            flattened = await self.engine.emergency_flatten("TELEGRAM_KILL_SWITCH")
            return (
                f"🚨 <b>EMERGENCY KILL SWITCH EXECUTED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Liquidated {len(flattened)} open positions.\n"
                f"🔒 Algorithmic session is now LOCKED from new entries.\n"
                f"⏰ <i>{datetime.now(IST_TZ).strftime('%H:%M:%S IST')}</i>"
            )

        else:
            return (
                f"🤖 <b>Trillionaire Bot Command Menu</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"/status - Real-time daily MTM and engine health\n"
                f"/positions - Open positions and live unrealized PnL\n"
                f"/kill - <b>Emergency kill switch: flatten all & lock</b>"
            )

    async def process_webhook_update(self, update: Dict[str, Any]) -> Optional[str]:
        """Parses an incoming Telegram Webhook update dictionary and dispatches reply."""
        message = update.get("message", {})
        text = message.get("text", "")
        sender_chat = message.get("chat", {})
        sender_id = str(sender_chat.get("id", ""))

        if not text or not sender_id:
            return None

        reply_text = await self.handle_command(text, sender_id)
        await self.send_message(reply_text)
        return reply_text
