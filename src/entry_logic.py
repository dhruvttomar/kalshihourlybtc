"""
Entry condition evaluation — the final gate before an order is placed.

evaluate_entry() walks through all 9 conditions in spec order, returning the
first failure with a human-readable reason, or a passing EntryDecision with
the order parameters. Every call is logged by the caller.

Conditions (in order):
  1. Timing: minutes_into_hour >= 25, seconds_remaining > 60
  2. Blackout window not active
  3. Hourly line count < max_lines_per_hour
  4. Hourly capital deployed < max_capital_per_hour
  5. Risk circuit breakers not open
  6. Volatility filters pass (RV_60 <= 1.5 * baseline, RV_60 <= 0.70 ceiling)
  7. YES price in [0.98, 0.99]
  8. Buffer >= dynamic_buffer(spot, rv, minutes_remaining)
  9. Orderbook depth >= target_size * safety_factor
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from src.blackout_calendar import BlackoutCalendar
from src.config import StrategyConfig
from src.models import EntryDecision, PositionTrackerProtocol, QualifyingTrade, RiskState
from src.price_feed import PriceState
from src.vol_calculator import dynamic_buffer


def evaluate_entry(
    trade: QualifyingTrade,
    price_state: PriceState,
    blackout: BlackoutCalendar,
    risk_state: RiskState,
    position_tracker: PositionTrackerProtocol,
    now_et: datetime,
    config: StrategyConfig,
) -> EntryDecision:
    """
    Evaluate all entry conditions for a qualifying trade opportunity.

    Returns the FIRST failure with a reason string, or a passing decision
    with the target price and size.
    """
    market = trade.market

    def _reject(reason: str) -> EntryDecision:
        return EntryDecision(should_trade=False, reason=reason)

    # ── 1. Timing ─────────────────────────────────────────────────────────────
    minutes_into_hour = now_et.minute
    if minutes_into_hour < config.min_minutes_into_hour:
        return _reject(
            f"Too early: {minutes_into_hour} min into hour "
            f"(need >= {config.min_minutes_into_hour})"
        )

    now_utc = now_et.astimezone(timezone.utc)
    seconds_remaining = (market.close_time - now_utc).total_seconds()
    if seconds_remaining <= config.hour_end_cutoff_seconds:
        return _reject(
            f"Too close to settlement: {seconds_remaining:.0f}s remaining "
            f"(cutoff: {config.hour_end_cutoff_seconds}s)"
        )

    # ── 2. Blackout ───────────────────────────────────────────────────────────
    blocked, reason = blackout.is_blocked(now_et)
    if blocked:
        return _reject(f"Blackout active: {reason}")

    # ── 3. Hourly line count ──────────────────────────────────────────────────
    lines_this_hour = position_tracker.lines_taken_this_hour(now_et)
    if lines_this_hour >= config.max_lines_per_hour:
        return _reject(
            f"Hourly line cap reached: {lines_this_hour}/{config.max_lines_per_hour}"
        )

    # ── 4. Hourly capital ─────────────────────────────────────────────────────
    capital_this_hour = position_tracker.capital_deployed_this_hour(now_et)
    if capital_this_hour >= config.max_capital_per_hour_usd:
        return _reject(
            f"Hourly capital cap reached: ${capital_this_hour:.0f} "
            f"(max ${config.max_capital_per_hour_usd:.0f})"
        )

    # ── 5. Risk circuit breakers ──────────────────────────────────────────────
    if risk_state.daily_loss_limit_hit:
        return _reject("Daily loss limit hit")
    if risk_state.weekly_loss_limit_hit:
        return _reject("Weekly loss limit hit")
    if (
        risk_state.vol_circuit_breaker_until is not None
        and now_utc < risk_state.vol_circuit_breaker_until
    ):
        return _reject(
            f"Vol circuit breaker active until {risk_state.vol_circuit_breaker_until.isoformat()}"
        )
    if (
        risk_state.liquidation_circuit_breaker_until is not None
        and now_utc < risk_state.liquidation_circuit_breaker_until
    ):
        return _reject(
            f"Liquidation circuit breaker active until "
            f"{risk_state.liquidation_circuit_breaker_until.isoformat()}"
        )

    # ── 6. Volatility filters ─────────────────────────────────────────────────
    rv_60 = price_state.rv_60_annualized
    if math.isnan(rv_60):
        return _reject("RV_60 unavailable (insufficient price history)")

    rv_baseline = price_state.rv_baseline_7d_median
    if not math.isnan(rv_baseline) and rv_baseline > 0:
        if rv_60 > config.rv_baseline_multiplier * rv_baseline:
            return _reject(
                f"Vol elevated: RV_60={rv_60:.3f} > "
                f"{config.rv_baseline_multiplier}x baseline {rv_baseline:.3f}"
            )

    if rv_60 > config.rv_hard_ceiling_annualized:
        return _reject(
            f"Vol ceiling breached: RV_60={rv_60:.3f} > {config.rv_hard_ceiling_annualized}"
        )

    # ── 7. YES price filter ───────────────────────────────────────────────────
    ask = trade.ask_price
    if ask is None:
        return _reject("No ask price available for this side")

    if not (config.yes_price_min <= ask <= config.yes_price_max):
        return _reject(
            f"Price out of range: {ask:.4f} "
            f"(need [{config.yes_price_min}, {config.yes_price_max}])"
        )

    # ── 8. Buffer requirement ─────────────────────────────────────────────────
    spot = price_state.spot
    minutes_remaining = max(1, int(seconds_remaining / 60))
    required_buffer = dynamic_buffer(
        spot=spot,
        rv_annualized=rv_60,
        minutes_remaining=minutes_remaining,
        floor=config.buffer_floor_usd,
        sigma_multiplier=config.buffer_sigma_multiplier,
    )
    actual_buffer = trade.buffer_vs_spot(spot)
    if actual_buffer < required_buffer:
        return _reject(
            f"Buffer too small: ${actual_buffer:.0f} < required ${required_buffer:.0f} "
            f"(spot={spot:.0f}, strike={market.floor_strike:.0f}, side={trade.side})"
        )

    # ── 9. Orderbook depth ────────────────────────────────────────────────────
    capital_remaining_on_line = config.max_capital_per_line_usd - capital_this_hour
    target_fill = min(capital_remaining_on_line, config.max_capital_per_line_usd)
    required_depth = target_fill * config.orderbook_depth_safety_factor
    actual_depth = trade.depth_usd
    if actual_depth < required_depth:
        return _reject(
            f"Insufficient depth: ${actual_depth:.0f} < required ${required_depth:.0f}"
        )

    # ── All checks passed ─────────────────────────────────────────────────────
    target_price_cents = round(ask * 100)
    remaining_capacity = config.max_capital_per_line_usd - (
        capital_this_hour % config.max_capital_per_line_usd
        if capital_this_hour > 0 else 0
    )
    target_size = min(remaining_capacity, config.max_capital_per_line_usd)

    return EntryDecision(
        should_trade=True,
        reason="All entry conditions satisfied",
        market_ticker=market.ticker,
        side=trade.side,
        target_price_cents=target_price_cents,
        target_size_usd=target_size,
    )
