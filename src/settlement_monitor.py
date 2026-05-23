"""
Settlement monitor: background task that checks expired markets and records P&L.

Runs every 90 seconds. For each open line whose close_time_utc has passed,
queries Kalshi for the market result, computes net P&L from the fill data,
and calls tracker.settle_line() + sends a Telegram alert.

P&L formula (matches the backtest fee model):
  fee_per_order = ceil(7 × qty × p × (1−p)) / 100   where p = price_cents / 100
  win P&L = qty × (100 − price_cents) / 100 − fee
  loss P&L = −notional_usd  (paid price_cents for each contract, worth $0)
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone

from src.alerts import Alerts
from src.database import Database
from src.kalshi_client import KalshiClient
from src.position_tracker import PositionTracker

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 90
_SETTLE_GRACE_S = 120   # wait 2 min after close_time before querying result


async def run_settlement_monitor(
    kalshi: KalshiClient,
    tracker: PositionTracker,
    db: Database,
    alerts: Alerts,
    shutdown_event: asyncio.Event,
) -> None:
    """Long-running coroutine — run as an asyncio task in main."""
    while not shutdown_event.is_set():
        await asyncio.sleep(_POLL_INTERVAL_S)
        try:
            await _check_settlements(kalshi, tracker, db, alerts)
        except Exception as exc:
            log.warning("settlement_monitor error: %s", exc)


async def _check_settlements(
    kalshi: KalshiClient,
    tracker: PositionTracker,
    db: Database,
    alerts: Alerts,
) -> None:
    open_lines = tracker.open_positions()
    if not open_lines:
        return

    now_utc = datetime.now(timezone.utc)

    for line in open_lines:
        close_time_str = line["close_time_utc"]
        if not close_time_str:
            continue

        try:
            close_time = datetime.fromisoformat(close_time_str)
        except ValueError:
            continue

        # Ensure timezone-aware comparison
        if close_time.tzinfo is None:
            close_time = close_time.replace(tzinfo=timezone.utc)

        seconds_since_close = (now_utc - close_time).total_seconds()
        if seconds_since_close < _SETTLE_GRACE_S:
            continue  # market hasn't closed long enough yet

        ticker = line["market_ticker"]
        market_data = await kalshi.get_market(ticker)
        result = market_data.get("result")  # "yes" | "no" | None

        if result is None:
            log.debug("settlement_monitor: %s not yet settled (status=%s)",
                      ticker, market_data.get("status"))
            continue

        # Compute P&L from all fills for this line
        orders = db.get_orders_for_line(line["id"])
        total_pnl = 0.0
        for order in orders:
            qty = int(order["quantity"])
            if qty == 0:
                continue
            price_cents = int(order["order_price"])
            p = price_cents / 100.0
            notional = float(order["notional_usd"] or (qty * p))
            fee = math.ceil(7 * qty * p * (1 - p)) / 100.0
            side = order["side"]

            if side == result:
                total_pnl += qty * (100 - price_cents) / 100.0 - fee
            else:
                total_pnl += -notional

        outcome = "win" if line["side"] == result else "loss"
        tracker.settle_line(line["id"], outcome, round(total_pnl, 2))

        log.info(
            "settled line=%s ticker=%s outcome=%s pnl=%.2f",
            line["id"][:8], ticker, outcome, total_pnl,
        )
        await alerts.notify_settlement(ticker, outcome, round(total_pnl, 2))
