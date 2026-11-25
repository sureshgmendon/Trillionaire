"""
Trillionaire Engine - Daily Session State Persistence (IST Date-Keyed)
Persists daily orders, open positions, immutable closed trades, funnel counters,
and paper capital balance to disk (logs/daily_state_{YYYY-MM-DD}.json) so page
reloads or backend container restarts preserve the active session state.
"""

from datetime import date, datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Optional
from loguru import logger

from shared.constants import IST_TZ

DEFAULT_STATE_DIR = Path(__file__).resolve().parents[2] / "logs"


def _resolve_writable_state_dir(preferred_dir: Path = DEFAULT_STATE_DIR) -> Path:
    try:
        preferred_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(preferred_dir, os.W_OK):
            raise PermissionError(f"{preferred_dir} not writable")
        return preferred_dir
    except Exception:
        fallback = Path(tempfile.gettempdir()) / "trillionaire_state"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


class DailyStateStore:
    """Atomic date-keyed JSON store for intraday trading state."""

    def __init__(self, state_dir: Optional[Path] = None):
        env_dir = os.environ.get("TRILLIONAIRE_STATE_DIR")
        resolved_dir = state_dir or (Path(env_dir) if env_dir else DEFAULT_STATE_DIR)
        self.state_dir = _resolve_writable_state_dir(resolved_dir)

    def _file_for_date(self, trade_date: Optional[date] = None) -> Path:
        target = trade_date or datetime.now(IST_TZ).date()
        return self.state_dir / f"daily_state_{target.isoformat()}.json"

    def save_state(
        self,
        trade_date: Optional[date],
        orders: List[Dict[str, Any]],
        open_positions: List[Dict[str, Any]],
        closed_trades: List[Dict[str, Any]],
        funnel_metrics: Dict[str, int],
        paper_balance: float,
        allocated_capital: float,
        realized_pnl: float,
        risk_per_trade_pct: float = 0.5,
    ) -> None:
        """Atomically writes daily state snapshot to disk."""
        target_file = self._file_for_date(trade_date)
        payload = {
            "trade_date": (trade_date or datetime.now(IST_TZ).date()).isoformat(),
            "updated_at_ist": datetime.now(IST_TZ).isoformat(),
            "allocated_capital": round(float(allocated_capital), 2),
            "paper_balance": round(float(paper_balance), 2),
            "realized_pnl": round(float(realized_pnl), 2),
            "risk_per_trade_pct": float(risk_per_trade_pct),
            "funnel_metrics": dict(funnel_metrics),
            "orders": orders,
            "open_positions": open_positions,
            "closed_trades": closed_trades,
        }
        try:
            tmp_path = target_file.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, target_file)
        except Exception as e:
            logger.debug(f"[DAILY STATE] Could not persist state to {target_file}: {e}")

    def load_state(self, trade_date: Optional[date] = None) -> Optional[Dict[str, Any]]:
        """Loads persisted session state for target IST date if available."""
        target_file = self._file_for_date(trade_date)
        if not target_file.exists():
            return None
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[DAILY STATE] Failed loading {target_file}: {e}")
            return None
