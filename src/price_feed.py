"""
Coinbase Advanced Trade WebSocket price feed for BTC-USD.

Maintains a rolling buffer of 1-minute closes. Bootstraps from Binance REST
on startup, then keeps state via WebSocket ticks. Reconnects with exponential
backoff. Persists state to disk every minute for crash recovery.
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
import websockets
import websockets.exceptions

from src.vol_calculator import realized_vol_annualized

# Kraken WebSocket — public, no auth, no geo-restrictions from US servers
_KRAKEN_WS_URL = "wss://ws.kraken.com"
_KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
_COINBASE_REST_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
_KRAKEN_SPOT_URL = "https://api.kraken.com/0/public/Ticker"

_MINUTES_PER_YEAR = 525_600
_BUFFER_MINUTES = 10_080  # 7 days of 1-min data for rv_baseline_7d_median
_1H_MINUTES = 60
_24H_MINUTES = 1_440
_STATE_STALE_SECONDS = 30


@dataclass
class PriceState:
    spot: float
    timestamp: datetime
    rv_60_annualized: float          # trailing 60-min realized vol, annualized
    rv_24h_annualized: float         # trailing 24h realized vol, annualized
    rv_baseline_7d_median: float     # median of daily rv over last 7 days
    is_stale: bool                   # True if last tick > 30s ago


class PriceFeed:
    """
    Async Coinbase BTC-USD price feed.

    Usage:
        feed = PriceFeed(state_file="data/price_state.json")
        asyncio.create_task(feed.run())
        state = await feed.get_current_state()
    """

    def __init__(
        self,
        config=None,
        state_file: str = "data/price_state.json",
    ):
        # Accept either a CoinbaseConfig object or fall back to defaults
        if config is not None and hasattr(config, "ws_url"):
            self._ws_url = config.ws_url if "kraken" in config.ws_url else _KRAKEN_WS_URL
            self._product_id = getattr(config, "product_id", "BTC-USD")
            self._backoff_initial = getattr(config, "reconnect_backoff_initial", 1.0)
            self._backoff_max = getattr(config, "reconnect_backoff_max", 60.0)
        else:
            self._ws_url = _KRAKEN_WS_URL
            self._product_id = "BTC-USD"
            self._backoff_initial = 1.0
            self._backoff_max = 60.0
        self._state_file = Path(state_file)

        # Deque of (unix_ts_float, close_price_float), oldest first
        self._deque: deque[tuple[float, float]] = deque(maxlen=_BUFFER_MINUTES)
        self._lock = asyncio.Lock()

        self._last_tick_ts: float = 0.0
        self._last_tick_price: float = 0.0
        self._current_minute: int = 0  # unix minute bucket currently accumulating

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_current_state(self) -> PriceState:
        async with self._lock:
            data = list(self._deque)
            last_tick_ts = self._last_tick_ts
            last_tick_price = self._last_tick_price

        spot = last_tick_price if last_tick_price else (data[-1][1] if data else float("nan"))
        is_stale = (time.time() - last_tick_ts) > _STATE_STALE_SECONDS if last_tick_ts else True

        prices_all = [p for _, p in data]
        prices_60 = [p for ts, p in data if ts >= time.time() - _1H_MINUTES * 60]
        prices_24h = [p for ts, p in data if ts >= time.time() - _24H_MINUTES * 60]

        rv_60 = realized_vol_annualized(prices_60) if len(prices_60) >= 2 else float("nan")
        rv_24h = realized_vol_annualized(prices_24h) if len(prices_24h) >= 2 else float("nan")
        rv_baseline = self._compute_7d_median(data)

        return PriceState(
            spot=spot,
            timestamp=datetime.fromtimestamp(last_tick_ts or time.time(), tz=timezone.utc),
            rv_60_annualized=rv_60,
            rv_24h_annualized=rv_24h,
            rv_baseline_7d_median=rv_baseline,
            is_stale=is_stale,
        )

    def get_recent_prices(self, n: int) -> list[float]:
        """Return up to the last n 1-minute close prices (chronological order)."""
        data = list(self._deque)
        prices = [p for _, p in data]
        return prices[-n:] if len(prices) >= n else prices

    async def run(self) -> None:
        """Main loop — bootstraps then maintains WebSocket connection forever."""
        self._load_persisted_state()
        if len(self._deque) < _24H_MINUTES:
            await asyncio.get_event_loop().run_in_executor(None, self._bootstrap_from_binance)

        import logging
        _log = logging.getLogger(__name__)
        _log.info("price_feed connecting to %s", self._ws_url)
        backoff = self._backoff_initial
        while True:
            try:
                await self._ws_loop()
                backoff = self._backoff_initial  # reset on clean disconnect
            except Exception as exc:
                _log.warning("price_feed ws error (retry in %.0fs): %s", backoff, exc)
            await asyncio.sleep(min(backoff, self._backoff_max))
            backoff = min(backoff * 2, self._backoff_max)

    # ── WebSocket loop ────────────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        # Kraken WS v1: subscribe to ticker, parse last-trade price from "c" field.
        # Message format: [channelID, {"c": ["price", "qty"], ...}, "ticker", "XBT/USD"]
        subscribe_msg = json.dumps({
            "event": "subscribe",
            "pair": ["XBT/USD"],
            "subscription": {"name": "ticker"},
        })
        async with websockets.connect(self._ws_url, ping_interval=20, ping_timeout=30) as ws:
            await ws.send(subscribe_msg)
            last_persist = time.time()
            async for raw in ws:
                msg = json.loads(raw)
                if not isinstance(msg, list) or len(msg) != 4 or msg[2] != "ticker":
                    continue
                price_str = msg[1].get("c", [None])[0]
                if not price_str:
                    continue
                price = float(price_str)
                now_ts = time.time()
                self._last_tick_ts = now_ts
                self._last_tick_price = price
                await self._record_tick(now_ts, price)

                if now_ts - last_persist >= 60:
                    self._persist_state()
                    last_persist = now_ts

    async def _record_tick(self, ts: float, price: float) -> None:
        minute_bucket = int(ts // 60)
        async with self._lock:
            if minute_bucket != self._current_minute:
                # Flush the completed minute: record last seen price as the close
                if self._current_minute > 0 and self._last_tick_price:
                    self._deque.append((self._current_minute * 60.0, self._last_tick_price))
                self._current_minute = minute_bucket

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    def _bootstrap_from_binance(self) -> None:
        """Pre-fill deque with historical 1-min closes from Kraken OHLC."""
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
            _log.warning("Kraken OHLC bootstrap failed: %s — falling back to spot", exc)

        # Fallback: single spot price
        try:
            spot = self._fetch_coinbase_spot()
        except Exception:
            try:
                spot = self._fetch_kraken_spot()
            except Exception:
                return
        now = time.time()
        self._deque.append((now, spot))
        self._last_tick_price = spot
        self._last_tick_ts = now

    def _fetch_kraken_ohlc(self, n: int) -> list[float]:
        """Fetch up to n 1-min closes from Kraken, oldest first."""
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
            closes.extend(float(c[4]) for c in candles)  # index 4 = close
            last_ts = int(data["result"].get("last", 0))
            if not last_ts or len(candles) < 720:
                break
            since = last_ts
        return closes[-n:]

    def _fetch_coinbase_spot(self) -> float:
        r = requests.get(_COINBASE_REST_URL, timeout=6)
        r.raise_for_status()
        return float(r.json()["data"]["amount"])

    def _fetch_kraken_spot(self) -> float:
        r = requests.get(_KRAKEN_SPOT_URL, params={"pair": "XBTUSD"}, timeout=6)
        r.raise_for_status()
        data = r.json()["result"]
        ticker = next(iter(data.values()))
        return float(ticker["c"][0])

    # ── State persistence ─────────────────────────────────────────────────────

    def _persist_state(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "ts": time.time(),
                "last_price": self._last_tick_price,
                "deque": list(self._deque)[-_24H_MINUTES:],  # persist last 24h only
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
            if time.time() - saved_ts > 300:  # stale if > 5 min old
                return
            for entry in raw.get("deque", []):
                self._deque.append((float(entry[0]), float(entry[1])))
            self._last_tick_price = float(raw.get("last_price", 0))
            self._last_tick_ts = saved_ts
        except Exception:
            pass

    # ── Vol helpers ───────────────────────────────────────────────────────────

    def _compute_7d_median(self, data: list[tuple[float, float]]) -> float:
        """
        Median of non-overlapping 24h realized vols over the last 7 days.
        Requires at least 2 complete 24h windows (48h of data).
        Returns nan if insufficient data.
        """
        if not data:
            return float("nan")
        now = time.time()
        seven_days_ago = now - 7 * 24 * 3600
        window = [(ts, p) for ts, p in data if ts >= seven_days_ago]
        if len(window) < _24H_MINUTES * 2:
            return float("nan")

        bin_vols: list[float] = []
        bin_size_s = _24H_MINUTES * 60.0
        if not window:
            return float("nan")
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


# ── CLI smoke test ─────────────────────────────────────────────────────────────

async def _smoke_test() -> None:
    import signal

    feed = PriceFeed()
    task = asyncio.create_task(feed.run())

    def _stop(*_: object) -> None:
        task.cancel()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _stop)
    loop.add_signal_handler(signal.SIGTERM, _stop)

    try:
        for _ in range(4):
            await asyncio.sleep(30)
            state = await feed.get_current_state()
            print(
                f"spot={state.spot:.2f} "
                f"rv_60={state.rv_60_annualized:.4f} "
                f"rv_24h={state.rv_24h_annualized:.4f} "
                f"rv_7d_med={state.rv_baseline_7d_median:.4f} "
                f"stale={state.is_stale}"
            )
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(_smoke_test())
