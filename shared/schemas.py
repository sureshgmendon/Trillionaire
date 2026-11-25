"""
Trillionaire Algo Engine - Shared Pydantic Schemas
Strict Pydantic v2 data models for market data, profiling, sentinel signals, and execution.
"""

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.constants import IST_TZ
from shared.enums import (
    ClusterRegime,
    OrderStatus,
    OrderType,
    SentinelStatus,
    Side,
    Timeframe,
)


def _ensure_ist_datetime(v: datetime | str | None) -> datetime | None:
    """Normalize naive or foreign timezone datetimes to IST (Asia/Kolkata)."""
    if v is None:
        return None
    if isinstance(v, str):
        # Handle ISO strings
        dt = datetime.fromisoformat(v)
    elif isinstance(v, datetime):
        dt = v
    else:
        raise ValueError(f"Expected datetime or ISO string, got {type(v)}")

    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST_TZ)
    return dt.astimezone(IST_TZ)


class StrictBaseModel(BaseModel):
    """Base schema enforcing strict extra-attribute rejection and validation."""
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
        use_enum_values=False,
    )


class Tick(StrictBaseModel):
    """Live Level-1 tick packet from Zerodha Kite Ticker."""
    instrument_token: int = Field(..., description="Kite instrument token")
    tradingsymbol: str = Field(..., description="NSE Trading symbol (e.g., RELIANCE)")
    timestamp_ist: datetime = Field(..., description="Tick timestamp normalized to IST")
    ltp: float = Field(..., gt=0, description="Last traded price")
    volume: int = Field(..., ge=0, description="Cumulative volume for the session")
    bid: float = Field(..., ge=0, description="Best bid price")
    ask: float = Field(..., ge=0, description="Best ask price")
    oi: int = Field(default=0, ge=0, description="Open interest")

    @field_validator("timestamp_ist", mode="before")
    @classmethod
    def validate_ist_timestamp(cls, v: datetime | str) -> datetime:
        dt = _ensure_ist_datetime(v)
        if dt is None:
            raise ValueError("timestamp_ist is required")
        return dt


class Candle(StrictBaseModel):
    """OHLCV candlestick bar augmented with technical factors."""
    tradingsymbol: str = Field(..., description="NSE Trading symbol")
    timeframe: Timeframe = Field(..., description="Candle interval (M1, M5, M15)")
    timestamp_ist: datetime = Field(..., description="Candle close timestamp in IST")
    open: float = Field(..., gt=0, description="Open price")
    high: float = Field(..., gt=0, description="High price")
    low: float = Field(..., gt=0, description="Low price")
    close: float = Field(..., gt=0, description="Close price")
    volume: int = Field(..., ge=0, description="Interval traded volume")
    vwap: float = Field(..., gt=0, description="Session Volume-Weighted Average Price")
    ema_200: float = Field(..., gt=0, description="200-period Exponential Moving Average")
    atr_14: float = Field(..., ge=0, description="14-period Average True Range")
    rvol: float = Field(..., ge=0, description="Relative Volume vs 20-day historical mean")

    @field_validator("timestamp_ist", mode="before")
    @classmethod
    def validate_ist_timestamp(cls, v: datetime | str) -> datetime:
        dt = _ensure_ist_datetime(v)
        if dt is None:
            raise ValueError("timestamp_ist is required")
        return dt


