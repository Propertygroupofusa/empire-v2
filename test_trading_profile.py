"""A switch that can turn off a stop will eventually be used to turn off a stop.

This one controls the ECONOMIC gates - "is this trade worth its fee" - and
cannot reach the safety gates. The distinction is the entire design, and
these tests are what keep it true when someone later adds a third profile.

The other thing they guard: the switch must not promise trading it cannot
deliver. The reserve gate runs FIRST on every buy and returns zero while the
wallet is under it, so at $79.30 against an $88 reserve both profiles place
exactly zero trades. A switch that stays quiet about that sends someone to
wait for trades that cannot come.
"""
import ast
import json

import trading_profile as T

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


print("\nAN UNRECOGNISED PROFILE FAILS SAFE, NEVER OPEN")

for bad in (None, "", "  ", "aug-2026", "AUG2026 ", "off", "none", 1, [], "guarded!"):
    got = T.normalise(bad)
    if bad in ("AUG2026 ",):
        ok(f"  {bad!r} -> aug2026 (case and space tolerated)", got == T.AUG2026, got)
    else:
        ok(f"  {bad!r} -> guarded", got == T.GUARDED, got)
ok("the two real names round-trip",
   T.normalise("guarded") == T.GUARDED and T.normalise("aug2026") == T.AUG2026)

print("\nthe default is the guarded one")

ok("describe() with nothing set is guarded", T.describe(None)["profile"] == T.GUARDED)
ok("and says it is the default", T.describe(None)["is_default"] is True)
ok("aug2026 is not the default", T.describe("aug2026")["is_default"] is False)

print("\nIT CANNOT TURN OFF A SAFETY GATE")

names = [g for g, _ in T.ALWAYS_ON]
ok("the cash reserve is always on", "cash_reserve" in names)
ok("the adaptive stops are always on", "adaptive_stops" in names)
ok("maker-only is always on", "maker_only" in names)
for p in T.PROFILES:
    d = T.describe(p)
    ok(f"  {p}: the always-on list is unchanged",
       [g["gate"] for g in d["always_on"]] == names)
    ok(f"  {p}: no safety gate appears among the switchable ones",
       not (set(names) & set(d["economic_gates"])),
       "a stop must not be reachable from a trading-rate switch")
ok("every always-on gate states WHY", all(w for _, w in T.ALWAYS_ON))

print("\nthe economic gates are exactly the two it claims")

ok("guarded has both on",
   T.describe("guarded")["economic_gates"] == {"net_edge_gate": True,
                                               "learning_veto": True})
ok("aug2026 has both off",
   T.describe("aug2026")["economic_gates"] == {"net_edge_gate": False,
                                               "learning_veto": False})
ok("the helpers agree with describe()",
   T.net_edge_gate_enabled("guarded") and not T.net_edge_gate_enabled("aug2026")
   and T.learning_veto_enabled("guarded") and not T.learning_veto_enabled("aug2026"))
ok("a garbage profile still leaves the gates ON",
   T.net_edge_gate_enabled("nonsense") and T.learning_veto_enabled(None))

print("\nit says what turning the gates off actually means")

s = T.describe("aug2026")["summary"]
ok("it says the gates are OFF", "OFF" in s)
ok("it says buys go in unchecked against fees", "clears its fee" in s)
ok("and names the window it reproduces", "2026-08-26" in s and "2026-09-24" in s)
ok("and that today's fees are lower than that window's",
   "maker" in s and "taker" in s)

b = T.describe("aug2026")["measured_basis"]
ok("the basis is measured, not asserted",
   "104 closes" in b and "$148.53" in b and "-$86.83" in b, b)
ok("and states how much of the loss was the fee tier", "91%" in b)

print("\nTHE CASH CHECK IS THE REAL ANSWER, AND IT LEADS")

