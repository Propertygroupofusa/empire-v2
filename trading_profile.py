"""One switch for the gate set, and an honest list of what it does not touch.

WHY

Between 2026-08-26 and 2026-09-24 the fleet placed 230 spot orders and
completed 104 round trips. Then it stopped: 910 scans, 0 qualified, for
days. The account owner asked to go back to the code that was trading.

Reverting is the wrong tool. Three hundred commits separate the two, and
they carry the maker-only execution path, the per-coin volatility stops,
and the repair of eleven ledger rows that could not reproduce their own
P&L. Worse, a revert restores the 1.50% taker fees that caused most of the
loss it is meant to undo:

    Aug 26 - Sep 24, measured     104 closes, $148.53 commission, -$86.83
    the same trades at 0.70%                   $69.31 commission,  -$7.61

91% of that window's loss was the fee tier, not the strategy. So the useful
experiment is the OLD trading rate at the NEW cost - which no revert can
produce, because the two live in different commits.

WHAT THE SWITCH ACTUALLY CONTROLS

Only the ECONOMIC gates - the ones that ask "is this trade worth taking".
It cannot touch the safety gates, and the distinction is the whole design:

    OFF in aug2026            net-edge gate      is this step worth its fee
                              learning veto      has this coin lost before

    ALWAYS ON, not switchable cash reserve       a rejected fee is a stuck
                                                 position - this is why the
                                                 fleet is blocked TODAY, at
                                                 $79.30 against an $88
                                                 reserve, and no profile
                                                 changes that
                              adaptive stops     per-coin volatility exits
                              maker-only         0.70% instead of 1.50%,
                                                 the improvement the whole
                                                 exercise depends on

A switch that could disable a stop would eventually be used to disable a
stop. This one cannot, and the tests assert it.

WHAT IT WILL NOT FIX

Nothing here creates cash. The first gate on the buy path is the reserve,
and at $79.30 in the wallet it returns zero before any gate this switch
controls is even reached. Both profiles place exactly zero trades until
there is money. The status payload says so rather than letting someone flip
this and wait for trades that cannot come.
"""
from __future__ import annotations

GUARDED = "guarded"
AUG2026 = "aug2026"
PROFILES = (GUARDED, AUG2026)

# Never switchable, whatever the profile. Named here so the list is one
# thing in one place rather than an assumption spread across call sites.
ALWAYS_ON = (
    ("cash_reserve", "a rejected fee is a stuck position"),
    ("adaptive_stops", "per-coin volatility exits"),
    ("maker_only", "0.70% round trip instead of 1.50%"),
)


def normalise(name) -> str:
    """Anything unrecognised resolves to GUARDED.

    Fail-safe, not fail-open: a typo in an environment variable must not
    silently remove the economic checks. The one direction that needs to be
    deliberate is turning them OFF.
    """
    n = str(name or "").strip().lower()
    return n if n in PROFILES else GUARDED


def net_edge_gate_enabled(profile) -> bool:
    return normalise(profile) == GUARDED


def learning_veto_enabled(profile) -> bool:
    return normalise(profile) == GUARDED


def describe(profile) -> dict:
    """What this profile does, in the words an operator needs."""
    p = normalise(profile)
    on = p == GUARDED
    return {
        "profile": p,
        "is_default": p == GUARDED,
        "economic_gates": {
            "net_edge_gate": on,
            "learning_veto": on,
        },
        "always_on": [{"gate": g, "why": w} for g, w in ALWAYS_ON],
        "summary": (
            "Every buy is checked against its own economics before it is "
            "placed. This is the default and the safe setting."
            if on else
            "The economic gates are OFF. Buys are placed without checking "
            "whether the step clears its fee, and without consulting what "
            "this coin has already cost. This reproduces the 2026-08-26 to "
            "2026-09-24 trading rate, at today's maker fees rather than that "
            "window's taker fees."),
        "measured_basis": (
            "That window: 230 spot orders, 104 closes, $148.53 commission, "
            "-$86.83 realized. The same trades at 0.70% maker would have "
            "cost $69.31 and read -$7.61, so 91% of the loss was the fee "
            "tier, not the strategy."),
        "will_not_fix": (
            "This does not create cash. The reserve gate runs FIRST on every "
            "buy and returns zero while the wallet is under it, so both "
            "profiles place zero trades until there is money to spend."),
    }


def blocked_by_cash(wallet_usd, reserve_usd, min_trade_usd) -> dict:
    """Would ANY profile trade right now? Usually the real answer."""
    try:
        w, r, m = float(wallet_usd), float(reserve_usd), float(min_trade_usd)
    except (TypeError, ValueError):
        return {"known": False,
                "note": "wallet or reserve unreadable - no claim made"}
    deployable = w - max(0.0, r)
    ok = deployable >= m
    return {
        "known": True,
        "wallet_usd": round(w, 2),
        "reserve_usd": round(r, 2),
        "deployable_usd": round(deployable, 2),
        "min_trade_usd": round(m, 2),
        "can_trade": ok,
        "shortfall_usd": (0.0 if ok else round(m - deployable, 2)),
        "note": ("There is cash to trade with; the profile decides whether a "
                 "given buy passes." if ok else
                 f"NO PROFILE WILL TRADE. The wallet is ${w:,.2f} against a "
                 f"${r:,.2f} reserve, so the first gate on the buy path "
                 f"returns zero. ${max(0.0, m - deployable):,.2f} more cash is "
                 f"needed before the profile matters at all."),
    }
