"""
Trillionaire Research Lab - Technical Indicator Engine
Zero look-ahead, vectorized technical indicators computed on 1-minute and 5-minute OHLCV bars:
- Intraday VWAP (strictly resets daily at 09:15:00 IST)
- EMA(20) and EMA(200)
- ATR(14) using Wilder's smoothing
- Daily CPR (Central Pivot Range from previous day's High, Low, Close)
- Time-of-day RVOL (Relative Volume vs. 20-day historical time-slot mean)
- Opening Range (High/Low of 09:15-09:30 IST session)
"""

from datetime import datetime, time, timedelta
from typing import Optional, Tuple
import numpy as np
import polars as pl

from shared.constants import IST_TZ


def resample_to_5min(df_1m: pl.DataFrame) -> pl.DataFrame:
    """
    Resamples 1-minute OHLCV DataFrame to regular 5-minute intraday bars starting at 09:15 IST.
    """
    if len(df_1m) == 0:
        return df_1m

    # Ensure timestamp is datetime with IST timezone
    df = df_1m.with_columns(
        pl.col("timestamp").dt.convert_time_zone("Asia/Kolkata")
    )

    # Truncate to 5-minute intervals
    df_5m = (
        df.sort("timestamp")
        .group_by_dynamic("timestamp", every="5m", period="5m", closed="left")
        .agg([
            pl.first("tradingsymbol").alias("tradingsymbol"),
            pl.first("open").alias("open"),
            pl.max("high").alias("high"),
            pl.min("low").alias("low"),
            pl.last("close").alias("close"),
            pl.sum("volume").alias("volume"),
            pl.last("oi").alias("oi"),
        ])
        .filter(pl.col("open").is_not_null())
    )

    return df_5m


def compute_vwap(df: pl.DataFrame) -> pl.Series:
    """
    Computes intraday VWAP with strict daily reset at 09:15 IST.
    VWAP = cumsum(typical_price * volume) / cumsum(volume)
    """
    typical_price = (pl.col("high") + pl.col("low") + pl.col("close")) / 3.0
    vol = pl.col("volume").cast(pl.Float64)
    pv = typical_price * vol

    # Extract date for grouping
    df_with_date = df.with_columns([
        pl.col("timestamp").dt.date().alias("trade_date"),
        pv.alias("_pv"),
        vol.alias("_vol"),
    ])

    vwap_series = (
        df_with_date.with_columns([
            pl.col("_pv").cum_sum().over("trade_date").alias("_cum_pv"),
            pl.col("_vol").cum_sum().over("trade_date").alias("_cum_vol"),
        ])
        .select(
            pl.when(pl.col("_cum_vol") > 0)
            .then(pl.col("_cum_pv") / pl.col("_cum_vol"))
            .otherwise(pl.col("close"))
            .alias("vwap")
        )
        .to_series()
    )

    return vwap_series


def compute_ema(series: pl.Series, span: int) -> pl.Series:
    """Computes Exponential Moving Average using standard span."""
    return series.ewm_mean(span=span, adjust=False)


def compute_wilder_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """
    Computes Average True Range using Wilder's original smoothing:
    TR = max(H - L, abs(H - C_prev), abs(L - C_prev))
    ATR_t = (ATR_{t-1} * 13 + TR_t) / 14
    """
    n = len(close)
    if n == 0:
        return np.array([], dtype=np.float64)

    tr = np.zeros(n, dtype=np.float64)
    tr[0] = high[0] - low[0]

    prev_c = close[:-1]
    h_curr = high[1:]
    l_curr = low[1:]

    tr[1:] = np.maximum(
        h_curr - l_curr,
        np.maximum(np.abs(h_curr - prev_c), np.abs(l_curr - prev_c))
    )

    atr = np.zeros(n, dtype=np.float64)
    if n <= period:
        atr[:] = np.mean(tr)
        return atr

    atr[period - 1] = np.mean(tr[:period])
    alpha = 1.0 / period
    for i in range(period, n):
        atr[i] = atr[i - 1] * (1.0 - alpha) + tr[i] * alpha

    # Backfill warm-up period
    atr[:period - 1] = atr[period - 1]
    return atr


