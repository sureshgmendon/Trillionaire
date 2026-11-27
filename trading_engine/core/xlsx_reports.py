"""
Trillionaire Reporting Engine - Multi-Tab XLSX Workbook Generator & Date-Wise Archive
Generates institutional multi-worksheet Excel (.xlsx) workbooks via openpyxl:
1. Trillionaire_Evaluation_Report_{label}.xlsx -> Tabs: All | Eval | Stock | Sector | Signal
2. Trillionaire_PL_Report_{label}.xlsx         -> Tabs: PL Report | Executed Orders
Stores daily EOD finalized reports under research_lab/artifacts/reports/{YYYY}/{MM}/{YYYY-MM-DD}/
and aggregates historical archives + live intraday buffers across custom or preset date ranges.
"""

from datetime import date, datetime, time, timedelta
from io import BytesIO
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from loguru import logger
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pydantic import BaseModel

from shared.constants import IST_TZ

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_REPORTS_DIR = ROOT_DIR / "research_lab" / "artifacts" / "reports"

EVAL_SHEET_NAMES = ["All", "Eval", "Stock", "Sector", "Signal"]
EVAL_HEADERS = [
    "Date",
    "Time IST",
    "Stage",
    "Symbol",
    "Sector",
    "Regime",
    "Passed",
    "Close",
    "VWAP",
    "EMA200",
    "RVOL",
    "Reason",
]

PL_HEADERS = [
    "Date",
    "Product",
    "Instrument",
    "Side",
    "Qty",
    "Avg Price",
    "LTP",
    "SL",
    "Target",
    "Exit Price",
    "Realized P&L",
    "Chg %",
    "Status / Reason",
]

EXECUTED_HEADERS = [
    "Date",
    "Time IST",
    "Type",
    "Instrument",
    "Product",
    "Qty",
    "Avg Price",
    "Status",
    "Reason",
]


def resolve_reports_root(reports_dir: Optional[Path] = None) -> Path:
    """Resolves writable root archive folder for date-wise XLSX workbooks."""
    if reports_dir is not None:
        return reports_dir
    env_dir = os.environ.get("TRILLIONAIRE_REPORTS_DIR")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_REPORTS_DIR


def date_folder_for(target_date: date, reports_dir: Optional[Path] = None) -> Path:
    """Returns date-wise folder path YYYY/MM/YYYY-MM-DD under reports root."""
    root = resolve_reports_root(reports_dir)
    return root / f"{target_date.year:04d}" / f"{target_date.month:02d}" / target_date.isoformat()


def _style_sheet(ws: Any, headers: List[str], header_color: str = "0F172A") -> None:
    header_fill = PatternFill(start_color=header_color, end_color=header_color, fill_type="solid")
    header_font = Font(color="F8FAFC", bold=True, size=10)
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center" if col_idx > 1 else "left", vertical="center")
    ws.freeze_panes = "A2"


def _autofit_columns(ws: Any, min_width: int = 10, max_width: int = 48) -> None:
    for col_cells in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col_cells[0].column)
        for c in col_cells:
            val_str = "" if c.value is None else str(c.value)
            if len(val_str) > max_len:
                max_len = len(val_str)
        ws.column_dimensions[col_letter].width = max(min_width, min(max_width, max_len + 3))


def _serialize_item(item: Any) -> Dict[str, Any]:
    if isinstance(item, BaseModel):
        return item.model_dump(mode="json")
    if isinstance(item, dict):
        return dict(item)
    return {}


def _format_ist_time(iso_val: Any) -> Tuple[str, str]:
    """Returns (YYYY-MM-DD, HH:MM:SS) IST strings from an ISO datetime or date/time."""
    if not iso_val:
        now = datetime.now(IST_TZ)
        return now.date().isoformat(), now.strftime("%H:%M:%S")
    try:
        dt = iso_val if isinstance(iso_val, datetime) else datetime.fromisoformat(str(iso_val))
        dt_ist = dt.astimezone(IST_TZ) if dt.tzinfo else dt.replace(tzinfo=IST_TZ)
        return dt_ist.date().isoformat(), dt_ist.strftime("%H:%M:%S")
    except Exception:
        s = str(iso_val)
        return datetime.now(IST_TZ).date().isoformat(), s[:8]


