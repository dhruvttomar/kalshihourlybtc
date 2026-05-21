"""
Risk manager: enforces loss limits and circuit breakers.

Circuit breaker triggers:
  - Vol spike  >2% in 5 min  → pause 60 min
  - Vol spike  >5% in 30 min → pause 24 h
  - Daily P&L  <= -3% bankroll → daily_loss_limit_hit flag
  - Weekly P&L <= -7% bankroll → weekly_loss_limit_hit flag

All circuit breaker events are persisted to the `risk_events` table so they
survive restarts. On startup, _load_circuit_breakers() restores any active ones.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.config import RiskConfig
from src.database import Database
from src.models import RiskState

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc
log = logging.getLogger(__name__)


class RiskManager:
    """
    Tracks P&L, evaluates loss limits, and manages circuit breakers.

    Call `refresh_pnl()` periodically (e.g. each main-loop iteration) to
    recompute daily/weekly P&L from settled lines in the DB. Call
    `check_can_trade()` before every scan to gate order placement.
    """

    def __init__(self, config: RiskConfig, db: Database) -> None:
        self._config = config
        self._db = db
        self._state = RiskState(bankroll=config.starting_bankroll_usd)
        self._load_circuit_breakers()

    @property
    def state(self) -> RiskState:
        return self._state

    # ── P&L refresh ───────────────────────────────────────────────────────────

    def refresh_pnl(self, now_utc: datetime | None = None) -> None:
        """
        Query settled lines from DB and update daily_pnl, weekly_pnl,
        and the corresponding loss-limit flags.
        """
        if now_utc is None:
            now_utc = datetime.now(_UTC)
        now_et = now_utc.astimezone(_ET)

        day_start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = day_start_et.astimezone(_UTC)

        days_since_monday = now_et.weekday()  # 0 = Monday
        week_start_et = (now_et - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_start_utc = week_start_et.astimezone(_UTC)

        daily_pnl = self._db.get_realized_pnl_since(day_start_utc.isoformat())
        weekly_pnl = self._db.get_realized_pnl_since(week_start_utc.isoformat())

        bankroll = self._state.bankroll
        self._state.daily_pnl = daily_pnl
        self._state.weekly_pnl = weekly_pnl
        self._state.daily_loss_limit_hit = (
            daily_pnl <= -(self._config.daily_loss_limit_pct * bankroll)
        )
        self._state.weekly_loss_limit_hit = (
            weekly_pnl <= -(self._config.weekly_loss_limit_pct * bankroll)
        )

        if self._state.daily_loss_limit_hit:
            log.warning("Daily loss limit hit: P&L=$%.2f (limit=-$%.2f)", daily_pnl, self._config.daily_loss_limit_pct * bankroll)
        if self._state.weekly_loss_limit_hit:
            log.warning("Weekly loss limit hit: P&L=$%.2f (limit=-$%.2f)", weekly_pnl, self._config.weekly_loss_limit_pct * bankroll)

    # ── Trade gate ────────────────────────────────────────────────────────────

    def check_can_trade(self, now_utc: datetime) -> tuple[bool, str]:
        """
        Returns (True, "") if trading is currently permitted.
        Returns (False, reason) for the first condition that blocks trading.
        """
        s = self._state
        if s.daily_loss_limit_hit:
            return False, f"Daily loss limit hit (P&L: ${s.daily_pnl:.2f})"
        if s.weekly_loss_limit_hit:
            return False, f"Weekly loss limit hit (P&L: ${s.weekly_pnl:.2f})"
        if s.vol_circuit_breaker_until and now_utc < s.vol_circuit_breaker_until:
            return False, f"Vol circuit breaker active until {s.vol_circuit_breaker_until.isoformat()}"
        if s.liquidation_circuit_breaker_until and now_utc < s.liquidation_circuit_breaker_until:
            return False, f"Liquidation circuit breaker active until {s.liquidation_circuit_breaker_until.isoformat()}"
        return True, ""

    # ── Vol spike detection ───────────────────────────────────────────────────

    def check_vol_spike(self, now_utc: datetime, recent_prices: list[float]) -> bool:
        """
        Inspect recent 1-minute closes for vol spikes.

        Triggers (per spec Section 6.7):
          - |move| > vol_spike_5min_threshold  in 5 min → pause vol_spike_5min_pause_minutes
          - |move| > vol_spike_30min_threshold in 30 min → pause vol_spike_30min_pause_hours * 60

        Returns True if any circuit breaker was triggered.
        """
        if len(recent_prices) < 2:
            return False

        current = recent_prices[-1]
        triggered = False

        if len(recent_prices) >= 5:
            ref = recent_prices[-5]
            if ref > 0:
                move = abs(current - ref) / ref
                if move > self._config.vol_spike_5min_threshold:
                    pause_until = now_utc + timedelta(
                        minutes=self._config.vol_spike_5min_pause_minutes
                    )
                    self._set_vol_breaker(
                        now_utc, pause_until,
                        f"5-min move {move:.2%} > {self._config.vol_spike_5min_threshold:.0%}",
                    )
                    triggered = True

        if len(recent_prices) >= 30:
            ref = recent_prices[-30]
            if ref > 0:
                move = abs(current - ref) / ref
                if move > self._config.vol_spike_30min_threshold:
                    pause_until = now_utc + timedelta(
                        hours=self._config.vol_spike_30min_pause_hours
                    )
                    self._set_vol_breaker(
                        now_utc, pause_until,
                        f"30-min move {move:.2%} > {self._config.vol_spike_30min_threshold:.0%}",
                    )
                    triggered = True

        return triggered

    def trigger_liquidation_breaker(self, now_utc: datetime, reason: str = "manual") -> None:
        """Set a 4-hour liquidation cascade circuit breaker."""
        pause_until = now_utc + timedelta(hours=4)
        if (
            self._state.liquidation_circuit_breaker_until is None
            or pause_until > self._state.liquidation_circuit_breaker_until
        ):
            self._state.liquidation_circuit_breaker_until = pause_until
            self._db.record_risk_event({
                "event_type": "liquidation_circuit_breaker",
                "triggered_at": now_utc.isoformat(),
                "pause_until": pause_until.isoformat(),
                "context_json": json.dumps({"reason": reason}),
            })
            log.warning(
                "Liquidation circuit breaker: %s → pause until %s",
                reason, pause_until.isoformat(),
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _set_vol_breaker(
        self, now_utc: datetime, pause_until: datetime, reason: str
    ) -> None:
        """Set or extend the vol circuit breaker (never shorten it)."""
        existing = self._state.vol_circuit_breaker_until
        if existing is None or pause_until > existing:
            self._state.vol_circuit_breaker_until = pause_until
            self._db.record_risk_event({
                "event_type": "vol_circuit_breaker",
                "triggered_at": now_utc.isoformat(),
                "pause_until": pause_until.isoformat(),
                "context_json": json.dumps({"reason": reason}),
            })
            log.warning(
                "Vol circuit breaker: %s → pause until %s",
                reason, pause_until.isoformat(),
            )

    def _load_circuit_breakers(self) -> None:
        """Restore any active circuit breakers from the DB on startup."""
        now_utc = datetime.now(_UTC)
        try:
            events = self._db.get_active_risk_events(now_utc.isoformat())
        except Exception as exc:
            log.warning("Could not load circuit breakers from DB: %s", exc)
            return

        for event in events:
            pause_until_str = event["pause_until"]
            if not pause_until_str:
                continue
            try:
                pause_until = datetime.fromisoformat(pause_until_str)
            except (ValueError, TypeError):
                continue
            if pause_until.tzinfo is None:
                pause_until = pause_until.replace(tzinfo=_UTC)
            if pause_until <= now_utc:
                continue

            etype = event["event_type"]
            if etype == "vol_circuit_breaker":
                if (
                    self._state.vol_circuit_breaker_until is None
                    or pause_until > self._state.vol_circuit_breaker_until
                ):
                    self._state.vol_circuit_breaker_until = pause_until
                    log.info(
                        "Restored vol circuit breaker from DB: until %s",
                        pause_until.isoformat(),
                    )
            elif etype == "liquidation_circuit_breaker":
                if (
                    self._state.liquidation_circuit_breaker_until is None
                    or pause_until > self._state.liquidation_circuit_breaker_until
                ):
                    self._state.liquidation_circuit_breaker_until = pause_until
                    log.info(
                        "Restored liquidation circuit breaker from DB: until %s",
                        pause_until.isoformat(),
                    )
