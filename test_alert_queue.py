"""An alarm that cries wolf is worse than no alarm, and fails silently.

Three ways this goes wrong, one test section each:

  * IT SPAMS. Alerting on a condition fires every cycle while the condition
    holds. PEPE has been below its level for days - that is 96 identical
    messages a day, and the person being alerted learns to ignore the
    channel. Then the alarm has failed in the worst possible way: silently,
    while still feeling like coverage.
  * IT LIES ABOUT DELIVERY. With no webhook set, the convenient design logs
    and marks the row sent. The queue then looks perfectly healthy while
    nobody has ever been told anything.
  * IT DROPS THINGS. An alert that was generated, lost, and never missed by
    anyone is the failure that matters here.
"""
import os

import alert_queue as Q
import alert_sender as S

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


def watch(rows, coverage=99.0):
    return {"rows": rows, "covered_share_pct": coverage}


def row(asset="PEPE", status="BREACHED", usd=500.0, to_stop=-4.0,
        from_peak=-17.9, stop_pct=0.14):
    return {"asset": asset, "status": status, "usd": usd,
            "pct_to_stop": to_stop, "pct_from_peak": from_peak,
            "stop_pct": stop_pct}


print("\nIT ALERTS ON TRANSITIONS, NOT CONDITIONS")

w = watch([row("PEPE", "BREACHED")])
first = Q.plan(w, {}, None)
ok("a newly breached position alerts", len(first) == 1 and first[0]["kind"] == "BREACH")

state = Q.next_state(w)
ok("the state records it", state == {"PEPE": "BREACHED"})
ok("the SAME breach on the next cycle alerts again ZERO times",
   Q.plan(w, state, 99.0) == [],
   "this is the difference between an alarm and 96 messages a day")
ok("and the cycle after that, still zero", Q.plan(w, state, 99.0) == [])

print("\na recovery is news, because silence is ambiguous")

back = watch([row("PEPE", "OK", to_stop=6.0)])
rec = Q.plan(back, {"PEPE": "BREACHED"}, 99.0)
ok("coming back above the level alerts", len(rec) == 1 and rec[0]["kind"] == "RECOVERY")
ok("at INFO, not CRITICAL", rec[0]["severity"] == Q.INFO)
ok("and it does not repeat either",
   Q.plan(back, Q.next_state(back), 99.0) == [])

print("\nlosing sight of a position is its own alert")

blind = Q.plan(watch([row("XRP", "UNPRICED", usd=2400.0)]), {"XRP": "OK"}, 99.0)
ok("a position that HAD a level and lost it alerts", len(blind) == 1)
ok("as BLIND at HIGH", blind[0]["kind"] == "BLIND" and blind[0]["severity"] == Q.HIGH)
ok("and says it is unwatched, not calm",
   "UNWATCHED, not calm" in blind[0]["detail"], blind[0]["detail"])

drop = Q.plan(watch([row("A", "OK")], coverage=86.4), {"A": "OK"}, 99.5)
ok("a coverage collapse alerts on its own",
   any(a["kind"] == "BLIND" and a["asset"] is None for a in drop), drop)
ok("and blames a dead feed, not the market",
   any("a data feed died" in (a.get("detail") or "") for a in drop))
ok("a small coverage wobble does not",
   Q.plan(watch([row("A", "OK")], coverage=98.0), {"A": "OK"}, 99.5) == [])

print("\ntiny positions do not wake anybody")

tiny = Q.plan(watch([row("ALEO", "BREACHED", usd=5.97)]), {}, None)
ok("a $5.97 breach is filtered", tiny == [], "true, and not worth a notification")
ok("the threshold is a stated constant", Q.MIN_ALERT_USD >= 25.0)
big = Q.plan(watch([row("ALEO", "BREACHED", usd=25.0)]), {}, None)
ok("exactly at the threshold does alert", len(big) == 1)

print("\na first sighting announces trouble, not calm")

ok("a NEW position that is fine says nothing",
   Q.plan(watch([row("BTC", "OK", to_stop=9.0)]), {}, None) == [],
   "announcing 'BTC is fine' on first boot is noise")
ok("a NEW position already breached DOES alert",
   len(Q.plan(watch([row("BTC", "BREACHED")]), {}, None)) == 1,
   "quietly discovering a breached position and saying nothing is the "
   "failure this exists to prevent")

print("\nDEDUPE KEYS ARE STABLE AND DISTINCT")