def _sort_executed_orders_stock_first(orders: Iterable[Any]) -> List[Dict[str, Any]]:
    """
    Groups executed orders (FILLED / CANCELLED / REJECTED) by tradingsymbol ordered by
    earliest order timestamp, and within each stock sorts Entries chronologically ascending
    followed chronologically by Exit / SL / TP / Cancelled legs.
    """
    normalized: List[Dict[str, Any]] = []
    for raw in orders or []:
        d = _serialize_item(raw)
        status = str(d.get("status") or "").upper()
        if status not in ("FILLED", "COMPLETE", "CANCELLED", "REJECTED"):
            continue
        ts_iso = str(d.get("created_at_ist") or "")
        d_str, t_str = _format_ist_time(ts_iso)
        d["_sort_date"] = d.get("date_ist") or d_str
        d["_sort_time"] = d.get("time_ist") or t_str
        d["_sort_key"] = f"{d['_sort_date']} {d['_sort_time']}"
        normalized.append(d)

    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for ord_d in normalized:
        sym = str(ord_d.get("tradingsymbol") or ord_d.get("symbol") or "UNKNOWN")
        by_symbol.setdefault(sym, []).append(ord_d)

    # Order stock groups by earliest order timestamp ascending
    ordered_symbols = sorted(
        by_symbol.keys(),
        key=lambda sym: min(item["_sort_key"] for item in by_symbol[sym]),
    )

    result: List[Dict[str, Any]] = []
    for sym in ordered_symbols:
        sym_orders = sorted(
            by_symbol[sym],
            key=lambda item: (
                1 if (item.get("is_exit") or str(item.get("status")).upper() == "CANCELLED") else 0,
                item["_sort_key"],
            ),
        )
        result.extend(sym_orders)
    return result


def build_evaluation_report_workbook(
    logs: List[Dict[str, Any]],
    is_live: bool = False,
    live_hhmm: Optional[str] = None,
) -> Workbook:
    """Builds 5-tab Trillionaire_Evaluation_Report workbook: All | Eval | Stock | Sector | Signal."""
    wb = Workbook()
    # Remove default sheet after creating named tabs cleanly
    default_ws = wb.active

    sorted_logs = sorted(
        [_serialize_item(x) for x in (logs or [])],
        key=lambda x: (str(x.get("date_ist") or ""), str(x.get("timestamp_ist") or "")),
    )

    tab_colors = {
        "All": "1E293B",
        "Eval": "1D4ED8",
        "Stock": "0E7490",
        "Sector": "B45309",
        "Signal": "047857",
    }

    for sheet_name in EVAL_SHEET_NAMES:
        ws = wb.create_sheet(title=sheet_name)
        ws.sheet_properties.tabColor = tab_colors[sheet_name]
        _style_sheet(ws, EVAL_HEADERS, header_color="0F172A")

        stage_upper = sheet_name.upper()
        rows = (
            sorted_logs
            if stage_upper == "ALL"
            else [row for row in sorted_logs if str(row.get("stage") or "").upper() == stage_upper]
        )

        for row in rows:
            ws.append([
                row.get("date_ist", ""),
                row.get("timestamp_ist", ""),
                row.get("stage", ""),
                row.get("symbol", ""),
                row.get("parent_index", ""),
                row.get("regime", ""),
                "PASS" if row.get("passed") else "BLOCK/SKIP",
                row.get("close", ""),
                row.get("vwap", ""),
                row.get("ema200", ""),
                row.get("rvol", ""),
                row.get("reason", ""),
            ])

        if is_live and live_hhmm:
            ws.oddHeader.right.text = f"LIVE AS OF {live_hhmm} IST"
        _autofit_columns(ws)

    wb.remove(default_ws)
    return wb


