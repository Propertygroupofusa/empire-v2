"""Paper test of daily_stock_analysis picks: parser against DSA's real English
line formats, the fixed trading rules, and the webhook end to end with a
throwaway DB and faked prices. No order endpoint exists to test against."""
import asyncio, os, tempfile
_db = os.path.join(tempfile.mkdtemp(), "dsa.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db}"
import dsa_paper as dp

fails = 0
def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if not cond else ""))
    fails += 0 if cond else 1

# Formats copied from DSA src/notification.py (summary, dashboard, aggregate).
report = """# 🚀 Decision Dashboard
🟢 **Apple(AAPL)**: Buy | Score 72 | Bullish
⚪ **NVIDIA(NVDA)**: Watch | Score 55 | Sideways
🔴 Tesla(TSLA): Sell | Score 30
🟢 **Meta(META)**: Strong Buy | Score 81 | Strong Bullish
🟠 **Intel(INTC)**: Reduce | Score 41 | Bearish
Note (see appendix): something | 99 unrelated
🟢 **Apple(AAPL)**: Sell | Score 20 | Bearish
"""
sig = dp.parse_report(report)
check("parses every DSA line", [s[0] for s in sig] == ["AAPL", "NVDA", "TSLA", "META", "INTC"], str(sig))
check("actions and scores", sig[0] == ("AAPL", "buy", 72) and sig[3] == ("META", "strong buy", 81))
check("first mention of a ticker wins", dict((t, a) for t, a, _ in sig)["AAPL"] == "buy")
check("prose is not a trade", all(t != "SEE" for t, _, _ in sig))
check("empty report -> no signals", dp.parse_report("") == [] and dp.parse_report(None) == [])

a = dp.new_account("t0")
a, tr, sk = dp.apply_signals(a, sig, {"AAPL": 100.0, "META": 500.0, "TSLA": 200.0}, "t1")
check("buys only Buy/Strong Buy", sorted(t["ticker"] for t in tr) == ["AAPL", "META"])
check("one slot is equity / 5", abs(a["positions"]["AAPL"]["qty"] * a["positions"]["AAPL"]["entry"] - 200) < 1e-6)
check("slippage is charged on the buy", a["positions"]["AAPL"]["entry"] == 100.0 * 1.001)
check("sell of a ticker not held does nothing", "TSLA" not in a["positions"] and a["cash"] == 600.0)
check("Watch/Reduce never trade", "NVDA" not in a["positions"] and "INTC" not in a["positions"])
a2, tr2, _ = dp.apply_signals(a, [("AAPL", "sell", 20)], {"AAPL": 110.0, "META": 500.0}, "t2")
exp = a["positions"]["AAPL"]["qty"] * 110.0 * 0.999
check("sell returns cash at price minus slippage", abs(a2["cash"] - (600.0 + exp)) < 1e-6)
check("sell records real pnl", tr2[0]["pnl"] > 0 and "AAPL" not in a2["positions"])
check("apply never mutates its input", "AAPL" in a["positions"])
_, tr3, sk3 = dp.apply_signals(dp.new_account("t"), [("ZZZ", "buy", 70)], {}, "t")
check("no live price -> skipped, never guessed", not tr3 and sk3 == [("ZZZ", "buy: no live price")])
full = dp.new_account("t")
buys = [(f"T{i}", "buy", 70) for i in range(7)]
full, trf, skf = dp.apply_signals(full, buys, {f"T{i}": 10.0 for i in range(7)}, "t")
check("never more than 5 positions", len(full["positions"]) == 5 and len(skf) == 2)
check("fully invested, not overdrawn", 0 <= full["cash"] < 1e-6)
check("equity is None while a holding is unpriced", dp.equity(full, {}) is None)

async def endpoint():
    from database import init_db
    await init_db()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routers.dsa_paper as r
    async def fake_prices(session, tickers):
        return {t: {"AAPL": 100.0, "META": 500.0, "SPY": 600.0}.get(t) for t in tickers if t in {"AAPL", "META", "SPY"}}
    async def fake_eq(session):
        return 983.10
    r._latest_prices, r._alpaca_equity = fake_prices, fake_eq
    app = FastAPI(); app.include_router(r.router, prefix="/api/trading-dashboard")
    c = TestClient(app)
    os.environ.pop("DSA_WEBHOOK_TOKEN", None)
    check("no token configured -> refused", c.post("/api/trading-dashboard/dsa-paper/webhook", json={"text": report}).status_code == 503)
    os.environ["DSA_WEBHOOK_TOKEN"] = "s3cret"
    check("wrong token -> 401", c.post("/api/trading-dashboard/dsa-paper/webhook", json={"text": report},
          headers={"Authorization": "Bearer nope"}).status_code == 401)
    res = c.post("/api/trading-dashboard/dsa-paper/webhook", json={"text": report, "content": report},
                 headers={"Authorization": "Bearer s3cret"})
    check("valid report -> paper trades", res.status_code == 200 and res.json()["trades"] == 2, res.text)
    os.environ["ADMIN_API_KEY"] = "adm"
    check("status needs the admin key", c.get("/api/trading-dashboard/dsa-paper/status").status_code == 401)
    s = c.get("/api/trading-dashboard/dsa-paper/status", headers={"X-Admin-Key": "adm"}).json()
    check("status shows the account", s["started"] and len(s["paper"]["positions"]) == 2, str(s))
    check("baselines recorded at start", s["spy_return_pct"] == 0.0 and s["alpaca_account_return_pct"] == 0.0, str(s))
    check("paper return reflects slippage only", -0.2 < s["paper"]["return_pct"] <= 0, str(s["paper"]))
    src = open("routers/dsa_paper.py").read() + open("dsa_paper.py").read()
    check("no order endpoint anywhere", "/v2/orders" not in src and "place_order" not in src)

asyncio.run(endpoint())
print("ALL PASS" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)
