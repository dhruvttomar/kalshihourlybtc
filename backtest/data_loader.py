"""
Historical data loader for the backtesting framework.

Responsibilities:
- Fetch and cache BTC 1-minute closes from Binance (up to 6+ months)
- Synthesize per-minute Kalshi market snapshots from price history
- Load historical economic calendar events

All data is cached to disk as JSON to avoid repeated API calls.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

_BINANCE_KLINES_URL = "https://api.binance.us/api/v3/klines"
_FINNHUB_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/economic"
_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc

# Kalshi series: strikes are multiples of $100 within the listed range
_STRIKE_STEP = 100.0


@dataclass
class MinuteBar:
    """One 1-minute OHLC bar."""
    ts: float          # unix timestamp (open of bar)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class KalshiSnapshot:
    """
    A simulated Kalshi market snapshot at a particular minute.

    The YES market settles YES if BTC > floor_strike at expiry.
    Settlement is at the top of `close_hour_utc`.
    """
    ticker: str
    floor_strike: float
    close_time: datetime       # UTC
    yes_ask: float             # simulated ask [0, 1]
    no_ask: float
    depth_yes_usd: float       # simulated depth available at ask
    depth_no_usd: float


def fetch_binance_1min(
    start: datetime,
    end: datetime,
    cache_dir: Path | str = "data/backtest_cache",
) -> list[MinuteBar]:
    """
    Download BTC/USDT 1-minute bars from Binance for [start, end].

    Results are cached to `cache_dir/btc_1min_<start_ms>_<end_ms>.json`.
    `start` and `end` must be UTC-aware datetimes.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    cache_file = cache_dir / f"btc_1min_{start_ms}_{end_ms}.json"

    if cache_file.exists():
        raw = json.loads(cache_file.read_text())
        return [MinuteBar(**b) for b in raw]

    bars = _download_binance_klines(start_ms, end_ms)
    cache_file.write_text(json.dumps([b.__dict__ for b in bars]))
    return bars


def _download_binance_klines(start_ms: int, end_ms: int) -> list[MinuteBar]:
    bars: list[MinuteBar] = []
    cursor_ms = start_ms

    while cursor_ms < end_ms:
        limit = min(1000, int((end_ms - cursor_ms) / 60_000) + 1)
        resp = requests.get(_BINANCE_KLINES_URL, params={
            "symbol": "BTCUSDT",
            "interval": "1m",
            "startTime": cursor_ms,
            "endTime": end_ms,
            "limit": limit,
        }, timeout=20)
        resp.raise_for_status()
        candles = resp.json()
        if not candles:
            break
        for c in candles:
            bars.append(MinuteBar(
                ts=float(c[0]) / 1000.0,
                open=float(c[1]),
                high=float(c[2]),
                low=float(c[3]),
                close=float(c[4]),
                volume=float(c[5]),
            ))
        cursor_ms = int(candles[-1][6]) + 1  # close_time of last candle + 1ms
        if len(candles) < limit:
            break
        time.sleep(0.05)  # be gentle with Binance rate limits

    return bars


def synthesize_kalshi_snapshots(
    bars: list[MinuteBar],
    tiers: list[tuple[float, float]] | None = None,
    depth_per_side_usd: float = 5000.0,
    # Legacy keyword args kept for callers that haven't been updated
    yes_ask: float = 0.99,
    no_ask: float = 0.99,
) -> dict[tuple[int, float], list[KalshiSnapshot]]:
    """
    Build simulated Kalshi market snapshots from historical price bars.

    Each tier is a (buffer_usd, ask_price) pair. For every tier the strike is
    set to floor((ref_spot - buffer_usd) / 100) * 100, generating a separate
    KXBTCD market per hour per tier. This lets the grid-backtest test 97¢ and
    95¢ contracts by using smaller buffers alongside the 99¢ default.

    Returns a dict keyed by (unix_minute_bucket, floor_strike) → list of
    KalshiSnapshot objects for every bar in that hour.
    """
    if tiers is None:
        tiers = [(600.0, yes_ask)]

    if not bars:
        return {}

    # Group bars by the UTC hour they fall in
    hour_buckets: dict[int, list[MinuteBar]] = {}
    for bar in bars:
        hour_ts = int(bar.ts // 3600) * 3600
        hour_buckets.setdefault(hour_ts, []).append(bar)

    snapshots: dict[tuple[int, float], list[KalshiSnapshot]] = {}

    for hour_ts, hour_bars in sorted(hour_buckets.items()):
        close_time = datetime.fromtimestamp(hour_ts + 3600, tz=_UTC)
        ref_spot = hour_bars[0].open

        for buffer_usd, ask_price in tiers:
            floor_strike = math.floor((ref_spot - buffer_usd) / _STRIKE_STEP) * _STRIKE_STEP
            ticker = _make_ticker(close_time, floor_strike)

            for bar in hour_bars:
                minute_bucket = int(bar.ts // 60)
                key = (minute_bucket, floor_strike)
                snap = KalshiSnapshot(
                    ticker=ticker,
                    floor_strike=floor_strike,
                    close_time=close_time,
                    yes_ask=ask_price,
                    no_ask=ask_price,
                    depth_yes_usd=depth_per_side_usd,
                    depth_no_usd=depth_per_side_usd,
                )
                snapshots.setdefault(key, []).append(snap)

    return snapshots


def _make_ticker(close_time: datetime, floor_strike: float) -> str:
    """Format a plausible KXBTCD ticker from settlement time and strike."""
    dt_et = close_time.astimezone(_ET)
    # e.g. KXBTCD-26MAY2115-T86299.99
    date_part = dt_et.strftime("%d%b%y").upper()
    hour_part = dt_et.strftime("%H")
    return f"KXBTCD-{date_part}{hour_part}-T{floor_strike:.2f}"


def load_econ_events(
    start: datetime,
    end: datetime,
    finnhub_api_key: str = "",
    cache_dir: Path | str = "data/backtest_cache",
) -> list[dict]:
    """
    Load historical Finnhub economic calendar events for [start, end].

    Returns a list of event dicts with keys: event, time, country, impact.
    Falls back to an empty list if Finnhub key is not provided or request fails.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    cache_file = cache_dir / f"econ_{start_str}_{end_str}.json"

    if cache_file.exists():
        return json.loads(cache_file.read_text())

    if not finnhub_api_key:
        return []

    try:
        resp = requests.get(_FINNHUB_CALENDAR_URL, params={
            "from": start_str,
            "to": end_str,
            "token": finnhub_api_key,
        }, timeout=15)
        resp.raise_for_status()
        events = resp.json().get("economicCalendar", [])
        cache_file.write_text(json.dumps(events))
        return events
    except Exception:
        return []
