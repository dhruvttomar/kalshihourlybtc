"""
Async Kalshi API wrapper with RSA authentication and paper mode.

Paper mode (default): all mutating operations (place/cancel order) are logged
and return synthetic responses — no real orders ever reach Kalshi.

Usage:
    async with KalshiClient(config, paper_mode=True) as client:
        markets = await client.get_active_btc_markets()
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.config import KalshiConfig
from src.models import Market

log = logging.getLogger(__name__)

_UTC = timezone.utc


class KalshiClient:
    """
    Async Kalshi REST client.

    All trading operations check `paper_mode` first. In paper mode every
    order call logs what it *would* do and returns a synthetic response so
    the rest of the pipeline can exercise its code paths end-to-end.
    """

    def __init__(
        self,
        config: KalshiConfig,
        api_key_id: str | None = None,
        private_key_path: str | None = None,
        paper_mode: bool = True,
    ) -> None:
        self._config = config
        self._api_key_id = api_key_id or os.environ.get("KALSHI_API_KEY_ID", "")
        self._key_path = private_key_path or os.environ.get("KALSHI_KEY_PATH", "")
        self._paper_mode = paper_mode
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "KalshiClient":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def paper_mode(self) -> bool:
        return self._paper_mode

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _signing_headers(self, method: str, path: str) -> dict[str, str]:
        """RSA-signed headers required by trading endpoints."""
        if not self._api_key_id:
            raise RuntimeError("KALSHI_API_KEY_ID environment variable not set")
        if not self._key_path or not os.path.exists(self._key_path):
            raise RuntimeError(
                f"RSA private key not found at {self._key_path!r}. "
                "Set KALSHI_KEY_PATH to the PEM file generated on kalshi.com."
            )

        ts_ms = str(int(time.time() * 1000))
        with open(self._key_path, "rb") as fh:
            priv = serialization.load_pem_private_key(fh.read(), password=None)

        msg = (ts_ms + method.upper() + path).encode()
        # Both prod and demo APIs now use RSA-PSS (confirmed 2026-05-21)
        pad: Any = padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        )

        sig = base64.b64encode(priv.sign(msg, pad, hashes.SHA256())).decode()
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }

    def _api_path(self, path: str) -> str:
        """Full URL path for the signing message (includes /trade-api/v2 prefix)."""
        base_path = urlparse(self._config.api_base_url).path.rstrip("/")
        return base_path + path

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    async def _get(
        self,
        path: str,
        params: dict | None = None,
        auth: str = "none",
    ) -> dict:
        url = self._config.api_base_url + path
        headers: dict[str, str] = {}
        if auth == "trading":
            headers = self._signing_headers("GET", self._api_path(path))
        return await self._request("GET", url, headers=headers, params=params)

    async def _post(self, path: str, body: dict) -> dict:
        url = self._config.api_base_url + path
        headers = self._signing_headers("POST", self._api_path(path))
        return await self._request("POST", url, headers=headers, json=body)

    async def _delete(self, path: str) -> dict:
        url = self._config.api_base_url + path
        headers = self._signing_headers("DELETE", self._api_path(path))
        return await self._request("DELETE", url, headers=headers)

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict:
        if self._session is None:
            raise RuntimeError(
                "KalshiClient must be used as an async context manager: "
                "`async with KalshiClient(...) as client:`"
            )
        backoff = 1.0
        for attempt in range(3):
            try:
                async with self._session.request(method, url, **kwargs) as resp:
                    if resp.status in (429, 500, 502, 503, 504) and attempt < 2:
                        log.warning(
                            "Kalshi %s → %d, retry in %.1fs (attempt %d)",
                            url, resp.status, backoff, attempt + 1,
                        )
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientError as exc:
                if attempt < 2:
                    log.warning("HTTP error, retry in %.1fs: %s", backoff, exc)
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    raise
        raise RuntimeError(f"All retries exhausted for {method} {url}")

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_active_btc_markets(self) -> list[Market]:
        """
        Fetch all open markets for the configured BTC series.

        Returns Market objects with yes_ask/no_ask in dollars [0, 1].
        depth_yes_usd and depth_no_usd are left at 0.0; call get_orderbook()
        for qualifying markets if you need depth.
        """
        series = self._config.market_series_ticker
        raw: list[dict] = []
        cursor: str | None = None

        while True:
            params: dict[str, Any] = {
                "series_ticker": series,
                "status": "open",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/markets", params=params)
            batch: list[dict] = data.get("markets", [])
            raw.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break

        markets: list[Market] = []
        for m in raw:
            floor_strike = m.get("floor_strike")
            if floor_strike is None:
                continue
            close_str = m.get("close_time", "")
            try:
                close_time = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue

            # Kalshi v2 returns prices in cents (int 1–99); normalize to dollars
            yes_cents = m.get("yes_ask")
            no_cents = m.get("no_ask")
            yes_ask = yes_cents / 100.0 if yes_cents is not None else None
            no_ask = no_cents / 100.0 if no_cents is not None else None

            markets.append(Market(
                ticker=m["ticker"],
                floor_strike=float(floor_strike),
                yes_ask=yes_ask,
                no_ask=no_ask,
                close_time=close_time,
                depth_yes_usd=0.0,
                depth_no_usd=0.0,
            ))

        log.debug("Fetched %d active %s markets", len(markets), series)
        return markets

    async def get_market(self, ticker: str) -> dict:
        """
        Fetch a single market by ticker.

        Returns the raw market dict. Key fields after settlement:
          result: "yes" | "no" | None
          status: "settled" | "open" | "closed"
        """
        try:
            data = await self._get(f"/markets/{ticker}")
            return data.get("market", {})
        except Exception as exc:
            log.warning("get_market(%s) failed: %s", ticker, exc)
            return {}

    async def get_orderbook(self, ticker: str) -> dict:
        """
        Fetch orderbook for a market.

        Returns:
            {
                "yes_best_ask_cents": int | None,
                "no_best_ask_cents":  int | None,
                "depth_yes_usd":      float,   # notional at yes best ask
                "depth_no_usd":       float,   # notional at no best ask
            }

        Orderbook entries format: [[price_cents, quantity], ...] sorted
        best-price-first. 1 contract at P cents costs P/100 dollars.
        """
        result: dict[str, Any] = {
            "yes_best_ask_cents": None,
            "no_best_ask_cents": None,
            "depth_yes_usd": 0.0,
            "depth_no_usd": 0.0,
        }
        try:
            data = await self._get(f"/markets/{ticker}/orderbook")
            book = data.get("orderbook", {})
            for side in ("yes", "no"):
                entries = book.get(side, [])
                if entries:
                    price_cents, qty = entries[0][0], entries[0][1]
                    result[f"{side}_best_ask_cents"] = price_cents
                    result[f"depth_{side}_usd"] = qty * (price_cents / 100.0)
        except Exception as exc:
            log.warning("get_orderbook(%s) failed: %s", ticker, exc)
        return result

    # ── Order management ──────────────────────────────────────────────────────

    async def place_limit_order(
        self,
        market_ticker: str,
        side: str,
        price_cents: int,
        quantity: int,
        line_id: str,
    ) -> dict:
        """
        Place a limit buy order.

        In paper mode: logs the intended order and returns a synthetic
        "resting" response so downstream code can proceed normally.

        Args:
            market_ticker: Kalshi market ticker (e.g. KXBTCD-26MAY2111-T86299.99)
            side: "yes" or "no"
            price_cents: 98 or 99
            quantity: number of contracts (each worth $1 at expiry)
            line_id: UUID linking this order to its Line record in the DB
        """
        client_order_id = str(uuid.uuid4())
        notional = quantity * price_cents / 100.0

        if self._paper_mode:
            synthetic_id = f"PAPER-{uuid.uuid4()}"
            log.info(
                "[PAPER] limit order: %s %s %d contracts @ %dc "
                "(notional $%.2f, line=%s, coid=%s)",
                market_ticker, side.upper(), quantity, price_cents,
                notional, line_id, client_order_id,
            )
            return {
                "order": {
                    "order_id": synthetic_id,
                    "client_order_id": client_order_id,
                    "status": "filled",   # paper orders assumed to fill immediately
                    "count": quantity,
                    "yes_price": price_cents,
                    "notional_usd": notional,
                }
            }

        body = {
            "ticker": market_ticker,
            "client_order_id": client_order_id,
            "type": "limit",
            "action": "buy",
            "side": side,
            "count": quantity,
            "yes_price": price_cents,
        }
        log.debug("Placing live order: %s %s %d @ %dc", market_ticker, side, quantity, price_cents)
        return await self._post("/portfolio/orders", body)

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if successfully cancelled."""
        if self._paper_mode:
            log.info("[PAPER] cancel order: %s", order_id)
            return True
        try:
            await self._delete(f"/portfolio/orders/{order_id}")
            return True
        except Exception as exc:
            log.warning("cancel_order(%s) failed: %s", order_id, exc)
            return False

    async def get_order_status(self, order_id: str) -> dict:
        """Fetch current status of an order. Returns {} in paper mode."""
        if self._paper_mode:
            return {"order_id": order_id, "status": "filled"}
        try:
            data = await self._get(f"/portfolio/orders/{order_id}", auth="trading")
            return data.get("order", {})
        except Exception as exc:
            log.warning("get_order_status(%s) failed: %s", order_id, exc)
            return {}

    async def get_open_positions(self) -> list[dict]:
        """Return open positions from Kalshi portfolio (empty in paper mode)."""
        if self._paper_mode:
            return []
        try:
            data = await self._get("/portfolio/positions", auth="trading")
            return data.get("market_positions", [])
        except Exception as exc:
            log.warning("get_open_positions failed: %s", exc)
            return []

    async def get_account_balance(self) -> float:
        """Return available balance in dollars (0.0 in paper mode)."""
        if self._paper_mode:
            return 0.0
        try:
            data = await self._get("/portfolio/balance", auth="trading")
            cents = data.get("available_balance", 0)
            return cents / 100.0
        except Exception as exc:
            log.warning("get_account_balance failed: %s", exc)
            return 0.0