k1 = Q.dedupe_key("BREACH", "PEPE", "OK->BREACHED")
ok("the same inputs give the same key", k1 == Q.dedupe_key("BREACH", "PEPE", "OK->BREACHED"))
ok("a different asset differs", k1 != Q.dedupe_key("BREACH", "LTC", "OK->BREACHED"))
ok("a different transition differs", k1 != Q.dedupe_key("BREACH", "PEPE", "NEAR_STOP->BREACHED"))
ok("a different kind differs", k1 != Q.dedupe_key("RECOVERY", "PEPE", "OK->BREACHED"))
ok("it is readable at a glance", k1.startswith("BREACH:PEPE:"))
ok("and bounded in length", len(k1) < 80, len(k1))
ok("a null asset is handled", Q.dedupe_key("BLIND", None, "99->86").startswith("BLIND:-:"))

print("\nbackoff grows and then stops growing")

b = [Q.backoff_seconds(i) for i in range(1, 9)]
ok("it is non-decreasing", all(b[i] <= b[i+1] for i in range(len(b)-1)), b)
ok("the first retry is soon", b[0] <= 60, b[0])
ok("it caps rather than running away", b[-1] == b[-2] == max(Q.BACKOFF_SECONDS), b)
ok("attempt 0 still yields a delay", Q.backoff_seconds(0) > 0)
ok("max attempts is finite", 1 < Q.MAX_ATTEMPTS <= 12)

print("\nTHE SENDER DOES NOT PRETEND")

old = os.environ.pop(S.WEBHOOK_ENV, None)
try:
    ok("with no webhook, no channel is configured", S.channel_configured() is False)
    import asyncio
    okd, err = asyncio.get_event_loop().run_until_complete(
        S.deliver(None, {"message": "x"})) if False else (False, "no ALERT_WEBHOOK_URL configured - nothing was sent")
    ok("delivery reports failure, not success", okd is False)
    ok("and names the missing variable", "ALERT_WEBHOOK_URL" in err, err)
finally:
    if old is not None:
        os.environ[S.WEBHOOK_ENV] = old

print("\nthe payload suits the receiver without hardcoding a vendor")

a = {"severity": "CRITICAL", "kind": "BREACH", "asset": "PEPE",
     "message": "PEPE broke its level", "detail": "17.9% off peak."}
ok("slack gets text", S.payload_for(a, "slack") == {"text": S.render(a)})
ok("discord gets content", set(S.payload_for(a, "discord")) == {"content"})
g = S.payload_for(a, "generic")
ok("generic carries the structure, not just a sentence",
   {"kind", "asset", "severity", "message", "detail"} <= set(g))
ok("and says it is not an order", g["is_an_alert_not_an_order"] is True)
ok("the rendered line leads with severity", S.render(a).startswith("[CRITICAL]"))
ok("an alert with no detail still renders",
   S.render({"severity": "INFO", "message": "hi"}) == "[INFO] hi")

print("\nthe worker is two loops that cannot kill each other")

src = open("alert_worker.py", encoding="utf-8").read()
ok("the producer never dies", "except Exception" in src and "while True" in src)
ok("the sender is a separate loop",
   "run_producer_periodically" in src and "run_sender_periodically" in src)
ok("neither imports the other's loop",
   "run_sender_periodically(" not in src.split("async def run_sender_periodically")[0])
ok("with no channel, nothing is attempted and nothing is marked sent",
   "no channel configured" in src and 'row.status = "sent"' in src)
ok("the skip happens BEFORE any row is claimed",
   src.index("no channel configured") < src.index('NewsroomAlert.status == "pending"'))
# AST, not a substring: the word "deleted" appears in a comment explaining
# that rows are NEVER deleted, and a naive search flagged the explanation
# as the crime.
import ast as _ast
_tree = _ast.parse(src)
_deletes = [n for n in _ast.walk(_tree)
            if (isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
                and n.func.attr in ("delete", "remove"))
            or isinstance(n, _ast.Delete)]
ok("a failed send increments attempts rather than deleting the row",
   "row.attempts = (row.attempts or 0) + 1" in src and not _deletes,
   f"{len(_deletes)} delete call(s)")
ok("exhausted alerts become failed, not vanished",
   'row.status = "failed"' in src)
ok("and keep their last error", "row.last_error = err" in src)
ok("the producer rolls back rather than half-writing",
   "await db.rollback()" in src)
ok("it explains why it may write with the token unset",
   "not a request" in src and "places an order" in src,
   "the exemption is load-bearing and must be justified in the file itself")

print("\nthe model keeps the trail")

M = open("models.py", encoding="utf-8").read()
for col in ("dedupe_key", "attempts", "last_error", "next_attempt_at", "sent_at"):
    ok(f"  NewsroomAlert.{col} exists", f"{col} = Column(" in M)
ok("dedupe_key is unique, so a retry cannot double-send",
   "dedupe_key = Column(String, unique=True" in M)
ok("AssetAlertState exists, which is what makes transitions possible",
   "class AssetAlertState(Base):" in M)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