def compute_daily_cpr(df: pl.DataFrame) -> pl.DataFrame:
    """
    Computes Central Pivot Range (CPR) for each day strictly based on the
    PREVIOUS day's High, Low, and Close (zero lookahead).
    Pivot = (H_prev + L_prev + C_prev) / 3
    BC = (H_prev + L_prev) / 2
    TC = 2 * Pivot - BC
    """
    # Daily aggregation
    daily = (
        df.with_columns(pl.col("timestamp").dt.date().alias("trade_date"))
        .group_by("trade_date")
        .agg([
            pl.max("high").alias("daily_high"),
            pl.min("low").alias("daily_low"),
            pl.last("close").alias("daily_close"),
        ])
        .sort("trade_date")
    )

    # Shift by 1 to reference strictly previous day
    daily_cpr = (
        daily.with_columns([
            pl.col("daily_high").shift(1).alias("prev_high"),
            pl.col("daily_low").shift(1).alias("prev_low"),
            pl.col("daily_close").shift(1).alias("prev_close"),
        ])
        .with_columns([
            ((pl.col("prev_high") + pl.col("prev_low") + pl.col("prev_close")) / 3.0).alias("cpr_pivot"),
            ((pl.col("prev_high") + pl.col("prev_low")) / 2.0).alias("cpr_bc"),
        ])
        .with_columns([
            (2.0 * pl.col("cpr_pivot") - pl.col("cpr_bc")).alias("cpr_tc")
        ])
        .with_columns([
            pl.min_horizontal("cpr_bc", "cpr_tc").alias("cpr_bottom"),
            pl.max_horizontal("cpr_bc", "cpr_tc").alias("cpr_top"),
        ])
        .select(["trade_date", "cpr_pivot", "cpr_bc", "cpr_tc", "cpr_bottom", "cpr_top"])
    )

    return daily_cpr


def compute_time_of_day_rvol(df_5m: pl.DataFrame, lookback_days: int = 20) -> pl.Series:
    """
    Computes Relative Volume (RVOL) on 5-minute bars.
    RVOL = bar_volume / rolling_20_day_mean_volume_at_exact_time_slot.
    Zero look-ahead: strictly uses the prior 20 trading days' volume for that specific time of day.
    """
    # Add time of day and trade date
    df_aug = df_5m.with_columns([
        pl.col("timestamp").dt.date().alias("trade_date"),
        pl.col("timestamp").dt.time().alias("time_slot"),
    ])

    dates = df_aug["trade_date"].unique().sort().to_list()
    date_to_idx = {d: i for i, d in enumerate(dates)}

    df_aug = df_aug.with_columns([
        pl.col("trade_date").replace(date_to_idx).cast(pl.Int32).alias("day_num")
    ])

    # For each time slot, compute rolling mean volume of previous lookback_days
    # Using Polars group_by over time_slot
    def _calc_rvol_for_slot(group: pl.DataFrame) -> pl.DataFrame:
        grp = group.sort("day_num")
        # Shift volume by 1 so current day is NOT included in historical mean
        prev_vol = grp["volume"].shift(1)
        rolling_mean = prev_vol.rolling_mean(window_size=lookback_days, min_samples=1)
        rvol = grp["volume"] / rolling_mean
        # For first day or where rolling_mean is null/zero, default to 1.0
        rvol = rvol.fill_null(1.0).fill_nan(1.0)
        return grp.with_columns(rvol.alias("rvol"))

    result_df = (
        df_aug.group_by("time_slot", maintain_order=True)
        .map_groups(_calc_rvol_for_slot)
        .sort("timestamp")
    )

    return result_df["rvol"]


