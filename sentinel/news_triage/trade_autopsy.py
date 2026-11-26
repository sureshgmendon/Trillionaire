"""
Trillionaire Post-Market Gemini "Trade Autopsy" Coach (03:35 PM IST)
Analyzes daily ClosedTrade outcomes, funnel conversion, and realized MTM to produce:
  (a) Session Scorecard (Win Rate, Profit Factor, Net Post-Tax PnL)
  (b) Loss & Trap Diagnosis (time-of-day clustering, regime misfires, tight ATR stops)
  (c) Actionable Parameter Recommendations for the next session
Persists reports to Redis + sentinel/artifacts/latest_trade_autopsy.json and notifies Telegram.
"""

from collections import Counter
from datetime import date, datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

from config.settings import settings
from sentinel.news_triage.gemini_client import call_gemini_async
from shared.constants import IST_TZ
from shared.schemas import ClosedTrade

DEFAULT_AUTOPSY_ARTIFACT = Path(__file__).resolve().parents[1] / "artifacts" / "latest_trade_autopsy.json"


def _resolve_autopsy_path(artifact_path: Optional[Path] = None) -> Path:
    if artifact_path is not None:
        return artifact_path
    env_dir = os.environ.get("TRILLIONAIRE_STATE_DIR")
    if env_dir:
        return Path(env_dir) / "latest_trade_autopsy.json"
    return DEFAULT_AUTOPSY_ARTIFACT


def _bucket_ist_session(dt_val: Any) -> str:
    try:
        dt = dt_val if isinstance(dt_val, datetime) else datetime.fromisoformat(str(dt_val))
        dt_ist = dt.astimezone(IST_TZ) if dt.tzinfo else dt.replace(tzinfo=IST_TZ)
        mins = dt_ist.hour * 60 + dt_ist.minute
        if mins < 11 * 60:
            return "Morning (09:15-11:00)"
        if mins < 13 * 60 + 30:
            return "Midday (11:00-13:30)"
        return "Afternoon (13:30-15:15)"
    except Exception:
        return "Unknown"


def compute_session_scorecard(
    closed_trades: List[ClosedTrade | Dict[str, Any]],
    funnel_metrics: Optional[Dict[str, int]],
    daily_mtm: float,
    realized_pnl: float,
    allocated_capital: float = 100000.0,
) -> Dict[str, Any]:
    """Computes exact deterministic intraday session scorecard metrics."""
    normalized: List[Dict[str, Any]] = [
        t.model_dump(mode="json") if isinstance(t, ClosedTrade) else dict(t)
        for t in (closed_trades or [])
    ]

    total = len(normalized)
    winners = [t for t in normalized if float(t.get("realized_pnl", 0.0)) > 0]
    losers = [t for t in normalized if float(t.get("realized_pnl", 0.0)) < 0]
    breakevens = total - len(winners) - len(losers)

    gross_win = round(sum(float(t.get("realized_pnl", 0.0)) for t in winners), 2)
    gross_loss = round(abs(sum(float(t.get("realized_pnl", 0.0)) for t in losers)), 2)

    win_rate_pct = round((len(winners) / total) * 100.0, 2) if total > 0 else 0.0
    if gross_loss > 0:
        profit_factor = round(gross_win / gross_loss, 2)
    elif gross_win > 0:
        profit_factor = 99.0
    else:
        profit_factor = 0.0

    exit_reasons = Counter(str(t.get("exit_reason", "UNKNOWN")) for t in normalized)
    bucket_pnl: Dict[str, float] = {}
    bucket_losses: Dict[str, int] = {}
    for t in normalized:
        bucket = _bucket_ist_session(t.get("entry_time"))
        pnl_val = float(t.get("realized_pnl", 0.0))
        bucket_pnl[bucket] = round(bucket_pnl.get(bucket, 0.0) + pnl_val, 2)
        if pnl_val < 0:
            bucket_losses[bucket] = bucket_losses.get(bucket, 0) + 1

    return {
        "total_trades": total,
        "winning_trades": len(winners),
        "losing_trades": len(losers),
        "breakeven_trades": breakevens,
        "win_rate_pct": win_rate_pct,
        "profit_factor": profit_factor,
        "gross_profit_inr": gross_win,
        "gross_loss_inr": gross_loss,
        "net_post_tax_pnl_inr": round(float(realized_pnl), 2),
        "daily_mtm_inr": round(float(daily_mtm), 2),
        "return_pct": round((float(daily_mtm) / allocated_capital) * 100.0, 2) if allocated_capital > 0 else 0.0,
        "exit_reason_breakdown": dict(exit_reasons),
        "time_of_day_pnl": bucket_pnl,
        "time_of_day_losses": bucket_losses,
        "funnel_metrics": dict(funnel_metrics or {}),
    }


