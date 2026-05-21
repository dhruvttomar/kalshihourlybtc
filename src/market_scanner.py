"""
Market scanner: finds qualifying Kalshi BTC contracts for the current trading hour.

Scan steps:
  1. Fetch all open BTC markets from Kalshi
  2. Filter to markets settling at the top of the current ET hour
  3. Check YES side: yes_ask in [price_min, price_max]
  4. Check NO side: no_ask in [price_min, price_max]
  5. For each qualifying candidate, fetch orderbook depth
  6. Return list sorted by buffer-vs-spot descending (best-buffered first)

Buffer for YES: spot - floor_strike (we're above the strike)
Buffer for NO:  floor_strike - spot (we're below the strike)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.config import StrategyConfig
from src.kalshi_client import KalshiClient
from src.models import Market, QualifyingTrade
from src.price_feed import PriceState

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc
log = logging.getLogger(__name__)

# Tolerance when matching a market's close_time to the expected settlement time.
_SETTLEMENT_MATCH_TOLERANCE_S = 60


def _settlement_time_utc(now_et: datetime) -> datetime:
    """Top of the next ET hour expressed in UTC."""
    next_hour_et = now_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return next_hour_et.astimezone(_UTC)


def _markets_for_current_hour(
    markets: list[Market],
    settlement_utc: datetime,
) -> list[Market]:
    """Return markets whose close_time matches the expected settlement within tolerance."""
    result = []
    for m in markets:
        ct = m.close_time
        if ct.tzinfo is None:
            ct = ct.replace(tzinfo=_UTC)
        if abs((ct - settlement_utc).total_seconds()) < _SETTLEMENT_MATCH_TOLERANCE_S:
            result.append(m)
    return result


async def scan_qualifying_markets(
    price_state: PriceState,
    kalshi: KalshiClient,
    config: StrategyConfig,
    now_et: datetime,
) -> list[QualifyingTrade]:
    """
    Return QualifyingTrade objects for every qualifying Kalshi BTC opportunity
    in the current hour, sorted by buffer descending.

    A market qualifies for a side when:
      - close_time matches the current hour settlement (within 60s)
      - ask price for that side is in [config.yes_price_min, config.yes_price_max]

    Depth is fetched separately for each unique qualifying ticker.
    """
    settlement_utc = _settlement_time_utc(now_et)
    spot = price_state.spot

    try:
        all_markets = await kalshi.get_active_btc_markets()
    except Exception as exc:
        log.error("Failed to fetch BTC markets from Kalshi: %s", exc)
        return []

    current_hour_markets = _markets_for_current_hour(all_markets, settlement_utc)
    if not current_hour_markets:
        log.info(
            "No BTC markets found for settlement at %s UTC",
            settlement_utc.strftime("%H:%M"),
        )
        return []

    # ── Price filter: collect candidate (market, side) pairs ──────────────────
    candidates: list[QualifyingTrade] = []
    lo, hi = config.yes_price_min, config.yes_price_max

    for market in current_hour_markets:
        if market.yes_ask is not None and lo <= market.yes_ask <= hi:
            candidates.append(QualifyingTrade(market=market, side="yes"))
        if market.no_ask is not None and lo <= market.no_ask <= hi:
            candidates.append(QualifyingTrade(market=market, side="no"))

    if not candidates:
        log.debug(
            "No markets with ask in [%.2f, %.2f] for %s UTC settlement (spot=%.0f)",
            lo, hi, settlement_utc.strftime("%H:%M"), spot,
        )
        return []

    # ── Fetch orderbook depth for each unique qualifying ticker ───────────────
    unique_tickers = {t.market.ticker for t in candidates}
    depth_by_ticker: dict[str, dict] = {}
    for ticker in unique_tickers:
        depth_by_ticker[ticker] = await kalshi.get_orderbook(ticker)

    # ── Build final list with depth populated ─────────────────────────────────
    result: list[QualifyingTrade] = []
    for trade in candidates:
        ob = depth_by_ticker.get(trade.market.ticker, {})
        m = trade.market
        enriched = Market(
            ticker=m.ticker,
            floor_strike=m.floor_strike,
            yes_ask=m.yes_ask,
            no_ask=m.no_ask,
            close_time=m.close_time,
            depth_yes_usd=ob.get("depth_yes_usd", 0.0),
            depth_no_usd=ob.get("depth_no_usd", 0.0),
        )
        result.append(QualifyingTrade(market=enriched, side=trade.side))

    result.sort(key=lambda t: t.buffer_vs_spot(spot), reverse=True)

    log.info(
        "Scanner: %d qualifying trade(s) for %s UTC settlement "
        "(spot=%.0f, %d markets checked)",
        len(result),
        settlement_utc.strftime("%H:%M"),
        spot,
        len(current_hour_markets),
    )
    return result
