"""
Unit tests for entry_logic.py.

Tests each of the 9 entry conditions individually (one failure at a time)
and a full integrated pass scenario. Uses stubs for all external dependencies.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.blackout_calendar import BlackoutCalendar
from src.config import AppConfig, StrategyConfig, load_config
from src.entry_logic import evaluate_entry
from src.models import EntryDecision, Market, QualifyingTrade, RiskState
from src.price_feed import PriceState

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc

# Fixed simulated clock: Thursday 2026-05-21 at 10:35 AM ET = 14:35 UTC.
# Markets settle at the top of the hour → 11:00 AM ET = 15:00 UTC.
_SIM_CLOSE_UTC = datetime(2026, 5, 21, 15, 0, 0, tzinfo=_UTC)
_SIM_NOW_UTC = datetime(2026, 5, 21, 14, 35, 0, tzinfo=_UTC)  # 10:35 AM ET

# ── Stubs ─────────────────────────────────────────────────────────────────────


class _StubPositionTracker:
    def __init__(self, lines: int = 0, capital: float = 0.0):
        self._lines = lines
        self._capital = capital

    def lines_taken_this_hour(self, now_et: datetime) -> int:
        return self._lines

    def capital_deployed_this_hour(self, now_et: datetime) -> float:
        return self._capital


def _open_config() -> StrategyConfig:
    return load_config("config/default.yaml").strategy


def _open_price_state(spot: float = 86_000.0, rv_60: float = 0.35) -> PriceState:
    return PriceState(
        spot=spot,
        timestamp=datetime.now(_UTC),
        rv_60_annualized=rv_60,
        rv_24h_annualized=0.38,
        rv_baseline_7d_median=0.36,
        is_stale=False,
    )


def _open_risk() -> RiskState:
    return RiskState(
        daily_pnl=0.0,
        weekly_pnl=0.0,
        bankroll=50_000.0,
        daily_loss_limit_hit=False,
        weekly_loss_limit_hit=False,
        vol_circuit_breaker_until=None,
        liquidation_circuit_breaker_until=None,
    )


def _open_blackout() -> BlackoutCalendar:
    """Blackout calendar with no events and no active windows."""
    return BlackoutCalendar(
        config_path="config/blackouts.yaml",
        max_risk_level=1,
        econ_events=[],
    )


def _open_trade(
    ask: float = 0.99,
    floor_strike: float = 85_300.0,  # 700 below spot of 86_000
    side: str = "yes",
    depth_usd: float = 2500.0,
    close_time: datetime | None = None,
) -> QualifyingTrade:
    if close_time is None:
        close_time = _SIM_CLOSE_UTC  # 11:00 AM ET = 15:00 UTC, 25 min from simulated now
    m = Market(
        ticker="KXBTCD-26MAY2115-T85299.99",
        floor_strike=floor_strike,
        yes_ask=ask if side == "yes" else None,
        no_ask=ask if side == "no" else None,
        close_time=close_time,
        depth_yes_usd=depth_usd if side == "yes" else 0.0,
        depth_no_usd=depth_usd if side == "no" else 0.0,
    )
    return QualifyingTrade(market=m, side=side)


def _now_et(minute: int = 35) -> datetime:
    """A valid ET trading time: Thursday May 21 2026 at :XX ET (no blackout windows active)."""
    return datetime(2026, 5, 21, 10, minute, 0, tzinfo=_ET)


# ── Helper to run evaluation ───────────────────────────────────────────────────


def _eval(
    trade: QualifyingTrade | None = None,
    price: PriceState | None = None,
    blackout: BlackoutCalendar | None = None,
    risk: RiskState | None = None,
    tracker: _StubPositionTracker | None = None,
    now_et: datetime | None = None,
    config: StrategyConfig | None = None,
) -> EntryDecision:
    return evaluate_entry(
        trade=trade or _open_trade(),
        price_state=price or _open_price_state(),
        blackout=blackout or _open_blackout(),
        risk_state=risk or _open_risk(),
        position_tracker=tracker or _StubPositionTracker(),
        now_et=now_et or _now_et(),
        config=config or _open_config(),
    )


# ── Integrated pass ───────────────────────────────────────────────────────────


def test_all_conditions_pass():
    result = _eval()
    assert result.should_trade is True
    assert result.market_ticker is not None
    assert result.side == "yes"
    assert result.target_price_cents == 99


# ── Condition 1: Timing ───────────────────────────────────────────────────────


def test_timing_too_early_in_hour():
    result = _eval(now_et=_now_et(minute=10))
    assert not result.should_trade
    assert "Too early" in result.reason


def test_timing_at_exactly_min_25_passes():
    result = _eval(now_et=_now_et(minute=25))
    assert result.should_trade


def test_timing_too_close_to_settlement():
    # Simulated now is 10:59 ET = 14:59 UTC; close_time 30s later → 30s remaining
    sim_now_et = _now_et(minute=59)
    sim_now_utc = datetime(2026, 5, 21, 14, 59, 0, tzinfo=_UTC)
    close_time = sim_now_utc + timedelta(seconds=30)
    trade = _open_trade(close_time=close_time)
    result = _eval(trade=trade, now_et=sim_now_et)
    assert not result.should_trade
    assert "settlement" in result.reason.lower() or "remaining" in result.reason.lower()


def test_timing_exactly_at_cutoff_is_blocked():
    # Simulated now is 10:59 ET = 14:59 UTC; close_time exactly 60s later → should fail (need > 60)
    sim_now_et = _now_et(minute=59)
    sim_now_utc = datetime(2026, 5, 21, 14, 59, 0, tzinfo=_UTC)
    close_time = sim_now_utc + timedelta(seconds=60)
    trade = _open_trade(close_time=close_time)
    result = _eval(trade=trade, now_et=sim_now_et)
    assert not result.should_trade


# ── Condition 2: Blackout ─────────────────────────────────────────────────────


def test_blackout_active_blocks():
    # Simulate a blackout by patching with a calendar whose is_blocked returns True
    class _AlwaysBlockedCalendar(BlackoutCalendar):
        def is_blocked(self, now_et):
            return True, "Test blackout active"

    result = _eval(blackout=_AlwaysBlockedCalendar.__new__(_AlwaysBlockedCalendar))

    # Manually construct without __init__ (bypass config loading)
    cal = object.__new__(_AlwaysBlockedCalendar)
    cal._max_risk_level = 1
    cal._econ_events = []
    cal._last_refresh = None
    cal._cfg = {}

    # Override is_blocked directly
    from unittest.mock import MagicMock, patch
    mock_cal = MagicMock()
    mock_cal.is_blocked.return_value = (True, "Test blackout active")

    result = evaluate_entry(
        trade=_open_trade(),
        price_state=_open_price_state(),
        blackout=mock_cal,
        risk_state=_open_risk(),
        position_tracker=_StubPositionTracker(),
        now_et=_now_et(),
        config=_open_config(),
    )
    assert not result.should_trade
    assert "Blackout" in result.reason


# ── Condition 3: Hourly line count ────────────────────────────────────────────


def test_line_cap_reached():
    result = _eval(tracker=_StubPositionTracker(lines=2))
    assert not result.should_trade
    assert "line cap" in result.reason.lower()


def test_one_line_taken_still_allowed():
    result = _eval(tracker=_StubPositionTracker(lines=1))
    assert result.should_trade


# ── Condition 4: Hourly capital ───────────────────────────────────────────────


def test_capital_cap_reached():
    result = _eval(tracker=_StubPositionTracker(capital=2000.0))
    assert not result.should_trade
    assert "capital cap" in result.reason.lower()


def test_partial_capital_still_allowed():
    result = _eval(tracker=_StubPositionTracker(capital=1500.0))
    assert result.should_trade


# ── Condition 5: Risk circuit breakers ───────────────────────────────────────


def test_daily_loss_limit_blocks():
    risk = _open_risk()
    risk.daily_loss_limit_hit = True
    result = _eval(risk=risk)
    assert not result.should_trade
    assert "Daily loss limit" in result.reason


def test_weekly_loss_limit_blocks():
    risk = _open_risk()
    risk.weekly_loss_limit_hit = True
    result = _eval(risk=risk)
    assert not result.should_trade
    assert "Weekly loss limit" in result.reason


def test_vol_circuit_breaker_blocks():
    risk = _open_risk()
    risk.vol_circuit_breaker_until = datetime.now(_UTC) + timedelta(hours=1)
    result = _eval(risk=risk)
    assert not result.should_trade
    assert "Vol circuit breaker" in result.reason


def test_expired_vol_circuit_breaker_does_not_block():
    risk = _open_risk()
    risk.vol_circuit_breaker_until = _SIM_NOW_UTC - timedelta(seconds=1)
    result = _eval(risk=risk)
    assert result.should_trade


def test_liquidation_circuit_breaker_blocks():
    risk = _open_risk()
    risk.liquidation_circuit_breaker_until = datetime.now(_UTC) + timedelta(hours=4)
    result = _eval(risk=risk)
    assert not result.should_trade
    assert "Liquidation" in result.reason


# ── Condition 6: Volatility filters ──────────────────────────────────────────


def test_rv_unavailable_blocks():
    price = _open_price_state(rv_60=float("nan"))
    result = _eval(price=price)
    assert not result.should_trade
    assert "unavailable" in result.reason.lower() or "RV_60" in result.reason


def test_rv_above_baseline_multiplier_blocks():
    # RV_60 = 0.60, baseline = 0.36 → 0.60 > 1.5 * 0.36 = 0.54
    price = PriceState(
        spot=86_000.0,
        timestamp=datetime.now(_UTC),
        rv_60_annualized=0.60,
        rv_24h_annualized=0.50,
        rv_baseline_7d_median=0.36,
        is_stale=False,
    )
    result = _eval(price=price)
    assert not result.should_trade
    assert "elevated" in result.reason.lower() or "baseline" in result.reason.lower()


def test_rv_above_hard_ceiling_blocks():
    # rv_60=0.75 > ceiling=0.70; use rv_baseline=0.60 so baseline check passes (0.75 < 1.5*0.60=0.90)
    price = PriceState(
        spot=86_000.0,
        timestamp=_SIM_NOW_UTC,
        rv_60_annualized=0.75,
        rv_24h_annualized=0.60,
        rv_baseline_7d_median=0.60,
        is_stale=False,
    )
    result = _eval(price=price)
    assert not result.should_trade
    assert "ceiling" in result.reason.lower()


def test_rv_baseline_nan_does_not_block_on_baseline_check():
    # If baseline is unavailable, skip baseline check but hard ceiling still applies
    price = PriceState(
        spot=86_000.0,
        timestamp=datetime.now(_UTC),
        rv_60_annualized=0.35,
        rv_24h_annualized=0.35,
        rv_baseline_7d_median=float("nan"),
        is_stale=False,
    )
    result = _eval(price=price)
    assert result.should_trade  # baseline NaN → skip baseline check


# ── Condition 7: Price filter ─────────────────────────────────────────────────


def test_price_too_low_blocks():
    trade = _open_trade(ask=0.97)
    result = _eval(trade=trade)
    assert not result.should_trade
    assert "Price out of range" in result.reason


def test_price_too_high_blocks():
    trade = _open_trade(ask=1.00)
    result = _eval(trade=trade)
    assert not result.should_trade
    assert "Price out of range" in result.reason


def test_price_at_lower_bound_passes():
    trade = _open_trade(ask=0.98)
    result = _eval(trade=trade)
    assert result.should_trade
    assert result.target_price_cents == 98


def test_price_at_upper_bound_passes():
    trade = _open_trade(ask=0.99)
    result = _eval(trade=trade)
    assert result.should_trade
    assert result.target_price_cents == 99


def test_no_ask_price_blocks():
    m = Market(
        ticker="KXBTCD-TEST",
        floor_strike=85_300.0,
        yes_ask=None,
        no_ask=None,
        close_time=datetime.now(_UTC) + timedelta(minutes=35),
        depth_yes_usd=3000.0,
    )
    trade = QualifyingTrade(market=m, side="yes")
    result = _eval(trade=trade)
    assert not result.should_trade
    assert "No ask price" in result.reason


# ── Condition 8: Buffer requirement ──────────────────────────────────────────


def test_buffer_too_small_blocks():
    # Spot 86_000, strike 85_800 → buffer = 200 (too small, floor is 550)
    trade = _open_trade(floor_strike=85_800.0, ask=0.99)
    result = _eval(trade=trade)
    assert not result.should_trade
    assert "Buffer too small" in result.reason


def test_buffer_exactly_at_floor_passes():
    # Spot 86_000, strike 85_450 → buffer = 550 = floor; should pass
    trade = _open_trade(floor_strike=85_450.0, ask=0.99)
    result = _eval(trade=trade)
    assert result.should_trade


def test_buffer_comfortably_above_floor():
    # Spot 86_000, strike 85_000 → buffer = 1000 (well above $550 floor)
    trade = _open_trade(floor_strike=85_000.0, ask=0.99)
    result = _eval(trade=trade)
    assert result.should_trade


def test_no_side_buffer_blocks():
    # NO side: strike above spot; spot=86_000, strike=85_500 → buffer_below is negative
    trade = _open_trade(floor_strike=85_500.0, side="no", ask=0.99)
    result = _eval(trade=trade)
    assert not result.should_trade
    assert "Buffer too small" in result.reason


# ── Condition 9: Orderbook depth ──────────────────────────────────────────────


def test_insufficient_depth_blocks():
    # Required: 1000 * 2 = 2000; available: 500
    trade = _open_trade(depth_usd=500.0)
    result = _eval(trade=trade)
    assert not result.should_trade
    assert "depth" in result.reason.lower()


def test_sufficient_depth_passes():
    trade = _open_trade(depth_usd=2500.0)
    result = _eval(trade=trade)
    assert result.should_trade


def test_exactly_at_required_depth_passes():
    # Required: 1000 * 2 = 2000; available: 2000
    trade = _open_trade(depth_usd=2000.0)
    result = _eval(trade=trade)
    assert result.should_trade


# ── Output fields ─────────────────────────────────────────────────────────────


def test_passing_decision_has_correct_fields():
    result = _eval()
    assert result.should_trade
    assert result.market_ticker == "KXBTCD-26MAY2115-T85299.99"
    assert result.side == "yes"
    assert result.target_price_cents == 99
    assert result.target_size_usd is not None
    assert result.target_size_usd > 0

def test_failing_decision_has_no_order_fields():
    result = _eval(now_et=_now_et(minute=5))  # too early
    assert not result.should_trade
    assert result.market_ticker is None
    assert result.side is None
    assert result.target_price_cents is None
