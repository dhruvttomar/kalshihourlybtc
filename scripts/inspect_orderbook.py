#!/usr/bin/env python3
"""
Print YES/NO ask prices for all KXBTCD markets settling this hour.
Run this to see what price range is actually available in the Kalshi orderbook.

Usage:
    docker compose exec bot python scripts/inspect_orderbook.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import aiohttp

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc


async def main() -> None:
    from src.config import load_config
    from src.kalshi_client import KalshiClient

    cfg = load_config("config/b97_micro_live.yaml")
    now_et = datetime.now(_ET)
    next_hour_et = now_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    settlement_utc = next_hour_et.astimezone(_UTC)

    print(f"\nBTC KXBTCD orderbook snapshot")
    print(f"Now ET:        {now_et.strftime('%H:%M:%S')}")
    print(f"Settlement UTC:{settlement_utc.strftime('%H:%M')}")
    print()

    async with aiohttp.ClientSession() as session:
        kalshi = KalshiClient(
            config=cfg.kalshi,
            api_key_id=os.environ.get("KALSHI_API_KEY_ID"),
            private_key_path=os.environ.get("KALSHI_KEY_PATH"),
            paper_mode=False,
        )
        async with kalshi:
            markets = await kalshi.get_active_btc_markets()

        # Filter to current hour
        tolerance = 60
        hour_markets = [
            m for m in markets
            if abs((m.close_time - settlement_utc).total_seconds()) < tolerance
        ]

        if not hour_markets:
            print(f"No markets found for {settlement_utc.strftime('%H:%M')} UTC settlement.")
            print(f"Total open markets: {len(markets)}")
            return

        # Sort by strike descending (nearest to spot first if we knew spot, descending anyway)
        hour_markets.sort(key=lambda m: m.floor_strike, reverse=True)

        print(f"{'Strike':>12}  {'YES ask':>8}  {'NO ask':>8}  {'YES cents':>10}  {'NO cents':>10}")
        print("-" * 60)

        yes_asks = []
        no_asks = []
        for m in hour_markets:
            y = m.yes_ask
            n = m.no_ask
            if y is not None:
                yes_asks.append(y)
            if n is not None:
                no_asks.append(n)
            y_str = f"{y:.2f}" if y is not None else "  null"
            n_str = f"{n:.2f}" if n is not None else "  null"
            y_c = f"{round(y*100)}¢" if y is not None else ""
            n_c = f"{round(n*100)}¢" if n is not None else ""
            print(f"${m.floor_strike:>11,.0f}  {y_str:>8}  {n_str:>8}  {y_c:>10}  {n_c:>10}")

        print()
        print(f"Total markets for this hour: {len(hour_markets)}")
        if yes_asks:
            print(f"YES ask range: {min(yes_asks):.2f} – {max(yes_asks):.2f}")
            in_range = [y for y in yes_asks if 0.95 <= y <= 0.97]
            print(f"YES asks in [0.95, 0.97]: {len(in_range)}")
            in_range_99 = [y for y in yes_asks if 0.98 <= y <= 0.99]
            print(f"YES asks in [0.98, 0.99]: {len(in_range_99)}")


if __name__ == "__main__":
    asyncio.run(main())
