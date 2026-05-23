"""
Unit tests for risk_manager.py.

Uses real SQLite (tmp_path fixture) so get_realized_pnl_since and
get_active_risk_events are exercised end-to-end.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.config import load_config
from src.database import Database
from src.risk_manager import RiskManager

_UTC = timezone.utc
_ET = ZoneInfo("America/New_York")


def _utc(offset_hours: float = 0) -> datetime:
    return datetime(2026, 5, 21, 14, 0, 0, tzinfo=_UTC) + timedelta(hours=offset_hours)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def config():
    return load_config("config/default.yaml").risk


@pytest.fixture
def rm(config, db):
    return RiskManager(config, db)


# ── check_can_trade: clean state ──────────────────────────────────────────────

def test_can_trade_clean_state(rm):
    ok, reason = rm.check_can_trade(_utc())
    assert ok
    assert reason == ""


# ── Daily / weekly loss limits ────────────────────────────────────────────────

def test_daily_loss_limit_blocks(rm):
    rm._state.daily_loss_limit_hit = True
    ok, reason = rm.check_can_trade(_utc())
    assert not ok
    assert "Daily loss limit" in reason


def test_daily_loss_limit_below_threshold_allows(rm):
    # -2.9% of $50k = -$1,450; daily limit is -3% = -$1,500
    rm._state.daily_pnl = -1450.0
    rm._state.daily_loss_limit_hit = False
    ok, _ = rm.check_can_trade(_utc())
    assert ok


def test_weekly_loss_limit_blocks(rm):
    rm._state.weekly_loss_limit_hit = True
    ok, reason = rm.check_can_trade(_utc())
    assert not ok
    assert "Weekly loss limit" in reason


# ── Vol circuit breaker ───────────────────────────────────────────────────────

def test_vol_circuit_breaker_active_blocks(rm):
    rm._state.vol_circuit_breaker_until = _utc(+1)
    ok, reason = rm.check_can_trade(_utc())
    assert not ok
    assert "Vol circuit breaker" in reason


def test_vol_circuit_breaker_expired_allows(rm):
    rm._state.vol_circuit_breaker_until = _utc(-1)  # expired 1h ago
    ok, _ = rm.check_can_trade(_utc())
    assert ok


def test_vol_circuit_breaker_exactly_now_allows(rm):
    # pause_until == now → not strictly less than → allowed
    now = _utc()
    rm._state.vol_circuit_breaker_until = now
    ok, _ = rm.check_can_trade(now)
    assert ok


# ── Liquidation circuit breaker ───────────────────────────────────────────────

def test_liquidation_circuit_breaker_blocks(rm):
    rm._state.liquidation_circuit_breaker_until = _utc(+4)
    ok, reason = rm.check_can_trade(_utc())
    assert not ok
    assert "Liquidation" in reason


def test_liquidation_circuit_breaker_expired_allows(rm):
    rm._state.liquidation_circuit_breaker_until = _utc(-1)
    ok, _ = rm.check_can_trade(_utc())
    assert ok


def test_trigger_liquidation_breaker_persists_to_db(rm, db):
    now = datetime.now(_UTC)
    rm.trigger_liquidation_breaker(now, "test")
    assert rm.state.liquidation_circuit_breaker_until is not None
    assert rm.state.liquidation_circuit_breaker_until > now
    events = db.get_active_risk_events(now.isoformat())
    assert any(e["event_type"] == "liquidation_circuit_breaker" for e in events)


# ── Vol spike detection ───────────────────────────────────────────────────────

def test_check_vol_spike_no_trigger_small_moves(rm):
    prices = [86_000.0 + i * 10 for i in range(30)]  # tiny uptrend, <0.5%
    triggered = rm.check_vol_spike(_utc(), prices)
    assert not triggered
    assert rm.state.vol_circuit_breaker_until is None


def test_check_vol_spike_5min_triggers_pause(rm):
    # >2% in 5 min: price goes from 86_000 to 87_800 in last 5 bars
    prices = [86_000.0] * 30
    prices[-1] = 87_900.0  # ~2.2% up from prices[-5]=86000
    triggered = rm.check_vol_spike(_utc(), prices)
    assert triggered
    assert rm.state.vol_circuit_breaker_until is not None
    assert rm.state.vol_circuit_breaker_until > _utc()


def test_check_vol_spike_5min_pause_duration(rm, config):
    prices = [86_000.0] * 30
    prices[-1] = 88_000.0  # ~2.3% from prices[-5]
    now = _utc()
    rm.check_vol_spike(now, prices)
    expected_pause = now + timedelta(minutes=config.vol_spike_5min_pause_minutes)
    delta = abs((rm.state.vol_circuit_breaker_until - expected_pause).total_seconds())
    assert delta < 2


def test_check_vol_spike_30min_triggers_24h_pause(rm, config):
    # >5% in 30 min: price[-30] = 86_000, price[-1] = 91_000 (~5.8%)
    prices = [86_000.0] * 30
    prices[-1] = 91_000.0
    now = _utc()
    triggered = rm.check_vol_spike(now, prices)
    assert triggered
    expected_pause = now + timedelta(hours=config.vol_spike_30min_pause_hours)
    delta = abs((rm.state.vol_circuit_breaker_until - expected_pause).total_seconds())
    assert delta < 2


def test_check_vol_spike_extends_existing_breaker(rm):
    # Short breaker already set; a longer one should replace it
    rm._state.vol_circuit_breaker_until = _utc(+0.5)  # 30 min pause
    prices = [86_000.0] * 30
    prices[-1] = 91_000.0  # triggers 24h pause
    rm.check_vol_spike(_utc(), prices)
    assert rm.state.vol_circuit_breaker_until > _utc(+1)


def test_check_vol_spike_does_not_shorten_existing_breaker(rm):
    # Long breaker already set; a shorter trigger should not shorten it
    long_until = _utc(+25)
    rm._state.vol_circuit_breaker_until = long_until
    prices = [86_000.0] * 30
    prices[-1] = 87_900.0  # triggers only 60-min pause
    rm.check_vol_spike(_utc(), prices)
    assert rm.state.vol_circuit_breaker_until == long_until


def test_check_vol_spike_too_few_prices_no_trigger(rm):
    prices = [86_000.0, 87_000.0]  # only 2 prices
    triggered = rm.check_vol_spike(_utc(), prices)
    assert not triggered


def test_check_vol_spike_persists_event_to_db(rm, db):
    prices = [86_000.0] * 30
    prices[-1] = 91_000.0
    now = datetime.now(_UTC)
    rm.check_vol_spike(now, prices)
    events = db.get_active_risk_events(now.isoformat())
    assert any(e["event_type"] == "vol_circuit_breaker" for e in events)


# ── refresh_pnl from DB ───────────────────────────────────────────────────────

def _settle(db, hour_key: str, pnl: float, settled_offset_h: float = 0) -> None:
    """Helper: insert a settled line into DB."""
    import uuid
    line_id = str(uuid.uuid4())
    db.record_line({
        "id": line_id,
        "hour_key": hour_key,
        "market_ticker": "KXBTCD-TEST",
        "side": "yes",
        "strike_price": 85300,
        "cumulative_filled_usd": abs(pnl) / 0.99 * 0.99,
        "final_pnl_usd": pnl,
        "settled_at": (_utc(settled_offset_h)).isoformat(),
        "outcome": "win" if pnl > 0 else "loss",
        "close_time_utc": None,
    })


def test_refresh_pnl_with_no_settled_lines(rm):
    rm.refresh_pnl(_utc())
    assert rm.state.daily_pnl == pytest.approx(0.0)
    assert rm.state.weekly_pnl == pytest.approx(0.0)


def test_refresh_pnl_win_today(rm, db):
    _settle(db, "2026-05-21T10", pnl=9.90, settled_offset_h=-4)
    rm.refresh_pnl(_utc())
    assert rm.state.daily_pnl == pytest.approx(9.90)
    assert not rm.state.daily_loss_limit_hit


def test_refresh_pnl_loss_hits_daily_limit(rm, db, config):
    bankroll = config.starting_bankroll_usd  # $50k
    loss = -(config.daily_loss_limit_pct * bankroll)  # -$1500
    _settle(db, "2026-05-21T10", pnl=loss, settled_offset_h=-4)
    rm.refresh_pnl(_utc())
    assert rm.state.daily_loss_limit_hit


def test_refresh_pnl_loss_just_below_daily_limit(rm, db, config):
    bankroll = config.starting_bankroll_usd
    loss = -(config.daily_loss_limit_pct * bankroll) + 1.0  # $1 short of limit
    _settle(db, "2026-05-21T10", pnl=loss, settled_offset_h=-4)
    rm.refresh_pnl(_utc())
    assert not rm.state.daily_loss_limit_hit


# ── _load_circuit_breakers on restart ─────────────────────────────────────────

def test_load_active_circuit_breaker_on_startup(config, db):
    # pause_until must be ahead of real now so get_active_risk_events returns it
    real_now = datetime.now(_UTC)
    pause_until = real_now + timedelta(hours=2)
    db.record_risk_event({
        "event_type": "vol_circuit_breaker",
        "triggered_at": real_now.isoformat(),
        "pause_until": pause_until.isoformat(),
        "context_json": '{"reason": "test"}',
    })
    rm2 = RiskManager(config, db)
    assert rm2.state.vol_circuit_breaker_until is not None
    assert rm2.state.vol_circuit_breaker_until > real_now


def test_expired_circuit_breaker_not_restored(config, db):
    real_now = datetime.now(_UTC)
    expired = real_now - timedelta(hours=1)
    db.record_risk_event({
        "event_type": "vol_circuit_breaker",
        "triggered_at": (real_now - timedelta(hours=3)).isoformat(),
        "pause_until": expired.isoformat(),
        "context_json": "{}",
    })
    rm2 = RiskManager(config, db)
    assert rm2.state.vol_circuit_breaker_until is None
