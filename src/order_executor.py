"""
Order executor: places limit orders for a single line with liquidity-cap handling.

Per spec Section 4.5, a "line" may require multiple incremental orders if the
orderbook at the target price has insufficient depth. The executor loops until:
  - cumulative fill reaches $1,000 (line complete)
  - best ask rises above the target price (price drift — stop)
  - < 60 seconds remain until settlement (time cutoff)
  - an unhandled exception (cancels all pending orders, then re-raises)

In paper mode KalshiClient returns status="filled" immediately, so the fill
loop always completes in a single iteration without real API latency.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from src.database import Database
from src.kalshi_client import KalshiClient
from src.models import EntryDecision, Fill, Line
from src.position_tracker import PositionTracker

_UTC = timezone.utc
log = logging.getLogger(__name__)

_FILL_POLL_INTERVAL_S: float = 0.5
_FILL_TIMEOUT_S: float = 10.0
_LIQUIDITY_WAIT_S: float = 2.0


class OrderExecutor:
    """
    Executes a single trading line via incremental limit orders.

    One `OrderExecutor` instance should be used per `execute_line()` call
    so `_pending_order_ids` tracks only the current line's open orders.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._pending_order_ids: list[str] = []

    async def execute_line(
        self,
        decision: EntryDecision,
        kalshi: KalshiClient,
        tracker: PositionTracker,
        now_et: datetime,
        capacity_usd: float = 1000.0,
    ) -> Line:
        """
        Deploy capital for one line, respecting the liquidity cap.

        Opens a DB line record, loops placing limit orders until the line is
        complete or an exit condition fires, records every fill, and returns
        the final Line state.
        """
        assert decision.should_trade, "execute_line called on a rejected decision"
        assert decision.floor_strike is not None, "EntryDecision.floor_strike must be set"
        assert decision.market_close_time is not None, "EntryDecision.market_close_time must be set"
        assert decision.market_ticker is not None
        assert decision.side is not None
        assert decision.target_price_cents is not None

        line = Line(strike=decision.floor_strike, side=decision.side, capacity_usd=capacity_usd)
        close_time_utc = (
            decision.market_close_time.isoformat()
            if decision.market_close_time else None
        )
        line_id = tracker.open_new_line(
            now_et=now_et,
            market_ticker=decision.market_ticker,
            side=decision.side,
            strike_price_cents=int(decision.floor_strike),
            close_time_utc=close_time_utc,
        )
        price_cents = decision.target_price_cents

        log.info(
            "Opening line %s: %s %s @ %dc target=$%.0f",
            line_id, decision.market_ticker, decision.side.upper(),
            price_cents, line.remaining_capacity,
        )

        try:
            while not line.is_complete():
                now_utc = datetime.now(_UTC)

                # ── Time cutoff ───────────────────────────────────────────────
                seconds_remaining = (decision.market_close_time - now_utc).total_seconds()
                if seconds_remaining <= 60:
                    log.info(
                        "Line %s: stopping — %.0fs until settlement",
                        line_id, seconds_remaining,
                    )
                    break

                # ── Refresh orderbook ─────────────────────────────────────────
                ob = await kalshi.get_orderbook(decision.market_ticker)
                best_ask_cents = ob.get(f"{decision.side}_best_ask_cents")
                depth_usd = ob.get(f"depth_{decision.side}_usd", 0.0)

                # ── Price drift: best ask rose above target ───────────────────
                if best_ask_cents is not None and best_ask_cents > price_cents:
                    log.info(
                        "Line %s: stopping — price drifted to %dc (target=%dc)",
                        line_id, best_ask_cents, price_cents,
                    )
                    break

                # ── No liquidity yet: wait and retry ──────────────────────────
                if depth_usd <= 0:
                    log.debug(
                        "Line %s: no depth at %dc — waiting %.0fs",
                        line_id, price_cents, _LIQUIDITY_WAIT_S,
                    )
                    await asyncio.sleep(_LIQUIDITY_WAIT_S)
                    continue

                # ── Size and place the order ──────────────────────────────────
                fill_usd = min(line.remaining_capacity, depth_usd)
                quantity = max(1, int(fill_usd / (price_cents / 100.0)))
                notional = quantity * price_cents / 100.0

                resp = await kalshi.place_limit_order(
                    market_ticker=decision.market_ticker,
                    side=decision.side,
                    price_cents=price_cents,
                    quantity=quantity,
                    line_id=line_id,
                )
                order_data = resp.get("order", {})
                order_id = order_data.get("order_id", f"unknown-{uuid.uuid4()}")
                self._pending_order_ids.append(order_id)

                self._record_order(order_data, decision, line_id, now_et, notional)

                # ── Wait for fill ─────────────────────────────────────────────
                filled_qty = await self._wait_for_fill(order_id, kalshi, order_data)
                if filled_qty > 0:
                    fill_notional = filled_qty * price_cents / 100.0
                    line.fills.append(Fill(
                        order_id=order_id,
                        notional=fill_notional,
                        filled_at=datetime.now(_UTC),
                    ))
                    line.cumulative_filled += fill_notional
                    tracker.add_fill_to_line(line_id, fill_notional)

                    if order_id in self._pending_order_ids:
                        self._pending_order_ids.remove(order_id)

                    log.info(
                        "Line %s: filled %d contracts @ %dc = $%.2f "
                        "(cumulative=$%.2f / $%.0f)",
                        line_id, filled_qty, price_cents,
                        fill_notional, line.cumulative_filled, line.capacity_usd,
                    )
                else:
                    log.warning("Line %s: fill timeout for order %s", line_id, order_id)
                    await asyncio.sleep(_LIQUIDITY_WAIT_S)

        except Exception as exc:
            log.error(
                "Line %s: exception during execution — cancelling %d pending orders: %s",
                line_id, len(self._pending_order_ids), exc,
            )
            await self._cancel_all_pending(kalshi)
            raise

        log.info(
            "Line %s closed: %d fill(s), cumulative=$%.2f",
            line_id, len(line.fills), line.cumulative_filled,
        )
        return line

    # ── Fill waiting ──────────────────────────────────────────────────────────

    async def _wait_for_fill(
        self,
        order_id: str,
        kalshi: KalshiClient,
        order_data: dict,
    ) -> int:
        """
        Return number of contracts filled.

        Paper mode: order_data["status"] == "filled" → return count immediately.
        Live mode: poll get_order_status() until filled or timeout.
        """
        if kalshi.paper_mode or order_data.get("status") == "filled":
            return int(order_data.get("count", 0))

        deadline = time.monotonic() + _FILL_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                status = await kalshi.get_order_status(order_id)
                if status.get("status") in ("filled", "executed"):
                    return int(status.get("fill_count", status.get("count", 0)))
            except Exception as exc:
                log.warning("get_order_status(%s) error: %s", order_id, exc)
            await asyncio.sleep(_FILL_POLL_INTERVAL_S)

        log.warning("Fill timeout after %.0fs for order %s", _FILL_TIMEOUT_S, order_id)
        return 0

    # ── Safety: cancel all open orders on exception ───────────────────────────

    async def _cancel_all_pending(self, kalshi: KalshiClient) -> None:
        for oid in list(self._pending_order_ids):
            try:
                await kalshi.cancel_order(oid)
                log.info("Cancelled pending order %s", oid)
            except Exception as exc:
                log.warning("Failed to cancel order %s: %s", oid, exc)
        self._pending_order_ids.clear()

    # ── DB record ─────────────────────────────────────────────────────────────

    def _record_order(
        self,
        order_data: dict,
        decision: EntryDecision,
        line_id: str,
        now_et: datetime,
        notional_usd: float,
    ) -> None:
        order_id = order_data.get("order_id", str(uuid.uuid4()))
        is_paper = order_id.startswith("PAPER-")
        self._db.record_order({
            "id": order_id,
            "client_order_id": order_data.get("client_order_id", str(uuid.uuid4())),
            "kalshi_order_id": None if is_paper else order_id,
            "market_ticker": decision.market_ticker,
            "side": decision.side,
            "strike_price": int(decision.floor_strike or 0),
            "order_price": decision.target_price_cents,
            "quantity": order_data.get("count", 0),
            "status": order_data.get("status", "pending"),
            "created_at": datetime.now(_UTC).isoformat(),
            "filled_at": None,
            "notional_usd": notional_usd,
            "line_id": line_id,
            "spot_at_entry": None,
            "rv_60_at_entry": None,
            "minutes_into_hour": now_et.minute,
            "metadata_json": json.dumps({"paper_mode": is_paper}),
        })
