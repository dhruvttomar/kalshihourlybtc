from __future__ import annotations

import math

import numpy as np

_MINUTES_PER_YEAR = 525_600


def realized_vol_annualized(
    prices: list[float],
    minutes_per_year: int = _MINUTES_PER_YEAR,
) -> float:
    """
    Annualized realized volatility from 1-minute close prices.

    Uses log returns with ddof=1 (sample std), annualized by sqrt(minutes_per_year).
    Returns float('nan') if fewer than 2 prices are provided.
    Result is a decimal (e.g. 0.50 = 50% annualized vol).
    """
    if len(prices) < 2:
        return float("nan")
    arr = np.asarray(prices, dtype=float)
    log_returns = np.diff(np.log(arr))
    minute_vol = float(np.std(log_returns, ddof=1))
    return minute_vol * math.sqrt(minutes_per_year)


def dynamic_buffer(
    spot: float,
    rv_annualized: float,
    minutes_remaining: int,
    floor: float = 550.0,
    sigma_multiplier: float = 2.0,
    minutes_per_year: int = _MINUTES_PER_YEAR,
) -> float:
    """
    Minimum required dollar buffer between spot price and strike.

    Sized as sigma_multiplier * expected_move over the remaining time,
    floored at `floor` dollars. The $550 floor also absorbs the ~$50–100
    gap between Coinbase spot and the CF Benchmarks BRTI settlement index
    used by Kalshi (final-minute 60-sample average).
    """
    expected_move = spot * rv_annualized * math.sqrt(minutes_remaining / minutes_per_year)
    return max(floor, sigma_multiplier * expected_move)
