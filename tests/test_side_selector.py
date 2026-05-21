"""Unit tests for side_selector.py."""
import pytest
from src.side_selector import select_side


def test_clear_above_wins():
    # above_strike is much closer to spot → big above buffer
    result = select_side(spot=85_000, above_strike=84_000, below_strike=86_500)
    # buffer_above = 1000, buffer_below = 1500 → below wins
    assert result == "below"

def test_clear_below_wins():
    result = select_side(spot=85_000, above_strike=83_000, below_strike=85_800)
    # buffer_above = 2000, buffer_below = 800 → above wins
    assert result == "above"

def test_symmetric_returns_either():
    # buffer_above = 1000, buffer_below = 1000 → diff/max = 0 < 10%
    result = select_side(spot=85_000, above_strike=84_000, below_strike=86_000)
    assert result == "either"

def test_within_threshold_returns_either():
    # buffer_above = 1000, buffer_below = 1050 → diff=50, max=1050, ratio≈4.8% < 10%
    result = select_side(spot=85_000, above_strike=84_000, below_strike=86_050)
    assert result == "either"

def test_just_outside_threshold_picks_winner():
    # buffer_above = 1000, buffer_below = 1120 → diff=120, max=1120, ratio≈10.7% > 10%
    result = select_side(spot=85_000, above_strike=84_000, below_strike=86_120)
    assert result == "below"

def test_exact_symmetry_threshold_boundary():
    # Exactly at 10%: diff/max == 0.10 → NOT strictly less than, returns a side
    # buffer_above=1000, buffer_below=1000*(1+0.10)=1100 → diff=100, max=1100, ratio=100/1100≈9.09%
    # That's < 10% → "either". Let's try 1000 vs 1112 → ratio=112/1112≈10.07% > 10% → picks
    result = select_side(spot=85_000, above_strike=84_000, below_strike=86_112)
    assert result == "below"

def test_large_asymmetry():
    # buffer_above = 5000, buffer_below = 500
    result = select_side(spot=85_000, above_strike=80_000, below_strike=85_500)
    assert result == "above"

def test_both_zero_returns_either():
    # spot == above_strike == below_strike → both buffers are 0
    result = select_side(spot=85_000, above_strike=85_000, below_strike=85_000)
    assert result == "either"

def test_custom_symmetry_threshold():
    # With a 20% threshold, same inputs that would normally pick a side now return "either"
    # buffer_above=1000, buffer_below=1150 → diff=150, max=1150 ≈ 13% < 20%
    result = select_side(
        spot=85_000, above_strike=84_000, below_strike=86_150,
        symmetry_threshold=0.20,
    )
    assert result == "either"

def test_above_buffer_is_negative_spot_below_above_strike():
    # Spot is below above_strike → buffer_above is negative; below wins
    result = select_side(spot=84_000, above_strike=84_500, below_strike=85_000)
    # buffer_above = -500, buffer_below = 1000
    assert result == "below"

def test_realistic_btc_scenario():
    # BTC at $86,500; YES candidate at $85,800 (buffer 700), NO candidate at $87,300 (buffer 800)
    result = select_side(spot=86_500, above_strike=85_800, below_strike=87_300)
    # diff=100, max=800 = 12.5% > 10% → below wins
    assert result == "below"