def _prepare_pl_rows(
    closed_trades: Iterable[Any],
    open_positions: Iterable[Any],
    ticks: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], float, float, float]:
    rows: List[Dict[str, Any]] = []
    realized_total = 0.0
    unrealized_total = 0.0
    ticks_map = ticks or {}

    for pos_raw in open_positions or []:
        p = _serialize_item(pos_raw)
        qty = abs(int(p.get("quantity") or 0))
        if qty == 0:
            continue
        sym = str(p.get("tradingsymbol") or p.get("symbol") or "")
        side_str = str(p.get("side") or "BUY").upper()
        is_short = side_str in ("SELL", "SHORT")
        avg_p = float(p.get("average_entry_price") or p.get("avg_price") or 0.0)
        tick_entry = ticks_map.get(sym)
        live_ltp = float(
            (tick_entry.get("ltp") if isinstance(tick_entry, dict) else getattr(tick_entry, "ltp", None))
            or p.get("ltp")
            or avg_p
        )
        unrealized = round((avg_p - live_ltp) * qty if is_short else (live_ltp - avg_p) * qty, 2)
        chg_pct = round(
            (((avg_p - live_ltp) / avg_p) * 100.0 if is_short else ((live_ltp - avg_p) / avg_p) * 100.0)
            if avg_p > 0
            else 0.0,
            2,
        )
        d_str, _ = _format_ist_time(p.get("entry_time"))
        unrealized_total += unrealized
        rows.append({
            "date": d_str,
            "product": "MIS",
            "instrument": sym,
            "side": side_str,
            "qty": -qty if is_short else qty,
            "avg_price": round(avg_p, 2),
            "ltp": round(live_ltp, 2),
            "sl_price": round(float(p.get("stop_loss") or 0.0), 2),
            "target_price": round(float(p.get("target_price") or 0.0), 2),
            "exit_price": "",
            "pnl": unrealized,
            "chg_pct": chg_pct,
            "status": "OPEN",
        })

    for ct_raw in closed_trades or []:
        ct = _serialize_item(ct_raw)
        pnl_val = round(float(ct.get("realized_pnl") or ct.get("net_pnl") or 0.0), 2)
        realized_total += pnl_val
        d_str = str(ct.get("date") or "")
        if not d_str:
            d_str, _ = _format_ist_time(ct.get("exit_time") or ct.get("entry_time"))
        rows.append({
            "date": d_str,
            "product": str(ct.get("product") or "MIS"),
            "instrument": str(ct.get("symbol") or ct.get("instrument") or ""),
            "side": str(ct.get("side") or ""),
            "qty": int(ct.get("filled_qty") or 0),
            "avg_price": round(float(ct.get("entry_price") or 0.0), 2),
            "ltp": round(float(ct.get("actual_exit_price") or 0.0), 2),
            "sl_price": round(float(ct.get("sl_price") or 0.0), 2),
            "target_price": round(float(ct.get("target_price") or 0.0), 2),
            "exit_price": round(float(ct.get("actual_exit_price") or 0.0), 2),
            "pnl": pnl_val,
            "chg_pct": round(float(ct.get("pnl_pct") or 0.0), 2),
            "status": str(ct.get("exit_reason") or "CLOSED"),
        })

    realized_total = round(realized_total, 2)
    unrealized_total = round(unrealized_total, 2)
    net_total = round(realized_total + unrealized_total, 2)
    return rows, realized_total, unrealized_total, net_total


def build_pl_report_workbook(
    pl_rows: List[Dict[str, Any]],
    executed_orders: List[Dict[str, Any]],
    realized_total: float,
    unrealized_total: float,
    net_total: float,
    is_live: bool = False,
    live_hhmm: Optional[str] = None,
) -> Workbook:
    """Builds 2-tab Trillionaire_PL_Report workbook: PL Report | Executed Orders."""
    wb = Workbook()
    default_ws = wb.active

    # 1. PL Report Sheet
    ws_pl = wb.create_sheet(title="PL Report")
    ws_pl.sheet_properties.tabColor = "047857"
    _style_sheet(ws_pl, PL_HEADERS, header_color="0F172A")

    for r in pl_rows:
        ws_pl.append([
            r.get("date", ""),
            r.get("product", "MIS"),
            r.get("instrument", ""),
            r.get("side", ""),
            r.get("qty", 0),
            r.get("avg_price", 0.0),
            r.get("ltp", 0.0),
            r.get("sl_price", 0.0),
            r.get("target_price", 0.0),
            r.get("exit_price", ""),
            r.get("pnl", 0.0),
            r.get("chg_pct", 0.0),
            r.get("status", ""),
        ])

    total_row_idx = ws_pl.max_row + 1
    ws_pl.append([
        "TOTAL",
        "",
        f"Realized: {realized_total:+.2f} | Unrealized: {unrealized_total:+.2f}",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        round(net_total, 2),
        "",
        "COMBINED TOTAL P&L",
    ])
    total_fill = PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid")
    total_font = Font(bold=True, size=10, color="0F172A")
    for col_idx in range(1, len(PL_HEADERS) + 1):
        c = ws_pl.cell(row=total_row_idx, column=col_idx)
        c.fill = total_fill
        c.font = total_font

    if is_live and live_hhmm:
        ws_pl.oddHeader.right.text = f"LIVE AS OF {live_hhmm} IST"
    _autofit_columns(ws_pl)

    # 2. Executed Orders Sheet (stock-grouped chronologically)
    ws_ord = wb.create_sheet(title="Executed Orders")
    ws_ord.sheet_properties.tabColor = "6D28D9"
    _style_sheet(ws_ord, EXECUTED_HEADERS, header_color="0F172A")

    sorted_orders = _sort_executed_orders_stock_first(executed_orders)
    for ord_d in sorted_orders:
        exec_price = ord_d.get("average_fill_price") or ord_d.get("limit_price") or ord_d.get("trigger_price") or 0.0
        status_raw = str(ord_d.get("status") or "")
        status_label = "COMPLETE" if status_raw == "FILLED" else status_raw
        qty_str = f"{ord_d.get('filled_quantity', 0)}/{ord_d.get('quantity', 0)}"
        reason = ord_d.get("rejection_reason") or ord_d.get("exit_reason") or ("ENTRY" if not ord_d.get("is_exit") else "")
        ws_ord.append([
            ord_d.get("_sort_date", ""),
            ord_d.get("_sort_time", ""),
            ord_d.get("side", ""),
            ord_d.get("tradingsymbol", ""),
            ord_d.get("product", "MIS"),
            qty_str,
            round(float(exec_price), 2),
            status_label,
            reason,
        ])

    if is_live and live_hhmm:
        ws_ord.oddHeader.right.text = f"LIVE AS OF {live_hhmm} IST"
    _autofit_columns(ws_ord)

    wb.remove(default_ws)
    return wb


