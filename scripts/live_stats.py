#!/usr/bin/env python3
"""
Real-time stats viewer for the live B-97 run.

Reads directly from trades.db — safe to run while the bot is live.
Produces the same breakdown as analyze_backtest.py but from settled lines.

Usage:
    python scripts/live_stats.py
    python scripts/live_stats.py --db data/trades.db
    python scripts/live_stats.py --days 7    # last 7 days only
    python scripts/live_stats.py --open      # show open (unsettled) positions
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--db",   default="data/trades.db", help="Path to trades.db")
    p.add_argument("--days", type=int, default=0,       help="Restrict to last N days (0=all)")
    p.add_argument("--open", action="store_true",       help="Show open (unsettled) positions")
    return p.parse_args()


def conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _since_iso(days: int) -> str | None:
    if days <= 0:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat()


def print_header(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def overall_stats(c: sqlite3.Connection, since: str | None) -> None:
    where = "WHERE settled_at IS NOT NULL"
    params: list = []
    if since:
        where += " AND settled_at >= ?"
        params.append(since)

    rows = c.execute(f"SELECT outcome, final_pnl_usd FROM lines {where}", params).fetchall()
    if not rows:
        print("  No settled trades yet.")
        return

    wins   = [r for r in rows if r["outcome"] == "win"]
    losses = [r for r in rows if r["outcome"] == "loss"]
    total  = len(rows)
    net    = sum(r["final_pnl_usd"] for r in rows)
    gross  = sum(r["final_pnl_usd"] for r in wins)
    loss_sum = sum(r["final_pnl_usd"] for r in losses)

    print_header("OVERALL STATS")
    print(f"  Settled trades : {total:,}")
    print(f"  Wins           : {len(wins):,}  ({100*len(wins)/total:.1f}%)")
    print(f"  Losses         : {len(losses):,}  ({100*len(losses)/total:.1f}%)")
    print(f"  Net P&L        : ${net:+,.2f}")
    print(f"  Gross wins     : ${gross:+,.2f}")
    print(f"  Total losses   : ${loss_sum:+,.2f}")
    if total > 0:
        avg = net / total
        print(f"  Avg per trade  : ${avg:+.4f}")

    # Running max drawdown from equity curve
    running = 0.0
    peak    = 0.0
    max_dd  = 0.0
    settled_sorted = c.execute(
        f"SELECT final_pnl_usd FROM lines {where} ORDER BY settled_at", params
    ).fetchall()
    for r in settled_sorted:
        running += r["final_pnl_usd"]
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    print(f"  Max drawdown   : ${max_dd:,.2f}")


def daily_breakdown(c: sqlite3.Connection, since: str | None) -> None:
    where = "WHERE settled_at IS NOT NULL"
    params: list = []
    if since:
        where += " AND settled_at >= ?"
        params.append(since)

    rows = c.execute(
        f"SELECT settled_at, outcome, final_pnl_usd FROM lines {where} ORDER BY settled_at",
        params,
    ).fetchall()
    if not rows:
        return

    daily: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "net": 0.0})
    for r in rows:
        day = r["settled_at"][:10]
        daily[day]["trades"] += 1
        daily[day]["net"]    += r["final_pnl_usd"]
        if r["outcome"] == "win":
            daily[day]["wins"] += 1

    print_header("DAILY BREAKDOWN")
    print(f"  {'Date':<12} {'Trades':>7} {'Win%':>6} {'Net P&L':>12}")
    print("  " + "-" * 44)
    for day in sorted(daily.keys()):
        d = daily[day]
        wr = 100 * d["wins"] / d["trades"] if d["trades"] else 0
        print(f"  {day:<12} {d['trades']:>7} {wr:>5.1f}% {d['net']:>+12.2f}")


def hourly_breakdown(c: sqlite3.Connection, since: str | None) -> None:
    where = "WHERE l.settled_at IS NOT NULL"
    params: list = []
    if since:
        where += " AND l.settled_at >= ?"
        params.append(since)

    rows = c.execute(
        f"""
        SELECT o.minutes_into_hour, l.outcome, l.final_pnl_usd
        FROM lines l
        JOIN orders o ON o.line_id = l.id
        {where}
        GROUP BY l.id
        """,
        params,
    ).fetchall()

    hourly: dict[int, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "net": 0.0})
    for r in rows:
        # minutes_into_hour tells us the ET hour the trade was placed
        h = int(r["minutes_into_hour"] or 0)
        hourly[h]["trades"] += 1
        hourly[h]["net"]    += r["final_pnl_usd"]
        if r["outcome"] == "win":
            hourly[h]["wins"] += 1

    if not hourly:
        return

    print_header("BY MINUTE-INTO-HOUR AT ENTRY")
    print(f"  {'Min':>4} {'Trades':>7} {'Win%':>6} {'Net P&L':>12}")
    print("  " + "-" * 36)
    for minute in sorted(hourly.keys()):
        d = hourly[minute]
        wr = 100 * d["wins"] / d["trades"] if d["trades"] else 0
        print(f"  {minute:>4} {d['trades']:>7} {wr:>5.1f}% {d['net']:>+12.2f}")


def open_positions(c: sqlite3.Connection) -> None:
    rows = c.execute(
        "SELECT * FROM lines WHERE settled_at IS NULL ORDER BY rowid"
    ).fetchall()

    print_header("OPEN POSITIONS (unsettled)")
    if not rows:
        print("  No open positions.")
        return

    print(f"  {'Ticker':<40} {'Side':>4} {'Strike':>8} {'Fill $':>8} {'Closes':>25}")
    print("  " + "-" * 92)
    for r in rows:
        ticker = r["market_ticker"] or "?"
        close  = (r["close_time_utc"] or "")[:16]
        print(
            f"  {ticker:<40} {r['side']:>4} {r['strike_price']:>8} "
            f"{r['cumulative_filled_usd']:>8.2f} {close:>25}"
        )


def recent_losses(c: sqlite3.Connection, since: str | None, n: int = 10) -> None:
    where = "WHERE outcome = 'loss' AND settled_at IS NOT NULL"
    params: list = []
    if since:
        where += " AND settled_at >= ?"
        params.append(since)

    rows = c.execute(
        f"SELECT * FROM lines {where} ORDER BY settled_at DESC LIMIT ?",
        params + [n],
    ).fetchall()

    if not rows:
        return

    print_header(f"LAST {n} LOSSES")
    print(f"  {'Settled':>20} {'Ticker':<40} {'P&L':>9}")
    print("  " + "-" * 74)
    for r in rows:
        print(f"  {(r['settled_at'] or '')[:19]:>20} {r['market_ticker']:<40} {r['final_pnl_usd']:>+9.2f}")


def main() -> None:
    args = parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        print("Has the bot placed any orders yet?")
        return

    c = conn(str(db_path))
    since = _since_iso(args.days)
    label = f"last {args.days} days" if args.days else "all time"
    print(f"\nLive B-97 Stats — {label}  ({db_path})")

    overall_stats(c, since)
    daily_breakdown(c, since)
    recent_losses(c, since)

    if args.open:
        open_positions(c)

    print()


if __name__ == "__main__":
    main()
