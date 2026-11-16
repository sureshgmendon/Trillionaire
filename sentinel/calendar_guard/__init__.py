"""
Trillionaire Sentinel - Calendar Guard Package
Exports economic calendar blackout and corporate earnings gate functions.
"""

from sentinel.calendar_guard.event_calendar import (
    CALENDAR_EVENTS_PATH,
    EventCalendar,
    evaluate_calendar_guard,
)

__all__ = ["EventCalendar", "evaluate_calendar_guard", "CALENDAR_EVENTS_PATH"]