def _workbook_to_bytes(wb: Workbook) -> bytes:
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def archive_daily_reports(
    trade_date: date,
    funnel_logs: List[Dict[str, Any]],
    closed_trades: List[Any],
    open_positions: List[Any],
    orders: List[Any],
    ticks: Optional[Dict[str, Any]] = None,
    reports_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    """
    Writes finalized daily 5-tab Evaluation & 2-tab PL Report XLSX workbooks
    into date-wise folder YYYY/MM/YYYY-MM-DD/.
    """
    target_dir = date_folder_for(trade_date, reports_dir=reports_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    date_iso = trade_date.isoformat()

    eval_wb = build_evaluation_report_workbook(funnel_logs, is_live=False)
    eval_path = target_dir / f"Trillionaire_Evaluation_Report_{date_iso}.xlsx"
    eval_tmp = eval_path.with_suffix(".tmp.xlsx")
    eval_wb.save(eval_tmp)
    os.replace(eval_tmp, eval_path)

    pl_rows, real_tot, unreal_tot, net_tot = _prepare_pl_rows(closed_trades, open_positions, ticks=ticks)
    pl_wb = build_pl_report_workbook(
        pl_rows=pl_rows,
        executed_orders=list(orders or []),
        realized_total=real_tot,
        unrealized_total=unreal_tot,
        net_total=net_tot,
        is_live=False,
    )
    pl_path = target_dir / f"Trillionaire_PL_Report_{date_iso}.xlsx"
    pl_tmp = pl_path.with_suffix(".tmp.xlsx")
    pl_wb.save(pl_tmp)
    os.replace(pl_tmp, pl_path)

    logger.info(f"[XLSX ARCHIVE] Archived daily reports for {date_iso} -> {target_dir}")
    return {"evaluation": eval_path, "pl": pl_path}


def resolve_date_range(
    preset: str = "today",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    today: Optional[date] = None,
) -> Tuple[date, date]:
    """Resolves preset or custom range into inclusive (start_date, end_date) IST dates."""
    anchor = today or datetime.now(IST_TZ).date()
    p = (preset or "today").lower().strip()

    if p == "yesterday":
        y = anchor - timedelta(days=1)
        return y, y
    if p == "this_week":
        monday = anchor - timedelta(days=anchor.weekday())
        return monday, anchor
    if p == "this_month":
        return date(anchor.year, anchor.month, 1), anchor
    if p == "quarterly":
        q_start_month = ((anchor.month - 1) // 3) * 3 + 1
        return date(anchor.year, q_start_month, 1), anchor
    if p == "half_yearly":
        return anchor - timedelta(days=182), anchor
    if p == "yearly":
        return date(anchor.year, 1, 1), anchor
    if p == "five_years":
        try:
            start_5y = date(anchor.year - 5, anchor.month, anchor.day)
        except ValueError:
            start_5y = date(anchor.year - 5, anchor.month, 28)
        return start_5y, anchor
    if p == "custom":
        start_d = date.fromisoformat(from_date) if from_date else anchor
        end_d = date.fromisoformat(to_date) if to_date else anchor
        if start_d > end_d:
            start_d, end_d = end_d, start_d
        return start_d, end_d

    return anchor, anchor


def _read_archived_eval_logs(file_path: Path) -> List[Dict[str, Any]]:
    logs: List[Dict[str, Any]] = []
    if not file_path.exists():
        return logs
    wb = load_workbook(file_path, read_only=True, data_only=True)
    try:
        sheet = wb["All"] if "All" in wb.sheetnames else wb.active
        rows_iter = sheet.iter_rows(values_only=True)
        headers = next(rows_iter, None)
        if not headers:
            return logs
        for vals in rows_iter:
            if not any(vals):
                continue
            logs.append({
                "date_ist": str(vals[0] or ""),
                "timestamp_ist": str(vals[1] or ""),
                "stage": str(vals[2] or ""),
                "symbol": str(vals[3] or ""),
                "parent_index": str(vals[4] or ""),
                "regime": str(vals[5] or ""),
                "passed": str(vals[6] or "").upper() == "PASS",
                "close": vals[7],
                "vwap": vals[8],
                "ema200": vals[9],
                "rvol": vals[10],
                "reason": str(vals[11] or ""),
            })
    finally:
        wb.close()
    return logs


def _read_archived_pl_and_orders(file_path: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    pl_rows: List[Dict[str, Any]] = []
    orders: List[Dict[str, Any]] = []
    if not file_path.exists():
        return pl_rows, orders
    wb = load_workbook(file_path, read_only=True, data_only=True)
    try:
        if "PL Report" in wb.sheetnames:
            ws_pl = wb["PL Report"]
            rows_iter = ws_pl.iter_rows(values_only=True)
            next(rows_iter, None)
            for vals in rows_iter:
                if not any(vals) or str(vals[0] or "").upper() == "TOTAL":
                    continue
                pl_rows.append({
                    "date": str(vals[0] or ""),
                    "product": str(vals[1] or "MIS"),
                    "instrument": str(vals[2] or ""),
                    "side": str(vals[3] or ""),
                    "qty": int(vals[4] or 0),
                    "avg_price": float(vals[5] or 0.0),
                    "ltp": float(vals[6] or 0.0),
                    "sl_price": float(vals[7] or 0.0),
                    "target_price": float(vals[8] or 0.0),
                    "exit_price": vals[9] if vals[9] is not None else "",
                    "pnl": float(vals[10] or 0.0),
                    "chg_pct": float(vals[11] or 0.0),
                    "status": str(vals[12] or ""),
                })
        if "Executed Orders" in wb.sheetnames:
            ws_ord = wb["Executed Orders"]
            rows_iter = ws_ord.iter_rows(values_only=True)
            next(rows_iter, None)
            for vals in rows_iter:
                if not any(vals):
                    continue
                qty_raw = str(vals[5] or "0/0")
                filled_q, tot_q = (qty_raw.split("/", 1) + ["0"])[:2]
                status_str = str(vals[7] or "")
                reason_str = str(vals[8] or "")
                orders.append({
                    "date_ist": str(vals[0] or ""),
                    "time_ist": str(vals[1] or ""),
                    "created_at_ist": f"{vals[0]}T{vals[1]}+05:30" if vals[0] and vals[1] else "",
                    "side": str(vals[2] or ""),
                    "tradingsymbol": str(vals[3] or ""),
                    "product": str(vals[4] or "MIS"),
                    "filled_quantity": int(filled_q or 0),
                    "quantity": int(tot_q or filled_q or 0),
                    "average_fill_price": float(vals[6] or 0.0),
                    "status": "FILLED" if status_str == "COMPLETE" else status_str,
                    "is_exit": reason_str not in ("", "ENTRY"),
                    "exit_reason": reason_str if reason_str not in ("", "ENTRY") else None,
                })
    finally:
        wb.close()
    return pl_rows, orders


def export_reports_xlsx(
    preset: str = "today",
    report_type: str = "evaluation",
    engine: Optional[Any] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    reports_dir: Optional[Path] = None,
    now_ist: Optional[datetime] = None,
) -> Tuple[bytes, str, bool, str]:
    """
    Exports multi-tab XLSX bytes for either `evaluation` (5 tabs) or `pl` (2 tabs) across any date range,
    combining archived YYYY/MM/YYYY-MM-DD/ folders with today's live in-memory data when applicable.
    Returns (xlsx_bytes, filename, is_live, live_hhmm_label).
    """
    now = now_ist or datetime.now(IST_TZ)
    today_d = now.date()
    start_d, end_d = resolve_date_range(preset=preset, from_date=from_date, to_date=to_date, today=today_d)

    includes_today = start_d <= today_d <= end_d
    is_live = includes_today and (time(9, 15) <= now.time() < time(15, 35))
    live_hhmm = now.strftime("%H%M")
    live_label = now.strftime("%H:%M IST")

    label = start_d.isoformat() if start_d == end_d else f"{start_d.isoformat()}_to_{end_d.isoformat()}"
    live_suffix = f"_LIVE_AS_OF_{live_hhmm}IST" if is_live else ""

    # Gather historical archived days strictly before today (and today too if already archived & engine empty)
    day_cursor = start_d
    archived_eval_logs: List[Dict[str, Any]] = []
    archived_pl_rows: List[Dict[str, Any]] = []
    archived_orders: List[Dict[str, Any]] = []

    while day_cursor <= end_d:
        day_dir = date_folder_for(day_cursor, reports_dir=reports_dir)
        d_iso = day_cursor.isoformat()
        if report_type == "evaluation":
            eval_file = day_dir / f"Trillionaire_Evaluation_Report_{d_iso}.xlsx"
            if eval_file.exists() and (day_cursor != today_d or engine is None or len(getattr(engine.strategy_runner, "funnel_logs", [])) == 0):
                archived_eval_logs.extend(_read_archived_eval_logs(eval_file))
        else:
            pl_file = day_dir / f"Trillionaire_PL_Report_{d_iso}.xlsx"
            if pl_file.exists() and (day_cursor != today_d or engine is None):
                rows_d, ords_d = _read_archived_pl_and_orders(pl_file)
                archived_pl_rows.extend(rows_d)
                archived_orders.extend(ords_d)
        day_cursor += timedelta(days=1)

    if report_type == "evaluation":
        live_logs: List[Dict[str, Any]] = []
        if includes_today and engine is not None and hasattr(engine, "strategy_runner"):
            live_logs = [
                row for row in list(getattr(engine.strategy_runner, "funnel_logs", []))
                if str(row.get("date_ist") or "") == today_d.isoformat()
            ]
        combined_logs = archived_eval_logs + live_logs
        wb = build_evaluation_report_workbook(combined_logs, is_live=is_live, live_hhmm=live_hhmm)
        filename = f"Trillionaire_Evaluation_Report_{label}{live_suffix}.xlsx"
        return _workbook_to_bytes(wb), filename, is_live, live_label

    # P&L + Executed Orders workbook
    live_pl_rows: List[Dict[str, Any]] = []
    live_orders: List[Dict[str, Any]] = []
    real_today = 0.0
    unreal_today = 0.0
    if includes_today and engine is not None and hasattr(engine, "order_manager"):
        om = engine.order_manager
        ticks_cache = getattr(om, "_latest_ticks", {})
        live_pl_rows, real_today, unreal_today, _ = _prepare_pl_rows(
            closed_trades=list(getattr(om, "closed_trades", [])),
            open_positions=list(getattr(om, "open_positions", {}).values()),
            ticks=ticks_cache,
        )
        live_orders = [_serialize_item(o) for o in getattr(om, "orders", {}).values()]

    combined_pl = archived_pl_rows + live_pl_rows
    hist_realized = round(
        sum(float(r.get("pnl") or 0.0) for r in archived_pl_rows if r.get("status") != "OPEN"),
        2,
    )
    real_tot = round(hist_realized + real_today, 2)
    unreal_tot = round(unreal_today, 2)
    net_tot = round(real_tot + unreal_tot, 2)
    combined_orders = archived_orders + live_orders

    wb = build_pl_report_workbook(
        pl_rows=combined_pl,
        executed_orders=combined_orders,
        realized_total=real_tot,
        unrealized_total=unreal_tot,
        net_total=net_tot,
        is_live=is_live,
        live_hhmm=live_hhmm,
    )
    filename = f"Trillionaire_PL_Report_{label}{live_suffix}.xlsx"
    return _workbook_to_bytes(wb), filename, is_live, live_label
