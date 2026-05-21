"""
Unit tests for vol_calculator.py.

All tests are deterministic (no I/O, no randomness outside fixed seeds).
"""
import math

import numpy as np
import pytest

from src.vol_calculator import dynamic_buffer, realized_vol_annualized

_MINUTES_PER_YEAR = 525_600


# ── realized_vol_annualized ───────────────────────────────────────────────────


def test_realized_vol_too_short_empty():
    assert math.isnan(realized_vol_annualized([]))


def test_realized_vol_too_short_one_price():
    assert math.isnan(realized_vol_annualized([50_000.0]))


def test_realized_vol_flat_prices_is_zero():
    prices = [50_000.0] * 100
    vol = realized_vol_annualized(prices)
    assert vol == pytest.approx(0.0, abs=1e-12)


def test_realized_vol_two_prices_nonzero():
    # Two prices → one log return → std of a single value with ddof=1 is nan
    # (sample std of a single observation is undefined)
    vol = realized_vol_annualized([100.0, 101.0])
    assert math.isnan(vol)


def test_realized_vol_three_prices_nonzero():
    prices = [100.0, 101.0, 100.0]
    vol = realized_vol_annualized(prices)
    assert vol > 0.0
    assert not math.isnan(vol)


def test_realized_vol_known_sequence():
    """
    Build a synthetic sequence with a known per-minute std and verify
    the annualized result matches the expected formula.
    """
    rng = np.random.default_rng(42)
    per_min_sigma = 0.001  # 0.1% per minute → ~72% annualized
    log_returns = rng.normal(0.0, per_min_sigma, size=60)
    prices = [50_000.0 * np.exp(np.cumsum(log_returns)[i]) for i in range(60)]
    prices = [50_000.0] + prices  # prepend starting price so we get 60 returns

    expected_sample_std = float(np.std(log_returns, ddof=1))
    expected_vol = expected_sample_std * math.sqrt(_MINUTES_PER_YEAR)

    computed_vol = realized_vol_annualized(prices)
    assert computed_vol == pytest.approx(expected_vol, rel=1e-9)


def test_realized_vol_custom_minutes_per_year():
    prices = [100.0, 101.0, 100.5, 102.0, 101.5]
    vol_default = realized_vol_annualized(prices)
    vol_custom = realized_vol_annualized(prices, minutes_per_year=262_800)  # half year
    assert vol_custom == pytest.approx(vol_default / math.sqrt(2), rel=1e-9)


def test_realized_vol_returns_decimal_not_percent():
    # 50% annualized vol should return ~0.50, not 50
    rng = np.random.default_rng(7)
    # Build a sequence with ~50% annualized vol
    per_min_sigma = 0.50 / math.sqrt(_MINUTES_PER_YEAR)
    log_rets = rng.normal(0.0, per_min_sigma, size=200)
    prices = list(50_000.0 * np.exp(np.cumsum(np.concatenate([[0], log_rets]))))
    vol = realized_vol_annualized(prices)
    assert 0.01 < vol < 5.0, f"Expected decimal vol ~0.5, got {vol}"


# ── dynamic_buffer ────────────────────────────────────────────────────────────


def test_dynamic_buffer_returns_floor_when_vol_is_zero():
    buf = dynamic_buffer(spot=80_000.0, rv_annualized=0.0, minutes_remaining=30)
    assert buf == pytest.approx(550.0)


def test_dynamic_buffer_returns_floor_when_computed_is_smaller():
    # Very low vol, very few minutes → computed < 550
    buf = dynamic_buffer(
        spot=80_000.0,
        rv_annualized=0.01,  # 1% annualized
        minutes_remaining=1,
    )
    assert buf == pytest.approx(550.0)


def test_dynamic_buffer_above_floor():
    # High vol + many minutes → computed > floor
    buf = dynamic_buffer(
        spot=80_000.0,
        rv_annualized=0.80,  # 80% annualized
        minutes_remaining=35,
    )
    expected_move = 80_000.0 * 0.80 * math.sqrt(35 / _MINUTES_PER_YEAR)
    expected = max(550.0, 2.0 * expected_move)
    assert buf == pytest.approx(expected, rel=1e-9)
    assert buf > 550.0


def test_dynamic_buffer_custom_floor():
    buf = dynamic_buffer(
        spot=80_000.0,
        rv_annualized=0.0,
        minutes_remaining=30,
        floor=1000.0,
    )
    assert buf == pytest.approx(1000.0)


def test_dynamic_buffer_custom_sigma_multiplier():
    spot = 80_000.0
    rv = 0.50
    mins = 30
    buf_2x = dynamic_buffer(spot, rv, mins, sigma_multiplier=2.0)
    buf_3x = dynamic_buffer(spot, rv, mins, sigma_multiplier=3.0)
    # 3x buffer should be proportionally larger (when above floor)
    expected_move = spot * rv * math.sqrt(mins / _MINUTES_PER_YEAR)
    if 2.0 * expected_move > 550.0:
        assert buf_3x == pytest.approx(buf_2x * 1.5, rel=1e-9)


def test_dynamic_buffer_scales_with_sqrt_minutes():
    spot = 80_000.0
    rv = 0.60
    buf_30 = dynamic_buffer(spot, rv, minutes_remaining=30)
    buf_120 = dynamic_buffer(spot, rv, minutes_remaining=120)
    expected_move_30 = spot * rv * math.sqrt(30 / _MINUTES_PER_YEAR)
    if 2.0 * expected_move_30 > 550.0:
        assert buf_120 == pytest.approx(buf_30 * math.sqrt(4), rel=1e-9)


def test_dynamic_buffer_btc_realistic_scenario():
    # BTC at $85k, 60% annualized vol, 30 min remaining → comfortably above $550 floor
    # 2 * 85000 * 0.60 * sqrt(30/525600) ≈ 2 * 85000 * 0.60 * 0.00755 ≈ 770
    buf = dynamic_buffer(spot=85_000.0, rv_annualized=0.60, minutes_remaining=30)
    expected_move = 85_000.0 * 0.60 * math.sqrt(30 / _MINUTES_PER_YEAR)
    expected = max(550.0, 2.0 * expected_move)
    assert buf == pytest.approx(expected, rel=1e-9)
    assert buf > 550.0
