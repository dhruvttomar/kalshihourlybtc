"""
Alerts: Discord webhook notifications for fills, circuit breakers, and errors.

If `discord_webhook_url` is not configured, all alert methods are no-ops that
log at INFO level instead. The aiohttp session is injected so the caller can
share the same session used by KalshiClient.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from src.config import AlertsConfig
from src.models import EntryDecision, Line

log = logging.getLogger(__name__)

_DISCORD_COLOR_GREEN = 3066993
_DISCORD_COLOR_RED = 15158332
_DISCORD_COLOR_YELLOW = 16776960
_DISCORD_COLOR_GREY = 9807270


class Alerts:
    """
    Sends structured Discord embed notifications.

    All methods are safe to call when no webhook is configured — they
    fall back to structured log entries.
    """

    def __init__(self, config: AlertsConfig, session: Any | None = None) -> None:
        self._webhook_url = config.discord_webhook_url
        self._session = session
        self._alert_on_fill = config.alert_on_fill
        self._alert_on_circuit_breaker = config.alert_on_circuit_breaker

    def set_session(self, session: Any) -> None:
        """Inject an aiohttp.ClientSession after construction (from main loop)."""
        self._session = session

    # ── Public notification methods ───────────────────────────────────────────

    async def notify_fill(self, line: Line, decision: EntryDecision) -> None:
        """Post a fill notification when a line is executed."""
        if not self._alert_on_fill:
            return
        ticker = decision.market_ticker or "?"
        side = (decision.side or "?").upper()
        price_cents = decision.target_price_cents or 0
        msg = (
            f"**Fill** `{ticker}` {side} @ {price_cents}¢ — "
            f"${line.cumulative_filled:.2f} deployed across {len(line.fills)} order(s)"
        )
        log.info("FILL: %s", msg)
        await self._post_embed(
            title="Line Executed",
            description=msg,
            color=_DISCORD_COLOR_GREEN,
        )

    async def notify_circuit_breaker(
        self,
        event_type: str,
        reason: str,
        pause_until: datetime,
    ) -> None:
        """Post a circuit breaker activation alert."""
        if not self._alert_on_circuit_breaker:
            return
        msg = f"**{event_type}**: {reason}\nPaused until `{pause_until.isoformat()}`"
        log.warning("CIRCUIT BREAKER: %s", msg)
        await self._post_embed(
            title="Circuit Breaker Triggered",
            description=msg,
            color=_DISCORD_COLOR_YELLOW,
        )

    async def notify_error(self, exc: Exception, context: str = "") -> None:
        """Post an error notification for unhandled exceptions in the main loop."""
        ctx = f" ({context})" if context else ""
        msg = f"**{type(exc).__name__}**{ctx}: {exc}"
        log.error("ERROR ALERT: %s", msg)
        await self._post_embed(
            title="Bot Error",
            description=msg,
            color=_DISCORD_COLOR_RED,
        )

    async def notify_daily_summary(
        self,
        trades: int,
        daily_pnl: float,
        blackout_count: int,
        circuit_breaker_count: int,
    ) -> None:
        """Post the nightly summary report (called at midnight ET)."""
        sign = "+" if daily_pnl >= 0 else ""
        msg = (
            f"Trades: **{trades}** | P&L: **{sign}${daily_pnl:.2f}** | "
            f"Blackouts skipped: {blackout_count} | CB events: {circuit_breaker_count}"
        )
        log.info("DAILY SUMMARY: %s", msg)
        await self._post_embed(
            title="Daily Summary",
            description=msg,
            color=_DISCORD_COLOR_GREEN if daily_pnl >= 0 else _DISCORD_COLOR_RED,
        )

    # ── Discord HTTP ──────────────────────────────────────────────────────────

    async def _post_embed(
        self,
        title: str,
        description: str,
        color: int = _DISCORD_COLOR_GREY,
    ) -> None:
        if not self._webhook_url or not self._session:
            return
        payload = {
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "color": color,
                }
            ]
        }
        try:
            async with self._session.post(self._webhook_url, json=payload) as resp:
                if resp.status not in (200, 204):
                    log.warning(
                        "Discord webhook returned %d for alert '%s'",
                        resp.status, title,
                    )
        except Exception as exc:
            log.warning("Failed to send Discord alert '%s': %s", title, exc)
