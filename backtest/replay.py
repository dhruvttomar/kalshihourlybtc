"""
Tick-replay backtest engine.

Replays historical BTC 1-minute bars through the EXACT same code paths as
live trading:
  - entry_logic.evaluate_entry()        (unchanged)
  - BlackoutCalendar.is_blocked()       (unchanged)
  - vol_calculator.realized_vol_*       (unchanged)
  - dynamic_buffer()                    (unchanged)

No simplifications. The only differences from live are:
  - PriceState is built from the historical deque (not a live WebSocket)
  - Kalshi fills are simulated at the ask price with no slippage model beyond
    the 1% slippage override (used in the sanity gate report)
  - Settlement outcome is determined from the closing price of the last bar
    before the top of the hour
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backtest.data_loader import KalshiSnapshot, MinuteBar
from src.blackout_calendar import BlackoutCalendar
from src.config import StrategyConfig
from src.entry_logic import evaluate_entry
from src.models import EconEvent, Market, QualifyingTrade, RiskState
from src.price_feed import PriceState
from src.vol_calculator import realized_vol_annualized

_UTC = timezone.utc
_ET = ZoneInfo("America/New_York")

_MINUTES_PER_YEAR = 525_600
_1H_MIN = 60
_24H_MIN = 1_440
_7D_MIN = 10_080


@dataclass
class TradeRecord:
    """One completed backtest trade (entry + settlement)."""
    ticker: str
    side: str
    floor_strike: float
    close_time: datetime        # UTC settlement time

    entry_time_utc: datetime
    entry_spot: float
    ask_price: float            # 0.98 or 0.99
    fill_usd: float             # capital deployed
    quantity: int               # contracts

    settlement_price: float     # BTC price used for settlement
    won: bool
    gross_pnl_usd: float        # fill_usd * (1 - ask_price) on win; -fill_usd on loss
    fee_usd: float              # Kalshi fee
    net_pnl_usd: float          # gross_pnl_usd - fee_usd

    rv_60_at_entry: float
    rv_24h_at_entry: float
    buffer_usd: float
    reject_count_before: int    # how many bars were evaluated and rejected before this fill


@dataclass
class RejectRecord:
    """A bar that was evaluated but rejected by evaluate_entry()."""
    ticker: str
    side: str
    floor_strike: float
    close_time: datetime
    eval_time_utc: datetime
    reason: str


@dataclass
class BacktestResult:
    trades: list[TradeRecord] = field(default_factory=list)
    rejects: list[RejectRecord] = field(default_factory=list)


def _kalshi_fee(price_cents: int, quantity: int) -> float:
    """Kalshi quadratic fee formula (fee_multiplier=1)."""
    p = price_cents / 100.0
    return math.ceil(7 * quantity * p * (1 - p)) / 100.0


class _StubTracker:
    """
    In-memory PositionTracker for backtest — no DB required.
    Tracks hourly lines and capital deployed per hour key.
    """

    def __init__(self, config: StrategyConfig) -> None:
        self._config = config
        self._lines: dict[str, int] = {}      # hour_key → count
        self._capital: dict[str, float] = {}  # hour_key → USD

    @staticmethod
    def _key(now_et: datetime) -> str:
        return now_et.strftime("%Y-%m-%dT%H")

    def lines_taken_this_hour(self, now_et: datetime) -> int:
        return self._lines.get(self._key(now_et), 0)

    def capital_deployed_this_hour(self, now_et: datetime) -> float:
        return self._capital.get(self._key(now_et), 0.0)

    def record_fill(self, now_et: datetime, fill_usd: float) -> None:
        k = self._key(now_et)
        self._lines[k] = self._lines.get(k, 0) + 1
        self._capital[k] = self._capital.get(k, 0.0) + fill_usd


def _build_price_state(
    price_deque: deque[tuple[float, float]],
    last_tick_ts: float,
    last_tick_price: float,
) -> PriceState:
    """Reconstruct a PriceState from a historical deque, mirroring price_feed.py logic."""
    now = last_tick_ts
    data = list(price_deque)

    prices_60 = [p for ts, p in data if ts >= now - _1H_MIN * 60]
    prices_24h = [p for ts, p in data if ts >= now - _24H_MIN * 60]

    rv_60 = realized_vol_annualized(prices_60) if len(prices_60) >= 2 else float("nan")
    rv_24h = realized_vol_annualized(prices_24h) if len(prices_24h) >= 2 else float("nan")
    rv_baseline = _compute_7d_median(data, now)

    return PriceState(
        spot=last_tick_price,
        timestamp=datetime.fromtimestamp(last_tick_ts, tz=_UTC),
        rv_60_annualized=rv_60,
        rv_24h_annualized=rv_24h,
        rv_baseline_7d_median=rv_baseline,
        is_stale=False,
    )


def _compute_7d_median(data: list[tuple[float, float]], now: float) -> float:
    if not data:
        return float("nan")
    seven_days_ago = now - 7 * 24 * 3600
    window = [(ts, p) for ts, p in data if ts >= seven_days_ago]
    if len(window) < _24H_MIN * 2:
        return float("nan")

    bin_vols: list[float] = []
    bin_size_s = _24H_MIN * 60.0
    bin_start = window[0][0]
    bin_prices: list[float] = []

    for ts, price in window:
        if ts < bin_start + bin_size_s:
            bin_prices.append(price)
        else:
            if len(bin_prices) >= 2:
                v = realized_vol_annualized(bin_prices)
                if not math.isnan(v):
                    bin_vols.append(v)
            bin_prices = [price]
            bin_start = ts

    if len(bin_prices) >= 2:
        v = realized_vol_annualized(bin_prices)
        if not math.isnan(v):
            bin_vols.append(v)

    if len(bin_vols) < 2:
        return float("nan")

    import numpy as np
    return float(np.median(bin_vols))


def run_backtest(
    bars: list[MinuteBar],
    snapshots: dict[tuple[int, float], list[KalshiSnapshot]],
    blackout: BlackoutCalendar,
    config: StrategyConfig,
    slippage_pct: float = 0.0,
    fill_usd_per_trade: float = 1000.0,
    prev_hour_range_limit_usd: float = 0.0,
) -> BacktestResult:
    """
    Replay historical bars through the live entry logic.

    Parameters
    ----------
    bars:
        Chronological 1-minute bars (from data_loader.fetch_binance_1min).
    snapshots:
        Keyed by (minute_bucket, floor_strike) from data_loader.synthesize_kalshi_snapshots.
    blackout:
        Pre-constructed BlackoutCalendar (no economic events needed for structural rules).
    config:
        Strategy config — identical object used by live trading.
    slippage_pct:
        Additional slippage applied to simulated fills (e.g. 0.01 = 1%).
    fill_usd_per_trade:
        Capital assumed per filled trade (default $1,000 per spec).
    prev_hour_range_limit_usd:
        Skip trading in any hour where the prior settlement hour's BTC high-low
        range exceeded this value. 0.0 = disabled. 750.0 recommended.
    """
    result = BacktestResult()
    tracker = _StubTracker(config)
    risk_state = RiskState()

    # Rolling deque for vol computation — same size as live (7 days)
    price_deque: deque[tuple[float, float]] = deque(maxlen=_7D_MIN)
    current_minute: int = 0

    # Track pending (open) trades awaiting settlement: hour_ts → list[TradeRecord]
    pending: dict[int, list[TradeRecord]] = {}

    # Previous-hour range tracking for the volatility filter
    _curr_hr_ts: int = 0
    _curr_hr_high: float = 0.0
    _curr_hr_low: float = float("inf")
    _prev_hr_range: float = 0.0   # 0.0 until we have a full prior hour

    # Pre-index snapshots by minute_bucket for O(1) lookup (not O(n) per bar)
    snap_index: dict[int, list[KalshiSnapshot]] = {}
    for (mb, _strike), snaps in snapshots.items():
        snap_index.setdefault(mb, []).extend(snaps)

    for bar in bars:
        minute_bucket = int(bar.ts // 60)
        hour_ts = int(bar.ts // 3600) * 3600

        # ── Track previous-hour high/low range ────────────────────────────────
        if hour_ts != _curr_hr_ts:
            if _curr_hr_ts > 0 and _curr_hr_low < float("inf"):
                _prev_hr_range = _curr_hr_high - _curr_hr_low
            _curr_hr_ts = hour_ts
            _curr_hr_high = bar.high
            _curr_hr_low = bar.low
        else:
            if bar.high > _curr_hr_high:
                _curr_hr_high = bar.high
            if bar.low < _curr_hr_low:
                _curr_hr_low = bar.low

        # ── Advance the price deque (1-min close) ─────────────────────────────
        if minute_bucket != current_minute:
            if current_minute > 0:
                price_deque.append((current_minute * 60.0, bar.open))
            current_minute = minute_bucket

        # ── Settle any markets whose close_time has passed ────────────────────
        bar_ts = bar.ts
        for h_ts in list(pending.keys()):
            if bar_ts >= h_ts + 3600:
                settlement_price = bar.open  # first bar after close = settlement proxy
                for trade in pending[h_ts]:
                    _settle_trade(trade, settlement_price, result)
                del pending[h_ts]

        # ── Build PriceState ──────────────────────────────────────────────────
        price_state = _build_price_state(price_deque, bar.ts, bar.close)
        if price_state.is_stale:
            continue

        # ── Collect qualifying snapshots for this minute ──────────────────────
        qualifying: list[tuple[KalshiSnapshot, QualifyingTrade]] = []
        for snap in snap_index.get(minute_bucket, []):
            market = Market(
                ticker=snap.ticker,
                floor_strike=snap.floor_strike,
                yes_ask=snap.yes_ask,
                no_ask=snap.no_ask,
                close_time=snap.close_time,
                depth_yes_usd=snap.depth_yes_usd,
                depth_no_usd=snap.depth_no_usd,
            )
            for side in ("yes", "no"):
                qt = QualifyingTrade(market=market, side=side)
                qualifying.append((snap, qt))

        # ── Evaluate each qualifying trade ────────────────────────────────────
        now_et = datetime.fromtimestamp(bar.ts, tz=_ET)
        now_utc = datetime.fromtimestamp(bar.ts, tz=_UTC)

        # Range filter: skip this hour if prior hour was too volatile
        if (
            prev_hour_range_limit_usd > 0.0
            and _prev_hr_range > prev_hour_range_limit_usd
        ):
            for snap, qt in qualifying:
                result.rejects.append(RejectRecord(
                    ticker=snap.ticker,
                    side=qt.side,
                    floor_strike=snap.floor_strike,
                    close_time=snap.close_time,
                    eval_time_utc=now_utc,
                    reason=f"prev_hr_range={_prev_hr_range:.0f}>{prev_hour_range_limit_usd:.0f}",
                ))
            continue

        for snap, qt in qualifying:
            if tracker.lines_taken_this_hour(now_et) >= config.max_lines_per_hour:
                break

            decision = evaluate_entry(
                trade=qt,
                price_state=price_state,
                blackout=blackout,
                risk_state=risk_state,
                position_tracker=tracker,
                now_et=now_et,
                config=config,
            )

            if not decision.should_trade:
                result.rejects.append(RejectRecord(
                    ticker=snap.ticker,
                    side=qt.side,
                    floor_strike=snap.floor_strike,
                    close_time=snap.close_time,
                    eval_time_utc=now_utc,
                    reason=decision.reason,
                ))
                continue

            # ── Simulate fill ─────────────────────────────────────────────────
            ask = qt.ask_price or 0.99
            effective_ask = min(1.0, ask * (1 + slippage_pct))
            price_cents = round(effective_ask * 100)
            quantity = max(1, int(fill_usd_per_trade / effective_ask))
            actual_fill = quantity * effective_ask
            fee = _kalshi_fee(price_cents, quantity)

            trade = TradeRecord(
                ticker=snap.ticker,
                side=qt.side,
                floor_strike=snap.floor_strike,
                close_time=snap.close_time,
                entry_time_utc=now_utc,
                entry_spot=bar.close,
                ask_price=effective_ask,
                fill_usd=actual_fill,
                quantity=quantity,
                settlement_price=float("nan"),   # filled in at settlement
                won=False,
                gross_pnl_usd=float("nan"),
                fee_usd=fee,
                net_pnl_usd=float("nan"),
                rv_60_at_entry=price_state.rv_60_annualized,
                rv_24h_at_entry=price_state.rv_24h_annualized,
                buffer_usd=qt.buffer_vs_spot(bar.close),
                reject_count_before=0,  # will not track per-trade rejects here
            )

            # Register with pending settlement
            pending.setdefault(hour_ts, []).append(trade)
            tracker.record_fill(now_et, actual_fill)

    # ── Settle any remaining open positions at end of data ─────────────────────
    if bars:
        last_price = bars[-1].close
        for trades_list in pending.values():
            for trade in trades_list:
                _settle_trade(trade, last_price, result)

    return result


def _get_minute_snaps(
    snapshots: dict[tuple[int, float], list[KalshiSnapshot]],
    minute_bucket: int,
) -> list[tuple[float, list[KalshiSnapshot]]]:
    """Return all (strike, snaps) pairs that match the given minute bucket."""
    result = []
    for (mb, strike), snaps in snapshots.items():
        if mb == minute_bucket:
            result.append((strike, snaps))
    return result


def _settle_trade(trade: TradeRecord, settlement_price: float, result: BacktestResult) -> None:
    """Determine settlement outcome and compute P&L."""
    trade.settlement_price = settlement_price

    if trade.side == "yes":
        trade.won = settlement_price > trade.floor_strike
    else:
        trade.won = settlement_price <= trade.floor_strike

    if trade.won:
        # Collect (1 - ask) per dollar invested: profit = fill * (1/ask - 1) ≈ fill * 0.01/0.99
        payout_per_contract = 1.0  # Kalshi pays $1 per contract on win
        cost_per_contract = trade.ask_price
        trade.gross_pnl_usd = trade.quantity * (payout_per_contract - cost_per_contract)
    else:
        trade.gross_pnl_usd = -trade.fill_usd

    trade.net_pnl_usd = trade.gross_pnl_usd - trade.fee_usd
    result.trades.append(trade)