class StockProfile(StrictBaseModel):
    """Statistical characterization and regime classification for an instrument."""
    tradingsymbol: str = Field(..., description="NSE Trading symbol")
    instrument_token: int = Field(..., description="Kite instrument token")
    parent_index: str = Field(..., description="Parent Sector Index (e.g., NIFTY IT)")
    index_token: int = Field(..., description="Kite token for parent index")
    cluster_regime: ClusterRegime = Field(..., description="Behavioral cluster classification")
    beta_5y: float = Field(..., description="5-year beta against NIFTY 50")
    adr_pct: float = Field(..., gt=0, description="Average Daily Range percentage (20-day)")
    atr_multiplier_sl: float = Field(..., gt=0, description="ATR multiplier for stop loss sizing")
    atr_multiplier_tp: float = Field(..., gt=0, description="ATR multiplier for take profit target")
    min_rvol_threshold: float = Field(..., gt=0, description="Minimum RVOL threshold for entry")
    oos_sharpe: float = Field(..., description="Out-of-sample Sharpe Ratio from research lab")
    oos_profit_factor: float = Field(..., description="Out-of-sample Profit Factor from research lab")
    is_active: bool = Field(default=True, description="Whether stock is eligible for live scanning")


class NewsSentinelTag(StrictBaseModel):
    """Real-time news/macro risk verdict emitted by the Sentinel system."""
    tradingsymbol: str = Field(..., description="NSE Trading symbol")
    status: SentinelStatus = Field(..., description="Sentinel trade gate verdict")
    reason: str = Field(..., description="Reasoning / news event headline causing the tag")
    risk_multiplier: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Position sizing multiplier (1.0 = Normal, 0.5 = Caution, 0.0 = Lockout)"
    )
    updated_at_ist: datetime = Field(..., description="Time tag was generated or updated in IST")

    @field_validator("updated_at_ist", mode="before")
    @classmethod
    def validate_ist_timestamp(cls, v: datetime | str) -> datetime:
        dt = _ensure_ist_datetime(v)
        if dt is None:
            raise ValueError("updated_at_ist is required")
        return dt


class Signal(StrictBaseModel):
    """High-probability entry/exit trade signal generated by strategy engine."""
    signal_id: str = Field(..., description="Globally unique signal identifier (UUID)")
    tradingsymbol: str = Field(..., description="Target NSE Trading symbol")
    parent_index: str = Field(..., description="Parent index of the instrument")
    side: Side = Field(..., description="Trade direction (BUY or SELL)")
    entry_price: float = Field(..., gt=0, description="Suggested limit entry price")
    stop_loss: float = Field(..., gt=0, description="Initial hard stop loss level")
    target_price: float = Field(..., gt=0, description="Initial take profit target level")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Strategy confluence score [0, 1]")
    strategy_name: str = Field(..., description="Strategy name producing the signal")
    generated_at_ist: datetime = Field(..., description="Signal emission timestamp in IST")

    @field_validator("generated_at_ist", mode="before")
    @classmethod
    def validate_ist_timestamp(cls, v: datetime | str) -> datetime:
        dt = _ensure_ist_datetime(v)
        if dt is None:
            raise ValueError("generated_at_ist is required")
        return dt


class Order(StrictBaseModel):
    """Zerodha Kite execution order representation."""
    order_id: str = Field(..., description="Internal system order identifier (UUID)")
    broker_order_id: Optional[str] = Field(default=None, description="Order ID returned by Kite Connect")
    signal_id: Optional[str] = Field(default=None, description="Associated strategy signal ID")
    tradingsymbol: str = Field(..., description="NSE Trading symbol")
    side: Side = Field(..., description="BUY or SELL")
    product: str = Field(default="MIS", description="Zerodha product code (MIS for intraday margin)")
    order_type: OrderType = Field(..., description="LIMIT or SL_LIMIT")
    quantity: int = Field(..., gt=0, description="Order quantity in shares")
    filled_quantity: int = Field(default=0, ge=0, description="Executed quantity")
    limit_price: Optional[float] = Field(default=None, gt=0, description="Limit price for execution")
    trigger_price: Optional[float] = Field(default=None, gt=0, description="Trigger price for stop loss limit")
    average_fill_price: Optional[float] = Field(default=None, ge=0, description="Volume-weighted execution price")
    status: OrderStatus = Field(default=OrderStatus.PENDING_NEW, description="Lifecycle order status")
    exit_reason: Optional[str] = Field(default=None, description="Trade exit reason (TARGET_HIT, SL_HIT, FLATTEN_ALL, MANUAL_EXIT, EOD_SQUARE_OFF)")
    rejection_reason: Optional[str] = Field(default=None, description="Rejection reason from broker or sanity guard")
    sl_price: Optional[float] = Field(default=None, gt=0, description="Planned stop loss level")
    target_price: Optional[float] = Field(default=None, gt=0, description="Planned target level")
    is_exit: bool = Field(default=False, description="Whether order is an exit leg")
    client_order_tag: Optional[str] = Field(default=None, description="Unique client idempotency tag")
    created_at_ist: datetime = Field(..., description="Creation timestamp in IST")

    @field_validator("created_at_ist", mode="before")
    @classmethod
    def validate_ist_timestamp(cls, v: datetime | str) -> datetime:
        dt = _ensure_ist_datetime(v)
        if dt is None:
            raise ValueError("created_at_ist is required")
        return dt


