"""
Trillionaire Research Lab - Indian MIS Statutory Charge & Net PnL Calculator
Computes exact Zerodha Equity Intraday (MIS) statutory transaction costs down to the paisa.
"""

from typing import Union
from pydantic import BaseModel, ConfigDict, Field

from shared.constants import (
    BROKERAGE_CAP_INR,
    BROKERAGE_PCT,
    GST_RATE,
    NSE_EXCHANGE_TURNOVER_RATE,
    SEBI_TURNOVER_RATE,
    STAMP_DUTY_BUY_RATE,
    STT_INTRADAY_SELL_RATE,
)
from shared.enums import Side


class ChargesBreakdown(BaseModel):
    """Granular statutory charges and net profit breakdown for an intraday MIS trade."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    side: Side = Field(..., description="Trade side: BUY (Long) or SELL (Short)")
    entry_price: float = Field(..., description="Trade entry execution price")
    exit_price: float = Field(..., description="Trade exit execution price")
    quantity: int = Field(..., description="Number of shares traded")
    buy_turnover: float = Field(..., description="Total buy leg turnover in INR")
    sell_turnover: float = Field(..., description="Total sell leg turnover in INR")
    total_turnover: float = Field(..., description="Cumulative trade turnover in INR")
    gross_pnl: float = Field(..., description="Gross profit/loss before charges in INR")
    brokerage: float = Field(..., description="Zerodha brokerage (capped at Rs. 20 per executed leg)")
    stt: float = Field(..., description="Securities Transaction Tax (0.025% on sell leg only)")
    exchange_txn: float = Field(..., description="NSE exchange transaction charge (0.00297% on turnover)")
    sebi_charges: float = Field(..., description="SEBI turnover fees (Rs. 10 per crore)")
    stamp_duty: float = Field(..., description="State stamp duty (0.003% on buy leg only)")
    gst: float = Field(..., description="GST at 18% on (Brokerage + Exchange + SEBI)")
    total_charges: float = Field(..., description="Sum of all statutory charges & brokerage")
    net_pnl: float = Field(..., description="Net realized profit/loss after all costs")
    breakeven_points: float = Field(..., description="Price move per share required to cover costs")


def calculate_mis_net_pnl(
    entry_price: float,
    exit_price: float,
    quantity: int,
    side: Union[Side, str],
) -> ChargesBreakdown:
    """
    Computes exact Zerodha MIS intraday charges and net PnL.

    Parameters
    ----------
    entry_price : float
        Price at which the position was entered.
    exit_price : float
        Price at which the position was exited.
    quantity : int
        Traded share count.
    side : Side | str
        Side.BUY for Long (Buy entry, Sell exit).
        Side.SELL for Short (Sell entry, Buy exit).

    Returns
    -------
    ChargesBreakdown
        Detailed Pydantic model with exact charge breakdowns rounded to 2 decimal places.
    """
    if isinstance(side, str):
        side = Side(side)

    if quantity <= 0:
        raise ValueError(f"Quantity must be positive, got {quantity}")
    if entry_price <= 0 or exit_price <= 0:
        raise ValueError(f"Prices must be positive: entry={entry_price}, exit={exit_price}")

    if side == Side.BUY:
        # Long trade: Entry = Buy, Exit = Sell
        buy_turnover = entry_price * quantity
        sell_turnover = exit_price * quantity
        gross_pnl = (exit_price - entry_price) * quantity
        entry_turnover = buy_turnover
        exit_turnover = sell_turnover
    else:
        # Short trade: Entry = Sell, Exit = Buy
        sell_turnover = entry_price * quantity
        buy_turnover = exit_price * quantity
        gross_pnl = (entry_price - exit_price) * quantity
        entry_turnover = sell_turnover
        exit_turnover = buy_turnover

    total_turnover = buy_turnover + sell_turnover

    # 1. Brokerage: min(20.0, 0.03% * turnover) per executed leg
    brokerage_entry = min(BROKERAGE_CAP_INR, BROKERAGE_PCT * entry_turnover)
    brokerage_exit = min(BROKERAGE_CAP_INR, BROKERAGE_PCT * exit_turnover)
    brokerage = brokerage_entry + brokerage_exit

    # 2. STT: 0.025% on sell side only for equity intraday
    stt = STT_INTRADAY_SELL_RATE * sell_turnover

    # 3. NSE Exchange Transaction Charges: 0.00297% on total turnover
    exchange_txn = NSE_EXCHANGE_TURNOVER_RATE * total_turnover

    # 4. SEBI Turnover Charges: Rs 10 per crore (0.0001% or 0.000001)
    sebi_charges = SEBI_TURNOVER_RATE * total_turnover

    # 5. Stamp Duty: 0.003% on buy side only
    stamp_duty = STAMP_DUTY_BUY_RATE * buy_turnover

    # 6. GST: 18% on (Brokerage + Exchange Charges + SEBI Charges)
    gst = GST_RATE * (brokerage + exchange_txn + sebi_charges)

    total_charges = brokerage + stt + exchange_txn + sebi_charges + stamp_duty + gst
    net_pnl = gross_pnl - total_charges
    breakeven_points = total_charges / quantity

    return ChargesBreakdown(
        side=side,
        entry_price=round(entry_price, 2),
        exit_price=round(exit_price, 2),
        quantity=quantity,
        buy_turnover=round(buy_turnover, 2),
        sell_turnover=round(sell_turnover, 2),
        total_turnover=round(total_turnover, 2),
        gross_pnl=round(gross_pnl, 2),
        brokerage=round(brokerage, 2),
        stt=round(stt, 2),
        exchange_txn=round(exchange_txn, 2),
        sebi_charges=round(sebi_charges, 2),
        stamp_duty=round(stamp_duty, 2),
        gst=round(gst, 2),
        total_charges=round(total_charges, 2),
        net_pnl=round(net_pnl, 2),
        breakeven_points=round(breakeven_points, 4),
    )
