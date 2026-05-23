#!/usr/bin/env python3
"""
Regenerate monthly/annual/hourly P&L breakdown from a backtest trade CSV.

Usage:
    python analysis/analyze_backtest.py <path_to_trades.csv> [--slippage 0.01]

The CSV must have columns:
    ticker, side, floor_strike, close_time, entry_time_utc, entry_spot,
    ask_price, fill_usd, quantity, settlement_price, won,
    gross_pnl_usd, fee_usd, net_pnl_usd, rv_60_at_entry, rv_24h_at_entry, buffer_usd
"""

import csv
import sys
import argparse
from collections import defaultdict
from datetime import datetime


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("csv_path", help="Path to trades CSV")
    p.add_argument("--slippage", type=float, default=0.01, help="Slippage fraction (default 0.01 = 1%%)")
    return p.parse_args()


def load_trades(csv_path):
    trades = []
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            trades.append({
                "close_time": datetime.fromisoformat(row["close_time"]),
                "entry_time":  datetime.fromisoformat(row["entry_time_utc"]),
                "entry_spot":  float(row["entry_spot"]),
                "ask_price":   float(row["ask_price"]),
                "fill_usd":    float(row["fill_usd"]),
                "quantity":    int(row["quantity"]),
                "won":         row["won"] == "True",
                "gross":       float(row["gross_pnl_usd"]),
                "fee":         float(row["fee_usd"]),
                "net":         float(row["net_pnl_usd"]),
                "buffer_usd":  float(row["buffer_usd"]),
            })
    return trades


def compute_slippage_adjusted(trades, slippage_rate):
    total = 0.0
    for t in trades:
        slip = t["fill_usd"] * slippage_rate
        total += t["net"] - slip
    return total


def print_monthly(trades, slippage_rate):
    monthly = defaultdict(lambda: {"trades": 0, "gross": 0.0, "fees": 0.0, "net": 0.0, "wins": 0, "losses": 0, "loss_usd": 0.0})
    for t in trades:
        mk = t["close_time"].strftime("%Y-%m")
        monthly[mk]["trades"] += 1
        monthly[mk]["gross"]  += t["gross"]
        monthly[mk]["fees"]   += t["fee"]
        monthly[mk]["net"]    += t["net"]
        if t["won"]:
            monthly[mk]["wins"] += 1
        else:
            monthly[mk]["losses"] += 1
            monthly[mk]["loss_usd"] += t["net"]

    print("=" * 90)
    print("MONTHLY P&L BREAKDOWN")
    print("=" * 90)
    print(f"{'Month':<10} {'Trades':>7} {'Win%':>6} {'Gross $':>12} {'Fees $':>10} {'Net $':>12} {'Losses':>7} {'Loss $':>10}")
    print("-" * 90)

    for month in sorted(monthly.keys()):
        d = monthly[month]
        wr = 100 * d["wins"] / d["trades"] if d["trades"] else 0
        loss_str = f"${d['loss_usd']:>9.2f}" if d["losses"] else "   —      "
        print(f"{month:<10} {d['trades']:>7,} {wr:>5.1f}% {d['gross']:>+12.2f} {d['fees']:>10.2f} {d['net']:>+12.2f} {d['losses']:>7} {loss_str}")

    print("-" * 90)
    total_net = sum(d["net"] for d in monthly.values())
    total_trades = sum(d["trades"] for d in monthly.values())
    slip_adj = compute_slippage_adjusted(trades, slippage_rate)
    print(f"{'TOTAL':<10} {total_trades:>7,}        {sum(d['gross'] for d in monthly.values()):>+12.2f} "
          f"{sum(d['fees'] for d in monthly.values()):>10.2f} {total_net:>+12.2f}")
    print(f"\n  Net P&L post-slippage ({slippage_rate*100:.1f}%): ${slip_adj:+,.2f}")

    pos = sum(1 for d in monthly.values() if d["net"] >= 0)
    neg = sum(1 for d in monthly.values() if d["net"] < 0)
    print(f"  Profitable months: {pos} / {pos + neg}")


def print_annual(trades):
    annual = defaultdict(lambda: {"trades": 0, "net": 0.0, "wins": 0, "losses": 0})
    for t in trades:
        yr = t["close_time"].strftime("%Y")
        annual[yr]["trades"] += 1
        annual[yr]["net"]    += t["net"]
        if t["won"]: annual[yr]["wins"] += 1
        else:        annual[yr]["losses"] += 1

    print("\n" + "=" * 60)
    print("ANNUAL SUMMARY")
    print("=" * 60)
    print(f"{'Year':<6} {'Trades':>7} {'Win%':>6} {'Net P&L':>14} {'Losses':>8}")
    print("-" * 50)
    for yr in sorted(annual.keys()):
        d = annual[yr]
        wr = 100 * d["wins"] / d["trades"] if d["trades"] else 0
        print(f"{yr:<6} {d['trades']:>7,} {wr:>5.1f}% {d['net']:>+14.2f} {d['losses']:>8}")


def print_hourly(trades):
    hourly = defaultdict(lambda: {"trades": 0, "net": 0.0, "wins": 0, "losses": 0})
    for t in trades:
        hk = t["entry_time"].strftime("%H")
        hourly[hk]["trades"] += 1
        hourly[hk]["net"]    += t["net"]
        if t["won"]: hourly[hk]["wins"] += 1
        else:        hourly[hk]["losses"] += 1

    print("\n" + "=" * 60)
    print("BY HOUR OF DAY (UTC entry time)")
    print("=" * 60)
    print(f"{'Hour':>6} {'Trades':>7} {'Win%':>6} {'Net P&L':>14} {'Losses':>8}")
    print("-" * 50)
    for hr in sorted(hourly.keys()):
        d = hourly[hr]
        wr = 100 * d["wins"] / d["trades"] if d["trades"] else 0
        print(f"{hr+':xx':>6} {d['trades']:>7,} {wr:>5.1f}% {d['net']:>+14.2f} {d['losses']:>8}")


def print_worst_losses(trades, n=20):
    losses = sorted([t for t in trades if not t["won"]], key=lambda x: x["net"])
    print(f"\n" + "=" * 70)
    print(f"TOP {n} WORST INDIVIDUAL LOSSES")
    print("=" * 70)
    for t in losses[:n]:
        dt = t["close_time"].strftime("%Y-%m-%d %H:%M")
        print(f"  {dt}  BTC=${t['entry_spot']:>10,.0f}  buffer=${t['buffer_usd']:>5.0f}  loss={t['net']:>+9.2f}")


def main():
    args = parse_args()
    trades = load_trades(args.csv_path)
    print(f"\nLoaded {len(trades):,} trades from {args.csv_path}\n")

    total_wins   = sum(1 for t in trades if t["won"])
    total_losses = sum(1 for t in trades if not t["won"])
    total_net    = sum(t["net"] for t in trades)
    wr           = 100 * total_wins / len(trades)
    slip_adj     = compute_slippage_adjusted(trades, args.slippage)

    print(f"  Win rate: {wr:.2f}%  ({total_wins:,} wins / {total_losses:,} losses)")
    print(f"  Net P&L (pre-slip):  ${total_net:+,.2f}")
    print(f"  Net P&L (post-slip): ${slip_adj:+,.2f}")

    print_monthly(trades, args.slippage)
    print_annual(trades)
    print_hourly(trades)
    print_worst_losses(trades)


if __name__ == "__main__":
    main()