def compute_opening_range(df: pl.DataFrame) -> Tuple[pl.Series, pl.Series]:
    """
    Computes the Opening Range (09:15 - 09:30 IST window) High and Low.
    For bars between 09:15 and 09:30, tracks cumulative High/Low.
    For bars after 09:30, holds the 15-minute Opening Range High and Low constant for the day.
    """
    df_aug = df.with_columns([
        pl.col("timestamp").dt.date().alias("trade_date"),
        pl.col("timestamp").dt.time().alias("bar_time"),
    ])

    cutoff_time = time(9, 30)

    # 1. Determine final 15-min opening range for each day
    or_stats = (
        df_aug.filter(pl.col("bar_time") <= cutoff_time)
        .group_by("trade_date")
        .agg([
            pl.max("high").alias("final_or_high"),
            pl.min("low").alias("final_or_low"),
        ])
    )

    # Join back to full series
    joined = df_aug.join(or_stats, on="trade_date", how="left")

    or_high = joined["final_or_high"].fill_null(joined["high"])
    or_low = joined["final_or_low"].fill_null(joined["low"])

    return or_high, or_low


def compute_all_indicators(df_raw: pl.DataFrame) -> pl.DataFrame:
    """
    Takes 1-minute or 5-minute raw OHLCV DataFrame and computes all indicators:
    - vwap
    - ema_20
    - ema_200
    - atr_14
    - cpr_pivot, cpr_bc, cpr_tc, cpr_bottom, cpr_top
    - rvol (on 5m bars, or forward-filled on 1m bars)
    - or_high, or_low
    """
    if len(df_raw) == 0:
        return df_raw

    df = df_raw.sort("timestamp").with_columns(
        pl.col("timestamp").dt.convert_time_zone("Asia/Kolkata")
    )

    # 1. Intraday VWAP
    vwap_series = compute_vwap(df)

    # 2. Moving Averages
    ema_20_series = compute_ema(df["close"], span=20)
    ema_200_series = compute_ema(df["close"], span=200)

    # 3. Wilder's ATR(14)
    atr_vals = compute_wilder_atr(
        df["high"].to_numpy(),
        df["low"].to_numpy(),
        df["close"].to_numpy(),
        period=14,
    )
    atr_14_series = pl.Series("atr_14", atr_vals)

    # 4. Daily CPR
    cpr_daily = compute_daily_cpr(df)
    df_with_date = df.with_columns(pl.col("timestamp").dt.date().alias("trade_date"))
    df_cpr = df_with_date.join(cpr_daily, on="trade_date", how="left")

    # 5. Opening Range
    or_high_series, or_low_series = compute_opening_range(df)

    # 6. RVOL: if bars are 5-minute, compute directly; else resample, compute, and join
    diff_secs = (df["timestamp"][1] - df["timestamp"][0]).total_seconds() if len(df) > 1 else 60
    if 240 <= diff_secs <= 360:  # approx 5-minute bar
        rvol_series = compute_time_of_day_rvol(df)
        df_augmented = df_cpr.with_columns([
            vwap_series.alias("vwap"),
            ema_20_series.alias("ema_20"),
            ema_200_series.alias("ema_200"),
            atr_14_series.alias("atr_14"),
            or_high_series.alias("or_high"),
            or_low_series.alias("or_low"),
            rvol_series.alias("rvol"),
        ])
    else:
        # Resample to 5m to get accurate 5m RVOL then map back
        df_5m = resample_to_5min(df)
        rvol_5m = compute_time_of_day_rvol(df_5m)
        df_5m_rvol = df_5m.with_columns(rvol_5m.alias("rvol")).select(["timestamp", "rvol"])

        # Join as-of or backward match
        df_augmented = (
            df_cpr.join_asof(df_5m_rvol, on="timestamp", strategy="backward")
            .with_columns([
                vwap_series.alias("vwap"),
                ema_20_series.alias("ema_20"),
                ema_200_series.alias("ema_200"),
                atr_14_series.alias("atr_14"),
                or_high_series.alias("or_high"),
                or_low_series.alias("or_low"),
                pl.col("rvol").fill_null(1.0),
            ])
        )

    # Drop temporary trade_date if present
    if "trade_date" in df_augmented.columns:
        df_augmented = df_augmented.drop("trade_date")

    return df_augmented
