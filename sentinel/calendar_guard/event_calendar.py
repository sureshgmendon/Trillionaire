"""
Trillionaire Sentinel - Earnings & Macro Event Calendar Guard
Maintains calendar blackout dates for high-impact macro announcements and corporate earnings.
Enforces trade lockouts (DO_NOT_TRADE / 0.0x size) and caution gates (CAUTION_HALF_SIZE / 0.5x size).
"""

from datetime import date, datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger

from shared.constants import IST_TZ
from shared.enums import SentinelStatus

CALENDAR_EVENTS_PATH = Path(__file__).resolve().parent / "events.json"


class EventCalendar:
    """Manages scheduled economic releases and corporate earnings blackouts."""

    def __init__(self, events_path: Path = CALENDAR_EVENTS_PATH):
        self.events_path = events_path
        self._events_data: Dict[str, Any] = {}
        self.load_events()

    def load_events(self) -> Dict[str, Any]:
        """Loads events schedule from JSON file."""
        if not self.events_path.exists():
            logger.warning(f"Calendar events file not found at {self.events_path}. Initializing empty.")
            self._events_data = {"macro_events": [], "corporate_events": {}}
            return self._events_data

        with open(self.events_path, "r", encoding="utf-8") as f:
            self._events_data = json.load(f)
        return self._events_data

    def save_events(self) -> None:
        """Persists in-memory calendar schedule back to JSON."""
        with open(self.events_path, "w", encoding="utf-8") as f:
            json.dump(self._events_data, f, indent=2)

    def add_macro_event(
        self,
        event_date: date,
        event_name: str,
        posture: SentinelStatus = SentinelStatus.CAUTION_HALF_SIZE,
        risk_multiplier: float = 0.5,
        description: str = "",
    ) -> None:
        """Registers a scheduled macro event."""
        date_str = event_date.isoformat()
        macro_list = self._events_data.setdefault("macro_events", [])
        macro_list.append({
            "date": date_str,
            "event": event_name,
            "impact": "HIGH",
            "posture": posture.value,
            "risk_multiplier": risk_multiplier,
            "description": description or event_name,
        })
        self.save_events()

    def add_corporate_event(
        self,
        tradingsymbol: str,
        event_date: date,
        event_type: str = "EARNINGS",
        posture: SentinelStatus = SentinelStatus.DO_NOT_TRADE,
        risk_multiplier: float = 0.0,
        description: str = "",
    ) -> None:
        """Registers a corporate action or quarterly earnings announcement."""
        date_str = event_date.isoformat()
        corp_dict = self._events_data.setdefault("corporate_events", {})
        sym_events = corp_dict.setdefault(tradingsymbol, [])
        sym_events.append({
            "date": date_str,
            "event_type": event_type,
            "posture": posture.value,
            "risk_multiplier": risk_multiplier,
            "description": description or f"{tradingsymbol} {event_type}",
        })
        self.save_events()

    def evaluate_calendar_guard(
        self, tradingsymbol: str, current_date_ist: Optional[date] = None
    ) -> Tuple[SentinelStatus, float, str]:
        """
        Evaluates scheduled calendar risk for a given instrument on a target date:
        - Stock earnings day -> (DO_NOT_TRADE, 0.0, 'Quarterly earnings blackout')
        - Macro policy day -> (CAUTION_HALF_SIZE, 0.5, 'Macro event volatility guard: <event>')
        - Otherwise -> (NORMAL, 1.0, 'No scheduled event')
        """
        if current_date_ist is None:
            current_date_ist = datetime.now(IST_TZ).date()

        date_str = current_date_ist.isoformat()

        # 1. Check stock-specific corporate events first (higher specificity)
        corp_events = self._events_data.get("corporate_events", {}).get(tradingsymbol, [])
        for ev in corp_events:
            if ev.get("date") == date_str:
                ev_type = ev.get("event_type", "EVENT")
                posture_str = ev.get("posture", SentinelStatus.DO_NOT_TRADE.value)
                status = SentinelStatus(posture_str)
                mult = float(ev.get("risk_multiplier", 0.0))
                desc = ev.get("description", f"{tradingsymbol} corporate event")

                if ev_type == "EARNINGS" or status == SentinelStatus.DO_NOT_TRADE:
                    reason = f"Quarterly earnings blackout: {desc}"
                else:
                    reason = f"Corporate event guard: {desc}"

                return status, mult, reason

        # 2. Check market-wide macro events
        macro_events = self._events_data.get("macro_events", [])
        for ev in macro_events:
            if ev.get("date") == date_str:
                posture_str = ev.get("posture", SentinelStatus.CAUTION_HALF_SIZE.value)
                status = SentinelStatus(posture_str)
                mult = float(ev.get("risk_multiplier", 0.5))
                ev_name = ev.get("event", "Macro Event")
                reason = f"Macro event volatility guard: {ev_name}"
                return status, mult, reason

        # 3. Clean operational posture
        return SentinelStatus.NORMAL, 1.0, "No scheduled event"


# Indian NSE Equity Trading Holidays (2026 Schedule)
NSE_HOLIDAYS_2026: Dict[str, str] = {
    "2026-01-26": "Republic Day",
    "2026-02-17": "Maha Shivratri",
    "2026-03-03": "Holi",
    "2026-03-20": "Id-Ul-Fitr (Ramzan Id)",
    "2026-03-27": "Ram Navami",
    "2026-03-31": "Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-27": "Bakri Id / Eid ul-Adha",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-04": "Janmashtami",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-09": "Diwali Laxmi Pujan (Muhurat Trading)",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}


def get_market_holiday_name(date_ist: Optional[date] = None) -> Optional[str]:
    """
    Returns the holiday name if target date is an Indian NSE Equity trading holiday or weekend, else None.
    """
    if date_ist is None:
        date_ist = datetime.now(IST_TZ).date()

    # Check weekend first (Saturday / Sunday)
    if date_ist.weekday() == 5:
        return "Weekend (Saturday)"
    if date_ist.weekday() == 6:
        return "Weekend (Sunday)"

    return NSE_HOLIDAYS_2026.get(date_ist.isoformat())


# Singleton instance
_default_calendar = EventCalendar()


def evaluate_calendar_guard(
    tradingsymbol: str, current_date_ist: Optional[date] = None
) -> Tuple[SentinelStatus, float, str]:
    """Module-level entrypoint querying default calendar schedule."""
    return _default_calendar.evaluate_calendar_guard(tradingsymbol, current_date_ist)
