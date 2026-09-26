#!/usr/bin/env python3
"""
Calculate Crypto Grid Bot Validation Metrics
Real-time tracking of crypto bot performance over 50+ trades per symbol
"""
import csv
import sys
from datetime import datetime
from collections import defaultdict

def load_trades(csv_file):
    """Load trades from CSV tracker"""
    trades = []
    try:
        with open(csv_file, 'r') as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                # Skip empty rows
                if not row.get('pnl') or row['pnl'].strip() == '':
                    continue

                try:
                    trade = {
                        'number': i,
                        'date': row.get('date', ''),
                        'product_id': row.get('product_id', ''),
                        'entry_price': float(row.get('entry_price', 0)),
                        'exit_price': float(row.get('exit_price', 0)),
                        'qty': float(row.get('quantity', 0)),
                        'pnl': float(row.get('pnl', 0)),
                        'status': 'win' if float(row.get('pnl', 0)) > 0 else 'loss'
                    }
                    trades.append(trade)
                except (ValueError, KeyError):
                    pass
    except FileNotFoundError:
        print(f"Error: {csv_file} not found")
        return []

    return trades

def calculate_metrics(trades):
    """Calculate all validation metrics"""
    if not trades:
        return None

    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]

    total_pnl = sum(t['pnl'] for t in trades)
    total_wins = sum(t['pnl'] for t in wins) if wins else 0
    total_losses = sum(t['pnl'] for t in losses) if losses else 0

    metrics = {
        'total_trades': len(trades),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': (len(wins) / len(trades) * 100) if trades else 0,
        'total_pnl': total_pnl,
        'total_wins': total_wins,
        'total_losses': total_losses,
        'avg_winner': (total_wins / len(wins)) if wins else 0,
        'avg_loser': (total_losses / len(losses)) if losses else 0,
        'profit_factor': abs(total_wins / total_losses) if total_losses != 0 else float('inf') if total_wins > 0 else 0,
        'expectancy': ((len(wins) / len(trades) * (total_wins / len(wins))) if wins else 0) - ((len(losses) / len(trades) * abs(total_losses / len(losses))) if losses else 0)
    }

    return metrics

def symbol_breakdown(trades):
    """Performance by symbol/product"""
    by_symbol = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0, 'trades': 0})

    for t in trades:
        symbol = t['product_id']
        if t['pnl'] > 0:
            by_symbol[symbol]['wins'] += 1
        else:
            by_symbol[symbol]['losses'] += 1
        by_symbol[symbol]['pnl'] += t['pnl']
        by_symbol[symbol]['trades'] += 1

    return by_symbol

def print_metrics(metrics, trades):
    """Print formatted metrics report"""
    if not metrics:
        print("\n⏳ No trades logged yet. Fill in the CRYPTO_VALIDATION_TRACKER.csv file.")
        return

    print("\n" + "=" * 110)
    print("CRYPTO GRID BOT VALIDATION METRICS (50+ Trades Per Symbol)")
    print("=" * 110)

    # Main metrics
    print(f"\nProgress: {metrics['total_trades']} total trades")
    print(f"\n{'Metric':<30} {'Value':<20} {'Target/Status':<25}")
    print("-" * 110)
    print(f"{'Win Rate':<30} {metrics['win_rate']:.1f}%{'':<15} Target: ≥50%")
    print(f"{'Profit Factor':<30} {metrics['profit_factor']:.2f}x{'':<16} Target: ≥1.5x")
    print(f"{'Total P&L':<30} ${metrics['total_pnl']:,.2f}{'':<17} Growing capital")
    print(f"{'Avg Winner':<30} ${metrics['avg_winner']:,.2f}{'':<17}")
    print(f"{'Avg Loser':<30} ${metrics['avg_loser']:,.2f}{'':<17}")
    print(f"{'Expectancy/Trade':<30} ${metrics['expectancy']:,.2f}{'':<17} Should be positive")

    # Checkpoint indicators
    print("\n" + "-" * 110)
    print("CHECKPOINTS (By Oct 7):")
    print("-" * 110)

    trade_count = metrics['total_trades']
    pf = metrics['profit_factor']
    wr = metrics['win_rate']

    if trade_count >= 250:  # 50 trades per symbol × 5 coins
        if pf >= 1.5:
            print("✅ FULL VALIDATION (250+ trades): SUCCESS - PF ≥ 1.5x")
            print("   → Ready for Apex deployment alongside Alpaca")
        else:
            print("⚠️  FULL VALIDATION (250+ trades): MARGINAL")
            print("   → Investigate symbol-level performance")
    elif trade_count >= 50:
        if pf >= 1.3:
            print("✅ EARLY VALIDATION (50+ trades): ON TRACK - PF ≥ 1.3x")
            print("   → Continue accumulating; targeting 250+ total")
        else:
            print("⚠️  EARLY VALIDATION (50+ trades): WATCHFUL")
            print("   → Wait for more data before assessment")
    else:
        print(f"🚀 STARTING: {trade_count} trades logged, accumulating...")
        if trade_count >= 10:
            print(f"   Current PF: {pf:.2f}x (need ≥1.3x at 50 trades)")

    # Symbol breakdown
    symbols = symbol_breakdown(trades)
    if symbols:
        print("\n" + "-" * 110)
        print("PERFORMANCE BY SYMBOL (50+ Trades Each Target):")
        print("-" * 110)
        print(f"{'Symbol':<15} {'Trades':<10} {'Win%':<10} {'Total P&L':<15} {'Avg/Trade':<15}")
        for symbol in sorted(symbols.keys()):
            s = symbols[symbol]
            total = s['trades']
            wr_pct = (s['wins'] / total * 100) if total > 0 else 0
            avg_trade = s['pnl'] / total if total > 0 else 0
            status = "✓" if total >= 50 else "⏳"
            print(f"{symbol:<15} {total:<10} {wr_pct:<10.1f}% ${s['pnl']:<14,.2f} ${avg_trade:<14,.2f} {status}")

    # Recent trades (last 5)
    print("\n" + "-" * 110)
    print("LAST 5 TRADES:")
    print("-" * 110)
    print(f"{'#':<5} {'Date':<12} {'Symbol':<10} {'Entry':<12} {'Exit':<12} {'P&L':<12} {'Result':<8}")

    for trade in trades[-5:]:
        result = "✓ WIN" if trade['pnl'] > 0 else "✗ LOSS"
        print(f"{trade['number']:<5} {trade['date']:<12} {trade['product_id']:<10} ${trade['entry_price']:<11,.2f} ${trade['exit_price']:<11,.2f} ${trade['pnl']:<11,.2f} {result:<8}")

    print("\n" + "=" * 110)
    print(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 110)

def main():
    csv_file = '/home/user/empire-v2/CRYPTO_VALIDATION_TRACKER.csv'

    trades = load_trades(csv_file)
    metrics = calculate_metrics(trades)
    print_metrics(metrics, trades)

if __name__ == '__main__':
    main()
