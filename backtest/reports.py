"""
Backtest reporting: aggregate stats, bucketed P&L analysis, and sanity gates.

All functions are pure — they take a BacktestResult and return structured dicts
or print formatted summaries. No I/O except for optional CSV export.
"""
from __future__ import annotations

import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from backtest.replay import BacktestResult, TradeRecord

_UTC = timezone.utc
_ET = ZoneInfo("America/New_York")


@dataclass
class AggregateStats:
    total_trades: int
    wins: int
    losses: int
    win_rate: float             # 0–1
    gross_pnl_usd: float
    total_fees_usd: float
    net_pnl_usd: float
    max_drawdown_usd: float
    sharpe_ratio: float         # annualized, assumes daily returns
    avg_net_pnl_per_trade: float
    avg_fill_usd: float
    total_capital_deployed: float


@dataclass
class SanityGateResult:
    passed: bool
    net_pnl_usd: float
    net_pnl_with_slippage_usd: float
    max_drawdown_usd: float
    win_rate: float
    months_covered: float
    reasons_failed: list[str]


def compute_aggregate_stats(result: BacktestResult) -> AggregateStats:
    """Compute overall performance metrics from a BacktestResult."""
    trades = result.trades
    if not trades:
        return AggregateStats(
            total_trades=0, wins=0, losses=0, win_rate=0.0,
            gross_pnl_usd=0.0, total_fees_usd=0.0, net_pnl_usd=0.0,
            max_drawdown_usd=0.0, sharpe_ratio=float("nan"),
            avg_net_pnl_per_trade=float("nan"), avg_fill_usd=0.0,
            total_capital_deployed=0.0,
        )

    wins = sum(1 for t in trades if t.won)
    losses = len(trades) - wins
    gross_pnl = sum(t.gross_pnl_usd for t in trades)
    total_fees = sum(t.fee_usd for t in trades)
    net_pnl = sum(t.net_pnl_usd for t in trades)
    total_capital = sum(t.fill_usd for t in trades)

    max_dd = _max_drawdown(trades)
    sharpe = _annualized_sharpe(trades)

    return AggregateStats(
        total_trades=len(trades),
        wins=wins,
        losses=losses,
        win_rate=wins / len(trades),
        gross_pnl_usd=gross_pnl,
        total_fees_usd=total_fees,
        net_pnl_usd=net_pnl,
        max_drawdown_usd=max_dd,
        sharpe_ratio=sharpe,
        avg_net_pnl_per_trade=net_pnl / len(trades),
        avg_fill_usd=total_capital / len(trades),
        total_capital_deployed=total_capital,
    )


def bucket_by_hour_of_day(result: BacktestResult) -> dict[int, dict]:
    """Net P&L and trade count grouped by hour of day (ET)."""
    buckets: dict[int, list[TradeRecord]] = defaultdict(list)
    for t in result.trades:
        hour_et = t.entry_time_utc.astimezone(_ET).hour
        buckets[hour_et].append(t)
    return _bucket_summary(buckets)


def bucket_by_day_of_week(result: BacktestResult) -> dict[str, dict]:
    """Net P&L and trade count grouped by day of week (ET, Monday=0)."""
    _days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    buckets: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in result.trades:
        dow = _days[t.entry_time_utc.astimezone(_ET).weekday()]
        buckets[dow].append(t)
    return _bucket_summary(buckets)


def bucket_by_vol_regime(
    result: BacktestResult,
    low_cutoff: float = 0.30,
    high_cutoff: float = 0.60,
) -> dict[str, dict]:
    """Net P&L grouped by RV_60 regime at entry (low/medium/high)."""
    buckets: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in result.trades:
        rv = t.rv_60_at_entry
        if math.isnan(rv):
            label = "unknown"
        elif rv < low_cutoff:
            label = "low"
        elif rv < high_cutoff:
            label = "medium"
        else:
            label = "high"
        buckets[label].append(t)
    return _bucket_summary(buckets)


def check_sanity_gates(
    result: BacktestResult,
    slippage_pct: float = 0.01,
    min_months: float = 6.0,
    max_drawdown_limit_usd: float = 5000.0,
) -> SanityGateResult:
    """
    Verify backtest results meet the pre-deployment sanity gates from spec Section 9:
      1. At least 6 months of data
      2. Net P&L (after fees + 1% slippage) must be positive
      3. Max drawdown within tolerance
    """
    failures: list[str] = []
    trades = result.trades

    if not trades:
        return SanityGateResult(
            passed=False, net_pnl_usd=0.0, net_pnl_with_slippage_usd=0.0,
            max_drawdown_usd=0.0, win_rate=0.0, months_covered=0.0,
            reasons_failed=["No trades in backtest"],
        )

    # Months covered
    start_ts = min(t.entry_time_utc.timestamp() for t in trades)
    end_ts = max(t.entry_time_utc.timestamp() for t in trades)
    months = (end_ts - start_ts) / (30.44 * 86400)

    if months < min_months:
        failures.append(f"Insufficient history: {months:.1f} months (need {min_months}+)")

    # P&L with slippage
    net_pnl = sum(t.net_pnl_usd for t in trades)
    slippage_cost = sum(t.fill_usd * slippage_pct for t in trades)
    net_pnl_slippage = net_pnl - slippage_cost

    if net_pnl_slippage <= 0:
        failures.append(
            f"Net P&L after fees+slippage is negative: ${net_pnl_slippage:.2f}"
        )

    max_dd = _max_drawdown(trades)
    if max_dd > max_drawdown_limit_usd:
        failures.append(
            f"Max drawdown ${max_dd:.2f} exceeds limit ${max_drawdown_limit_usd:.2f}"
        )

    wins = sum(1 for t in trades if t.won)
    win_rate = wins / len(trades)

    return SanityGateResult(
        passed=len(failures) == 0,
        net_pnl_usd=net_pnl,
        net_pnl_with_slippage_usd=net_pnl_slippage,
        max_drawdown_usd=max_dd,
        win_rate=win_rate,
        months_covered=months,
        reasons_failed=failures,
    )


