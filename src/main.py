"""
Main entry point and trading loop orchestrator.

Run with:
    python -m src.main                    # paper mode (default)
    python -m src.main --live             # live trading (requires API keys)
    python -m src.main --config path.yaml # custom config file

Paper mode is the default. Pass --live only after validating via the Phase 5
backtest and completing a clean 2-week paper trading run per the spec.

Signal handling:
    SIGTERM / SIGINT → sets shutdown_event → main loop exits cleanly after
    cancelling any pending orders. The bot must be stoppable at any time
    without leaving orphaned orders on Kalshi.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv

from src.alerts import Alerts
from src.blackout_calendar import BlackoutCalendar
from src.config import load_config
from src.database import Database
from src.entry_logic import evaluate_entry
from src.finnhub_client import FinnhubClient
from src.kalshi_client import KalshiClient
from src.logger import configure_logging, get_logger
from src.market_scanner import scan_qualifying_markets
from src.models import RiskState
from src.order_executor import OrderExecutor
from src.position_tracker import PositionTracker
from src.price_feed import PriceFeed
from src.risk_manager import RiskManager
from src.settlement_monitor import run_settlement_monitor

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc

log = get_logger(__name__)

# How often the main loop polls for new opportunities (seconds)
_LOOP_INTERVAL_S: int = 15

# How long to sleep when blocked by blackout or risk (seconds)
_BLOCKED_SLEEP_S: int = 30


# ── Background tasks ──────────────────────────────────────────────────────────


async def _refresh_calendar_daily(
    blackout: BlackoutCalendar,
    finnhub: FinnhubClient,
) -> None:
    """Refresh the Finnhub economic calendar once per day at 00:05 ET."""
    while True:
        now_et = datetime.now(_ET)
        # Compute seconds until next 00:05 ET
        target = now_et.replace(hour=0, minute=5, second=0, microsecond=0)
        if now_et >= target:
            target += timedelta(days=1)
        sleep_s = (target - now_et).total_seconds()
        log.info("Next calendar refresh in %.0f minutes", sleep_s / 60)
        await asyncio.sleep(sleep_s)
        try:
            await blackout.refresh_economic_calendar(finnhub)
            log.info("Economic calendar refreshed")
        except Exception as exc:
            log.warning("Calendar refresh failed (stale data retained): %s", exc)


async def _monitor_vol_spikes(
    risk: RiskManager,
    price_feed: PriceFeed,
    alerts: Alerts,
) -> None:
    """Check for vol spikes every 60 seconds and trigger circuit breakers."""
    while True:
        await asyncio.sleep(60)
        try:
            state = await price_feed.get_current_state()
            prices = list(price_feed.get_recent_prices(30))
            if not prices:
                continue
            now_utc = datetime.now(_UTC)
            triggered = risk.check_vol_spike(now_utc, prices)
            if triggered:
                cb_until = risk.state.vol_circuit_breaker_until
                if cb_until:
                    await alerts.notify_circuit_breaker(
                        "Vol circuit breaker",
                        f"Spike detected (spot={state.spot:.0f})",
                        cb_until,
                    )
        except Exception as exc:
            log.warning("Vol spike monitor error: %s", exc)


async def _daily_summary(risk: RiskManager, alerts: Alerts) -> None:
    """Send a daily summary at 00:00 ET."""
    while True:
        now_et = datetime.now(_ET)
        target = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        if now_et >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now_et).total_seconds())
        try:
            risk.refresh_pnl()
            await alerts.notify_daily_summary(
                trades=0,  # would query DB for daily trade count
                daily_pnl=risk.state.daily_pnl,
                blackout_count=0,
                circuit_breaker_count=0,
            )
        except Exception as exc:
            log.warning("Daily summary failed: %s", exc)


# ── Main trading loop ─────────────────────────────────────────────────────────


async def _trading_loop(
    config,
    kalshi: KalshiClient,
    price_feed: PriceFeed,
    blackout: BlackoutCalendar,
    risk: RiskManager,
    tracker: PositionTracker,
    alerts: Alerts,
    db: Database,
    shutdown_event: asyncio.Event,
) -> None:
    while not shutdown_event.is_set():
        try:
            now_et = datetime.now(_ET)
            now_utc = now_et.astimezone(_UTC)

            # ── Hourly counter reset (top of hour) ────────────────────────────
            if now_et.minute == 0 and now_et.second < _LOOP_INTERVAL_S:
                tracker.reset_hourly_counters()

            # ── Blackout check ────────────────────────────────────────────────
            blocked, reason = blackout.is_blocked(now_et)
            if blocked:
                log.info("blackout_active", reason=reason)
                await asyncio.sleep(_BLOCKED_SLEEP_S)
                continue

            # ── Risk check ────────────────────────────────────────────────────
            risk.refresh_pnl(now_utc)
            can_trade, risk_reason = risk.check_can_trade(now_utc)
            if not can_trade:
                log.warning("risk_blocked", reason=risk_reason)
                await asyncio.sleep(_BLOCKED_SLEEP_S)
                continue

            # ── Too early in the hour ─────────────────────────────────────────
            if now_et.minute < config.strategy.min_minutes_into_hour:
                sleep_s = (config.strategy.min_minutes_into_hour - now_et.minute) * 60
                log.debug("Waiting %ds for min_minutes_into_hour=%d", sleep_s, config.strategy.min_minutes_into_hour)
                await asyncio.sleep(min(sleep_s, _LOOP_INTERVAL_S))
                continue

            # ── Stale price feed ──────────────────────────────────────────────
            price_state = await price_feed.get_current_state()
            if price_state.is_stale:
                log.error("stale_price_feed — skipping scan")
                await asyncio.sleep(_LOOP_INTERVAL_S)
                continue

            # ── Hourly cap already reached ────────────────────────────────────
            if tracker.lines_taken_this_hour(now_et) >= config.strategy.max_lines_per_hour:
                log.debug("Hourly line cap reached — waiting")
                await asyncio.sleep(_LOOP_INTERVAL_S)
                continue

            # ── Scan and evaluate ─────────────────────────────────────────────
            opportunities = await scan_qualifying_markets(
                price_state, kalshi, config.strategy, now_et
            )

            for opp in opportunities:
                if tracker.lines_taken_this_hour(now_et) >= config.strategy.max_lines_per_hour:
                    break

                decision = evaluate_entry(
                    trade=opp,
                    price_state=price_state,
                    blackout=blackout,
                    risk_state=risk.state,
                    position_tracker=tracker,
                    now_et=now_et,
                    config=config.strategy,
                )

                if not decision.should_trade:
                    log.info(
                        "entry_rejected ticker=%s side=%s reason=%s",
                        opp.market.ticker, opp.side, decision.reason,
                    )
                    continue

                log.info(
                    "entry_approved ticker=%s side=%s price=%dc",
                    decision.market_ticker, decision.side, decision.target_price_cents,
                )

                executor = OrderExecutor(db)
                try:
                    line = await executor.execute_line(
                        decision=decision,
                        kalshi=kalshi,
                        tracker=tracker,
                        now_et=now_et,
                        capacity_usd=config.strategy.max_capital_per_line_usd,
                    )
                    if line.fills:
                        await alerts.notify_fill(line, decision)
                        log.info(
                            "line_complete ticker=%s fills=%d total=$%.2f",
                            decision.market_ticker, len(line.fills), line.cumulative_filled,
                        )
                except Exception as exc:
                    log.exception("execute_line failed: %s", exc)
                    await alerts.notify_error(exc, context=f"execute_line {decision.market_ticker}")

            await asyncio.sleep(_LOOP_INTERVAL_S)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.exception("main_loop_error: %s", exc)
            await alerts.notify_error(exc, context="main_loop")
            await asyncio.sleep(_BLOCKED_SLEEP_S)


# ── Entry point ───────────────────────────────────────────────────────────────


async def main(config_path: str = "config/default.yaml", live: bool = False) -> None:
    load_dotenv()

    config = load_config(config_path)
    configure_logging(
        level=config.logging.level,
        log_file=config.logging.log_file,
        rotate_daily=config.logging.rotate_daily,
    )

    paper_mode = not live
    if paper_mode:
        log.info("=" * 60)
        log.info("PAPER MODE — no real orders will be placed")
        log.info("=" * 60)
    else:
        log.warning("LIVE MODE — real orders will be placed on Kalshi")

    db = Database("data/trades.db")
    tracker = PositionTracker(db)
    risk = RiskManager(config.risk, db)

    finnhub = FinnhubClient(
        api_key=os.environ.get("FINNHUB_KEY", ""),
        base_url=config.finnhub.base_url,
    )
    blackout = BlackoutCalendar(
        config_path="config/blackouts.yaml",
        max_risk_level=config.strategy.max_day_risk_level_to_trade,
        econ_events=[],
    )
    # Initial calendar load (best-effort)
    try:
        await blackout.refresh_economic_calendar(finnhub)
    except Exception as exc:
        log.warning("Initial calendar load failed (continuing without events): %s", exc)

    price_feed = PriceFeed(config.coinbase)

    shutdown_event = asyncio.Event()

    async with aiohttp.ClientSession() as session:
        alerts = Alerts(config.alerts, session=session)

        kalshi = KalshiClient(
            config=config.kalshi,
            api_key_id=os.environ.get("KALSHI_API_KEY_ID"),
            private_key_path=os.environ.get("KALSHI_KEY_PATH"),
            paper_mode=paper_mode,
        )

        # ── Signal handlers ───────────────────────────────────────────────────
        loop = asyncio.get_running_loop()

        def _on_shutdown() -> None:
            log.info("Shutdown signal received — exiting cleanly")
            shutdown_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _on_shutdown)
            except NotImplementedError:
                pass  # Windows doesn't support add_signal_handler

        # ── Background tasks ──────────────────────────────────────────────────
        tasks = [
            asyncio.create_task(price_feed.run(), name="price_feed"),
            asyncio.create_task(
                _refresh_calendar_daily(blackout, finnhub),
                name="calendar_refresh",
            ),
            asyncio.create_task(
                _monitor_vol_spikes(risk, price_feed, alerts),
                name="vol_monitor",
            ),
            asyncio.create_task(
                _daily_summary(risk, alerts),
                name="daily_summary",
            ),
            asyncio.create_task(
                run_settlement_monitor(
                    kalshi=kalshi,
                    tracker=tracker,
                    db=db,
                    alerts=alerts,
                    shutdown_event=shutdown_event,
                ),
                name="settlement_monitor",
            ),
        ]

        log.info("Bot started — entering main trading loop")

        try:
            async with kalshi:
                await _trading_loop(
                    config=config,
                    kalshi=kalshi,
                    price_feed=price_feed,
                    blackout=blackout,
                    risk=risk,
                    tracker=tracker,
                    alerts=alerts,
                    db=db,
                    shutdown_event=shutdown_event,
                )
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("Bot shut down cleanly")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Kalshi BTC hourly trading bot")
    p.add_argument("--config", default="config/default.yaml", help="Config YAML path")
    p.add_argument(
        "--live",
        action="store_true",
        help="Enable live trading (default: paper mode)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(config_path=args.config, live=args.live))
