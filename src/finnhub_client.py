"""
Thin async Finnhub economic calendar client.

Fetches high-impact US economic events for a date range and converts them
to EconEvent objects with UTC timestamps for use by BlackoutCalendar.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import aiohttp

from src.models import EconEvent

_DEFAULT_BASE = "https://finnhub.io/api/v1"


class FinnhubClient:
    def __init__(self, api_key: str | None = None, base_url: str = _DEFAULT_BASE):
        self._api_key = api_key or os.getenv("FINNHUB_API_KEY", "")
        self._base_url = base_url.rstrip("/")

    async def fetch_upcoming_events(self, days_ahead: int = 7) -> list[EconEvent]:
        """
        Fetch US economic events for the next `days_ahead` days.

        Finnhub returns event times as UTC in HH:MM:SS format combined with
        the event date in YYYY-MM-DD. We treat the combined datetime as UTC.

        Only returns events where impact == "high" and country == "US".
        """
        if not self._api_key:
            return []

        today = datetime.now(timezone.utc).date()
        from_date = today.isoformat()
        to_date = (today + timedelta(days=days_ahead)).isoformat()

        url = f"{self._base_url}/calendar/economic"
        params = {"from": from_date, "to": to_date, "token": self._api_key}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                data = await resp.json()

        events: list[EconEvent] = []
        for item in data.get("economicCalendar", []):
            if item.get("country") != "US":
                continue
            if item.get("impact", "").lower() not in ("high", "3"):
                continue

            date_str = item.get("time", "")  # Finnhub uses ISO 8601 or epoch
            event_dt = self._parse_event_time(date_str)
            if event_dt is None:
                continue

            events.append(EconEvent(
                name=item.get("event", ""),
                event_time_utc=event_dt,
                impact="high",
                country="US",
            ))

        return events

    @staticmethod
    def _parse_event_time(time_str: str) -> datetime | None:
        """Parse Finnhub event time. Handles ISO 8601 strings."""
        if not time_str:
            return None
        try:
            # Finnhub returns Unix timestamps as integers in some versions
            if time_str.isdigit():
                return datetime.fromtimestamp(int(time_str), tz=timezone.utc)
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None
