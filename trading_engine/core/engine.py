"""
Trillionaire Trading Engine - Unified Master Engine Coordinator
Orchestrates the entire real-time quantitative algorithmic trading system:
1. Coordinates EventBus, CandleAggregator, AnomalyDetector, SentinelOrchestrator,
   LiveStrategyRunner, PortfolioRiskGatekeeper, OrderManager, and Broker (Paper/Live).
2. Manages system lifecycle:
   - boot_premarket(): Authenticates session, syncs NSE master, and runs pre-market news triage.
   - start(): Activates live data feeds, registers reactive signal listeners, and starts reconciler.
   - stop(): Gracefully shuts down feeds, flattens remaining open positions, and flushes states.
"""

import asyncio
from datetime import date, datetime
import json
from typing import TYPE_CHECKING, Any, Dict, Optional
from loguru import logger

from config.settings import settings
from sentinel.orchestrator import SentinelOrchestrator
from shared.constants import IST_TZ
from shared.enums import DataFeederType, OrderStatus, OrderType, Side, Timeframe, TradingMode
from shared.schemas import Candle, ClosedTrade, Order, Position, Signal

from trading_engine.brokers.kite.auth import KiteAuthManager
from trading_engine.brokers.kite.instruments import InstrumentSync
from trading_engine.core.daily_state import DailyStateStore

if TYPE_CHECKING:
    from trading_engine.brokers.kite.kite_broker import KiteBroker

from trading_engine.core.event_bus import (
    EventBus,
    SignalEvent,
    bus,
)
from trading_engine.data.anomaly_detector import AnomalyDetector
from trading_engine.data.candle_aggregator import CandleAggregator
from trading_engine.execution.order_manager import OrderManager
from trading_engine.execution.paper_broker import PaperBroker
from trading_engine.risk.portfolio_risk import PortfolioRiskGatekeeper
from trading_engine.strategy.live_runner import LiveStrategyRunner


