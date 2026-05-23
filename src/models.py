
"""Shared dataclasses and protocols used across modules."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


# ── Market data ───────────────────────────────────────────────────────────────

@dataclass
class Market:
    ticker: str
    floor_strike: float       # strike threshold in dollars
    yes_ask: float | None     # cost to buy YES (above strike), in dollars [0, 1]
    no_ask: float | None      # cost to buy NO (below strike), in dollars [0, 1]
    close_time: datetime      # UTC settlement time

    # Orderbook depth at the ask price, in dollars of notional
    depth_yes_usd: float = 0.0
    depth_no_usd: float = 0.0


@dataclass
class QualifyingTrade:
    """A market + side pair that has passed the scanner's initial filter."""
    market: Market
    side: str  # "yes" | "no"

    @property
    def ask_price(self) -> float | None:
        return self.market.yes_ask if self.side == "yes" else self.market.no_ask

    @property
    def depth_usd(self) -> float:
        return self.market.depth_yes_usd if self.side == "yes" else self.market.depth_no_usd

    def buffer_vs_spot(self, spot: float) -> float:
        """Dollar distance between spot and strike for the evaluated side."""
        if self.side == "yes":
            return spot - self.market.floor_strike   # positive when above strike
        return self.market.floor_strike - spot        # positive when below strike


# ── Risk state ────────────────────────────────────────────────────────────────

@dataclass
class RiskState:
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    bankroll: float = 50_000.0
    daily_loss_limit_hit: bool = False
    weekly_loss_limit_hit: bool = False
    vol_circuit_breaker_until: datetime | None = None
    liquidation_circuit_breaker_until: datetime | None = None


# ── Position tracker protocol ─────────────────────────────────────────────────

@runtime_checkable
class PositionTrackerProtocol(Protocol):
    def lines_taken_this_hour(self, now_et: datetime) -> int: ...
    def capital_deployed_this_hour(self, now_et: datetime) -> float: ...


# ── Economic calendar ─────────────────────────────────────────────────────────

@dataclass
class EconEvent:
    name: str
    event_time_utc: datetime
    impact: str = "high"   # "high" | "medium" | "low"
    country: str = "US"


# ── Order / fill / line ───────────────────────────────────────────────────────

@dataclass
class Fill:
    order_id: str
    notional: float
    filled_at: datetime


@dataclass
class Line:
    strike: float
    side: str      # "yes" | "no"
    capacity_usd: float = 1000.0
    cumulative_filled: float = 0.0
    fills: list[Fill] = field(default_factory=list)

    def is_complete(self) -> bool:
        return self.cumulative_filled >= self.capacity_usd

    @property
    def remaining_capacity(self) -> float:
        return max(0.0, self.capacity_usd - self.cumulative_filled)


# ── Entry decision ────────────────────────────────────────────────────────────

@dataclass
class EntryDecision:
    should_trade: bool
    reason: str
    market_ticker: str | None = None
    side: str | None = None
    target_price_cents: int | None = None   # 98 or 99
    target_size_usd: float | None = None
    floor_strike: float | None = None       # BTC strike price in dollars
    market_close_time: datetime | None = None  # UTC settlement time
