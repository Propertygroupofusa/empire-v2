#!/usr/bin/env python3
"""
Grid Bot Learning Memory Engine
- Reads lessons before executing trades
- Writes lessons after trades close
- Prevents repeating losing patterns
- Scales proven winning patterns
"""

import json
from datetime import datetime
from typing import Dict, List, Optional

class BotLearningEngine:
    def __init__(self, learnings_file: str = "bot_learnings.json"):
        self.learnings_file = learnings_file
        self.memory = self.load_memory()

    def load_memory(self) -> Dict:
        """Load bot's memory from learnings file"""
        try:
            with open(self.learnings_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {"winning_patterns": [], "losing_patterns": [], "trade_history": []}

    def save_memory(self):
        """Save bot's memory to learnings file"""
        with open(self.learnings_file, 'w') as f:
            json.dump(self.memory, f, indent=2)

    def check_before_trade(self, coin: str, condition: str) -> Dict:
        """
        BEFORE executing a trade: Check memory for lessons
        Returns: {"safe": bool, "reason": str, "confidence": float}
        """
        losing_patterns = self.memory.get("losing_patterns", [])

        # Check if this coin/condition combo is in losing patterns
        for pattern in losing_patterns:
            if pattern["coin"] == coin and pattern["condition"] in condition:
                return {
                    "safe": False,
                    "reason": f"⚠️ MEMORY WARNING: {pattern['lesson']}",
                    "confidence": pattern["confidence"],
                    "avoid_until": pattern.get("avoid_until", "unknown")
                }

        # Check winning patterns
        winning_patterns = self.memory.get("winning_patterns", [])
        for pattern in winning_patterns:
            if pattern["coin"] == coin and pattern["confidence"] > 0.55:
                return {
                    "safe": True,
                    "reason": f"✅ MEMORY CONFIRMED: {pattern['lesson']}",
                    "confidence": pattern["confidence"]
                }

        return {
            "safe": True,
            "reason": "No prior lessons - new condition",
            "confidence": 0.5
        }

    def log_trade_result(self, trade_id: str, coin: str, entry: float, exit: float,
                        profit: float, status: str):
        """
        AFTER trade closes: Write lesson to memory
        """
        lesson = self._generate_lesson(coin, profit, status)

        trade_record = {
            "trade_id": trade_id,
            "coin": coin,
            "entry_price": entry,
            "exit_price": exit,
            "profit": profit,
            "status": status,
            "lesson": lesson,
            "updated": datetime.now().isoformat()
        }

        # Add to trade history
        if "trade_history" not in self.memory:
            self.memory["trade_history"] = []
        self.memory["trade_history"].append(trade_record)

        # Update patterns
        if profit > 0:
            self._update_winning_pattern(coin, lesson)
        else:
            self._update_losing_pattern(coin, lesson)

        self.save_memory()
        print(f"📝 Lesson logged: {lesson}")

    def _generate_lesson(self, coin: str, profit: float, status: str) -> str:
        """Generate a lesson from trade result"""
        if profit > 0:
            return f"{coin} wins at this condition - KEEP DOING THIS"
        else:
            return f"{coin} loses at this condition - CHANGE SOMETHING before retrying"

    def _update_winning_pattern(self, coin: str, lesson: str):
        """Add/update winning pattern"""
        patterns = self.memory.get("winning_patterns", [])

        # Find existing pattern
        found = False
        for p in patterns:
            if p["coin"] == coin:
                p["trades_confirming"] += 1
                p["confidence"] = min(0.95, p["trades_confirming"] / 10)
                found = True
                break

        if not found:
            patterns.append({
                "pattern_id": f"win_{len(patterns):03d}",
                "coin": coin,
                "condition": "grid 1%, normal volatility",
                "lesson": lesson,
                "confidence": 0.55,
                "trades_confirming": 1,
                "first_seen": datetime.now().isoformat()
            })

        self.memory["winning_patterns"] = patterns

    def _update_losing_pattern(self, coin: str, lesson: str):
        """Add/update losing pattern"""
        patterns = self.memory.get("losing_patterns", [])

        # Find existing pattern
        found = False
        for p in patterns:
            if p["coin"] == coin:
                p["trades_confirming"] += 1
                found = True
                break

        if not found:
            patterns.append({
                "pattern_id": f"loss_{len(patterns):03d}",
                "coin": coin,
                "condition": "unknown - needs investigation",
                "lesson": lesson,
                "confidence": 0.5,
                "trades_confirming": 1,
                "avoid_until": "confirmation of x y z",
                "first_seen": datetime.now().isoformat()
            })

        self.memory["losing_patterns"] = patterns

    def get_best_coin_by_history(self) -> Optional[str]:
        """Get the coin with highest win rate from memory"""
        trade_history = self.memory.get("trade_history", [])

        if not trade_history:
            return None

        coin_stats = {}
        for trade in trade_history:
            coin = trade["coin"]
            profit = trade["profit"]

            if coin not in coin_stats:
                coin_stats[coin] = {"wins": 0, "losses": 0, "total_profit": 0}

            if profit > 0:
                coin_stats[coin]["wins"] += 1
            else:
                coin_stats[coin]["losses"] += 1
            coin_stats[coin]["total_profit"] += profit

        # Sort by win rate
        best = max(coin_stats.items(),
                  key=lambda x: x[1]["wins"] / (x[1]["wins"] + x[1]["losses"]) if (x[1]["wins"] + x[1]["losses"]) > 0 else 0)
        return best[0] if best else None

    def print_memory_summary(self):
        """Print what the bot has learned"""
        print("\n" + "="*70)
        print("🧠 BOT MEMORY SUMMARY")
        print("="*70)

        winning = self.memory.get("winning_patterns", [])
        losing = self.memory.get("losing_patterns", [])
        history = self.memory.get("trade_history", [])

        print(f"\n✅ Winning Patterns: {len(winning)}")
        for w in winning[:3]:
            print(f"   {w['coin']}: {w['lesson']} (confidence: {w['confidence']:.0%})")

        print(f"\n❌ Losing Patterns: {len(losing)}")
        for l in losing[:3]:
            print(f"   {l['coin']}: {l['lesson']}")

        print(f"\n📊 Trades Logged: {len(history)}")
        if history:
            total_profit = sum(t["profit"] for t in history)
            wins = sum(1 for t in history if t["profit"] > 0)
            print(f"   Wins: {wins}/{len(history)} ({wins/len(history)*100:.0f}%)")
            print(f"   Total Profit: ${total_profit:.2f}")

        best_coin = self.get_best_coin_by_history()
        if best_coin:
            print(f"\n🏆 Best Coin: {best_coin}")

        print("="*70 + "\n")

# Example usage
if __name__ == "__main__":
    engine = BotLearningEngine()

    # Check before trade
    check = engine.check_before_trade("BTC-USD", "grid 1%, normal vol")
    print(f"Trade check: {check}")

    # Log a winning trade
    engine.log_trade_result(
        "2026-09-07-btc-001",
        "BTC-USD",
        entry=100.00,
        exit=101.50,
        profit=15.00,
        status="closed_profit"
    )

    # Log a losing trade
    engine.log_trade_result(
        "2026-09-07-stx-001",
        "STX-USD",
        entry=2.00,
        exit=1.95,
        profit=-10.00,
        status="closed_loss"
    )

    # Print what bot learned
    engine.print_memory_summary()
