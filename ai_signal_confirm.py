"""
TRIPLE AI SIGNAL CONFIRMATION
==============================
Claude + GPT-4o + Grok must ALL agree before any trade fires.
If any one disagrees → trade is blocked.
"""

import os
import json
import logging
import httpx

log = logging.getLogger("ai_confirm")

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
GROK_KEY      = os.getenv("GROK_API_KEY", "")

CLAUDE_MODEL  = "claude-haiku-4-5-20251001"
GPT_MODEL     = "gpt-4o-mini"
GROK_MODEL    = "grok-beta"

# ── Prompt sent to all 3 AIs ─────────────────────────────────
def build_prompt(signal: dict) -> str:
    return f"""You are a trading risk analyst. Analyze this trade signal and respond with ONLY a JSON object.

SIGNAL:
- Symbol: {signal.get('symbol')}
- Action: {signal.get('action')} (BUY or SELL)
- Price: {signal.get('price')}
- RSI: {signal.get('rsi')}
- Trend: {signal.get('trend')}
- Volume: {signal.get('volume')}
- Account Balance: ${signal.get('balance')}
- Hard Stop: ${signal.get('hard_stop', 80000)}

Respond ONLY with this exact JSON — no extra text:
{{
  "approve": true or false,
  "confidence": 0-100,
  "reason": "one sentence"
}}

Approve only if: confidence > 70, trend supports action, RSI not overbought/oversold extreme."""


# ── Claude ────────────────────────────────────────────────────
async def ask_claude(prompt: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 150,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            text = r.json()["content"][0]["text"].strip()
            return json.loads(text)
    except Exception as e:
        log.error(f"Claude error: {e}")
        return {"approve": False, "confidence": 0, "reason": f"Claude error: {e}"}


# ── GPT-4o ────────────────────────────────────────────────────
async def ask_gpt(prompt: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GPT_MODEL,
                    "max_tokens": 150,
                    "temperature": 0.1,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            text = r.json()["choices"][0]["message"]["content"].strip()
            return json.loads(text)
    except Exception as e:
        log.error(f"GPT error: {e}")
        return {"approve": False, "confidence": 0, "reason": f"GPT error: {e}"}


# ── Grok ──────────────────────────────────────────────────────
async def ask_grok(prompt: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROK_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROK_MODEL,
                    "max_tokens": 150,
                    "temperature": 0.1,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            text = r.json()["choices"][0]["message"]["content"].strip()
            return json.loads(text)
    except Exception as e:
        log.error(f"Grok error: {e}")
        return {"approve": False, "confidence": 0, "reason": f"Grok error: {e}"}


# ── MAIN CONFIRMATION GATE ────────────────────────────────────
async def confirm_trade(signal: dict) -> dict:
    """
    Call this before every trade.
    Returns: { "approved": True/False, "results": {...} }
    
    Usage:
        result = await confirm_trade(signal)
        if result["approved"]:
            execute_trade()
    """
    prompt = build_prompt(signal)

    import asyncio
    claude_r, gpt_r, grok_r = await asyncio.gather(
        ask_claude(prompt),
        ask_gpt(prompt),
        ask_grok(prompt),
    )

    results = {
        "claude": claude_r,
        "gpt":    gpt_r,
        "grok":   grok_r,
    }

    # All 3 must approve
    all_approved = (
        claude_r.get("approve") is True and
        gpt_r.get("approve")    is True and
        grok_r.get("approve")   is True
    )

    avg_confidence = (
        claude_r.get("confidence", 0) +
        gpt_r.get("confidence", 0) +
        grok_r.get("confidence", 0)
    ) / 3

    if all_approved:
        log.info(
            f"✅ TRADE APPROVED | {signal.get('symbol')} {signal.get('action')} "
            f"| Avg confidence: {avg_confidence:.0f}%"
        )
    else:
        blockers = [
            ai for ai, r in results.items()
            if not r.get("approve")
        ]
        log.warning(
            f"🚫 TRADE BLOCKED | {signal.get('symbol')} {signal.get('action')} "
            f"| Blocked by: {', '.join(blockers)}"
        )
        for ai, r in results.items():
            if not r.get("approve"):
                log.warning(f"  {ai}: {r.get('reason')}")

    return {
        "approved":       all_approved,
        "avg_confidence": round(avg_confidence, 1),
        "results":        results,
        "signal":         signal,
    }


# ── TEST (run directly to verify all 3 keys work) ─────────────
if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)

    test_signal = {
        "symbol":    "SPY",
        "action":    "BUY",
        "price":     445.50,
        "rsi":       52,
        "trend":     "bullish",
        "volume":    "above average",
        "balance":   100000,
        "hard_stop": 80000,
    }

    result = asyncio.run(confirm_trade(test_signal))

    print("\n" + "="*50)
    print("TRIPLE AI CONFIRMATION RESULT")
    print("="*50)
    print(f"APPROVED: {result['approved']}")
    print(f"AVG CONFIDENCE: {result['avg_confidence']}%")
    print("\nIndividual Results:")
    for ai, r in result["results"].items():
        status = "✅" if r.get("approve") else "🚫"
        print(f"  {status} {ai.upper()}: {r.get('confidence')}% — {r.get('reason')}")
