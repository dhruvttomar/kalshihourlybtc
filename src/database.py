from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    client_order_id TEXT UNIQUE NOT NULL,
    kalshi_order_id TEXT,
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    strike_price INTEGER NOT NULL,
    order_price INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    filled_at TIMESTAMP,
    notional_usd REAL,
    line_id TEXT NOT NULL,
    spot_at_entry REAL,
    rv_60_at_entry REAL,
    minutes_into_hour INTEGER,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS lines (
    id TEXT PRIMARY KEY,
    hour_key TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    strike_price INTEGER NOT NULL,
    cumulative_filled_usd REAL DEFAULT 0,
    final_pnl_usd REAL,
    settled_at TIMESTAMP,
    outcome TEXT
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    triggered_at TIMESTAMP NOT NULL,
    pause_until TIMESTAMP,
    context_json TEXT
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    timestamp TIMESTAMP PRIMARY KEY,
    spot REAL NOT NULL,
    rv_60_annualized REAL,
    rv_24h_annualized REAL
);

CREATE INDEX IF NOT EXISTS idx_orders_line    ON orders(line_id);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_lines_hour     ON lines(hour_key);
"""


class Database:
    def __init__(self, path: str = "data/trades.db"):
        self._path = path
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        with self._lock:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # ── orders ────────────────────────────────────────────────────────────────

    def record_order(self, order: dict[str, Any]) -> None:
        sql = """
        INSERT OR REPLACE INTO orders
            (id, client_order_id, kalshi_order_id, market_ticker, side,
             strike_price, order_price, quantity, status, created_at,
             filled_at, notional_usd, line_id, spot_at_entry, rv_60_at_entry,
             minutes_into_hour, metadata_json)
        VALUES
            (:id, :client_order_id, :kalshi_order_id, :market_ticker, :side,
             :strike_price, :order_price, :quantity, :status, :created_at,
             :filled_at, :notional_usd, :line_id, :spot_at_entry, :rv_60_at_entry,
             :minutes_into_hour, :metadata_json)
        """
        with self._conn() as conn:
            conn.execute(sql, order)

    def update_order_status(
        self,
        order_id: str,
        status: str,
        kalshi_order_id: str | None = None,
        filled_at: str | None = None,
        notional_usd: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE orders SET status = ?, kalshi_order_id = COALESCE(?, kalshi_order_id),
                    filled_at = COALESCE(?, filled_at),
                    notional_usd = COALESCE(?, notional_usd)
                WHERE id = ?
                """,
                (status, kalshi_order_id, filled_at, notional_usd, order_id),
            )

    def get_pending_orders(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM orders WHERE status IN ('pending', 'partial') ORDER BY created_at"
            ).fetchall()

    # ── lines ─────────────────────────────────────────────────────────────────

    def record_line(self, line: dict[str, Any]) -> None:
        sql = """
        INSERT OR REPLACE INTO lines
            (id, hour_key, market_ticker, side, strike_price,
             cumulative_filled_usd, final_pnl_usd, settled_at, outcome)
        VALUES
            (:id, :hour_key, :market_ticker, :side, :strike_price,
             :cumulative_filled_usd, :final_pnl_usd, :settled_at, :outcome)
        """
        with self._conn() as conn:
            conn.execute(sql, line)

    def get_lines_this_hour(self, hour_key: str) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM lines WHERE hour_key = ?", (hour_key,)
            ).fetchall()

    def update_line_fill(self, line_id: str, additional_usd: float) -> None:
        """Add `additional_usd` to cumulative_filled_usd for a line."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE lines SET cumulative_filled_usd = cumulative_filled_usd + ? WHERE id = ?",
                (additional_usd, line_id),
            )

    def get_open_lines(self) -> list[sqlite3.Row]:
        """All lines where settled_at IS NULL (not yet settled)."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM lines WHERE settled_at IS NULL ORDER BY rowid"
            ).fetchall()

    def settle_line(
        self, line_id: str, outcome: str, final_pnl_usd: float, settled_at: str | None = None
    ) -> None:
        if settled_at is None:
            settled_at = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE lines SET outcome = ?, final_pnl_usd = ?, settled_at = ? WHERE id = ?",
                (outcome, final_pnl_usd, settled_at, line_id),
            )

    # ── risk events ───────────────────────────────────────────────────────────

    def record_risk_event(self, event: dict[str, Any]) -> int:
        sql = """
        INSERT INTO risk_events (event_type, triggered_at, pause_until, context_json)
        VALUES (:event_type, :triggered_at, :pause_until, :context_json)
        """
        with self._conn() as conn:
            cur = conn.execute(sql, event)
            return cur.lastrowid  # type: ignore[return-value]

    # ── risk events ───────────────────────────────────────────────────────────

    def get_active_risk_events(self, now_iso: str) -> list[sqlite3.Row]:
        """Return risk events whose pause_until is after now_iso."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM risk_events WHERE pause_until > ? ORDER BY triggered_at DESC",
                (now_iso,),
            ).fetchall()

    # ── realized P&L ──────────────────────────────────────────────────────────

    def get_realized_pnl_since(self, since_iso: str) -> float:
        """Sum of final_pnl_usd for all settled lines with settled_at >= since_iso."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(final_pnl_usd), 0.0) FROM lines WHERE settled_at >= ?",
                (since_iso,),
            ).fetchone()
            return float(row[0]) if row else 0.0

    # ── price snapshots ───────────────────────────────────────────────────────

    def record_price_snapshot(
        self,
        timestamp: str,
        spot: float,
        rv_60_annualized: float | None,
        rv_24h_annualized: float | None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO price_snapshots
                    (timestamp, spot, rv_60_annualized, rv_24h_annualized)
                VALUES (?, ?, ?, ?)
                """,
                (timestamp, spot, rv_60_annualized, rv_24h_annualized),
            )
