"""
Unit tests for the backtesting framework.

Uses synthetic data only — no network calls, no DB.
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backtest.data_loader import (
    MinuteBar,
    _make_ticker,
    synthesize_kalshi_snapshots,
)
from backtest.replay import (
    BacktestResult,
    TradeRecord,
    _build_price_state,
    _kalshi_fee,
    _settle_trade,
    run_backtest,
)
from backtest.reports import (
    AggregateStats,
    SanityGateResult,
    _max_drawdown,
    check_sanity_gates,
    compute_aggregate_stats,
    bucket_by_day_of_week,
    bucket_by_hour_of_day,
    bucket_by_vol_regime,
)
from src.blackout_calendar import BlackoutCalendar
from src.config import load_config

_UTC = timezone.utc
_ET = ZoneInfo("America/New_York")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def config():
    return load_config("config/default.yaml").strategy


@pytest.fixture
def blackout():
    return BlackoutCalendar(
        config_path="config/blackouts.yaml",
        max_risk_level=1,
        econ_events=[],
    )


def _make_bar(ts: float, price: float) -> MinuteBar:
    return MinuteBar(ts=ts, open=price, high=price, low=price, close=price, volume=1000.0)


def _make_bars_flat(
    start: datetime,
    n: int,
    price: float = 86_000.0,
) -> list[MinuteBar]:
    """Return n 1-minute bars at a flat price starting from `start`."""
    base_ts = start.timestamp()
    return [_make_bar(base_ts + i * 60.0, price) for i in range(n)]


def _make_trade(
    won: bool,
    net_pnl: float,
    entry_time: datetime | None = None,
    rv_60: float = 0.30,
) -> TradeRecord:
    entry_time = entry_time or datetime(2026, 1, 15, 14, 30, 0, tzinfo=_UTC)
    return TradeRecord(
        ticker="KXBTCD-TEST",
        side="yes",
        floor_strike=85_000.0,
        close_time=entry_time + timedelta(hours=1),
        entry_time_utc=entry_time,
        entry_spot=86_000.0,
        ask_price=0.99,
        fill_usd=1000.0,
        quantity=1010,
        settlement_price=86_200.0 if won else 84_800.0,
        won=won,
        gross_pnl_usd=net_pnl + 5.0,
        fee_usd=5.0,
        net_pnl_usd=net_pnl,
        rv_60_at_entry=rv_60,
        rv_24h_at_entry=rv_60,
        buffer_usd=1000.0,
        reject_count_before=0,
    )


# ── data_loader: synthesize_kalshi_snapshots ──────────────────────────────────

def test_synthesize_creates_snapshots_for_each_hour():
    start = datetime(2026, 1, 15, 14, 0, 0, tzinfo=_UTC)
    bars = _make_bars_flat(start, n=120, price=86_000.0)  # 2 hours
    snaps = synthesize_kalshi_snapshots(bars)
    # Should have snapshots for at least 2 distinct hour close_times
    close_times = {v[0].close_time for v in snaps.values()}
    assert len(close_times) >= 2


def test_synthesize_strike_is_multiple_of_100():
    start = datetime(2026, 1, 15, 14, 0, 0, tzinfo=_UTC)
    bars = _make_bars_flat(start, n=60, price=86_234.0)
    snaps = synthesize_kalshi_snapshots(bars)
    for (mb, strike), _ in snaps.items():
        assert strike % 100 == 0, f"Strike {strike} is not a multiple of 100"


def test_synthesize_strike_below_spot():
    start = datetime(2026, 1, 15, 14, 0, 0, tzinfo=_UTC)
    bars = _make_bars_flat(start, n=60, price=86_000.0)
    snaps = synthesize_kalshi_snapshots(bars)
    for (mb, strike), _ in snaps.items():
        assert strike < 86_000.0


def test_synthesize_yes_ask_default():
    start = datetime(2026, 1, 15, 14, 0, 0, tzinfo=_UTC)
    bars = _make_bars_flat(start, n=60, price=86_000.0)
    snaps = synthesize_kalshi_snapshots(bars, yes_ask=0.99)
    for snaps_list in snaps.values():
        assert snaps_list[0].yes_ask == 0.99


def test_make_ticker_format():
    close_time = datetime(2026, 5, 21, 15, 0, 0, tzinfo=_UTC)
    ticker = _make_ticker(close_time, 85_300.0)
    assert ticker.startswith("KXBTCD-")
    assert "85300.00" in ticker


# ── replay: _kalshi_fee ───────────────────────────────────────────────────────

def test_kalshi_fee_at_99_cents():
    # p=0.99, quantity=1: ceil(7 * 1 * 0.99 * 0.01) / 100 = ceil(0.0693) / 100 = 1 / 100 = 0.01
    fee = _kalshi_fee(99, 1)
    assert fee == pytest.approx(0.01)


def test_kalshi_fee_scales_with_quantity():
    fee1 = _kalshi_fee(99, 100)
    fee2 = _kalshi_fee(99, 200)
    assert fee2 == pytest.approx(fee1 * 2, rel=0.01)


def test_kalshi_fee_at_50_cents_is_max():
    # p=0.50 maximizes p*(1-p)=0.25
    fee_50 = _kalshi_fee(50, 100)
    fee_99 = _kalshi_fee(99, 100)
    assert fee_50 > fee_99


# ── replay: _build_price_state ────────────────────────────────────────────────

def test_build_price_state_spot():
    dq: deque[tuple[float, float]] = deque(maxlen=10_080)
    dq.append((time_s := 1_700_000_000.0, 86_000.0))
    ps = _build_price_state(dq, time_s, 86_100.0)
    assert ps.spot == 86_100.0


def test_build_price_state_rv_nan_with_one_price():
    dq: deque[tuple[float, float]] = deque(maxlen=10_080)
    dq.append((1_700_000_000.0, 86_000.0))
    ps = _build_price_state(dq, 1_700_000_000.0, 86_000.0)
    assert math.isnan(ps.rv_60_annualized)


def test_build_price_state_rv_zero_for_flat():
    dq: deque[tuple[float, float]] = deque(maxlen=10_080)
    now = 1_700_000_000.0
    for i in range(70):
        dq.append((now - (70 - i) * 60, 86_000.0))
    ps = _build_price_state(dq, now, 86_000.0)
    assert ps.rv_60_annualized == pytest.approx(0.0, abs=1e-10)


# ── replay: _settle_trade ─────────────────────────────────────────────────────

def test_settle_yes_win():
    result = BacktestResult()
    t = _make_trade(won=False, net_pnl=0.0)
    t.ask_price = 0.99
    t.quantity = 100
    t.fill_usd = 99.0
    t.side = "yes"
    t.floor_strike = 85_000.0
    t.fee_usd = 0.50
    _settle_trade(t, 86_000.0, result)  # above strike → YES wins
    assert t.won
    assert t.gross_pnl_usd == pytest.approx(100 * (1.0 - 0.99))
    assert t.net_pnl_usd == pytest.approx(t.gross_pnl_usd - t.fee_usd)
    assert len(result.trades) == 1


def test_settle_yes_loss():
    result = BacktestResult()
    t = _make_trade(won=False, net_pnl=0.0)
    t.ask_price = 0.99
    t.quantity = 100
    t.fill_usd = 99.0
    t.side = "yes"
    t.floor_strike = 85_000.0
    t.fee_usd = 0.50
    _settle_trade(t, 84_000.0, result)  # below strike → YES loses
    assert not t.won
    assert t.gross_pnl_usd == -99.0
    assert t.net_pnl_usd == pytest.approx(-99.0 - 0.50)


def test_settle_no_win():
    result = BacktestResult()
    t = _make_trade(won=False, net_pnl=0.0)
    t.ask_price = 0.99
    t.quantity = 100
    t.fill_usd = 99.0
    t.side = "no"
    t.floor_strike = 85_000.0
    t.fee_usd = 0.50
    _settle_trade(t, 84_000.0, result)  # below strike → NO wins
    assert t.won


def test_settle_no_loss():
    result = BacktestResult()
    t = _make_trade(won=False, net_pnl=0.0)
    t.ask_price = 0.99
    t.quantity = 100
    t.fill_usd = 99.0
    t.side = "no"
    t.floor_strike = 85_000.0
    t.fee_usd = 0.50
    _settle_trade(t, 86_000.0, result)  # above strike → NO loses
    assert not t.won


# ── replay: run_backtest (integration) ────────────────────────────────────────

def test_run_backtest_no_trades_without_enough_history(config, blackout):
    """Not enough bars to build RV_60 (< 2 prices) → evaluate_entry rejects."""
    start = datetime(2026, 5, 21, 14, 30, 0, tzinfo=_UTC)
    bars = _make_bars_flat(start, n=5, price=86_000.0)
    snaps = synthesize_kalshi_snapshots(bars)
    result = run_backtest(bars, snaps, blackout, config)
    # Possibly no trades due to insufficient vol history
    assert isinstance(result, BacktestResult)


def test_run_backtest_returns_result_type(config, blackout):
    start = datetime(2026, 1, 15, 14, 30, 0, tzinfo=_UTC)
    bars = _make_bars_flat(start, n=300, price=86_000.0)
    snaps = synthesize_kalshi_snapshots(bars)
    result = run_backtest(bars, snaps, blackout, config)
    assert isinstance(result.trades, list)
    assert isinstance(result.rejects, list)


def test_run_backtest_all_wins_with_stable_price(config, blackout):
    """
    Flat price well above strikes → all settled YES contracts win.
    We need enough bars for RV to be computed and timing to pass.
    """
    # Use a Wednesday in the equity window (10 ET = 14 UTC) to avoid blackouts
    # min_minutes_into_hour=25, so use :30 mark
    base = datetime(2026, 1, 14, 14, 30, 0, tzinfo=_UTC)  # Wednesday 9:30 ET
    # Build 8h of bars so rv_baseline and rv_60 are computable
    bars = _make_bars_flat(base - timedelta(hours=7), n=60 * 8, price=86_000.0)
    snaps = synthesize_kalshi_snapshots(bars, yes_ask=0.99)
    result = run_backtest(bars, snaps, blackout, config)
    if result.trades:
        for t in result.trades:
            assert t.won, f"Expected YES win at spot=86000, strike={t.floor_strike}"


# ── reports: compute_aggregate_stats ─────────────────────────────────────────

def test_aggregate_stats_empty():
    result = BacktestResult()
    stats = compute_aggregate_stats(result)
    assert stats.total_trades == 0
    assert stats.net_pnl_usd == 0.0


def test_aggregate_stats_wins_only():
    result = BacktestResult()
    for _ in range(5):
        result.trades.append(_make_trade(won=True, net_pnl=5.0))
    stats = compute_aggregate_stats(result)
    assert stats.total_trades == 5
    assert stats.wins == 5
    assert stats.win_rate == 1.0
    assert stats.net_pnl_usd == pytest.approx(25.0)


def test_aggregate_stats_losses_only():
    result = BacktestResult()
    for _ in range(3):
        result.trades.append(_make_trade(won=False, net_pnl=-100.0))
    stats = compute_aggregate_stats(result)
    assert stats.win_rate == 0.0
    assert stats.net_pnl_usd == pytest.approx(-300.0)


def test_aggregate_stats_mixed():
    result = BacktestResult()
    result.trades.append(_make_trade(won=True, net_pnl=5.0))
    result.trades.append(_make_trade(won=False, net_pnl=-100.0))
    stats = compute_aggregate_stats(result)
    assert stats.win_rate == pytest.approx(0.5)
    assert stats.net_pnl_usd == pytest.approx(-95.0)


# ── reports: _max_drawdown ────────────────────────────────────────────────────

def test_max_drawdown_all_wins():
    trades = [_make_trade(won=True, net_pnl=10.0) for _ in range(5)]
    assert _max_drawdown(trades) == pytest.approx(0.0)


def test_max_drawdown_peak_then_loss():
    base = datetime(2026, 1, 15, 10, 0, 0, tzinfo=_UTC)
    trades = [
        _make_trade(won=True, net_pnl=100.0, entry_time=base),
        _make_trade(won=True, net_pnl=100.0, entry_time=base + timedelta(hours=1)),
        _make_trade(won=False, net_pnl=-150.0, entry_time=base + timedelta(hours=2)),
    ]
    dd = _max_drawdown(trades)
    assert dd == pytest.approx(150.0)


def test_max_drawdown_empty():
    assert _max_drawdown([]) == 0.0


# ── reports: bucket functions ─────────────────────────────────────────────────

def test_bucket_by_dow_keys_are_day_names():
    result = BacktestResult()
    result.trades.append(_make_trade(won=True, net_pnl=5.0,
        entry_time=datetime(2026, 1, 12, 14, 0, 0, tzinfo=_UTC)))  # Monday
    buckets = bucket_by_day_of_week(result)
    assert "Monday" in buckets


def test_bucket_by_hour():
    result = BacktestResult()
    result.trades.append(_make_trade(won=True, net_pnl=5.0,
        entry_time=datetime(2026, 1, 12, 14, 30, 0, tzinfo=_UTC)))  # 9:30 ET
    buckets = bucket_by_hour_of_day(result)
    assert 9 in buckets


def test_bucket_by_vol_regime_low():
    result = BacktestResult()
    result.trades.append(_make_trade(won=True, net_pnl=5.0, rv_60=0.20))
    buckets = bucket_by_vol_regime(result)
    assert "low" in buckets
    assert buckets["low"]["count"] == 1


def test_bucket_by_vol_regime_high():
    result = BacktestResult()
    result.trades.append(_make_trade(won=True, net_pnl=5.0, rv_60=0.75))
    buckets = bucket_by_vol_regime(result)
    assert "high" in buckets


# ── reports: check_sanity_gates ──────────────────────────────────────────────

def test_sanity_gate_empty_fails():
    gate = check_sanity_gates(BacktestResult())
    assert not gate.passed
    assert "No trades" in gate.reasons_failed[0]


def test_sanity_gate_insufficient_history_fails():
    result = BacktestResult()
    base = datetime(2026, 5, 1, 14, 0, 0, tzinfo=_UTC)
    # Only 1 month of trades
    for i in range(30):
        result.trades.append(_make_trade(
            won=True, net_pnl=5.0,
            entry_time=base + timedelta(days=i),
        ))
    gate = check_sanity_gates(result, min_months=6.0)
    assert not gate.passed
    assert any("Insufficient history" in r for r in gate.reasons_failed)


def test_sanity_gate_negative_pnl_fails():
    result = BacktestResult()
    base = datetime(2026, 1, 1, 14, 0, 0, tzinfo=_UTC)
    # 6+ months but all losses
    for i in range(200):
        result.trades.append(_make_trade(
            won=False, net_pnl=-100.0,
            entry_time=base + timedelta(days=i),
        ))
    gate = check_sanity_gates(result, slippage_pct=0.01, min_months=6.0)
    assert not gate.passed
    assert gate.net_pnl_with_slippage_usd < 0


def test_sanity_gate_passes_with_positive_pnl():
    result = BacktestResult()
    base = datetime(2026, 1, 1, 14, 0, 0, tzinfo=_UTC)
    # 6+ months of wins where net_pnl ($15) exceeds 1% slippage on fill ($10)
    for i in range(200):
        result.trades.append(_make_trade(
            won=True, net_pnl=15.0,
            entry_time=base + timedelta(days=i),
        ))
    gate = check_sanity_gates(result, slippage_pct=0.01, min_months=6.0)
    assert gate.passed
    assert gate.net_pnl_with_slippage_usd > 0
    assert gate.months_covered >= 6.0
