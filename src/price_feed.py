"""
BTC-USD price feed using Kraken REST polling.

Polls Kraken spot price every 10 seconds. Bootstraps 1-minute OHLC history
from Kraken on startup. No WebSocket — REST is simpler and reliable enough
for a 15-second trading loop.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

from src.vol_calculator import realized_vol_annualized

_KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
_KRAKEN_SPOT_URL = "https://api.kraken.com/0/public/Ticker"
_COINBASE_REST_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

_MINUTES_PER_YEAR = 525_600
_BUFFER_MINUTES = 10_080   # 7 days of 1-min data
_1H_MINUTES = 60
_24H_MINUTES = 1_440
_STATE_STALE_SECONDS = 45  # stale if no successful poll in 45s
_POLL_INTERVAL_S = 10      # fetch spot price every 10 seconds


@dataclass
class PriceState:
    spot: float
    timestamp: datetime
    rv_60_annualized: float
    rv_24h_annualized: float
    rv_baseline_7d_median: float
    is_stale: bool


class PriceFeed:
    """
    BTC-USD price feed backed by Kraken REST polling.

    Usage:
        feed = PriceFeed()
        asyncio.create_task(feed.run())
        state = await feed.get_current_state()
    """

    def __init__(self, config=None, state_file: str = "data/price_state.json"):
        self._state_file = Path(state_file)
        self._deque: deque[tuple[float, float]] = deque(maxlen=_BUFFER_MINUTES)
        self._lock = asyncio.Lock()
        self._last_tick_ts: float = 0.0
        self._last_tick_price: float = 0.0
        self._current_minute: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_current_state(self) -> PriceState:
        async with self._lock:
            data = list(self._deque)
            last_tick_ts = self._last_tick_ts
            last_tick_price = self._last_tick_price

        spot = last_tick_price if last_tick_price else (data[-1][1] if data else float("nan"))
        is_stale = (time.time() - last_tick_ts) > _STATE_STALE_SECONDS if last_tick_ts else True

        now = time.time()
        prices_60 = [p for ts, p in data if ts >= now - _1H_MINUTES * 60]
        prices_24h = [p for ts, p in data if ts >= now - _24H_MINUTES * 60]

        rv_60 = realized_vol_annualized(prices_60) if len(prices_60) >= 2 else float("nan")
        rv_24h = realized_vol_annualized(prices_24h) if len(prices_24h) >= 2 else float("nan")
        rv_baseline = self._compute_7d_median(data)

        return PriceState(
            spot=spot,
            timestamp=datetime.fromtimestamp(last_tick_ts or now, tz=timezone.utc),
            rv_60_annualized=rv_60,
            rv_24h_annualized=rv_24h,
            rv_baseline_7d_median=rv_baseline,
            is_stale=is_stale,
        )

    def get_recent_prices(self, n: int) -> list[float]:
        data = list(self._deque)
        prices = [p for _, p in data]
        return prices[-n:] if len(prices) >= n else prices

    async def run(self) -> None:
        """Main loop: bootstrap history, then poll spot price every 10 seconds."""
        import logging
        _log = logging.getLogger(__name__)

        self._load_persisted_state()
        if len(self._deque) < _24H_MINUTES:
            await asyncio.get_event_loop().run_in_executor(None, self._bootstrap_ohlc)

        _log.info("price_feed polling Kraken REST every %ds", _POLL_INTERVAL_S)
        last_persist = time.time()

        while True:
            try:
                spot = await asyncio.get_event_loop().run_in_executor(
                    None, self._fetch_spot
                )
                now_ts = time.time()
                async with self._lock:
                    self._last_tick_ts = now_ts
                    self._last_tick_price = spot
                    minute_bucket = int(now_ts // 60)
                    if minute_bucket != self._current_minute:
                        if self._current_minute > 0:
                            self._deque.append((self._current_minute * 60.0, spot))
                        self._current_minute = minute_bucket

                if now_ts - last_persist >= 60:
                    self._persist_state()
                    last_persist = now_ts

            except Exception as exc:
                _log.warning("price_feed poll error: %s", exc)

            await asyncio.sleep(_POLL_INTERVAL_S)

    # ── REST helpers ──────────────────────────────────────────────────────────

    def _fetch_spot(self) -> float:
        """Fetch current BTC/USD spot from Kraken, fallback to Coinbase."""
        try:
            r = requests.get(_KRAKEN_SPOT_URL, params={"pair": "XBTUSD"}, timeout=8)
            r.raise_for_status()
            data = r.json()
            if not data.get("error"):
                ticker = next(iter(data["result"].values()))
                return float(ticker["c"][0])
        except Exception:
            pass
        # Fallback: Coinbase public REST
        r = requests.get(_COINBASE_REST_URL, timeout=8)
        r.raise_for_status()
        return float(r.json()["data"]["amount"])

    def _bootstrap_ohlc(self) -> None:
        """Pre-fill deque with Kraken 1-min OHLC history (up to 7 days)."""
        import logging
        _log = logging.getLogger(__name__)
        try:
            closes = self._fetch_kraken_ohlc(_BUFFER_MINUTES)
            if closes:
                now = time.time()
                self._deque.clear()
                for i, price in enumerate(closes):
                    ts = now - (len(closes) - 1 - i) * 60.0
                    self._deque.append((ts, price))
                self._last_tick_price = closes[-1]
                self._last_tick_ts = time.time()
                _log.info("price_feed bootstrapped %d candles from Kraken", len(closes))
                return
        except Exception as exc:
            _log.warning("OHLC bootstrap failed: %s — will build history via polling", exc)

        # No history: seed with a single spot price so is_stale clears immediately
        try:
            spot = self._fetch_spot()
            now = time.time()
            self._deque.append((now, spot))
            self._last_tick_price = spot
            self._last_tick_ts = now
            _log.info("price_feed seeded with spot price %.2f", spot)
        except Exception as exc:
            _log.warning("Spot seed also failed: %s", exc)

    def _fetch_kraken_ohlc(self, n: int) -> list[float]:
        closes: list[float] = []
        since = int(time.time()) - n * 60
        while len(closes) < n:
            r = requests.get(
                _KRAKEN_OHLC_URL,
                params={"pair": "XBTUSD", "interval": 1, "since": since},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("error"):
                raise ValueError(f"Kraken OHLC error: {data['error']}")
            result = data.get("result", {})
            candles = result.get("XXBTZUSD") or result.get("XBTUSD") or []
            if not candles:
                break
            closes.extend(float(c[4]) for c in candles)
            last_ts = int(data["result"].get("last", 0))
            if not last_ts or len(candles) < 720:
                break
            since = last_ts
        return closes[-n:]

    # ── State persistence ─────────────────────────────────────────────────────

    def _persist_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "ts": time.time(),
                "last_price": self._last_tick_price,
                "deque": list(self._deque)[-_24H_MINUTES:],
            }
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot))
            tmp.replace(self._state_file)
        except Exception:
            pass

    def _load_persisted_state(self) -> None:
        try:
            if not self._state_file.exists():
                return
            raw = json.loads(self._state_file.read_text())
            saved_ts = float(raw.get("ts", 0))
            if time.time() - saved_ts > 300:
                return
            for entry in raw.get("deque", []):
                self._deque.append((float(entry[0]), float(entry[1])))
            self._last_tick_price = float(raw.get("last_price", 0))
            self._last_tick_ts = saved_ts
        except Exception:
            pass

    # ── Vol helpers ───────────────────────────────────────────────────────────

    def _compute_7d_median(self, data: list[tuple[float, float]]) -> float:
        if not data:
            return float("nan")
        now = time.time()
        window = [(ts, p) for ts, p in data if ts >= now - 7 * 24 * 3600]
        if len(window) < _24H_MINUTES * 2:
            return float("nan")

        bin_vols: list[float] = []
        bin_size_s = _24H_MINUTES * 60.0
        bin_start = window[0][0]
        bin_prices: list[float] = []

        for ts, price in window:
            if ts < bin_start + bin_size_s:
                bin_prices.append(price)
            else:
                if len(bin_prices) >= 2:
                    v = realized_vol_annualized(bin_prices)
                    if not math.isnan(v):
                        bin_vols.append(v)
                bin_prices = [price]
                bin_start = ts

        if len(bin_prices) >= 2:
            v = realized_vol_annualized(bin_prices)
            if not math.isnan(v):
                bin_vols.append(v)

        if len(bin_vols) < 2:
            return float("nan")
        return float(np.median(bin_vols))