def _build_suggested_toggle_actions(
    scorecard: Dict[str, Any],
    closed_trades: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Maps quantitative session diagnostics directly onto persisted Settings & Keys toggles
    for instant operator action.
    """
    actions: List[Dict[str, Any]] = []
    losers_by_slot = scorecard.get("time_of_day_losses", {})
    exit_reasons = scorecard.get("exit_reason_breakdown", {})
    sl_hits = int(exit_reasons.get("SL_HIT", 0))
    tp_hits = int(exit_reasons.get("TARGET_HIT", 0))
    total = int(scorecard.get("total_trades", 0))
    pf = float(scorecard.get("profit_factor", 0.0))

    morning_losses = losers_by_slot.get("Morning (09:15-11:00)", 0)
    midday_losses = losers_by_slot.get("Midday (11:00-13:30)", 0)

    if midday_losses > 0:
        actions.append({
            "action": "TURN ON: Lunch Volume Filter (RVOL >= 2.0)",
            "setting_key": "enable_lunch_volume_filter",
            "recommended_value": True,
            "reason": f"{midday_losses} losing trade(s) during midday window; require RVOL >= 2.0 between 11:45 and 13:15 IST.",
        })

    if morning_losses > 0:
        actions.append({
            "action": "SET START TIME: 09:30 IST",
            "setting_key": "entry_start_time_ist",
            "recommended_value": "09:30",
            "reason": f"{morning_losses} losing trade(s) in morning volatility; defer entries until 09:30 IST opening stabilization.",
        })

    if sl_hits > tp_hits or (total > 0 and pf < 1.0):
        actions.append({
            "action": "TURN ON: Profit-Only Trade Manager",
            "setting_key": "enable_profit_trade_manager",
            "recommended_value": True,
            "reason": "Step SL 50% closer to Entry at >=50% to TP and lock TP on strong RVOL extensions.",
        })
        actions.append({
            "action": "TURN ON: 50% Partial Profit Booking",
            "setting_key": "enable_partial_booking",
            "recommended_value": True,
            "reason": "Bank 50% quantity at +1R and shift runner SL to breakeven to protect unrealized gains.",
        })
        actions.append({
            "action": "TURN OFF: Mean Reversion",
            "setting_key": "enable_mean_reversion_strategy",
            "recommended_value": False,
            "reason": "Pause counter-trend VWAP+CPR fades while intraday directional stops dominate.",
        })
    elif total == 0:
        actions.append({
            "action": "SET START TIME: 09:25 IST",
            "setting_key": "entry_start_time_ist",
            "recommended_value": "09:25",
            "reason": "Keep 09:25 IST start armed to catch early opening range breakouts.",
        })
    else:
        actions.append({
            "action": "TURN ON: Gemini Vision Chart Filter",
            "setting_key": "enable_vision_filter",
            "recommended_value": True,
            "reason": "Maintain pre-order M5 bull/bear trap screening alongside profitable setups.",
        })

    return actions


def _build_rule_based_autopsy(
    scorecard: Dict[str, Any],
    closed_trades: List[Dict[str, Any]],
    trade_date: date,
) -> Dict[str, Any]:
    """Deterministic post-market coaching fallback when Gemini API key is unset or unavailable."""
    diagnoses: List[str] = []
    recs: List[str] = []

    total = scorecard["total_trades"]
    sl_hits = scorecard["exit_reason_breakdown"].get("SL_HIT", 0)
    tp_hits = scorecard["exit_reason_breakdown"].get("TARGET_HIT", 0)
    losers_by_slot = scorecard.get("time_of_day_losses", {})
    funnel = scorecard.get("funnel_metrics", {})

    if total == 0:
        diagnoses.append(
            f"Zero closed trades executed on {trade_date.isoformat()}. Funnel evaluated "
            f"{funnel.get('candles_evaluated', 0)} M5 candles ({funnel.get('signals_emitted', 0)} signals)."
        )
        recs.append("Inspect Sector Filter / RVOL thresholds if zero setups fired during active trend days.")
    else:
        if sl_hits > tp_hits:
            diagnoses.append(
                f"Elevated stop-out ratio ({sl_hits} SL_HIT vs {tp_hits} TARGET_HIT out of {total} trades) suggests stops are sitting inside noise."
            )
            recs.append("Widen ATR SL multiplier by +0.2x on choppy names or tighten RVOL filter before breakout entries.")
        else:
            diagnoses.append(
                f"Constructive exit distribution ({tp_hits} TARGET_HIT vs {sl_hits} SL_HIT) with win rate {scorecard['win_rate_pct']}%."
            )

        if losers_by_slot:
            worst_slot = max(losers_by_slot.items(), key=lambda kv: kv[1])
            if worst_slot[1] > 0:
                diagnoses.append(f"Highest loss clustering occurred in {worst_slot[0]} ({worst_slot[1]} losing trade(s)).")
                if "Morning" in worst_slot[0]:
                    recs.append("Consider waiting until 09:30 IST after initial opening-bell spread spikes settle.")
                elif "Midday" in worst_slot[0]:
                    recs.append("Tighten TREND_MOMENTUM sector alignment threshold during midday chop (11:30-13:30 IST).")

        if scorecard["profit_factor"] < 1.0:
            recs.append("Review bottom-performing symbols and enforce Vision chart trap screening on counter-trend bounces.")
        else:
            recs.append("Preserve current risk-per-trade sizing and allow +1R breakeven trailing stops to capture trend extensions.")

    toggle_actions = _build_suggested_toggle_actions(scorecard, closed_trades)

    md = "\n".join([
        f"## Post-Market Trade Autopsy ({trade_date.isoformat()})",
        "",
        "### (a) Session Scorecard",
        f"- **Net Post-Tax PnL:** Rs.{scorecard['net_post_tax_pnl_inr']:,.2f} ({scorecard['return_pct']:+.2f}%)",
        f"- **Trades / Win Rate / PF:** {total} trades | {scorecard['win_rate_pct']}% win rate | PF {scorecard['profit_factor']}",
        "",
        "### (b) Loss & Trap Diagnosis",
        *[f"- {d}" for d in diagnoses],
        "",
        "### (c) Actionable Parameter Recommendations",
        *[f"- {r}" for r in recs],
        "",
        "### (d) Suggested Toggle Actions",
        *[f"- {a['action']}: {a['reason']}" for a in toggle_actions],
    ])

    return {
        "loss_and_trap_diagnosis": diagnoses,
        "parameter_recommendations": recs,
        "suggested_toggle_actions": toggle_actions,
        "markdown_report": md,
    }


def _resolve_history_path(artifact_path: Optional[Path] = None) -> Path:
    base = _resolve_autopsy_path(artifact_path)
    return base.parent / "latest_trade_autopsy_history.json"


async def generate_daily_trade_autopsy(
    closed_trades: List[ClosedTrade | Dict[str, Any]],
    funnel_metrics: Optional[Dict[str, int]] = None,
    daily_mtm: float = 0.0,
    realized_pnl: float = 0.0,
    allocated_capital: float = 100000.0,
    trade_date: Optional[date] = None,
    send_telegram: bool = True,
    enable_redis: bool = True,
    artifact_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Generates structured JSON + Markdown post-market Trade Autopsy coaching report,
    persists it to Redis & disk artifact, and broadcasts summary over Telegram if configured.
    """
    now_ist = datetime.now(IST_TZ)
    target_date = trade_date or now_ist.date()

    serialized_trades = [
        t.model_dump(mode="json") if isinstance(t, ClosedTrade) else dict(t)
        for t in (closed_trades or [])
    ]
    scorecard = compute_session_scorecard(
        closed_trades=serialized_trades,
        funnel_metrics=funnel_metrics,
        daily_mtm=daily_mtm,
        realized_pnl=realized_pnl,
        allocated_capital=allocated_capital,
    )

    prompt = (
        "You are an institutional NSE intraday quantitative trading coach reviewing today's closed trades.\n"
        f"Trade Date (IST): {target_date.isoformat()}\n"
        f"Computed Scorecard: {json.dumps(scorecard)}\n"
        f"Closed Trades Ledger: {json.dumps(serialized_trades[-40:])}\n\n"
        "Return strict JSON with keys:\n"
        '  "loss_and_trap_diagnosis": array of 2-4 specific concise strings analyzing time-of-day clustering, regime misfires, or tight ATR stops,\n'
        '  "parameter_recommendations": array of 2-4 concrete actionable recommendations,\n'
        '  "suggested_toggle_actions": array of 1-4 objects { "action": "TURN ON: ... | TURN OFF: ... | SET START TIME: ...", "setting_key": "enable_trend_strategy|enable_mean_reversion_strategy|enable_rvol_breakout_strategy|enable_profit_trade_manager|enable_partial_booking|enable_lunch_volume_filter|enable_vision_filter|entry_start_time_ist", "recommended_value": true|false|"09:25"|"09:30", "reason": "brief justification" },\n'
        '  "markdown_report": concise Markdown string covering (a) Session Scorecard, (b) Loss & Trap Diagnosis, and (c) Actionable Parameter Recommendations.\n'
    )

    ai_res = await call_gemini_async(
        parts=[{"text": prompt}],
        model_candidates=("gemini-3.8-flash", "gemini-2.5-pro"),
        timeout_sec=15.0,
    )

    if ai_res.get("ok") and isinstance(ai_res.get("data"), dict):
        data = ai_res["data"]
        diagnosis = data.get("loss_and_trap_diagnosis") or []
        recommendations = data.get("parameter_recommendations") or []
        toggle_actions = data.get("suggested_toggle_actions") or _build_suggested_toggle_actions(scorecard, serialized_trades)
        markdown_report = str(data.get("markdown_report") or "")
        source = f"gemini:{ai_res.get('model')}"
    else:
        fallback = _build_rule_based_autopsy(scorecard, serialized_trades, target_date)
        diagnosis = fallback["loss_and_trap_diagnosis"]
        recommendations = fallback["parameter_recommendations"]
        toggle_actions = fallback["suggested_toggle_actions"]
        markdown_report = fallback["markdown_report"]
        source = "rule_based_fallback"

    ist_time_label = f"{now_ist.strftime('%Y-%m-%d')} • {now_ist.strftime('%H:%M')} IST"
    report_entry = {
        "trade_date": target_date.isoformat(),
        "generated_at_ist": now_ist.isoformat(),
        "generated_time_label": ist_time_label,
        "source": source,
        "session_scorecard": scorecard,
        "loss_and_trap_diagnosis": diagnosis,
        "parameter_recommendations": recommendations,
        "suggested_toggle_actions": toggle_actions,
        "markdown_report": markdown_report,
    }

    # Update newest-first history stack (up to 12 reviews per session)
    history: List[Dict[str, Any]] = []
    hist_path = _resolve_history_path(artifact_path)
    if enable_redis:
        try:
            import redis
            r_hist = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.3,
                socket_timeout=0.3,
            )
            raw_hist = r_hist.get(f"trillionaire:autopsy:history:{target_date.isoformat()}")
            if raw_hist:
                loaded_h = json.loads(raw_hist)
                if isinstance(loaded_h, list):
                    history = loaded_h
        except Exception:
            pass
    if not history and hist_path.exists():
        try:
            with open(hist_path, "r", encoding="utf-8") as fh:
                loaded_h = json.load(fh)
                if isinstance(loaded_h, list):
                    history = [h for h in loaded_h if h.get("trade_date") == target_date.isoformat()]
        except Exception:
            pass

    history = [report_entry, *history][:12]
    report = {**report_entry, "history": history}

    # Persist latest + history artifacts atomically
    target_path = _resolve_autopsy_path(artifact_path)
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        os.replace(tmp_path, target_path)

        tmp_hist = hist_path.with_suffix(".tmp")
        with open(tmp_hist, "w", encoding="utf-8") as fh:
            json.dump(history, fh, indent=2)
        os.replace(tmp_hist, hist_path)
    except Exception as e:
        logger.warning(f"[TRADE AUTOPSY] Failed saving disk artifact {target_path}: {e}")

    # Persist to Redis under trillionaire:autopsy:{YYYY-MM-DD} + history key
    if enable_redis:
        try:
            import redis
            r = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.3,
                socket_timeout=0.3,
            )
            r.set(f"trillionaire:autopsy:{target_date.isoformat()}", json.dumps(report), ex=86400 * 7)
            r.set(f"trillionaire:autopsy:history:{target_date.isoformat()}", json.dumps(history), ex=86400 * 7)
        except Exception as e:
            logger.debug(f"[TRADE AUTOPSY] Redis persist bypassed: {e}")

    # Broadcast summary via Telegram if configured
    if send_telegram and settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            from trading_engine.core.telegram_bot import TelegramNotifier
            notifier = TelegramNotifier()
            diag_preview = "\n".join(f"• {d}" for d in diagnosis[:2])
            rec_preview = "\n".join(f"• {r}" for r in recommendations[:2])
            msg = (
                f"🧠 <b>TRILLIONAIRE TRADE AUTOPSY ({target_date.isoformat()})</b>\n"
                f"Trades: <b>{scorecard['total_trades']}</b> | Win Rate: <b>{scorecard['win_rate_pct']}%</b> | PF: <b>{scorecard['profit_factor']}</b>\n"
                f"Net PnL: <b>Rs.{scorecard['net_post_tax_pnl_inr']:,.2f}</b>\n\n"
                f"<b>Diagnosis:</b>\n{diag_preview}\n\n"
                f"<b>Next Session Actions:</b>\n{rec_preview}"
            )
            await notifier.send_message(msg)
        except Exception as e:
            logger.debug(f"[TRADE AUTOPSY] Telegram notification skipped: {e}")

    logger.info(
        f"[TRADE AUTOPSY] Generated {target_date.isoformat()} report via {source} "
        f"(Trades={scorecard['total_trades']}, PnL=Rs.{scorecard['net_post_tax_pnl_inr']:.2f})"
    )
    return report