c = T.blocked_by_cash(79.30, 88.00, 5.00)
ok("at the live balance, no profile trades", c["can_trade"] is False)
ok("and it says so in capitals", "NO PROFILE WILL TRADE" in c["note"])
ok("with the exact shortfall", c["shortfall_usd"] == 13.70, c["shortfall_usd"])
ok("deployable is wallet minus reserve", c["deployable_usd"] == -8.70)
ok("it explains the reserve runs FIRST", "first gate" in c["note"])

c2 = T.blocked_by_cash(500.0, 88.00, 5.00)
ok("with cash, it says the profile decides", c2["can_trade"] is True)
ok("and shortfall is zero, not negative", c2["shortfall_usd"] == 0.0)
ok("exactly at the minimum counts as tradeable",
   T.blocked_by_cash(93.0, 88.0, 5.0)["can_trade"] is True)
ok("a cent under does not",
   T.blocked_by_cash(92.99, 88.0, 5.0)["can_trade"] is False)
ok("an unreadable wallet makes NO claim",
   T.blocked_by_cash(None, 88.0, 5.0)["known"] is False,
   "not knowing is not the same as 'can trade'")
ok("a negative reserve is clamped, not trusted",
   T.blocked_by_cash(10.0, -50.0, 5.0)["deployable_usd"] == 10.0)

print("\nand it refuses to promise what it cannot do")

w = T.describe("aug2026")["will_not_fix"]
ok("it says it does not create cash", "does not create cash" in w)
ok("and that both profiles trade zero without it", "zero trades" in w)

print("\nthe whole thing serialises")
json.dumps(T.describe("aug2026")); json.dumps(T.blocked_by_cash(1, 2, 3))
ok("JSON-serialisable", True)

print("\nTHE LIVE WIRING MATCHES THE POLICY")

BOT = open("crypto_grid_bot.py", encoding="utf-8").read()
tree = ast.parse(BOT)
fns = {n.name: n for n in ast.walk(tree)
       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ok("there is a DB-persisted getter", "get_trading_profile" in fns)
ok("and a setter", "set_trading_profile" in fns)

gsrc = ast.get_source_segment(BOT, fns["get_trading_profile"]) or ""
ok("the getter never raises", "except Exception" in gsrc)
ok("and resolves an unreadable setting to GUARDED",
   "staying guarded" in gsrc and "trading_profile.GUARDED" in gsrc,
   "fail-open here means money committed with no economic check")

gate = ast.get_source_segment(BOT, fns["_net_edge_gate_ok"]) or ""
ok("the net-edge gate consults the profile", "net_edge_gate_enabled" in gate)
# AST, not substring: the function's docstring names is_net_edge_gate_active
# in prose above the code, so a text search finds the explanation before the
# call and compares the wrong two things.
_gate_ast = fns["_net_edge_gate_ok"]
_calls = [(n.lineno, ast.unparse(n.func)) for n in ast.walk(_gate_ast)
          if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))]
_prof = min((ln for ln, f in _calls if "net_edge_gate_enabled" in f), default=None)
_own = min((ln for ln, f in _calls if "is_net_edge_gate_active" in f), default=None)
ok("and the profile check comes BEFORE the gate's own switch",
   _prof is not None and _own is not None and _prof < _own,
   f"profile at line {_prof}, own switch at {_own}")

ok("the learning veto consults it too", "learning_veto_enabled" in BOT)

# The gates the switch must NOT be able to reach.
ok("the reserve gate does not consult the profile",
   "trading_profile" not in (ast.get_source_segment(BOT, fns["spendable_for_slice"]) or ""),
   "the reserve is why the fleet is blocked today; no profile may lift it")
ok("_resolve_branch_stop does not consult the profile",
   "trading_profile" not in (ast.get_source_segment(BOT, fns.get("_resolve_branch_stop"))
                             or "" if "_resolve_branch_stop" in fns else ""))
ok("maker-only does not consult the profile",
   "trading_profile" not in (ast.get_source_segment(BOT, fns["is_maker_orders_active"]) or ""))

print("\nthe endpoints: read is open, the switch is guarded twice")