class Position(StrictBaseModel):
    """Active intraday position tracking market exposure and PnL."""
    tradingsymbol: str = Field(..., description="NSE Trading symbol")
    side: Side = Field(..., description="BUY (Long) or SELL (Short)")
    quantity: int = Field(..., gt=0, description="Open share quantity")
    average_entry_price: float = Field(..., gt=0, description="Average execution entry price")
    ltp: float = Field(..., gt=0, description="Current Last Traded Price")
    unrealized_pnl: float = Field(default=0.0, description="Current mark-to-market unrealized PnL in INR")
    realized_pnl: float = Field(default=0.0, description="Closed / realized PnL in INR")
    stop_loss: float = Field(..., gt=0, description="Active trailing or hard stop loss price")
    target_price: float = Field(..., gt=0, description="Active profit target price")
    entry_time: Optional[datetime] = Field(default=None, description="Entry execution timestamp in IST")
    entry_order_id: Optional[str] = Field(default=None, description="Entry order ID")
    sl_order_id: Optional[str] = Field(default=None, description="Paired OCO SL exit order ID")
    tp_order_id: Optional[str] = Field(default=None, description="Paired OCO Target exit order ID")

    @field_validator("entry_time", mode="before")
    @classmethod
    def validate_entry_time(cls, v: datetime | str | None) -> datetime | None:
        return _ensure_ist_datetime(v)


class ClosedTrade(StrictBaseModel):
    """Immutable executed round-trip trade record persisted to daily ledger."""
    order_id: str = Field(..., description="Exit order ID for this closed trade")
    symbol: str = Field(..., description="NSE Trading symbol")
    product: str = Field(default="MIS", description="Product code (MIS)")
    side: Side = Field(..., description="Original entry side (BUY or SELL)")
    filled_qty: int = Field(..., gt=0, description="Executed exit quantity")
    entry_price: float = Field(..., gt=0, description="Average entry price")
    sl_price: float = Field(..., gt=0, description="Stop loss price")
    target_price: float = Field(..., gt=0, description="Target price")
    actual_exit_price: float = Field(..., gt=0, description="Actual exit execution price")
    realized_pnl: float = Field(..., description="Net realized PnL in INR")
    pnl_pct: float = Field(..., description="Return percentage")
    exit_reason: str = Field(..., description="TARGET_HIT, SL_HIT, FLATTEN_ALL, MANUAL_EXIT, or EOD_SQUARE_OFF")
    entry_time: datetime = Field(..., description="Entry execution timestamp in IST")
    exit_time: datetime = Field(..., description="Exit execution timestamp in IST")

    @field_validator("entry_time", "exit_time", mode="before")
    @classmethod
    def validate_ist_timestamps(cls, v: datetime | str) -> datetime:
        dt = _ensure_ist_datetime(v)
        if dt is None:
            raise ValueError("entry_time and exit_time are required")
        return dt
