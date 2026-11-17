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
from typing import TYPE_CHECKING, Any, Dict, Optional
from loguru import logger

from config.settings import settings
from sentinel.orchestrator import SentinelOrchestrator
from shared.constants import IST_TZ
from shared.enums import TradingMode
from shared.schemas import Signal

if TYPE_CHECKING:
    from trading_engine.brokers.kite.auth import KiteAuthManager
    from trading_engine.brokers.kite.instruments import InstrumentSync
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
    ):
        self.trading_mode = trading_mode or settings.trading_mode
        self.bus = event_bus or bus

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
            self.instrument_sync = instrument_sync
        elif self.trading_mode == TradingMode.LIVE:
            from trading_engine.brokers.kite.auth import KiteAuthManager
            from trading_engine.brokers.kite.instruments import InstrumentSync
            from trading_engine.brokers.kite.kite_broker import KiteBroker

            self.auth_manager = auth_manager or KiteAuthManager()
            kite_client = self.auth_manager.get_kite_client()
            self.instrument_sync = instrument_sync or InstrumentSync(kite_client=kite_client)
            self.broker = KiteBroker(
                kite_client=kite_client,
                event_bus=self.bus,
                instrument_sync=self.instrument_sync,
            )
        else:
            from trading_engine.brokers.kite.instruments import InstrumentSync

            self.broker = PaperBroker(event_bus=self.bus)
            self.auth_manager = None
            self.instrument_sync = instrument_sync or InstrumentSync()

        # 6. Order Manager
        self.order_manager = order_manager or OrderManager(
            event_bus=self.bus,
            broker=self.broker,
            risk_gatekeeper=self.risk_gatekeeper,
        )

        # Attach OrderManager to KiteBroker if live
        if hasattr(self.broker, "set_order_manager") and callable(self.broker.set_order_manager):
            self.broker.set_order_manager(self.order_manager)

        # Engine state
        self.is_running = False
        self.is_booted = False
        self.current_trade_date: Optional[date] = None

        logger.info(
            f"TrillionaireEngine initialized [Mode: {self.trading_mode.value}, Tier: {self.risk_gatekeeper.tier_name}]"
        )

    async def boot_premarket(self, trade_date: Optional[date] = None) -> Dict[str, Any]:
        """
        Executes pre-market startup sequence (08:30–08:45 AM IST):
        1. Authenticates live broker session (if LIVE mode).
        2. Downloads and synchronizes daily NSE instrument definitions.
        3. Runs overnight news classification & calendar event blackouts.
        4. Resets intraday PnL and risk circuit breakers.
        5. Refreshes active stock profiles.
        """
        self.current_trade_date = trade_date or datetime.now(IST_TZ).date()
        logger.info(f"Booting TrillionaireEngine for trade date: {self.current_trade_date}...")

        # 1. Live Authentication & Instrument Master Sync
        if self.trading_mode == TradingMode.LIVE and self.auth_manager is not None:
            try:
                self.auth_manager.get_valid_access_token(trade_date=self.current_trade_date)
                if self.instrument_sync is not None:
                    self.instrument_sync.sync_instruments()
            except Exception as e:
                logger.error(f"Premarket authentication/instrument sync failed: {e}")
                raise

        # 2. Sentinel Pre-Market Triage
        sentinel_tags = self.sentinel.run_pre_market_triage(current_date_ist=self.current_trade_date)

        # 3. Reset Daily Risk Gatekeeper State
        self.risk_gatekeeper.reset_daily_state(trade_date=self.current_trade_date)

        # 4. Reload Stock Profiles
        self.strategy_runner.load_profiles_from_disk()

        # 5. Morning Indicator Warmup from Parquet
        warmed = self.candle_aggregator.warmup_from_parquet(
            symbols=list(self.strategy_runner.profiles.keys())
        )
        # Seed parent index EMA(20) into strategy runner
        for sym, metrics in warmed.items():
            if "NIFTY" in sym:
                self.strategy_runner.seed_index_ema_20(sym, metrics.get("ema_20", 0.0))

        self.is_booted = True
        logger.info(
            f"Premarket boot sequence complete. Active monitored universe: {len(self.strategy_runner.profiles)} stocks, "
            f"Sentinel tags: {len(sentinel_tags)}, Pre-warmed instruments: {len(warmed)}"
        )

        return {
            "status": "booted",
            "trade_date": self.current_trade_date.isoformat(),
            "monitored_stocks": len(self.strategy_runner.profiles),
            "sentinel_tags": len(sentinel_tags),
            "warmed_up_symbols": len(warmed),
            "trading_mode": self.trading_mode.value,
        }

    async def on_signal(self, event: SignalEvent) -> None:
        """
        EventBus listener for validated strategy SignalEvents.
        Queries Sentinel risk tag, sizes position dynamically, and submits marketable order.
        """
        if not self.is_running:
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

    async def start(self) -> None:
        """Starts real-time engine operations, activates broker, and subscribes signal router."""
        if not self.is_booted:
            await self.boot_premarket()

        self.is_running = True

        # Wire reactive signal routing on EventBus
        self.bus.subscribe(SignalEvent, self.on_signal)

        # Start live broker background tasks if supported
        if hasattr(self.broker, "start") and callable(self.broker.start):
            await self.broker.start()

        logger.info(f"[ENGINE STARTED] TrillionaireEngine running in {self.trading_mode.value} mode")

    async def stop(self) -> None:
        """Gracefully halts trading operations and flattens any remaining positions."""
        logger.warning("[ENGINE STOP] Initiating TrillionaireEngine shutdown...")
        self.is_running = False

        # 1. Flatten all active positions if any remain
        if len(self.order_manager.open_positions) > 0:
            logger.info("Flattening open positions prior to engine shutdown...")
            await self.order_manager.flatten_all_positions(reason="ENGINE_SHUTDOWN")

        # 2. Stop live broker background tasks if supported
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

    def set_trading_mode(self, mode: TradingMode) -> None:
        """Updates trading mode dynamically."""
        self.trading_mode = mode
        logger.info(f"[ENGINE MODE UPDATED] Trading mode set to {mode.value}")

    def get_status(self) -> Dict[str, Any]:
        """Returns comprehensive diagnostic and operational metrics snapshot."""
        return {
            "is_running": self.is_running,
            "is_booted": self.is_booted,
            "trading_mode": self.trading_mode.value,
            "current_trade_date": self.current_trade_date.isoformat() if self.current_trade_date else None,
            "capital_tier": self.risk_gatekeeper.tier_name,
            "allocated_capital": self.risk_gatekeeper.allocated_capital,
            "daily_realized_pnl": self.risk_gatekeeper.realized_daily_pnl,
            "daily_total_mtm": self.risk_gatekeeper.get_total_daily_mtm(),
            "kill_switch_active": self.risk_gatekeeper.kill_switch_active,
            "profit_lock_armed": self.risk_gatekeeper.profit_lock_armed,
            "open_positions_count": len(self.order_manager.open_positions),
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
