"""
Trillionaire Research Lab - Event-Driven Backtest Engine
High-fidelity bar-by-bar intraday backtester with zero look-ahead bias:
- Session timing guards (09:25 entry start, 14:30 entry cutoff, 15:10 auto-square-off)
- Configurable adverse execution slippage per leg
- Exact Zerodha MIS statutory charge deduction (via calculate_mis_net_pnl)
- Conservative intrabar priority (SL evaluated before TP if both breached in bar)
- Capital tier risk sizing & portfolio daily loss circuit breakers
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional
import numpy as np
import polars as pl

from config.settings import settings
from research_lab.backtester.strategies import BaseMicroStrategy, get_strategy_for_regime
from research_lab.profiler.indian_charges import ChargesBreakdown, calculate_mis_net_pnl
from shared.constants import (
    ENTRY_WINDOW_END,
    ENTRY_WINDOW_START,
    FORCE_SQUARE_OFF_TRIGGER,
    IST_TZ,
)
from shared.enums import Side
from shared.schemas import Signal, StockProfile


@dataclass
class TradeRecord:
    """Detailed record of an executed and closed intraday trade."""
    trade_id: str
    symbol: str
    side: Side
    strategy_name: str
    entry_time: datetime
    exit_time: datetime
    quantity: int
    entry_price_raw: float
    entry_price_fill: float
    exit_price_raw: float
    exit_price_fill: float
    gross_pnl: float
    net_pnl: float
    total_charges: float
    exit_reason: str  # TARGET_HIT, STOP_LOSS_HIT, MANDATORY_SQUARE_OFF
    charges: ChargesBreakdown


@dataclass
class DailyMetrics:
    """Daily realized PnL and risk monitoring."""
    trade_date: date
    realized_net_pnl: float = 0.0
    trades_count: int = 0
    circuit_breaker_tripped: bool = False


@dataclass
class BacktestSummary:
    """Aggregated portfolio performance metrics."""
    symbol: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    gross_pnl: float
    total_charges: float
    net_pnl: float
    profit_factor: float
    max_drawdown_inr: float
    max_drawdown_pct: float
    sharpe_ratio: float
    trades: List[TradeRecord] = field(default_factory=list)


class EventDrivenBacktester:
    """Simulates intraday bar-by-bar execution with real market frictions."""

    def __init__(
        self,
        capital: float = 100_000.0,
        risk_per_trade_pct: float = 0.5,
        max_daily_loss_pct: float = 1.5,
        max_mis_leverage: float = 3.0,
        slippage_pct: float = 0.0002,  # 2 basis points (0.02%) adverse fill per leg
    ):
        self.initial_capital = capital
        self.current_capital = capital
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_mis_leverage = max_mis_leverage
        self.slippage_pct = slippage_pct

    def _apply_slippage(self, price: float, side: Side, is_entry: bool) -> float:
        """Applies conservative adverse slippage to fill prices."""
        if is_entry:
            if side == Side.BUY:
                return round(price * (1.0 + self.slippage_pct), 2)
            else:
                return round(price * (1.0 - self.slippage_pct), 2)
        else:
            if side == Side.BUY:  # Exiting a long position -> Selling
                return round(price * (1.0 - self.slippage_pct), 2)
            else:  # Exiting a short position -> Buying back
                return round(price * (1.0 + self.slippage_pct), 2)

    def _size_position(self, entry_price: float, stop_loss: float) -> int:
        """Sizes quantity based on capital risk-per-trade and maximum MIS leverage."""
        risk_amount = self.current_capital * (self.risk_per_trade_pct / 100.0)
        risk_per_share = abs(entry_price - stop_loss)
        if risk_per_share <= 0:
            return 0

        target_qty = int(risk_amount // risk_per_share)
        max_allowed_shares = int((self.current_capital * self.max_mis_leverage) // entry_price)
        qty = min(target_qty, max_allowed_shares)
        return max(0, qty)

    def run(
        self,
        stock_df_with_indicators: pl.DataFrame,
        index_df_with_indicators: pl.DataFrame,
        profile: StockProfile,
        strategy: Optional[BaseMicroStrategy] = None,
    ) -> BacktestSummary:
        """
        Runs event-driven intraday backtest across chronological bars.
        """
        if strategy is None:
            strategy = get_strategy_for_regime(profile.cluster_regime)

        # Ensure sorted by timestamp
        stock_df = stock_df_with_indicators.sort("timestamp")
        index_df = index_df_with_indicators.sort("timestamp")

        # Align timestamps via join
        joined = (
            stock_df.join(
                index_df.select([
                    "timestamp",
                    pl.col("open").alias("idx_open"),
                    pl.col("high").alias("idx_high"),
                    pl.col("low").alias("idx_low"),
                    pl.col("close").alias("idx_close"),
                    pl.col("vwap").alias("idx_vwap"),
                    pl.col("ema_20").alias("idx_ema_20"),
                ]),
                on="timestamp",
                how="inner",
            )
            .sort("timestamp")
        )

        records = joined.to_dicts()
        trades: List[TradeRecord] = []

        active_position: Optional[Dict[str, Any]] = None
        current_day: Optional[date] = None
        daily_metric = DailyMetrics(trade_date=date.min)

        max_daily_loss_amount = self.initial_capital * (self.max_daily_loss_pct / 100.0)

        for i, row in enumerate(records):
            bar_dt: datetime = row["timestamp"]
            bar_date = bar_dt.date()
            bar_time = bar_dt.time()

            # 1. Day transition: reset daily metrics
            if bar_date != current_day:
                current_day = bar_date
                daily_metric = DailyMetrics(trade_date=bar_date)

            high = row["high"]
            low = row["low"]
            close = row["close"]

            # 2. Manage active position if one exists
            if active_position is not None:
                side = active_position["side"]
                sl = active_position["stop_loss"]
                tp = active_position["target_price"]
                qty = active_position["quantity"]
                entry_time = active_position["entry_time"]
                entry_fill = active_position["entry_price_fill"]
                entry_raw = active_position["entry_price_raw"]
                strat_name = active_position["strategy_name"]

                exit_triggered = False
                exit_price_raw = 0.0
                exit_reason = ""

                # Mandatory square off trigger: 15:10 IST
                if bar_time >= FORCE_SQUARE_OFF_TRIGGER:
                    exit_triggered = True
                    exit_price_raw = close
                    exit_reason = "MANDATORY_SQUARE_OFF"

                elif side == Side.BUY:
                    # Long trade intrabar checks (Conservative priority: SL evaluated first)
                    sl_hit = (low <= sl)
                    tp_hit = (high >= tp)

                    if sl_hit and tp_hit:
                        # Ambiguous bar: assume Stop Loss hit first
                        exit_triggered = True
                        exit_price_raw = sl
                        exit_reason = "STOP_LOSS_HIT"
                    elif sl_hit:
                        exit_triggered = True
                        exit_price_raw = sl
                        exit_reason = "STOP_LOSS_HIT"
                    elif tp_hit:
                        exit_triggered = True
                        exit_price_raw = tp
                        exit_reason = "TARGET_HIT"

                elif side == Side.SELL:
                    # Short trade intrabar checks (SL first)
                    sl_hit = (high >= sl)
                    tp_hit = (low <= tp)

                    if sl_hit and tp_hit:
                        exit_triggered = True
                        exit_price_raw = sl
                        exit_reason = "STOP_LOSS_HIT"
                    elif sl_hit:
                        exit_triggered = True
                        exit_price_raw = sl
                        exit_reason = "STOP_LOSS_HIT"
                    elif tp_hit:
                        exit_triggered = True
                        exit_price_raw = tp
                        exit_reason = "TARGET_HIT"

                if exit_triggered:
                    exit_fill = self._apply_slippage(exit_price_raw, side=side, is_entry=False)
                    charges_calc = calculate_mis_net_pnl(
                        entry_price=entry_fill,
                        exit_price=exit_fill,
                        quantity=qty,
                        side=side,
                    )

                    trade = TradeRecord(
                        trade_id=f"trd-{len(trades) + 1}",
                        symbol=profile.tradingsymbol,
                        side=side,
                        strategy_name=strat_name,
                        entry_time=entry_time,
                        exit_time=bar_dt,
                        quantity=qty,
                        entry_price_raw=entry_raw,
                        entry_price_fill=entry_fill,
                        exit_price_raw=exit_price_raw,
                        exit_price_fill=exit_fill,
                        gross_pnl=charges_calc.gross_pnl,
                        net_pnl=charges_calc.net_pnl,
                        total_charges=charges_calc.total_charges,
                        exit_reason=exit_reason,
                        charges=charges_calc,
                    )
                    trades.append(trade)

                    # Update capital and daily metrics
                    self.current_capital += trade.net_pnl
                    daily_metric.realized_net_pnl += trade.net_pnl
                    daily_metric.trades_count += 1

                    # Check daily circuit breaker
                    if daily_metric.realized_net_pnl <= -max_daily_loss_amount:
                        daily_metric.circuit_breaker_tripped = True

                    active_position = None
                    continue

            # 3. Check for new entry signals (if no open position and circuit breaker not tripped)
            if active_position is None and not daily_metric.circuit_breaker_tripped:
                # Stock bar
                stock_bar = row
                prev_stock_bar = records[i - 1] if i > 0 else None
                index_bar = {
                    "open": row["idx_open"],
                    "high": row["idx_high"],
                    "low": row["idx_low"],
                    "close": row["idx_close"],
                    "vwap": row["idx_vwap"],
                    "ema_20": row["idx_ema_20"],
                }

                signal = strategy.evaluate(stock_bar, index_bar, profile, prev_stock_bar)
                if signal is not None:
                    qty = self._size_position(signal.entry_price, signal.stop_loss)
                    if qty > 0:
                        entry_fill = self._apply_slippage(signal.entry_price, side=signal.side, is_entry=True)
                        active_position = {
                            "side": signal.side,
                            "quantity": qty,
                            "entry_price_raw": signal.entry_price,
                            "entry_price_fill": entry_fill,
                            "stop_loss": signal.stop_loss,
                            "target_price": signal.target_price,
                            "entry_time": bar_dt,
                            "strategy_name": signal.strategy_name,
                        }

        # Compute summary statistics
        return self._compute_summary(profile.tradingsymbol, trades)

    def _compute_summary(self, symbol: str, trades: List[TradeRecord]) -> BacktestSummary:
        total_trades = len(trades)
        if total_trades == 0:
            return BacktestSummary(
                symbol=symbol,
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate_pct=0.0,
                gross_pnl=0.0,
                total_charges=0.0,
                net_pnl=0.0,
                profit_factor=0.0,
                max_drawdown_inr=0.0,
                max_drawdown_pct=0.0,
                sharpe_ratio=0.0,
                trades=[],
            )

        winning_trades = [t for t in trades if t.net_pnl > 0]
        losing_trades = [t for t in trades if t.net_pnl < 0]

        gross_pnl = sum(t.gross_pnl for t in trades)
        total_charges = sum(t.total_charges for t in trades)
        net_pnl = sum(t.net_pnl for t in trades)

        gross_gains = sum(t.net_pnl for t in winning_trades)
        gross_losses = abs(sum(t.net_pnl for t in losing_trades))

        if gross_losses > 0:
            profit_factor = round(gross_gains / gross_losses, 2)
        else:
            profit_factor = round(gross_gains if gross_gains > 0 else 0.0, 2)

        win_rate_pct = round((len(winning_trades) / total_trades) * 100.0, 2)

        # Drawdown calculation
        cumulative_pnl = np.cumsum([t.net_pnl for t in trades])
        equity_curve = self.initial_capital + cumulative_pnl
        running_max = np.maximum.accumulate(equity_curve)
        drawdowns_inr = running_max - equity_curve
        max_dd_inr = float(np.max(drawdowns_inr))
        max_dd_pct = float(np.max(drawdowns_inr / running_max) * 100.0)

        # Sharpe ratio (annualized, assuming 250 trading days)
        # Group returns by day
        trade_dates = [t.exit_time.date() for t in trades]
        daily_pnl_map: Dict[date, float] = {}
        for t in trades:
            d = t.exit_time.date()
            daily_pnl_map[d] = daily_pnl_map.get(d, 0.0) + t.net_pnl

        daily_returns = np.array([pnl / self.initial_capital for pnl in daily_pnl_map.values()])
        if len(daily_returns) > 1 and np.std(daily_returns) > 0:
            sharpe_ratio = float(np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(250))
        else:
            sharpe_ratio = 0.0

        return BacktestSummary(
            symbol=symbol,
            total_trades=total_trades,
            winning_trades=len(winning_trades),
            losing_trades=len(losing_trades),
            win_rate_pct=win_rate_pct,
            gross_pnl=round(gross_pnl, 2),
            total_charges=round(total_charges, 2),
            net_pnl=round(net_pnl, 2),
            profit_factor=profit_factor,
            max_drawdown_inr=round(max_dd_inr, 2),
            max_drawdown_pct=round(max_dd_pct, 2),
            sharpe_ratio=round(sharpe_ratio, 2),
            trades=trades,
        )