def print_report(result: BacktestResult, slippage_pct: float = 0.01) -> None:
    """Print a formatted backtest report to stdout."""
    stats = compute_aggregate_stats(result)
    gate = check_sanity_gates(result, slippage_pct=slippage_pct)

    print("=" * 60)
    print("BACKTEST REPORT")
    print("=" * 60)
    print(f"  Trades:              {stats.total_trades}")
    print(f"  Win rate:            {stats.win_rate:.1%}")
    print(f"  Gross P&L:           ${stats.gross_pnl_usd:,.2f}")
    print(f"  Total fees:          ${stats.total_fees_usd:,.2f}")
    print(f"  Net P&L:             ${stats.net_pnl_usd:,.2f}")
    print(f"  Net P&L (+slippage): ${gate.net_pnl_with_slippage_usd:,.2f}")
    print(f"  Max drawdown:        ${stats.max_drawdown_usd:,.2f}")
    print(f"  Sharpe ratio:        {stats.sharpe_ratio:.2f}")
    print(f"  Months covered:      {gate.months_covered:.1f}")
    print()

    print("── By day of week ──────────────────────────────────────")
    for day, s in sorted(bucket_by_day_of_week(result).items()):
        print(f"  {day:<12} trades={s['count']:>4}  net=${s['net_pnl_usd']:>8,.2f}"
              f"  win_rate={s['win_rate']:.1%}")
    print()

    print("── By hour of day (ET) ─────────────────────────────────")
    for hour, s in sorted(bucket_by_hour_of_day(result).items()):
        print(f"  {hour:02d}:xx       trades={s['count']:>4}  net=${s['net_pnl_usd']:>8,.2f}"
              f"  win_rate={s['win_rate']:.1%}")
    print()

    print("── By vol regime ───────────────────────────────────────")
    for regime, s in sorted(bucket_by_vol_regime(result).items()):
        print(f"  {regime:<10}   trades={s['count']:>4}  net=${s['net_pnl_usd']:>8,.2f}"
              f"  win_rate={s['win_rate']:.1%}")
    print()

    print("── Sanity gates ────────────────────────────────────────")
    status = "PASS" if gate.passed else "FAIL"
    print(f"  Status: {status}")
    for reason in gate.reasons_failed:
        print(f"  FAIL: {reason}")
    if gate.passed:
        print("  All gates passed — bot cleared for paper trading phase")
    print("=" * 60)


def export_trades_csv(result: BacktestResult, path: str | Path) -> None:
    """Write per-trade log to CSV for external analysis."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "ticker", "side", "floor_strike", "close_time", "entry_time_utc",
        "entry_spot", "ask_price", "fill_usd", "quantity",
        "settlement_price", "won", "gross_pnl_usd", "fee_usd", "net_pnl_usd",
        "rv_60_at_entry", "rv_24h_at_entry", "buffer_usd",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for t in result.trades:
            writer.writerow({
                "ticker": t.ticker,
                "side": t.side,
                "floor_strike": t.floor_strike,
                "close_time": t.close_time.isoformat(),
                "entry_time_utc": t.entry_time_utc.isoformat(),
                "entry_spot": t.entry_spot,
                "ask_price": t.ask_price,
                "fill_usd": t.fill_usd,
                "quantity": t.quantity,
                "settlement_price": t.settlement_price,
                "won": t.won,
                "gross_pnl_usd": t.gross_pnl_usd,
                "fee_usd": t.fee_usd,
                "net_pnl_usd": t.net_pnl_usd,
                "rv_60_at_entry": t.rv_60_at_entry,
                "rv_24h_at_entry": t.rv_24h_at_entry,
                "buffer_usd": t.buffer_usd,
            })


# ── Internal helpers ──────────────────────────────────────────────────────────

def _bucket_summary(buckets: dict) -> dict:
    out = {}
    for key, trades in buckets.items():
        wins = sum(1 for t in trades if t.won)
        net = sum(t.net_pnl_usd for t in trades)
        out[key] = {
            "count": len(trades),
            "wins": wins,
            "win_rate": wins / len(trades) if trades else 0.0,
            "net_pnl_usd": net,
        }
    return out


def _max_drawdown(trades: list[TradeRecord]) -> float:
    """Peak-to-trough max drawdown in dollars on cumulative net P&L."""
    if not trades:
        return 0.0
    sorted_trades = sorted(trades, key=lambda t: t.entry_time_utc)
    peak = 0.0
    cumulative = 0.0
    max_dd = 0.0
    for t in sorted_trades:
        cumulative += t.net_pnl_usd
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _annualized_sharpe(trades: list[TradeRecord]) -> float:
    """
    Annualized Sharpe ratio from daily net P&L series.
    Uses trading days in the period; returns nan if fewer than 5 unique days.
    """
    if not trades:
        return float("nan")

    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        day_key = t.entry_time_utc.astimezone(_ET).strftime("%Y-%m-%d")
        daily[day_key] += t.net_pnl_usd

    returns = list(daily.values())
    if len(returns) < 5:
        return float("nan")

    arr = np.array(returns)
    mu = float(np.mean(arr))
    std = float(np.std(arr, ddof=1))
    if std == 0:
        return float("nan")

    # Annualize assuming ~252 trading days
    return (mu / std) * math.sqrt(252)
