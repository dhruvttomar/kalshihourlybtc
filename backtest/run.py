"""
Full historical backtest runner.

Usage:
    python -m backtest.run              # 7 months (default)
    python -m backtest.run --months 9
    python -m backtest.run --start 2025-08-01 --end 2026-05-21

Outputs:
    - Console report (aggregate stats, bucketed P&L, sanity gates)
    - data/backtest_cache/trades_<timestamp>.csv
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# Ensure project root is on the path when run as `python -m backtest.run`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.data_loader import (
    MinuteBar,
    fetch_binance_1min,
    load_econ_events,
    synthesize_kalshi_snapshots,
)
from backtest.replay import run_backtest
from backtest.reports import export_trades_csv, print_report
from src.blackout_calendar import BlackoutCalendar
from src.config import load_config
from src.models import EconEvent

_UTC = timezone.utc
_ET = ZoneInfo("America/New_York")


# ── Economic calendar helpers ─────────────────────────────────────────────────

def _raw_events_to_econ(raw_events: list[dict]) -> list[EconEvent]:
    """Convert raw Finnhub dicts to EconEvent objects (high-impact US only)."""
    events: list[EconEvent] = []
    for item in raw_events:
        if item.get("country") != "US":
            continue
        impact = str(item.get("impact", "")).lower()
        if impact not in ("high", "3"):
            continue
        time_str = item.get("time", "")
        if not time_str:
            continue
        try:
            if str(time_str).isdigit():
                dt = datetime.fromtimestamp(int(time_str), tz=_UTC)
            else:
                dt = datetime.fromisoformat(str(time_str).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_UTC)
        except (ValueError, TypeError):
            continue
        events.append(EconEvent(
            name=item.get("event", ""),
            event_time_utc=dt,
            impact="high",
            country="US",
        ))
    return events


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Run Kalshi BTC strategy backtest")
    p.add_argument("--months", type=float, default=7.0,
                   help="How many months of history to fetch (default: 7)")
    p.add_argument("--start", type=str, default="",
                   help="Override start date YYYY-MM-DD (UTC)")
    p.add_argument("--end", type=str, default="",
                   help="Override end date YYYY-MM-DD (UTC, default: today)")
    p.add_argument("--slippage", type=float, default=0.01,
                   help="Slippage pct for sanity gate (default: 0.01 = 1%%)")
    p.add_argument("--cache-dir", default="data/backtest_cache",
                   help="Directory for cached Binance data")
    p.add_argument("--no-csv", action="store_true",
                   help="Skip CSV export")
    p.add_argument("--range-limit", type=float, default=0.0,
                   help="Skip hours where prior hour BTC range > this USD value (0=off, 750=recommended)")
    p.add_argument("--fill-usd", type=float, default=0.0,
                   help="Capital per trade in USD (0=use config default)")
    # ── Grid / tier overrides ─────────────────────────────────────────────────
    p.add_argument("--tiers", type=str, default="600:0.99",
                   help="Comma-separated buffer:price tiers, e.g. '600:0.99,400:0.97,200:0.95'")
    p.add_argument("--price-min", type=float, default=0.0,
                   help="Override yes_price_min in config (0=use config default)")
    p.add_argument("--price-max", type=float, default=0.0,
                   help="Override yes_price_max in config (0=use config default)")
    p.add_argument("--buffer-floor", type=float, default=0.0,
                   help="Override buffer_floor_usd in config (0=use config default)")
    p.add_argument("--max-lines", type=int, default=0,
                   help="Override max_lines_per_hour in config (0=use config default)")
    p.add_argument("--label", type=str, default="",
                   help="Optional label printed in the report header")
    args = p.parse_args()

    config = load_config("config/default.yaml")

    # ── Parse tiers ───────────────────────────────────────────────────────────
    tiers: list[tuple[float, float]] = []
    for tier_str in args.tiers.split(","):
        parts = tier_str.strip().split(":")
        tiers.append((float(parts[0]), float(parts[1])))

    # ── Apply config overrides ────────────────────────────────────────────────
    strategy_updates: dict = {}
    if args.price_min > 0:
        strategy_updates["yes_price_min"] = args.price_min
    if args.price_max > 0:
        strategy_updates["yes_price_max"] = args.price_max
    if args.buffer_floor > 0:
        strategy_updates["buffer_floor_usd"] = args.buffer_floor
    if args.max_lines > 0:
        strategy_updates["max_lines_per_hour"] = args.max_lines
        fill = args.fill_usd if args.fill_usd > 0 else config.strategy.max_capital_per_line_usd
        strategy_updates["max_capital_per_hour_usd"] = args.max_lines * fill
    if strategy_updates:
        config = config.model_copy(
            update={"strategy": config.strategy.model_copy(update=strategy_updates)}
        )

    # ── Date range ────────────────────────────────────────────────────────────
    end_dt = (
        datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=_UTC)
        if args.end
        else datetime.now(_UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    start_dt = (
        datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=_UTC)
        if args.start
        else end_dt - timedelta(days=int(args.months * 30.44))
    )

    label = f" [{args.label}]" if args.label else ""
    print(f"\n{'='*60}")
    print(f"Kalshi BTC Hourly Strategy — Historical Backtest{label}")
    print(f"{'='*60}")
    print(f"Range : {start_dt.date()} → {end_dt.date()}")
    print(f"Days  : {(end_dt - start_dt).days}")
    print()

    # ── Step 1: Fetch price data ──────────────────────────────────────────────
    print("Step 1/4 — Fetching BTC 1-min closes from Binance...")
    t0 = time.time()
    bars = fetch_binance_1min(start_dt, end_dt, cache_dir=args.cache_dir)
    elapsed = time.time() - t0
    print(f"         {len(bars):,} bars loaded in {elapsed:.1f}s")
    if len(bars) < 1000:
        print("ERROR: Too few bars fetched. Check Binance connectivity.")
        sys.exit(1)

    # ── Step 2: Load economic calendar ───────────────────────────────────────
    print("Step 2/4 — Loading economic calendar from Finnhub...")
    finnhub_key = os.environ.get("FINNHUB_KEY", "")
    raw_events = load_econ_events(start_dt, end_dt,
                                  finnhub_api_key=finnhub_key,
                                  cache_dir=args.cache_dir)
    econ_events = _raw_events_to_econ(raw_events)
    print(f"         {len(econ_events)} high-impact US events loaded")

    blackout = BlackoutCalendar(
        config_path="config/blackouts.yaml",
        max_risk_level=config.strategy.max_day_risk_level_to_trade,
        econ_events=econ_events,
    )

    # ── Step 3: Build Kalshi market snapshots ─────────────────────────────────
    print("Step 3/4 — Synthesizing Kalshi market snapshots...")
    t0 = time.time()
    snapshots = synthesize_kalshi_snapshots(
        bars,
        tiers=tiers,
        depth_per_side_usd=10_000.0,
    )
    elapsed = time.time() - t0
    hours_covered = len({v[0].close_time for v in snapshots.values()})
    print(f"         {len(snapshots):,} minute-snapshots across {hours_covered} hours "
          f"({elapsed:.1f}s)")

    # ── Step 4: Run replay ────────────────────────────────────────────────────
    print("Step 4/4 — Running replay engine...")
    t0 = time.time()
    result = run_backtest(
        bars=bars,
        snapshots=snapshots,
        blackout=blackout,
        config=config.strategy,
        slippage_pct=0.0,          # slippage applied separately in sanity gate
        fill_usd_per_trade=args.fill_usd if args.fill_usd > 0 else config.strategy.max_capital_per_line_usd,
        prev_hour_range_limit_usd=args.range_limit,
    )
    elapsed = time.time() - t0
    print(f"         Done in {elapsed:.1f}s — "
          f"{len(result.trades)} trades, {len(result.rejects)} rejects\n")

    # ── Report ────────────────────────────────────────────────────────────────
    print_report(result, slippage_pct=args.slippage)

    # ── CSV export ────────────────────────────────────────────────────────────
    if not args.no_csv and result.trades:
        ts_tag = datetime.now(_UTC).strftime("%Y%m%d_%H%M%S")
        csv_path = Path(args.cache_dir) / f"trades_{ts_tag}.csv"
        export_trades_csv(result, csv_path)
        print(f"\nTrade log exported → {csv_path}")


if __name__ == "__main__":
    main()
