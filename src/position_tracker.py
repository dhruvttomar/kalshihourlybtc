"""
Position tracker: SQLite-backed implementation of PositionTrackerProtocol.

Tracks per-hour line counts and capital deployed by querying the `lines` table
filtered by hour_key. All queries are read-from-DB so restarts are safe —
the counts are always derived from persistent state.

The hour_key format is "YYYY-MM-DDTHH" in ET (e.g. "2026-05-21T10").
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from src.database import Database

_ET = ZoneInfo("America/New_York")


def _hour_key(now_et: datetime) -> str:
    """Canonical key for a trading hour: '2026-05-21T10'."""
    return now_et.strftime("%Y-%m-%dT%H")


class PositionTracker:
    """
    DB-backed position tracker implementing PositionTrackerProtocol.

    All state is persisted in the `lines` table. Counter methods query the DB
    so they remain accurate across restarts.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── PositionTrackerProtocol ───────────────────────────────────────────────

    def lines_taken_this_hour(self, now_et: datetime) -> int:
        """Number of lines opened in the same ET trading hour as now_et."""
        rows = self._db.get_lines_this_hour(_hour_key(now_et))
        return len(rows)

    def capital_deployed_this_hour(self, now_et: datetime) -> float:
        """Total USD filled across all lines in the same ET hour as now_et."""
        rows = self._db.get_lines_this_hour(_hour_key(now_et))
        return sum(float(r["cumulative_filled_usd"]) for r in rows)

    # ── Line lifecycle ────────────────────────────────────────────────────────

    def open_new_line(
        self,
        now_et: datetime,
        market_ticker: str,
        side: str,
        strike_price_cents: int,
        close_time_utc: str | None = None,
    ) -> str:
        """
        Create a new line record in the DB and return its UUID.

        Call this once per new position before placing any orders.
        """
        line_id = str(uuid.uuid4())
        self._db.record_line({
            "id": line_id,
            "hour_key": _hour_key(now_et),
            "market_ticker": market_ticker,
            "side": side,
            "strike_price": strike_price_cents,
            "cumulative_filled_usd": 0.0,
            "final_pnl_usd": None,
            "settled_at": None,
            "outcome": None,
            "close_time_utc": close_time_utc,
        })
        return line_id

    def add_fill_to_line(self, line_id: str, additional_usd: float) -> None:
        """Increment cumulative fill amount on an existing line (one order filled)."""
        self._db.update_line_fill(line_id, additional_usd)

    def settle_line(self, line_id: str, outcome: str, final_pnl_usd: float) -> None:
        """Mark a line as settled with its outcome ('win' | 'loss') and final P&L."""
        self._db.settle_line(line_id, outcome, final_pnl_usd)

    # ── Position queries ──────────────────────────────────────────────────────

    def open_positions(self) -> list[sqlite3.Row]:
        """All unsettled lines (settled_at IS NULL)."""
        return self._db.get_open_lines()

    def positions_settling_this_hour(self, now_et: datetime) -> list[sqlite3.Row]:
        """Lines whose hour_key matches now_et's ET hour (settling this hour)."""
        return self._db.get_lines_this_hour(_hour_key(now_et))

    def reset_hourly_counters(self) -> None:
        """
        No-op: counters are derived live from DB queries filtered by hour_key.
        Called at the top of each hour by the main loop for compatibility.
        """