class TrillionaireEngine:
    """Master engine orchestrating data feeds, quantitative strategies, risk, and execution."""

    def __init__(
        self,
        trading_mode: Optional[TradingMode] = None,
        data_feeder: Optional[DataFeederType] = None,
        event_bus: Optional[EventBus] = None,
        candle_aggregator: Optional[CandleAggregator] = None,
        anomaly_detector: Optional[AnomalyDetector] = None,
        sentinel_orchestrator: Optional[SentinelOrchestrator] = None,
        strategy_runner: Optional[LiveStrategyRunner] = None,
        risk_gatekeeper: Optional[PortfolioRiskGatekeeper] = None,
        order_manager: Optional[OrderManager] = None,
        broker: Optional[Any] = None,
        auth_manager: Optional[KiteAuthManager] = None,
        instrument_sync: Optional[InstrumentSync] = None,
        free_feeder: Optional[Any] = None,
    ):
        self.trading_mode = trading_mode or settings.trading_mode
        self.data_feeder = DataFeederType.ZERODHA_KITE
        self.bus = event_bus or bus
        self.free_feeder = None

        # 1. Sentinel & Microstructure Guard
        self.sentinel = sentinel_orchestrator or SentinelOrchestrator()
        self.anomaly_detector = anomaly_detector or AnomalyDetector(event_bus=self.bus)

        # 2. Market Data & Streaming Indicators
        self.candle_aggregator = candle_aggregator or CandleAggregator(event_bus=self.bus)

        # 3. Strategy Runner
        self.strategy_runner = strategy_runner or LiveStrategyRunner(
            event_bus=self.bus,
            sentinel_orchestrator=self.sentinel,
            anomaly_detector=self.anomaly_detector,
        )

        # 4. Portfolio Risk Gatekeeper
        self.risk_gatekeeper = risk_gatekeeper or PortfolioRiskGatekeeper(
            event_bus=self.bus,
        )

        # 5. Broker Adapter (PaperBroker for PAPER/REPLAY, KiteBroker for LIVE)
        if broker is not None:
            self.broker = broker
            self.auth_manager = auth_manager
            self.instrument_sync = instrument_sync or getattr(broker, "instruments", None) or InstrumentSync()
        elif self.trading_mode == TradingMode.LIVE or self.data_feeder == DataFeederType.ZERODHA_KITE:
            from trading_engine.brokers.kite.kite_broker import KiteBroker

            self.auth_manager = auth_manager or (
                KiteAuthManager() if (settings.kite_api_key and settings.kite_user_id) else None
            )
            kite_client = None
            if self.auth_manager is not None:
                try:
                    kite_client = self.auth_manager.get_kite_client()
                except Exception as e:
                    logger.debug(f"Kite client not initialized during __init__: {e}")
            self.instrument_sync = instrument_sync or InstrumentSync(kite_client=kite_client)
            self.broker = KiteBroker(
                kite_client=kite_client,
                event_bus=self.bus,
                instrument_sync=self.instrument_sync,
                trading_mode=self.trading_mode,
            )
        else:
            self.broker = PaperBroker(event_bus=self.bus)
            self.auth_manager = auth_manager
            self.instrument_sync = instrument_sync or InstrumentSync()

        # 6. Order Manager & Daily State Persistence
        self.state_store = DailyStateStore()
        default_om_broker = (
            self.broker
            if (self.trading_mode == TradingMode.LIVE or isinstance(self.broker, PaperBroker))
            else PaperBroker(event_bus=self.bus)
        )
        self.order_manager = order_manager or OrderManager(
            event_bus=self.bus,
            broker=default_om_broker,
            risk_gatekeeper=self.risk_gatekeeper,
            on_state_change=self.persist_daily_state,
        )
        if getattr(self.order_manager, "on_state_change", None) is None:
            self.order_manager.on_state_change = self.persist_daily_state

        self._live_broker = self.broker

        # Attach OrderManager to KiteBroker if live
        if hasattr(self.broker, "set_order_manager") and callable(self.broker.set_order_manager):
            self.broker.set_order_manager(self.order_manager)

        # Engine state
        self.is_running = False
        self.is_booted = False
        self.is_replaying = False
        self.trading_paused = False
        self.current_trade_date: Optional[date] = None

        # Wire reactive signal routing on EventBus
        self.bus.subscribe(SignalEvent, self.on_signal)

        logger.info(
            f"TrillionaireEngine initialized [Mode: {self.trading_mode.value}, Feeder: {self.data_feeder.value}, Tier: {self.risk_gatekeeper.tier_name}]"
        )

    async def boot_premarket(self, trade_date: Optional[date] = None) -> Dict[str, Any]:
        """
        Executes pre-market startup sequence (08:30–08:45 AM IST):
        1. Unconditionally resets daily risk gatekeeper state, circuit breakers, and funnel metrics.
        2. Resiliently handles Kite live authentication & instrument master sync (falls back to cached instruments if login pending).
        3. Runs overnight news classification & calendar event blackouts via Sentinel.
        4. Loads active stock profiles.
        5. Pre-warms morning indicators from Parquet cache with live historical fallback.
        """
        self.current_trade_date = trade_date or datetime.now(IST_TZ).date()
        logger.info(f"Booting TrillionaireEngine for trade date: {self.current_trade_date}...")

        # ---------------------------------------------------------------------
        # Step 1: ALWAYS RUNS FIRST - Unconditional Daily Risk & Session Reset
        # ---------------------------------------------------------------------
        self.risk_gatekeeper.reset_daily_state(trade_date=self.current_trade_date)
        if hasattr(self.strategy_runner, "funnel_metrics"):
            self.strategy_runner.funnel_metrics = {
                "candles_evaluated": 0,
                "passed_stock_rule": 0,
                "passed_sector_filter": 0,
                "signals_emitted": 0,
            }

        # ---------------------------------------------------------------------
        # Step 2: Resilient Broker Auth & Instrument Sync (Protected against timeouts)
        # ---------------------------------------------------------------------
        if self.auth_manager is not None:
            try:
                self.auth_manager.get_valid_access_token(trade_date=self.current_trade_date)
                kite_client = self.auth_manager.get_kite_client()
                if self.instrument_sync is not None:
                    self.instrument_sync.kite = kite_client
                    self.instrument_sync.sync_instruments()
            except Exception as auth_err:
                logger.warning(
                    f"[PREMARKET AUTH] Kite live auth not completed for today ({auth_err}); "
                    f"loading cached NSE instruments from disk until OAuth login."
                )
                if self.instrument_sync is not None:
                    self.instrument_sync.load_cache_from_disk()

        # Enforce sole feeder
        self.data_feeder = DataFeederType.ZERODHA_KITE

        # ---------------------------------------------------------------------
        # Step 3: ALWAYS RUNS - Sentinel Triage, Profiles, & Indicator Warmup
        # ---------------------------------------------------------------------
        sentinel_tags = self.sentinel.run_pre_market_triage(current_date_ist=self.current_trade_date)
        self.strategy_runner.load_profiles_from_disk()
        warmed = self.warmup_indicators()

        # ---------------------------------------------------------------------
        # Step 4: Rehydrate today's executed orders & realized PnL from Redis
        # ---------------------------------------------------------------------
        rehydration_res = self.rehydrate_from_redis(trade_date=self.current_trade_date)

        self.is_booted = True
        logger.info(
            f"Premarket boot sequence complete. Active monitored universe: {len(self.strategy_runner.profiles)} stocks, "
            f"Sentinel tags: {len(sentinel_tags)}, Pre-warmed instruments: {len(warmed)}, "
            f"Rehydrated orders: {rehydration_res['orders_rehydrated']}"
        )

        return {
            "status": "booted",
            "trade_date": self.current_trade_date.isoformat(),
            "monitored_stocks": len(self.strategy_runner.profiles),
            "sentinel_tags": len(sentinel_tags),
            "warmed_up_symbols": len(warmed),
            "trading_mode": self.trading_mode.value,
        }

    @property
    def funnel_metrics(self) -> Dict[str, int]:
        """Returns live signal funnel telemetry metrics from LiveStrategyRunner."""
        return getattr(self.strategy_runner, "funnel_metrics", {
            "candles_evaluated": 0,
            "passed_stock_rule": 0,
            "passed_sector_filter": 0,
            "signals_emitted": 0,
        })

    @funnel_metrics.setter
    def funnel_metrics(self, value: Dict[str, int]) -> None:
        if hasattr(self.strategy_runner, "funnel_metrics"):
            self.strategy_runner.funnel_metrics = value

    async def on_signal(self, event: SignalEvent) -> None:
        """
        EventBus listener for validated strategy SignalEvents.
        Queries Sentinel risk tag, sizes position dynamically, and submits marketable order.
        """
        if not self.is_running and not getattr(self, "is_replaying", False):
            return

        if self.trading_paused:
            logger.info(
                f"[ENGINE ROUTING] Signal {event.signal.signal_id} on {event.signal.tradingsymbol} dropped: Trading is PAUSED"
            )
            return

        signal: Signal = event.signal
        sym = signal.tradingsymbol

        # 1. Fetch active Sentinel multiplier
        tag = self.sentinel.get_effective_risk_multiplier(sym)
        multiplier = tag.risk_multiplier

        # 2. Risk Gatekeeper dynamic sizing
        quantity = self.risk_gatekeeper.calculate_position_size(
            signal=signal,
            sentinel_multiplier=multiplier,
        )

        if quantity > 0:
            logger.info(
                f"[ENGINE ROUTING] Routing signal {signal.signal_id} -> {sym} {signal.side.value} x {quantity} shares"
            )
            await self.order_manager.submit_signal_order(signal=signal, quantity=quantity)
        else:
            logger.info(f"[ENGINE ROUTING] Signal {signal.signal_id} on {sym} rejected by risk sizing (Qty=0)")

    def _resolve_subscribed_tokens(self) -> list[int]:
        """
        Resolves Kite instrument tokens for all active StockProfiles and their parent sector indices.
        """
        tokens: set[int] = set()
        active_profiles = [p for p in self.strategy_runner.profiles.values() if p.is_active]
        if not active_profiles:
            active_profiles = list(self.strategy_runner.profiles.values())

        for profile in active_profiles:
            # 1. Resolve stock token
            tok = None
            if self.instrument_sync is not None:
                tok = self.instrument_sync.get_token(profile.tradingsymbol)
            if tok is None and profile.instrument_token:
                tok = profile.instrument_token
            if tok:
                tokens.add(int(tok))

            # 2. Resolve parent sector index token
            if profile.parent_index:
                idx_tok = None
                if self.instrument_sync is not None:
                    idx_tok = self.instrument_sync.get_token(profile.parent_index)
                if idx_tok is None and getattr(profile, "index_token", None):
                    idx_tok = profile.index_token
                if idx_tok:
                    tokens.add(int(idx_tok))

        resolved = sorted(list(tokens))
        logger.info(
            f"[ZERODHA FEED] Resolved {len(resolved)} instrument tokens for {len(active_profiles)} active profiles and sector indices."
        )
        return resolved

    async def start(self) -> None:
        """Starts real-time engine operations and activates KiteBroker WebSocket streaming."""
        if not self.is_booted:
            await self.boot_premarket()

        self.is_running = True
        self.data_feeder = DataFeederType.ZERODHA_KITE

        # Wire reactive signal routing on EventBus
        self.bus.subscribe(SignalEvent, self.on_signal)

        # In PAPER mode, ensure OrderManager routes through PaperBroker
        if self.trading_mode == TradingMode.PAPER:
            if not isinstance(self.order_manager.broker, PaperBroker):
                self.order_manager.broker = PaperBroker(event_bus=self.bus)

        # Always initialize and start KiteBroker + KiteTicker for live market streaming
        from trading_engine.brokers.kite.kite_broker import KiteBroker

        resolved_tokens = self._resolve_subscribed_tokens()
        kite_client = None
        access_token = None
        if self.auth_manager is not None:
            try:
                access_token = self.auth_manager.get_valid_access_token()
            except Exception as e:
                logger.debug(f"Kite access token not yet retrieved: {e}")
            try:
                kite_client = self.auth_manager.get_kite_client()
            except Exception as e:
                logger.debug(f"Kite client not yet authenticated for today: {e}")

        if not access_token:
            access_token = settings.kite_access_token

        ticker_client = None
        api_key = settings.kite_api_key or (getattr(kite_client, "api_key", "") if kite_client else "")
        if access_token and api_key and not api_key.startswith("your_"):
            try:
                from kiteconnect import KiteTicker
                ticker_client = KiteTicker(api_key=api_key, access_token=access_token)
                logger.info("[ENGINE] Instantiated KiteTicker with active access token.")
            except Exception as e:
                logger.warning(f"[ENGINE] Could not instantiate KiteTicker: {e}")

        if isinstance(self.broker, KiteBroker) or hasattr(self.broker, "tokens_to_subscribe"):
            self.broker.tokens_to_subscribe = resolved_tokens
            if hasattr(self.broker, "trading_mode"):
                self.broker.trading_mode = self.trading_mode
            if kite_client is not None and getattr(self.broker, "kite", None) is None:
                self.broker.kite = kite_client
            if ticker_client is not None and getattr(self.broker, "ticker", None) is None:
                self.broker.ticker = ticker_client
                if hasattr(self.broker, "_setup_ticker_callbacks"):
                    self.broker._setup_ticker_callbacks()
        else:
            self.broker = KiteBroker(
                kite_client=kite_client,
                ticker_client=ticker_client,
                event_bus=self.bus,
                instrument_sync=self.instrument_sync,
                tokens_to_subscribe=resolved_tokens,
                trading_mode=self.trading_mode,
            )

        if hasattr(self.broker, "set_order_manager") and callable(self.broker.set_order_manager):
            self.broker.set_order_manager(self.order_manager)

        if hasattr(self.broker, "start") and callable(self.broker.start):
            try:
                await self.broker.start()
            except Exception as broker_err:
                logger.warning(
                    f"[KITE STREAM PENDING] Kite access token missing for today ({broker_err}) — "
                    f"click 'Login with Kite' in Settings & Keys to start live KiteTicker stream"
                )

        logger.info(
            f"[ENGINE STARTED] TrillionaireEngine running [Mode: {self.trading_mode.value}, Feeder: {self.data_feeder.value}]"
        )

    async def stop(self) -> None:
        """Gracefully halts trading operations and flattens any remaining positions."""
        logger.warning("[ENGINE STOP] Initiating TrillionaireEngine shutdown...")
        self.is_running = False

        # 1. Flatten all active positions if any remain
        if len(self.order_manager.open_positions) > 0:
            logger.info("Flattening open positions prior to engine shutdown...")
            await self.order_manager.flatten_all_positions(reason="ENGINE_SHUTDOWN")

        # 2. Stop live Kite broker / ticker
        if hasattr(self.broker, "stop") and callable(self.broker.stop):
            await self.broker.stop()

        logger.info("[ENGINE STOPPED] Engine successfully halted.")

    async def emergency_flatten(self, reason: str = "EMERGENCY_FLATTEN") -> list[Any]:
        """Liquidates all open positions immediately and activates the risk kill-switch."""
        from trading_engine.core.event_bus import RiskAlertEvent

        logger.critical(f"[EMERGENCY FLATTEN] Triggered: {reason}")
        self.risk_gatekeeper.kill_switch_active = True
        self.risk_gatekeeper.circuit_breaker_reason = f"Emergency flatten: {reason}"
        flattened_orders = await self.order_manager.flatten_all_positions(reason=reason)
        await self.bus.publish(
            RiskAlertEvent(
                symbol="ALL",
                level="CIRCUIT_BREAKER",
                message=f"Emergency kill switch triggered: {reason}. Liquidated {len(flattened_orders)} positions.",
            )
        )
        return flattened_orders

    def pause_trading(self) -> Dict[str, Any]:
        """Pauses new trade entry evaluation while keeping feed and ticker streaming."""
        self.trading_paused = True
        if hasattr(self.strategy_runner, "is_paused"):
            self.strategy_runner.is_paused = True
        logger.warning("[ENGINE CONTROL] Trading execution PAUSED. Feeder & Ticker remain streaming.")
        return {"status": "paused", "trading_paused": True}

    def resume_trading(self) -> Dict[str, Any]:
        """Resumes strategy entry signal evaluation."""
        self.trading_paused = False
        if hasattr(self.strategy_runner, "is_paused"):
            self.strategy_runner.is_paused = False
        logger.info("[ENGINE CONTROL] Trading execution RESUMED.")
        return {"status": "resumed", "trading_paused": False}

    def persist_daily_state(self) -> None:
        """Persists today's orders, open positions, closed trades, funnel counters, and paper balance to disk."""
        if not hasattr(self, "state_store") or self.state_store is None:
            return
        target_date = self.current_trade_date or datetime.now(IST_TZ).date()
        realized = (
            round(sum(t.realized_pnl for t in self.order_manager.closed_trades), 2)
            if self.order_manager.closed_trades
            else self.risk_gatekeeper.realized_daily_pnl
        )
        paper_balance = round(self.risk_gatekeeper.allocated_capital + realized, 2)
        self.state_store.save_state(
            trade_date=target_date,
            orders=[o.model_dump(mode="json") for o in self.order_manager.orders.values()],
            open_positions=[p.model_dump(mode="json") for p in self.order_manager.open_positions.values()],
            closed_trades=[t.model_dump(mode="json") for t in self.order_manager.closed_trades],
            funnel_metrics=self.funnel_metrics,
            paper_balance=paper_balance,
            allocated_capital=self.risk_gatekeeper.allocated_capital,
            realized_pnl=realized,
            risk_per_trade_pct=self.risk_gatekeeper.risk_per_trade_pct,
        )

    def rehydrate_from_redis(self, trade_date: Optional[date] = None) -> Dict[str, Any]:
        """Rehydrates today's orders, closed_trades, open positions, funnel counts, and realized PnL from Redis + Disk."""
        target_date = trade_date or (self.current_trade_date or datetime.now(IST_TZ).date())
        today_iso = target_date.isoformat()
        orders_rehydrated = 0
        closed_rehydrated = 0
        pnl_rehydrated = 0.0

        status_map = {
            "PENDING_NEW": OrderStatus.PENDING_NEW,
            "OPEN": OrderStatus.OPEN,
            "TRIGGER PENDING": OrderStatus.TRIGGER_PENDING,
            "FILLED": OrderStatus.FILLED,
            "CANCELLED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
        }

        # 1. Rehydrate from Redis
        try:
            import redis
            from config.settings import settings

            r = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )
            order_key = f"trillionaire:orders:{today_iso}"
            raw_orders = r.hgetall(order_key)
            if raw_orders:
                for oid, raw_json in raw_orders.items():
                    data = json.loads(raw_json)
                    created_at = (
                        datetime.fromisoformat(data["created_at_ist"])
                        if data.get("created_at_ist")
                        else datetime.now(IST_TZ)
                    )
                    side_val = Side.BUY if data.get("side") == "BUY" else Side.SELL
                    status_enum = status_map.get(data.get("status"), OrderStatus.FILLED)
                    ord_type = OrderType.SL_LIMIT if data.get("order_type") == "SL_LIMIT" else OrderType.LIMIT
                    order = Order(
                        order_id=data.get("order_id", oid),
                        signal_id=data.get("signal_id"),
                        tradingsymbol=data.get("tradingsymbol", ""),
                        side=side_val,
                        product=data.get("product", "MIS"),
                        order_type=ord_type,
                        quantity=int(data.get("quantity", 0)),
                        limit_price=data.get("limit_price"),
                        trigger_price=data.get("trigger_price"),
                        sl_price=data.get("sl_price"),
                        target_price=data.get("target_price"),
                        filled_quantity=int(data.get("filled_quantity", 0)),
                        average_fill_price=data.get("average_fill_price"),
                        status=status_enum,
                        exit_reason=data.get("exit_reason"),
                        rejection_reason=data.get("rejection_reason"),
                        is_exit=bool(data.get("is_exit", False)),
                        client_order_tag=data.get("client_order_tag"),
                        created_at_ist=created_at,
                        broker_order_id=data.get("broker_order_id"),
                    )
                    self.order_manager.orders[order.order_id] = order
                    orders_rehydrated += 1

            # Rehydrate closed trades from Redis if present
            closed_key = f"trillionaire:closed_trades:{today_iso}"
            raw_closed = r.hgetall(closed_key)
            if raw_closed:
                existing_ids = {t.order_id for t in self.order_manager.closed_trades}
                for _, raw_ct in raw_closed.items():
                    try:
                        ct_data = json.loads(raw_ct)
                        if "actual_exit_price" in ct_data and ct_data.get("order_id") not in existing_ids:
                            ct = ClosedTrade.model_validate(ct_data)
                            self.order_manager.closed_trades.append(ct)
                            existing_ids.add(ct.order_id)
                            closed_rehydrated += 1
                    except Exception:
                        pass

            pnl_key = f"trillionaire:realized_pnl:{today_iso}"
            raw_pnl = r.get(pnl_key)
            if raw_pnl is not None:
                pnl_rehydrated = float(raw_pnl)
                self.risk_gatekeeper.realized_pnl = pnl_rehydrated
                logger.info(f"[REHYDRATION] Restored realized PnL from Redis: Rs.{pnl_rehydrated:.2f}")

            if orders_rehydrated > 0:
                logger.info(f"[REHYDRATION] Rehydrated {orders_rehydrated} orders from Redis ({order_key})")
        except Exception as e:
            logger.debug(f"[REHYDRATION] Redis rehydration bypassed: {e}")

        # 2. Rehydrate from Disk DailyStateStore (covers open_positions, closed_trades, funnel, paper balance)
        try:
            disk_state = self.state_store.load_state(trade_date=target_date) if hasattr(self, "state_store") else None
            if disk_state:
                for raw_o in disk_state.get("orders", []):
                    oid = raw_o.get("order_id")
                    if oid and oid not in self.order_manager.orders:
                        try:
                            self.order_manager.orders[oid] = Order.model_validate(raw_o)
                            orders_rehydrated += 1
                        except Exception:
                            pass

                existing_ct_ids = {t.order_id for t in self.order_manager.closed_trades}
                for raw_ct in disk_state.get("closed_trades", []):
                    if raw_ct.get("order_id") not in existing_ct_ids:
                        try:
                            ct = ClosedTrade.model_validate(raw_ct)
                            self.order_manager.closed_trades.append(ct)
                            existing_ct_ids.add(ct.order_id)
                            closed_rehydrated += 1
                        except Exception:
                            pass

                for raw_pos in disk_state.get("open_positions", []):
                    sym = raw_pos.get("tradingsymbol")
                    if sym and sym not in self.order_manager.open_positions:
                        try:
                            pos = Position.model_validate(raw_pos)
                            self.order_manager.open_positions[sym] = pos
                            self.risk_gatekeeper.register_position(pos)
                        except Exception:
                            pass

                if disk_state.get("funnel_metrics") and hasattr(self.strategy_runner, "funnel_metrics"):
                    saved_f = disk_state["funnel_metrics"]
                    for fk, fv in saved_f.items():
                        self.strategy_runner.funnel_metrics[fk] = max(
                            int(self.strategy_runner.funnel_metrics.get(fk, 0)), int(fv)
                        )

                if disk_state.get("allocated_capital"):
                    self.risk_gatekeeper.allocated_capital = float(disk_state["allocated_capital"])
                if disk_state.get("risk_per_trade_pct"):
                    self.risk_gatekeeper.risk_per_trade_pct = float(disk_state["risk_per_trade_pct"])

                if self.order_manager.closed_trades:
                    pnl_rehydrated = round(sum(t.realized_pnl for t in self.order_manager.closed_trades), 2)
                    self.risk_gatekeeper.realized_pnl = pnl_rehydrated
                elif disk_state.get("realized_pnl") and self.risk_gatekeeper.realized_pnl == 0.0:
                    pnl_rehydrated = float(disk_state["realized_pnl"])
                    self.risk_gatekeeper.realized_pnl = pnl_rehydrated

                logger.info(
                    f"[DAILY STATE RESTORED] Orders={len(self.order_manager.orders)}, "
                    f"ClosedTrades={len(self.order_manager.closed_trades)}, "
                    f"OpenPositions={len(self.order_manager.open_positions)}, RealizedPnL=Rs.{pnl_rehydrated:.2f}"
                )
        except Exception as e:
            logger.debug(f"[REHYDRATION] Disk state rehydration skipped: {e}")

        return {
            "orders_rehydrated": orders_rehydrated,
            "closed_trades_rehydrated": closed_rehydrated,
            "realized_pnl_rehydrated": pnl_rehydrated,
        }

    def get_lifecycle_milestones(self) -> Dict[str, Any]:
        """
        Computes structured readiness status for the 5 lifecycle milestones:
        1. kite_auth: Kite Session Authorized
        2. universe_warmed: Universe & Indicators Warmed
        3. ticker_connected: Live Market Stream Connected
        4. ticks_flowing: Live Ticks & Candles Flowing
        5. strategy_armed: Strategy & Risk Execution Armed
        """
        from shared.constants import KEY_KITE_TOKEN
        from sentinel.calendar_guard.event_calendar import get_market_holiday_name

        now = datetime.now(IST_TZ)
        today_date = now.date()
        today_iso = today_date.isoformat()
        holiday = get_market_holiday_name(today_date)

        # 1. kite_auth
        kite_auth_status = "waiting"
        kite_auth_detail = "Waiting for daily Kite authentication token"
        try:
            import redis
            from config.settings import settings
            r = redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=0.2, socket_timeout=0.2)
            raw = r.get(KEY_KITE_TOKEN)
            if raw:
                tok_data = json.loads(raw)
                if tok_data.get("date") == today_iso:
                    kite_auth_status = "passed"
                    kite_auth_detail = f"Session authorized for {today_iso} (expires tomorrow 06:00 IST)"
                else:
                    kite_auth_status = "warning"
                    kite_auth_detail = f"Token is for date {tok_data.get('date')}, today is {today_iso}"
            else:
                if getattr(self.broker, "kite", None) is not None:
                    kite_auth_status = "passed"
                    kite_auth_detail = "Mock/Internal Kite client authorized"
                else:
                    kite_auth_status = "warning"
                    kite_auth_detail = "Click 'Login with Kite' in Settings to authenticate today's session"
        except Exception:
            if getattr(self.broker, "kite", None) is not None:
                kite_auth_status = "passed"
                kite_auth_detail = "Kite client attached"
            else:
                kite_auth_status = "warning"
                kite_auth_detail = "Kite authentication pending"

        # 2. universe_warmed
        prof_count = len(self.strategy_runner.profiles)
        is_warmed = prof_count > 0 and self.is_booted
        if is_warmed:
            universe_status = "passed"
            universe_detail = f"{prof_count} profiles loaded, Sentinel tags synced, M5 indicators primed"
        else:
            universe_status = "waiting"
            universe_detail = f"{prof_count} profiles active, indicator warmup pending premarket boot"

        # 3. ticker_connected
        ticker_obj = getattr(self.broker, "ticker", None)
        is_conn = False
        if ticker_obj is not None:
            if hasattr(ticker_obj, "is_connected") and callable(ticker_obj.is_connected):
                is_conn = ticker_obj.is_connected()
            elif getattr(self.broker, "_is_running", False):
                is_conn = True
        elif getattr(self.broker, "_is_running", False):
            is_conn = True

        sub_count = len(getattr(self.broker, "tokens_to_subscribe", []))
        if is_conn:
            ticker_status = "passed"
            ticker_detail = f"KiteTicker WebSocket active, subscribed to {sub_count} tokens"
        else:
            ticker_status = "waiting"
            ticker_detail = "Ticker not connected. Starts automatically with Engine start"

        # 4. ticks_flowing
        recent_ticks_count = len(getattr(self.broker, "latest_ticks", {}))
        if holiday:
            ticks_status = "waiting"
            ticks_detail = f"NSE Market Holiday ({holiday})"
        elif now.hour < 9 or (now.hour == 9 and now.minute < 15):
            ticks_status = "waiting"
            ticks_detail = "Pre-Market (Opens 09:15 IST)"
        elif recent_ticks_count > 0:
            ticks_status = "passed"
            ticks_detail = f"Streaming live L1 ticks ({recent_ticks_count} symbols active)"
        elif is_conn:
            ticks_status = "waiting"
            ticks_detail = "Connected, awaiting market open ticks"
        else:
            ticks_status = "waiting"
            ticks_detail = "Awaiting live market tick feed"

        # 5. strategy_armed
        is_kill = self.risk_gatekeeper.kill_switch_active
        if is_kill:
            strategy_status = "error"
            strategy_detail = f"Circuit Breaker Tripped: {self.risk_gatekeeper.circuit_breaker_reason}"
        elif self.trading_paused:
            strategy_status = "warning"
            strategy_detail = "Strategy scanning PAUSED by operator"
        elif not self.is_running:
            strategy_status = "waiting"
            strategy_detail = "Engine stopped. Click 'Start Engine' to arm"
        else:
            strategy_status = "passed"
            strategy_detail = "Armed: scanning breakouts (09:25–15:10 IST) with risk limits active"

        milestones = [
            {"id": "kite_auth", "label": "Kite Session Authorized", "status": kite_auth_status, "detail": kite_auth_detail},
            {"id": "universe_warmed", "label": "Universe & Indicators Warmed", "status": universe_status, "detail": universe_detail},
            {"id": "ticker_connected", "label": "Live Market Stream Connected", "status": ticker_status, "detail": ticker_detail},
            {"id": "ticks_flowing", "label": "Live Ticks & Candles Flowing", "status": ticks_status, "detail": ticks_detail},
            {"id": "strategy_armed", "label": "Strategy & Risk Execution Armed", "status": strategy_status, "detail": strategy_detail},
        ]

        passed_count = sum(1 for m in milestones if m["status"] == "passed")
        total_count = len(milestones)

        # Overall summary text for top bar pill
        if holiday and passed_count >= 3:
            summary_label = f"{passed_count}/{total_count} NSE HOLIDAY"
        elif passed_count == total_count:
            summary_label = f"{passed_count}/{total_count} READY"
        elif kite_auth_status != "passed":
            summary_label = f"{passed_count}/{total_count} AUTH NEEDED"
        elif self.trading_paused:
            summary_label = f"{passed_count}/{total_count} PAUSED"
        else:
            summary_label = f"{passed_count}/{total_count} ARMED"

        return {
            "summary_label": summary_label,
            "passed_count": passed_count,
            "total_count": total_count,
            "milestones": milestones,
            "holiday": holiday,
            "trading_paused": self.trading_paused,
            "timestamp_ist": now.isoformat(),
        }

    def set_trading_mode(self, mode: TradingMode) -> None:
        """Updates trading mode dynamically and syncs broker reconciler & execution routing."""
        self.trading_mode = mode
        if hasattr(self.broker, "trading_mode"):
            self.broker.trading_mode = mode
        if mode == TradingMode.LIVE:
            if self._live_broker is not None and not isinstance(self._live_broker, PaperBroker):
                self.order_manager.broker = self._live_broker
        else:
            if not isinstance(self.order_manager.broker, PaperBroker):
                self.order_manager.broker = PaperBroker(event_bus=self.bus)
        logger.info(f"[ENGINE MODE UPDATED] Trading mode set to {mode.value}")

    def set_data_feeder(self, feeder_type: DataFeederType) -> None:
        """Enforces market data feeder as ZERODHA_KITE."""
        self.data_feeder = DataFeederType.ZERODHA_KITE
        logger.info("[FEEDER ENFORCED] Market data feeder locked to ZERODHA_KITE.")

        if self.trading_mode == TradingMode.LIVE:
            if self._live_broker is not None and not isinstance(self._live_broker, PaperBroker):
                self.order_manager.broker = self._live_broker
        else:
            if not isinstance(self.order_manager.broker, PaperBroker):
                self.order_manager.broker = PaperBroker(event_bus=self.bus)

        # If engine is actively running, restart data streaming on Kite
        if self.is_running:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._switch_feeder_runtime(DataFeederType.ZERODHA_KITE, DataFeederType.ZERODHA_KITE))
            except RuntimeError:
                pass

    def warmup_indicators(self) -> Dict[str, Dict[str, float]]:
        """
        Pre-warms technical indicators from Parquet or live historical fallback,
        seeding EMA(200), EMA(20), ATR(14), RVOL, and parent sector indices.
        """
        kite_client = None
        if self.auth_manager is not None:
            try:
                kite_client = self.auth_manager.get_kite_client()
            except Exception:
                pass

        warmed = self.candle_aggregator.warmup_from_parquet(
            symbols=list(self.strategy_runner.profiles.keys()),
            kite_client=kite_client,
            instrument_sync=self.instrument_sync,
        )
        # Seed parent index EMA(20) and baseline M5 candle into strategy runner
        for sym, metrics in warmed.items():
            if "NIFTY" in sym:
                self.strategy_runner.seed_index_ema_20(sym, metrics.get("ema_20", 0.0))
                if "prev_close" in metrics:
                    c = metrics["prev_close"]
                    self.strategy_runner._latest_candles[(sym, Timeframe.M5)] = Candle(
                        tradingsymbol=sym,
                        timeframe=Timeframe.M5,
                        timestamp_ist=datetime.now(IST_TZ),
                        open=c,
                        high=c,
                        low=c,
                        close=c,
                        volume=1000,
                        vwap=c,
                        ema_200=metrics.get("ema_200", c),
                        atr_14=metrics.get("atr_14", 1.0),
                        rvol=1.0,
                    )
        return warmed

    async def _switch_feeder_runtime(self, old_feeder: DataFeederType, new_feeder: DataFeederType) -> None:
        """Connects or restarts live KiteBroker WebSocket streaming while engine is active."""
        from trading_engine.brokers.kite.kite_broker import KiteBroker

        resolved_tokens = self._resolve_subscribed_tokens()
        if isinstance(self.broker, KiteBroker) or hasattr(self.broker, "tokens_to_subscribe"):
            self.broker.tokens_to_subscribe = resolved_tokens
            if hasattr(self.broker, "trading_mode"):
                self.broker.trading_mode = self.trading_mode
        else:
            kite_client = None
            if self.auth_manager is not None:
                try:
                    kite_client = self.auth_manager.get_kite_client()
                except Exception as e:
                    logger.debug(f"Failed to get kite_client for KiteBroker: {e}")
            self.broker = KiteBroker(
                kite_client=kite_client,
                event_bus=self.bus,
                instrument_sync=self.instrument_sync,
                tokens_to_subscribe=resolved_tokens,
                trading_mode=self.trading_mode,
            )
            if hasattr(self.broker, "set_order_manager") and callable(self.broker.set_order_manager):
                self.broker.set_order_manager(self.order_manager)

        if hasattr(self.broker, "start") and callable(self.broker.start):
            try:
                await self.broker.start()
            except Exception as e:
                logger.warning(f"[FEEDER CONNECT] Kite ticker start pending auth: {e}")

        # Pre-warm indicators so candles evaluate immediately
        try:
            self.warmup_indicators()
        except Exception as e:
            logger.warning(f"[FEEDER SWITCH] Indicator warmup failed: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Returns comprehensive diagnostic and operational metrics snapshot."""
        realized = (
            round(sum(t.realized_pnl for t in self.order_manager.closed_trades), 2)
            if self.order_manager.closed_trades
            else self.risk_gatekeeper.realized_daily_pnl
        )
        unrealized = round(sum(pos.unrealized_pnl for pos in self.order_manager.open_positions.values()), 2)
        total_mtm = round(realized + unrealized, 2)
        return {
            "is_running": self.is_running,
            "is_booted": self.is_booted,
            "trading_paused": self.trading_paused,
            "trading_mode": self.trading_mode.value,
            "data_feeder": self.data_feeder.value,
            "current_trade_date": self.current_trade_date.isoformat() if self.current_trade_date else None,
            "capital_tier": self.risk_gatekeeper.tier_name,
            "allocated_capital": self.risk_gatekeeper.allocated_capital,
            "paper_balance": round(self.risk_gatekeeper.allocated_capital + realized, 2),
            "risk_per_trade_pct": self.risk_gatekeeper.risk_per_trade_pct,
            "daily_realized_pnl": realized,
            "daily_unrealized_pnl": unrealized,
            "daily_total_mtm": total_mtm,
            "kill_switch_active": self.risk_gatekeeper.kill_switch_active,
            "profit_lock_armed": self.risk_gatekeeper.profit_lock_armed,
            "token_expired": bool(getattr(self.broker, "token_expired", False)),
            "ticker_reconnecting": bool(getattr(self.broker, "ticker_reconnecting", False)),
            "open_positions_count": len(self.order_manager.open_positions),
            "closed_trades_count": len(self.order_manager.closed_trades),
            "milestones": self.get_lifecycle_milestones(),
            "funnel_metrics": getattr(self.strategy_runner, "funnel_metrics", {
                "candles_evaluated": 0,
                "passed_stock_rule": 0,
                "passed_sector_filter": 0,
                "signals_emitted": 0,
            }),
            "open_positions": {
                sym: {
                    "side": pos.side.value,
                    "qty": pos.quantity,
                    "entry": pos.average_entry_price,
                    "ltp": pos.ltp,
                    "unrealized_pnl": pos.unrealized_pnl,
                    "sl": pos.stop_loss,
                    "tp": pos.target_price,
                }
                for sym, pos in self.order_manager.open_positions.items()
            },
        }
