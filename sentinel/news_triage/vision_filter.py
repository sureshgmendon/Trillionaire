"""
Trillionaire Pre-Order Gemini Vision Chart Trap Filter
Invoked strictly AFTER LiveStrategyRunner confirms a dual-confirmation signal (never per 5m bar).
Renders an in-memory dark-mode PNG chart of the last 40 M5 candles + VWAP + EMA(200) + Opening Range
levels and asks Hydrogen (or gemini-3.8-flash fallback) within a strict 3.5s timeout to veto obvious bull/bear traps.
Fails open (approved=True) on missing key, rate limits, errors, or timeout so execution is never delayed.
"""

import asyncio
import base64
from io import BytesIO
import json
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image, ImageDraw
from loguru import logger

from config.settings import settings
from sentinel.news_triage.gemini_client import call_gemini_async
from shared.enums import Side
from shared.schemas import Candle, Signal


def render_m5_chart_png(
    candles: List[Candle],
    symbol: str,
    side: Side,
    or_high: Optional[float] = None,
    or_low: Optional[float] = None,
    width: int = 640,
    height: int = 320,
) -> bytes:
    """
    Renders last 40 M5 candles + VWAP + EMA(200) + Opening Range levels purely in memory to PNG bytes.
    Zero disk writes.
    """
    bars = (candles or [])[-40:]
    img = Image.new("RGB", (width, height), (9, 13, 22))  # #090D16 cockpit dark background
    draw = ImageDraw.Draw(img)

    pad_left, pad_right, pad_top, pad_bottom = 16, 62, 28, 18
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    # Header badge
    draw.text(
        (pad_left, 8),
        f"{symbol} M5 | Candidate {side.value} | Last {len(bars)} Bars",
        fill=(226, 232, 240),
    )

    if not bars:
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    prices: List[float] = []
    for b in bars:
        prices.extend([float(b.high), float(b.low)])
        if b.vwap:
            prices.append(float(b.vwap))
        if b.ema_200:
            prices.append(float(b.ema_200))
    if or_high:
        prices.append(float(or_high))
    if or_low:
        prices.append(float(or_low))

    p_min = min(prices)
    p_max = max(prices)
    p_span = max(0.05, (p_max - p_min) * 1.1)
    p_low = p_min - 0.05 * p_span

    def _y(price: float) -> int:
        ratio = (price - p_low) / p_span
        y = pad_top + plot_h - int(ratio * plot_h)
        return max(pad_top, min(pad_top + plot_h, y))

    # Subtle horizontal gridlines
    for frac in (0.2, 0.5, 0.8):
        gy = int(pad_top + plot_h * frac)
        draw.line([(pad_left, gy), (pad_left + plot_w, gy)], fill=(31, 43, 68), width=1)

    # Opening Range High / Low levels
    if or_high:
        yh = _y(float(or_high))
        draw.line([(pad_left, yh), (pad_left + plot_w, yh)], fill=(129, 140, 248), width=1)
        draw.text((pad_left + plot_w + 4, yh - 6), "ORH", fill=(129, 140, 248))
    if or_low:
        yl = _y(float(or_low))
        draw.line([(pad_left, yl), (pad_left + plot_w, yl)], fill=(129, 140, 248), width=1)
        draw.text((pad_left + plot_w + 4, yl - 6), "ORL", fill=(129, 140, 248))

    # Draw candlesticks + VWAP / EMA200 curves
    n = len(bars)
    step = plot_w / max(n, 1)
    body_w = max(3, int(step * 0.58))

    vwap_pts: List[Tuple[int, int]] = []
    ema_pts: List[Tuple[int, int]] = []

    for idx, bar in enumerate(bars):
        cx = int(pad_left + (idx + 0.5) * step)
        yo, yh, yl, yc = _y(bar.open), _y(bar.high), _y(bar.low), _y(bar.close)
        bullish = bar.close >= bar.open
        color = (16, 185, 129) if bullish else (244, 63, 94)

        draw.line([(cx, yh), (cx, yl)], fill=color, width=1)
        top_b, bot_b = min(yo, yc), max(yo, yc)
        if bot_b == top_b:
            bot_b = top_b + 1
        half_w = body_w // 2
        draw.rectangle([cx - half_w, top_b, cx + half_w, bot_b], fill=color)

        if bar.vwap:
            vwap_pts.append((cx, _y(float(bar.vwap))))
        if bar.ema_200:
            ema_pts.append((cx, _y(float(bar.ema_200))))

    if len(vwap_pts) >= 2:
        draw.line(vwap_pts, fill=(6, 182, 212), width=2)  # Cyan VWAP
    if len(ema_pts) >= 2:
        draw.line(ema_pts, fill=(245, 158, 11), width=2)  # Amber EMA200

    draw.text((width - pad_right + 4, pad_top), f"{p_max:.1f}", fill=(148, 163, 184))
    draw.text((width - pad_right + 4, pad_top + plot_h - 10), f"{p_min:.1f}", fill=(148, 163, 184))

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class VisionTrapFilter:
    """Multimodal Gemini Vision chart trap screener with strict fail-open latency protection."""

    def __init__(self, timeout_sec: float = 3.5):
        self.timeout_sec = timeout_sec

    async def evaluate_signal(
        self,
        signal: Signal,
        candles: List[Candle],
        or_levels: Optional[Tuple[float, float]] = None,
        timeout_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Evaluates candidate signal chart PNG against Hydrogen / gemini-3.8-flash within timeout_sec (default 3.5s).
        Fails open (approved=True, confidence=0.5) on missing API key, timeout, rate limit, or error.
        """
        if not getattr(settings, "enable_vision_filter", True):
            return {
                "approved": True,
                "confidence": 1.0,
                "reason": "Bypassed: Vision filter disabled in Settings & Keys",
                "fail_open": True,
                "model": None,
            }
        effective_timeout = timeout_sec or self.timeout_sec
        key = (settings.gemini_api_key or "").strip()
        if not key or key.startswith("your_"):
            return {
                "approved": True,
                "confidence": 0.0,
                "reason": "Bypassed: GEMINI_API_KEY not configured",
                "fail_open": True,
                "model": None,
            }

        or_high, or_low = or_levels if or_levels else (None, None)
        try:
            png_bytes = render_m5_chart_png(
                candles=candles,
                symbol=signal.tradingsymbol,
                side=signal.side,
                or_high=or_high,
                or_low=or_low,
            )
            b64_png = base64.b64encode(png_bytes).decode("ascii")
        except Exception as e:
            logger.warning(f"[VISION FILTER] Chart render skipped for {signal.tradingsymbol}: {e}")
            return {
                "approved": True,
                "confidence": 0.5,
                "reason": f"Fail-open: chart render error ({e})",
                "fail_open": True,
                "model": None,
            }

        prompt = (
            f"Evaluate this NSE intraday M5 candlestick chart for a proposed {signal.side.value} "
            f"entry on {signal.tradingsymbol} at Rs.{signal.entry_price:.2f} "
            f"(SL: Rs.{signal.stop_loss:.2f}, TP: Rs.{signal.target_price:.2f}, "
            f"Strategy: {signal.strategy_name}). "
            "Cyan=VWAP, Amber=EMA200, Indigo=ORH/ORL. "
            "Reject (approved=false) ONLY if there is an obvious bull/bear trap such as buying directly "
            "into double-top resistance or parabolic vertical exhaustion / knife-catch breakdown. "
            'Respond strictly with JSON: {"approved": boolean, "confidence": float, "reason": string}.'
        )
        parts = [
            {"text": prompt},
            {"inlineData": {"mimeType": "image/png", "data": b64_png}},
        ]

        try:
            res = await asyncio.wait_for(
                call_gemini_async(
                    parts=parts,
                    model_candidates=("hydrogen", "gemini-3.8-flash"),
                    timeout_sec=effective_timeout,
                    api_key=key,
                ),
                timeout=effective_timeout,
            )
            if res.get("ok") and isinstance(res.get("data"), dict):
                data = res["data"]
                approved = bool(data.get("approved", True))
                confidence = float(data.get("confidence", 0.7))
                reason = str(data.get("reason") or ("Clean chart structure" if approved else "Chart trap veto"))
                return {
                    "approved": approved,
                    "confidence": max(0.0, min(1.0, round(confidence, 2))),
                    "reason": reason,
                    "fail_open": False,
                    "model": res.get("model"),
                }

            return {
                "approved": True,
                "confidence": 0.5,
                "reason": f"Fail-open: {res.get('error', 'Vision response unavailable')}",
                "fail_open": True,
                "model": "hydrogen",
            }
        except asyncio.TimeoutError:
            logger.warning(
                f"[VISION FILTER] Timeout (>{effective_timeout:.1f}s) on {signal.tradingsymbol} — failing open"
            )
            return {
                "approved": True,
                "confidence": 0.5,
                "reason": f"Fail-open: Hydrogen Vision timed out (>{effective_timeout:.1f}s)",
                "fail_open": True,
                "model": "hydrogen",
            }
        except Exception as exc:
            logger.warning(f"[VISION FILTER] Error on {signal.tradingsymbol} ({exc}) — failing open")
            return {
                "approved": True,
                "confidence": 0.5,
                "reason": f"Fail-open: {exc}",
                "fail_open": True,
                "model": "hydrogen",
            }
