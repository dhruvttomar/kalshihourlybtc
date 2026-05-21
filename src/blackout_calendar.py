"""
Blackout calendar — single source of truth for "is trading currently allowed."

Checks (in order):
  1. Top-of-hour algo burst (every hour, :00–:01)
  2. Day-of-week hourly risk profile
  3. Universal daily windows
  4. Weekly recurring windows
  5. Monthly/quarterly options expiry (last Friday of month)
  6. Economic calendar events (fetched from Finnhub, refreshed daily)

All times are compared in US Eastern Time (ET), DST-aware via zoneinfo.
"""
from __future__ import annotations

import calendar
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from src.models import EconEvent

_ET = ZoneInfo("America/New_York")

_WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_QUARTERLY_MONTHS = {3, 6, 9, 12}


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _last_friday_of_month(year: int, month: int) -> int:
    """Return the day-of-month (1-31) of the last Friday in the given month."""
    cal = calendar.monthcalendar(year, month)
    fridays = [week[4] for week in cal if week[4] != 0]
    return fridays[-1]


class BlackoutCalendar:
    """
    Evaluates whether trading is blocked at a given ET datetime.

    Args:
        config_path: Path to config/blackouts.yaml.
        max_risk_level: Block when hourly risk profile exceeds this value.
        econ_events: Pre-loaded economic events (used in tests / on startup).
    """

    def __init__(
        self,
        config_path: str = "config/blackouts.yaml",
        max_risk_level: int = 1,
        econ_events: list[EconEvent] | None = None,
    ):
        self._max_risk_level = max_risk_level
        self._econ_events: list[EconEvent] = econ_events or []
        self._last_refresh: datetime | None = None
        self._cfg = self._load_config(config_path)

    # ── Config loading ────────────────────────────────────────────────────────

    @staticmethod
    def _load_config(path: str) -> dict[str, Any]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Blackouts config not found: {path}")
        with p.open() as f:
            return yaml.safe_load(f)

    # ── Public API ────────────────────────────────────────────────────────────

    def is_blocked(self, now_et: datetime) -> tuple[bool, str | None]:
        """
        Returns (True, reason) if any blackout rule applies right now.
        Returns (False, None) if trading is permitted.

        Args:
            now_et: Current time localized to ET.
        """
        # 1. Top-of-hour burst (every hour, exactly minute 0)
        if now_et.minute == 0:
            return True, "Top of hour algo burst (minute 0 blackout)"

        # 2. Day-of-week hourly risk profile
        day_name = now_et.strftime("%A").lower()
        profiles = self._cfg.get("day_risk_profiles", {})
        if day_name in profiles:
            risk_level = profiles[day_name][now_et.hour]
            if risk_level > self._max_risk_level:
                return True, (
                    f"Day risk profile: {day_name} hour {now_et.hour:02d} "
                    f"= level {risk_level} (max allowed: {self._max_risk_level})"
                )

        # 3. Universal daily windows
        for window in self._cfg.get("universal_daily", []):
            if self._in_daily_window(now_et, window):
                return True, window["name"]

        # 4. Weekly recurring windows
        for window in self._cfg.get("weekly", []):
            if self._in_weekly_window(now_et, window):
                return True, window["name"]

        # 5. Monthly / quarterly expiry — last Friday of the month (all-day block)
        if now_et.weekday() == 4:  # Friday
            last_friday = _last_friday_of_month(now_et.year, now_et.month)
            if now_et.day == last_friday:
                if now_et.month in _QUARTERLY_MONTHS:
                    return True, "Quarterly options expiry (hard no — last Friday of quarter)"
                return True, "Monthly options expiry (last Friday of month)"

        # 6. Economic calendar
        for event in self._econ_events:
            blocked, reason = self._in_econ_window(now_et, event)
            if blocked:
                return True, reason

        return False, None

    def next_open_window(self, now_et: datetime) -> datetime:
        """Return the next minute at which trading is not blocked."""
        check = now_et.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(24 * 60):
            blocked, _ = self.is_blocked(check)
            if not blocked:
                return check
            check += timedelta(minutes=1)
        return check  # fallback: 24h out

    async def refresh_economic_calendar(self, finnhub_client: Any) -> None:
        """Fetch upcoming events. Called daily at 00:05 ET."""
        try:
            events = await finnhub_client.fetch_upcoming_events(days_ahead=7)
            self._econ_events = events
            self._last_refresh = datetime.now(timezone.utc)
        except Exception:
            # Keep stale events; caller is responsible for alerting if > 24h stale
            pass

    @property
    def econ_events_stale(self) -> bool:
        """True if the economic calendar has not been refreshed in > 24 hours."""
        if self._last_refresh is None:
            return True
        return (datetime.now(timezone.utc) - self._last_refresh).total_seconds() > 86_400

    def set_econ_events(self, events: list[EconEvent]) -> None:
        """Inject events directly (used in tests and for manual overrides)."""
        self._econ_events = events
        self._last_refresh = datetime.now(timezone.utc)

    # ── Window helpers ────────────────────────────────────────────────────────

    def _in_daily_window(self, now_et: datetime, window: dict[str, Any]) -> bool:
        if window.get("weekdays_only") and now_et.weekday() >= 5:
            return False
        start_h, start_m = _parse_hhmm(window["start"])
        end_h, end_m = _parse_hhmm(window["end"])
        window_start = now_et.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        window_end = now_et.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        return window_start <= now_et < window_end

    def _in_weekly_window(self, now_et: datetime, window: dict[str, Any]) -> bool:
        target_weekday = _WEEKDAY_NAMES[window["day"]]
        if now_et.weekday() != target_weekday:
            return False
        start_h, start_m = _parse_hhmm(window["start"])
        end_h, end_m = _parse_hhmm(window["end"])
        window_start = now_et.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        window_end = now_et.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        return window_start <= now_et < window_end

    def _in_econ_window(
        self, now_et: datetime, event: EconEvent
    ) -> tuple[bool, str | None]:
        triggers = self._cfg.get("economic_calendar_triggers", [])
        for trigger in triggers:
            if not any(
                kw.lower() in event.name.lower() for kw in trigger["keywords"]
            ):
                continue
            event_et = event.event_time_utc.astimezone(_ET)
            start = event_et + timedelta(minutes=trigger["blackout_start_offset_minutes"])
            end = event_et + timedelta(minutes=trigger["blackout_end_offset_minutes"])
            if start <= now_et < end:
                return True, f"Economic event blackout: {event.name}"
        return False, None
