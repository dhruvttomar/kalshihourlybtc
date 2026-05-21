"""
Unit tests for market_scanner.py.

All Kalshi API calls are replaced with AsyncMock stubs so no network is needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.config import load_config
from src.market_scanner import _markets_for_current_hour, _settlement_time_utc, scan_qualifying_markets
from src.models import Market

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc

# Simulated clock: Thursday 2026-05-21 10:35 AM ET → settlement at 11:00 AM ET = 15:00 UTC
_NOW_ET = datetime(2026, 5, 21, 10, 35, 0, tzinfo=_ET)
_SETTLEMENT_UTC = datetime(2026, 5, 21, 15, 0, 0, tzinfo=_UTC)


def _config():
    return load_config("config/default.yaml").strategy


def _make_market(
    ticker: str = "KXBTCD-26MAY2111-T85299.99",
    floor_strike: float = 85_300.0,
    yes_ask: float | None = 0.99,
    no_ask: float | None = None,
    close_time: datetime | None = None,
) -> Market:
    return Market(
        ticker=ticker,
        floor_strike=floor_strike,
        yes_ask=yes_ask,
        no_ask=no_ask,
        close_time=close_time or _SETTLEMENT_UTC,
        depth_yes_usd=0.0,
        depth_no_usd=0.0,
    )


def _make_price_state(spot: float = 86_000.0):
    """Minimal PriceState-like object for the scanner (only .spot is used)."""
    ps = MagicMock()
    ps.spot = spot
    return ps


def _make_kalshi(
    markets: list[Market] | None = None,
    orderbook: dict | None = None,
) -> AsyncMock:
    client = AsyncMock()
    client.get_active_btc_markets = AsyncMock(return_value=markets or [])
    client.get_orderbook = AsyncMock(return_value=orderbook or {
        "yes_best_ask_cents": 99,
        "no_best_ask_cents": None,
        "depth_yes_usd": 2500.0,
        "depth_no_usd": 0.0,
    })
    return client


# ── _settlement_time_utc helper ───────────────────────────────────────────────

def test_settlement_time_top_of_next_hour():
    now = datetime(2026, 5, 21, 10, 35, 0, tzinfo=_ET)
    s = _settlement_time_utc(now)
    assert s == datetime(2026, 5, 21, 15, 0, 0, tzinfo=_UTC)


def test_settlement_time_at_minute_zero():
    # 10:00 AM ET → settlement at 11:00 AM ET
    now = datetime(2026, 5, 21, 10, 0, 0, tzinfo=_ET)
    s = _settlement_time_utc(now)
    assert s == datetime(2026, 5, 21, 15, 0, 0, tzinfo=_UTC)


def test_settlement_time_midnight_rollover():
    now = datetime(2026, 5, 21, 23, 30, 0, tzinfo=_ET)
    s = _settlement_time_utc(now)
    # 11:00 PM ET + 1h = midnight ET = 04:00 UTC next day
    assert s.hour == 4
    assert s.day == 22


# ── _markets_for_current_hour helper ─────────────────────────────────────────

def test_markets_for_current_hour_exact_match():
    m = _make_market(close_time=_SETTLEMENT_UTC)
    result = _markets_for_current_hour([m], _SETTLEMENT_UTC)
    assert m in result


def test_markets_for_current_hour_within_tolerance():
    # 30 seconds off — still matches
    m = _make_market(close_time=_SETTLEMENT_UTC + timedelta(seconds=30))
    result = _markets_for_current_hour([m], _SETTLEMENT_UTC)
    assert m in result


def test_markets_for_current_hour_outside_tolerance():
    # Different hour entirely — excluded
    m = _make_market(close_time=_SETTLEMENT_UTC + timedelta(hours=1))
    result = _markets_for_current_hour([m], _SETTLEMENT_UTC)
    assert result == []


def test_markets_for_current_hour_multiple_hours():
    m_now = _make_market(ticker="A", close_time=_SETTLEMENT_UTC)
    m_next = _make_market(ticker="B", close_time=_SETTLEMENT_UTC + timedelta(hours=1))
    result = _markets_for_current_hour([m_now, m_next], _SETTLEMENT_UTC)
    assert m_now in result
    assert m_next not in result


# ── scan_qualifying_markets ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_empty_markets_returns_empty():
    client = _make_kalshi(markets=[])
    result = await scan_qualifying_markets(_make_price_state(), client, _config(), _NOW_ET)
    assert result == []


@pytest.mark.asyncio
async def test_scan_yes_side_in_range_qualifies():
    m = _make_market(yes_ask=0.99, floor_strike=85_300.0)
    client = _make_kalshi(markets=[m])
    result = await scan_qualifying_markets(_make_price_state(86_000), client, _config(), _NOW_ET)
    assert len(result) == 1
    assert result[0].side == "yes"
    assert result[0].market.ticker == m.ticker


@pytest.mark.asyncio
async def test_scan_no_side_in_range_qualifies():
    m = _make_market(yes_ask=None, no_ask=0.99, floor_strike=87_000.0)
    client = _make_kalshi(markets=[m], orderbook={
        "yes_best_ask_cents": None,
        "no_best_ask_cents": 99,
        "depth_yes_usd": 0.0,
        "depth_no_usd": 2500.0,
    })
    result = await scan_qualifying_markets(_make_price_state(86_000), client, _config(), _NOW_ET)
    assert len(result) == 1
    assert result[0].side == "no"


@pytest.mark.asyncio
async def test_scan_yes_price_too_low_excluded():
    m = _make_market(yes_ask=0.97)
    client = _make_kalshi(markets=[m])
    result = await scan_qualifying_markets(_make_price_state(), client, _config(), _NOW_ET)
    assert result == []


@pytest.mark.asyncio
async def test_scan_yes_price_too_high_excluded():
    m = _make_market(yes_ask=1.00)
    client = _make_kalshi(markets=[m])
    result = await scan_qualifying_markets(_make_price_state(), client, _config(), _NOW_ET)
    assert result == []


@pytest.mark.asyncio
async def test_scan_wrong_settlement_hour_excluded():
    # Market for the NEXT hour — excluded
    m = _make_market(close_time=_SETTLEMENT_UTC + timedelta(hours=1))
    client = _make_kalshi(markets=[m])
    result = await scan_qualifying_markets(_make_price_state(), client, _config(), _NOW_ET)
    assert result == []


@pytest.mark.asyncio
async def test_scan_both_sides_qualify_returns_both():
    m = _make_market(yes_ask=0.99, no_ask=0.99, floor_strike=86_000.0)
    client = _make_kalshi(markets=[m], orderbook={
        "yes_best_ask_cents": 99,
        "no_best_ask_cents": 99,
        "depth_yes_usd": 2000.0,
        "depth_no_usd": 2000.0,
    })
    result = await scan_qualifying_markets(_make_price_state(86_000), client, _config(), _NOW_ET)
    assert len(result) == 2
    sides = {t.side for t in result}
    assert sides == {"yes", "no"}


@pytest.mark.asyncio
async def test_scan_sorted_by_buffer_descending():
    # Two YES markets: big_buffer has 1000 buffer, small_buffer has 500
    small = _make_market(ticker="SMALL", floor_strike=85_500.0, yes_ask=0.99)
    big = _make_market(ticker="BIG", floor_strike=85_000.0, yes_ask=0.99)
    client = _make_kalshi(markets=[small, big])
    result = await scan_qualifying_markets(_make_price_state(86_000), client, _config(), _NOW_ET)
    assert len(result) == 2
    assert result[0].market.ticker == "BIG"    # buffer=1000 first
    assert result[1].market.ticker == "SMALL"  # buffer=500 second


@pytest.mark.asyncio
async def test_scan_depth_populated_from_orderbook():
    m = _make_market(yes_ask=0.99)
    ob = {
        "yes_best_ask_cents": 99,
        "no_best_ask_cents": None,
        "depth_yes_usd": 3000.0,
        "depth_no_usd": 0.0,
    }
    client = _make_kalshi(markets=[m], orderbook=ob)
    result = await scan_qualifying_markets(_make_price_state(), client, _config(), _NOW_ET)
    assert result[0].market.depth_yes_usd == pytest.approx(3000.0)


@pytest.mark.asyncio
async def test_scan_orderbook_called_once_per_unique_ticker():
    # Two candidates for the same market ticker (both sides qualify)
    m = _make_market(yes_ask=0.99, no_ask=0.99, floor_strike=86_000.0)
    client = _make_kalshi(markets=[m])
    await scan_qualifying_markets(_make_price_state(86_000), client, _config(), _NOW_ET)
    # get_orderbook should only be called once for the single ticker
    assert client.get_orderbook.call_count == 1


@pytest.mark.asyncio
async def test_scan_kalshi_failure_returns_empty():
    client = AsyncMock()
    client.get_active_btc_markets = AsyncMock(side_effect=RuntimeError("API down"))
    result = await scan_qualifying_markets(_make_price_state(), client, _config(), _NOW_ET)
    assert result == []


@pytest.mark.asyncio
async def test_scan_yes_at_lower_bound_qualifies():
    m = _make_market(yes_ask=0.98)
    client = _make_kalshi(markets=[m])
    result = await scan_qualifying_markets(_make_price_state(), client, _config(), _NOW_ET)
    assert len(result) == 1
    assert result[0].side == "yes"


@pytest.mark.asyncio
async def test_scan_multiple_markets_multiple_candidates():
    m1 = _make_market(ticker="KXBTCD-A", floor_strike=85_000.0, yes_ask=0.99)
    m2 = _make_market(ticker="KXBTCD-B", floor_strike=85_500.0, yes_ask=0.98)
    m3 = _make_market(ticker="KXBTCD-C", floor_strike=85_800.0, yes_ask=0.95)  # out of range
    client = _make_kalshi(markets=[m1, m2, m3])
    result = await scan_qualifying_markets(_make_price_state(86_000), client, _config(), _NOW_ET)
    tickers = [t.market.ticker for t in result]
    assert "KXBTCD-A" in tickers
    assert "KXBTCD-B" in tickers
    assert "KXBTCD-C" not in tickers
