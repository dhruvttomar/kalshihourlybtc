"""
Alerts: Telegram Bot notifications for fills, circuit breakers, and errors.

Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
If either is missing, all alert methods are no-ops that log at INFO level instead.
The aiohttp session is injected so the caller can share the session used by KalshiClient.

Setup:
  1. Message @BotFather on Telegram → /newbot → copy the token
  2. Add the bot to your channel/group, or start a DM with it
  3. Get the chat ID: message the bot, then visit
     https://api.telegram.org/bot<TOKEN>/getUpdates
     and copy "chat" → "id" from the response
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from src.config import AlertsConfig
from src.models import EntryDecision, Line

log = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram emoji markers for each alert type
_EMOJI_GREEN = "✅"   # ✅
_EMOJI_RED = "⚠️"  # ⚠️
_EMOJI_YELLOW = "\U0001f7e1"  # 🟡
_EMOJI_BLUE = "\U0001f4ca"   # 📊


class Alerts:
    """
    Sends Telegram Bot notifications.

    All methods are safe to call when credentials are not configured — they
    fall back to structured log entries.
    """

    def __init__(self, config: AlertsConfig, session: Any | None = None) -> None:
        self._bot_token = config.telegram_bot_token
        self._chat_id = config.telegram_chat_id
        self._session = session
        self._alert_on_fill = config.alert_on_fill
        self._alert_on_circuit_breaker = config.alert_on_circuit_breaker
        self._alert_on_loss_limit = config.alert_on_loss_limit
        self._alert_on_error = config.alert_on_error

    def set_session(self, session: Any) -> None:
        """Inject an aiohttp.ClientSession after construction (from main loop)."""
        self._session = session

    # ── Public notification methods ───────────────────────────────────────────

    async def notify_fill(self, line: Line, decision: EntryDecision) -> None:
        if not self._alert_on_fill:
            return
        ticker = decision.market_ticker or "?"
        side = (decision.side or "?").upper()
        price_cents = decision.target_price_cents or 0
        text = (
            f"{_EMOJI_GREEN} *Line Executed*\n"
            f"`{ticker}` {side} @ {price_cents}¢\n"
            f"${line.cumulative_filled:.2f} deployed across {len(line.fills)} order(s)"
        )
        log.info("FILL: %s", text)
        await self._send(text)

    async def notify_circuit_breaker(
        self,
        event_type: str,
        reason: str,
        pause_until: datetime,
    ) -> None:
        if not self._alert_on_circuit_breaker:
            return
        text = (
            f"{_EMOJI_YELLOW} *Circuit Breaker Triggered*\n"
            f"*{event_type}*: {reason}\n"
            f"Paused until `{pause_until.isoformat()}`"
        )
        log.warning("CIRCUIT BREAKER: %s", text)
        await self._send(text)

    async def notify_error(self, exc: Exception, context: str = "") -> None:
        if not self._alert_on_error:
            return
        ctx = f" ({context})" if context else ""
        text = (
            f"{_EMOJI_RED} *Bot Error*\n"
            f"*{type(exc).__name__}*{ctx}: {exc}"
        )
        log.error("ERROR ALERT: %s", text)
        await self._send(text)

    async def notify_settlement(
        self,
        market_ticker: str,
        outcome: str,
        final_pnl_usd: float,
    ) -> None:
        emoji = _EMOJI_GREEN if outcome == "win" else _EMOJI_RED
        sign = "+" if final_pnl_usd >= 0 else ""
        text = (
            f"{emoji} *Settlement*\n"
            f"`{market_ticker}`\n"
            f"Result: *{outcome.upper()}* | P&L: *{sign}${final_pnl_usd:.2f}*"
        )
        log.info("SETTLEMENT: %s", text)
        await self._send(text)

    async def notify_loss_limit(self, limit_type: str, pnl: float, limit_usd: float) -> None:
        if not self._alert_on_loss_limit:
            return
        text = (
            f"{_EMOJI_RED} *Loss Limit Hit*\n"
            f"*{limit_type}* limit reached\n"
            f"P&L: *${pnl:+.2f}* (limit: -${limit_usd:.2f})"
        )
        log.warning("LOSS LIMIT: %s", text)
        await self._send(text)

    async def notify_daily_summary(
        self,
        trades: int,
        daily_pnl: float,
        blackout_count: int,
        circuit_breaker_count: int,
    ) -> None:
        sign = "+" if daily_pnl >= 0 else ""
        text = (
            f"{_EMOJI_BLUE} *Daily Summary*\n"
            f"Trades: *{trades}* | P&L: *{sign}${daily_pnl:.2f}*\n"
            f"Blackouts skipped: {blackout_count} | CB events: {circuit_breaker_count}"
        )
        log.info("DAILY SUMMARY: %s", text)
        await self._send(text)

    # ── Telegram HTTP ─────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        if not self._bot_token or not self._chat_id or not self._session:
            return
        url = _TG_API.format(token=self._bot_token)
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning(
                        "Telegram API returned %d: %s", resp.status, body[:200]
                    )
        except Exception as exc:
            log.warning("Failed to send Telegram alert: %s", exc)
