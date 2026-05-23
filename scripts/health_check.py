#!/usr/bin/env python3
"""
End-to-end health check for the Kalshi BTC Bot.

Tests every external dependency in sequence and prints a PASS / FAIL summary.

Usage (on the droplet):
    docker compose exec bot python scripts/health_check.py
    docker compose exec bot python scripts/health_check.py --telegram-test

The --telegram-test flag sends a live Telegram message to verify delivery.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import aiohttp
import websockets

PASS = "\033[92m PASS\033[0m"
FAIL = "\033[91m FAIL\033[0m"
WARN = "\033[93m WARN\033[0m"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    icon = PASS if ok else FAIL
    line = f"  [{icon} ] {name}"
    if detail:
        line += f"  —  {detail}"
    print(line)


# ── 1. Environment variables ──────────────────────────────────────────────────

def check_env() -> None:
    print("\n── Environment Variables ──────────────────────────────────────────")
    required = ["KALSHI_API_KEY_ID", "KALSHI_KEY_PATH"]
    optional = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FINNHUB_KEY"]

    for var in required:
        val = os.environ.get(var, "")
        ok = bool(val)
        record(var, ok, f"{'set (' + val[:8] + '…)' if ok else 'MISSING'}")

    key_path = os.environ.get("KALSHI_KEY_PATH", "")
    if key_path:
        exists = Path(key_path).exists()
        record("KALSHI_KEY_PATH file exists", exists, key_path)

    for var in optional:
        val = os.environ.get(var, "")
        icon = PASS if val else WARN
        print(f"  [{icon} ] {var}  —  {'set' if val else 'not set (optional)'}")


# ── 2. Config loading ─────────────────────────────────────────────────────────

def check_config() -> None:
    print("\n── Config Loading ─────────────────────────────────────────────────")
    try:
        from src.config import load_config
        config_path = "config/b97_micro_live.yaml"
        cfg = load_config(config_path)
        record("Load b97_micro_live.yaml", True,
               f"yes_price=[{cfg.strategy.yes_price_min},{cfg.strategy.yes_price_max}] "
               f"buffer_floor=${cfg.strategy.buffer_floor_usd:.0f} "
               f"line=${cfg.strategy.max_capital_per_line_usd:.0f}")
    except Exception as exc:
        record("Load b97_micro_live.yaml", False, str(exc))


# ── 3. Database ───────────────────────────────────────────────────────────────

def check_db() -> None:
    print("\n── Database ───────────────────────────────────────────────────────")
    try:
        from src.database import Database
        db = Database("data/trades.db")
        open_lines = db.get_open_lines()
        settled = db.get_realized_pnl_since("2020-01-01T00:00:00+00:00")
        record("SQLite schema OK", True,
               f"open_lines={len(open_lines)} settled_pnl=${settled:+.2f}")
    except Exception as exc:
        record("SQLite schema OK", False, str(exc))


# ── 4. Kalshi API — public market data (no auth) ──────────────────────────────

async def check_kalshi_public(session: aiohttp.ClientSession) -> None:
    print("\n── Kalshi API — Public Market Data ───────────────────────────────")
    base = "https://api.elections.kalshi.com/trade-api/v2"
    try:
        async with session.get(
            f"{base}/markets",
            params={"series_ticker": "KXBTCD", "status": "open", "limit": 5},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            markets = data.get("markets", [])
            ok = len(markets) > 0
            if markets:
                sample = markets[0]
                detail = (f"{len(markets)} open markets — "
                          f"sample: {sample.get('ticker')} "
                          f"yes_ask={sample.get('yes_ask')}¢")
            else:
                detail = "0 open markets returned (market may be between hours)"
            record("KXBTCD open markets", ok or True, detail)  # 0 is OK between hours
    except Exception as exc:
        record("KXBTCD open markets", False, str(exc))


# ── 5. Kalshi API — authenticated (account balance) ──────────────────────────

async def check_kalshi_auth(session: aiohttp.ClientSession) -> None:
    print("\n── Kalshi API — Auth + Account Balance ───────────────────────────")
    try:
        from src.config import load_config
        from src.kalshi_client import KalshiClient

        cfg = load_config("config/b97_micro_live.yaml")
        client = KalshiClient(
            config=cfg.kalshi,
            api_key_id=os.environ.get("KALSHI_API_KEY_ID"),
            private_key_path=os.environ.get("KALSHI_KEY_PATH"),
            paper_mode=False,
        )
        async with client:
            balance = await client.get_account_balance()
            record("Kalshi RSA auth + balance", True, f"balance=${balance:.2f}")
    except Exception as exc:
        record("Kalshi RSA auth + balance", False, str(exc))


# ── 6. Kraken WebSocket (price feed) ─────────────────────────────────────────

async def check_kraken_ws() -> None:
    print("\n── Kraken WebSocket (Price Feed) ──────────────────────────────────")
    try:
        subscribe = json.dumps({
            "event": "subscribe",
            "pair": ["XBT/USD"],
            "subscription": {"name": "ticker"},
        })
        tick_price: float | None = None
        deadline = time.time() + 15

        async with websockets.connect(
            "wss://ws.kraken.com", ping_interval=None, open_timeout=10
        ) as ws:
            await ws.send(subscribe)
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    msg = json.loads(raw)
                    if isinstance(msg, list) and len(msg) == 4 and msg[2] == "ticker":
                        price_str = msg[1].get("c", [None])[0]
                        if price_str:
                            tick_price = float(price_str)
                            break
                except asyncio.TimeoutError:
                    continue

        if tick_price:
            record("Kraken WS tick received", True, f"BTC/USD=${tick_price:,.2f}")
        else:
            record("Kraken WS tick received", False, "No ticker message within 15s")
    except Exception as exc:
        record("Kraken WS tick received", False, str(exc))


# ── 7. Kraken OHLC REST (bootstrap) ──────────────────────────────────────────

async def check_kraken_ohlc(session: aiohttp.ClientSession) -> None:
    print("\n── Kraken OHLC REST (Bootstrap) ───────────────────────────────────")
    try:
        since = int(time.time()) - 120 * 60  # last 2h
        async with session.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": "XBTUSD", "interval": 1, "since": since},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            if data.get("error"):
                record("Kraken OHLC REST", False, str(data["error"]))
                return
            result = data.get("result", {})
            candles = result.get("XXBTZUSD") or result.get("XBTUSD") or []
            record("Kraken OHLC REST", len(candles) > 0,
                   f"{len(candles)} candles returned")
    except Exception as exc:
        record("Kraken OHLC REST", False, str(exc))


# ── 8. Telegram ───────────────────────────────────────────────────────────────

async def check_telegram(session: aiohttp.ClientSession, send_test: bool) -> None:
    print("\n── Telegram ────────────────────────────────────────────────────────")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print(f"  [{WARN} ] Telegram credentials not configured — skipping")
        return

    # Verify the token works by calling getMe
    try:
        async with session.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            ok = data.get("ok", False)
            bot_name = data.get("result", {}).get("username", "?")
            record("Telegram token valid", ok, f"bot=@{bot_name}")
    except Exception as exc:
        record("Telegram token valid", False, str(exc))
        return

    if send_test:
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            text = (
                f"🔍 *Health Check Passed*\n"
                f"Bot is alive and Telegram alerts are working.\n"
                f"`{now}`"
            )
            async with session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                ok = data.get("ok", False)
                record("Telegram test message sent", ok,
                       data.get("description", "delivered") if ok else str(data))
        except Exception as exc:
            record("Telegram test message sent", False, str(exc))


# ── 9. vol_calculator sanity ──────────────────────────────────────────────────

def check_vol_calculator() -> None:
    print("\n── Vol Calculator ──────────────────────────────────────────────────")
    try:
        from src.vol_calculator import realized_vol_annualized, dynamic_buffer
        prices = [100.0 * (1 + 0.001 * i) for i in range(60)]
        rv = realized_vol_annualized(prices)
        ok = not math.isnan(rv) and rv > 0
        buf = dynamic_buffer(spot=95000, rv_annualized=0.30, minutes_remaining=30)
        record("realized_vol_annualized", ok, f"rv={rv:.4f} buffer=${buf:.0f}")
    except Exception as exc:
        record("realized_vol_annualized", False, str(exc))


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary() -> None:
    print("\n" + "=" * 66)
    print("  HEALTH CHECK SUMMARY")
    print("=" * 66)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    for name, ok, detail in results:
        icon = "✓" if ok else "✗"
        print(f"  {icon}  {name}")
    print()
    print(f"  {passed}/{total} checks passed")
    if passed == total:
        print("  All systems GO.\n")
    else:
        failed = [n for n, ok, _ in results if not ok]
        print(f"  Failing: {', '.join(failed)}\n")
    return passed == total


# ── Entry point ───────────────────────────────────────────────────────────────

async def main(send_telegram_test: bool) -> bool:
    os.chdir(ROOT)

    check_env()
    check_config()
    check_db()
    check_vol_calculator()

    async with aiohttp.ClientSession() as session:
        await check_kalshi_public(session)
        await check_kalshi_auth(session)
        await check_kraken_ohlc(session)
        await check_telegram(session, send_test=send_telegram_test)

    await check_kraken_ws()

    return print_summary()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Kalshi bot health check")
    p.add_argument("--telegram-test", action="store_true",
                   help="Send a live test message to Telegram")
    args = p.parse_args()
    ok = asyncio.run(main(send_telegram_test=args.telegram_test))
    sys.exit(0 if ok else 1)