def load_latest_trade_autopsy(
    trade_date: Optional[date] = None,
    artifact_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Loads today's Trade Autopsy report from Redis or disk artifact."""
    target_date = (trade_date or datetime.now(IST_TZ).date()).isoformat()
    try:
        import redis
        r = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        raw = r.get(f"trillionaire:autopsy:{target_date}")
        if raw:
            return json.loads(raw)
    except Exception:
        pass

    path = _resolve_autopsy_path(artifact_path)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if "history" not in loaded:
                    loaded["history"] = [loaded]
                return loaded
        except Exception:
            pass
    return None


def load_trade_autopsy_history(
    trade_date: Optional[date] = None,
    artifact_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Loads today's newest-first Trade Autopsy review stack from Redis or disk history."""
    target_date = (trade_date or datetime.now(IST_TZ).date()).isoformat()
    try:
        import redis
        r = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        raw = r.get(f"trillionaire:autopsy:history:{target_date}")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
    except Exception:
        pass

    hist_path = _resolve_history_path(artifact_path)
    if hist_path.exists():
        try:
            with open(hist_path, "r", encoding="utf-8") as fh:
                parsed = json.load(fh)
                if isinstance(parsed, list):
                    return [h for h in parsed if h.get("trade_date") == target_date]
        except Exception:
            pass
    latest = load_latest_trade_autopsy(trade_date=trade_date, artifact_path=artifact_path)
    return [latest] if latest else []