DASH = open("routers/trading_dashboard.py", encoding="utf-8").read()
dfns = {n.name: n for n in ast.walk(ast.parse(DASH))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ok("a GET reports the status", "get_trading_profile_status" in dfns)
ok("a POST changes it", "set_trading_profile_endpoint" in dfns)
get_d = [ast.unparse(d) for d in dfns["get_trading_profile_status"].decorator_list]
post_d = [ast.unparse(d) for d in dfns["set_trading_profile_endpoint"].decorator_list]
ok("the status is a GET, so it answers without a token",
   any("router.get" in d for d in get_d), get_d)
ok("the change is a POST, so write_guard covers it",
   any("router.post" in d for d in post_d), post_d)

psrc = ast.get_source_segment(DASH, dfns["set_trading_profile_endpoint"]) or ""
ok("it demands confirm=yes on top of the token", 'confirm != "yes"' in psrc)
ok("an unknown profile is REFUSED, not silently normalised",
   "Refusing rather" in psrc,
   "resolving a typo to a setting nobody asked for is how this goes wrong")

gsrc2 = ast.get_source_segment(DASH, dfns["get_trading_profile_status"]) or ""
ok("the status leads with the cash check", "blocked_by_cash" in gsrc2)
ok("and makes no cash claim it cannot support",
   "wallet balance unreadable" in gsrc2)
ok("the status writes nothing", "commit" not in gsrc2)


print("\nthe dashboard panel leads with cash, not with the switch")

HTML = open("family_tree_dashboard.html", encoding="utf-8").read()
ok("the panel exists", 'id="trading-profile-wrap"' in HTML)
ok("and loads itself", "loadTradingProfile();" in HTML)
i = HTML.index("async function loadTradingProfile")
panel = HTML[i:HTML.index("async function loadAlertQueue")]
ok("the cash banner is built before the profile block",
   panel.index("cashBanner") < panel.index("d.economic_gates"),
   "the reserve gate runs first, so the panel must say so first")
ok("it shouts when nothing can trade", "NO PROFILE WILL TRADE" in panel)
ok("with the exact shortfall", "shortfall_usd" in panel)
ok("an unreadable wallet makes no claim", "making no claim" in panel)
ok("the not-switchable gates are rendered with their reasons",
   "Not switchable" in panel and "g.why" in panel)
ok("and labelled ALWAYS ON", "ALWAYS ON" in panel)
ok("the measured basis is shown, not just the summary",
   "measured_basis" in panel)
ok("an unreadable profile is reported as unknown, not as guarded",
   "not the same as guarded" in panel,
   "defaulting the DISPLAY to guarded would hide an open gate set")
ok("everything printed is escaped",
   panel.count("escText(") >= 6 and "innerHTML = cashBanner" in panel)
ok("the section copy states the measured window",
   "2026-08-26" in HTML and "$148.53" in HTML and "-$7.61" in HTML)
ok("and that the switch cannot reach a stop",
   "cannot reach a stop" in HTML)


print("\nthe status reads the SAME balance the gate receives")

ok("it calls engine.get_usd_balance, as the buy path does",
   "get_usd_balance" in gsrc2,
   "the buy path uses engine.get_usd_balance(session)")
# AST again: the comment above the fix NAMES the wrong function in order to
# explain why it is wrong, so a substring search flags the warning as the
# offence. Only an actual call counts.
_status_calls = {ast.unparse(n.func) for n in ast.walk(dfns["get_trading_profile_status"])
                 if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))}
ok("and never CALLS get_real_free_cash_usd",
   not any("get_real_free_cash_usd" in c for c in _status_calls),
   "that is the wallet minus branch reserves - it reported -$384.43 and a "
   "$477.43 shortfall when the real one was $13.70")
ok("it does call get_usd_balance",
   any("get_usd_balance" in c for c in _status_calls), sorted(_status_calls))
ok("a read error yields None, not a number",
   "wallet = None" in gsrc2 and "err is None" in gsrc2)
ok("the comment records what the wrong figure did",
   "35x wrong" in gsrc2 or "35x" in gsrc2)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
