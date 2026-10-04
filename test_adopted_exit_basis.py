"""The basis that decides a sale must be the basis that prices it.

WHAT WENT WRONG. GRID_TRUE_COST_BASIS was shipped with the stated promise
that it "never changes a recorded P&L" - it would only decide whether an
adopted slice was allowed to sell. That promise is what this file exists to
undo, because it produced two contradictory numbers for one event and then
persisted the wrong one.

The live evidence, from this account's own ledger on 2026-10-04:

  gate   : PARKED_SELL ZEC-USD +30.95% net on $133.33 of basis
  row 197: 0.08036146 @ 1331.94  booked -$26.72   restated +$25.23
  row 198: 0.37816667 @ 1330.02  booked -$123.21  restated +$118.05

A $293.21 swing across two rows. Not a display fault either: allocated_usd
moves by `pnl` and by nothing else - there is no decrement on the buy - so
$149.93 of working capital came off crypto_grid_21, and
_safe_num_levels_for_allocation() sizes the ladder off allocated_usd. The
measurement error was shrinking the real ladder.

And the headline read -$14.35 across 198 trades on a book whose own 196
round trips still stood at +$135.58.
"""

import ast
import asyncio
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# BEFORE ANY IMPORT. database.py builds its engine at import time, so a
# DATABASE_URL set later in a test body is read too late and the test
# silently runs against whatever sqlite file happens to be in the repo -
# which is how this first failed, with an old table missing exit_reason.
_TMP = tempfile.TemporaryDirectory()
_DB = pathlib.Path(_TMP.name) / "books.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB.as_posix()}"
os.environ.setdefault("GRID_TRUE_COST_BASIS", "ZEC-USD:1013.80")

PASS = []
FAIL = []


def ok(label, cond):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


SRC = (REPO / "crypto_grid_bot.py").read_text()


# ---------------------------------------------------------------- 1. no-op
# The whole safety case for touching the booking path is that
# sell_basis_for_slice() is the identity for every slice that is not an
# adopted slice of a product with a declared basis. If that is not true,
# this change repriced the entire book.
def test_identity_everywhere_else():
    os.environ["GRID_TRUE_COST_BASIS"] = "ZEC-USD:1013.80"
    import crypto_grid_bot as g
    g.GRID_TRUE_COST_BASIS = g._parse_true_cost_basis("ZEC-USD:1013.80")

    class S:
        def __init__(self, entry, adopted, pid):
            self.entry_price = entry
            self.adopted = adopted
            self.product_id = pid

    # grid-bought, declared product -> its own real entry, untouched
    ok("a slice the grid bought keeps its own entry even where a basis is declared",
       g.sell_basis_for_slice(S(1586.44, False, "ZEC-USD")) == 1586.44)
    # adopted, undeclared product -> unchanged
    ok("an adopted slice of an undeclared product is unchanged",
       g.sell_basis_for_slice(S(0.2731, True, "HBAR-USD")) == 0.2731)
    # adopted, declared -> the declared basis, the one case this exists for
    ok("an adopted slice of a declared product prices at the declared basis",
       g.sell_basis_for_slice(S(1659.17, True, "ZEC-USD")) == 1013.80)
    # empty declaration -> identity for everything
    g.GRID_TRUE_COST_BASIS = g._parse_true_cost_basis("")
    ok("with nothing declared every slice keeps its recorded entry",
       g.sell_basis_for_slice(S(1659.17, True, "ZEC-USD")) == 1659.17)
    g.GRID_TRUE_COST_BASIS = g._parse_true_cost_basis("ZEC-USD:1013.80")


# ------------------------------------------------- 2. the booking site itself
# Parsed, not grepped. An earlier test in this repo matched a phrase that
# appeared in its own explanatory comment ~300 chars above the call site and
# passed on code that did the opposite of what it claimed.
def test_every_consumer_uses_the_booked_basis():
    tree = ast.parse(SRC)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "run_grid_branch_cycle"), None)
    ok("run_grid_branch_cycle is where the sale is booked", fn is not None)
    if fn is None:
        return

    # the pnl call
    pnl_calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "_grid_slice_net_pnl"]
    ok("the real sell books exactly one net-pnl call", len(pnl_calls) == 1)
    if pnl_calls:
        basis_arg = pnl_calls[0].args[1]
        ok("the booked pnl is priced at _booked_basis, not the recorded entry",
           isinstance(basis_arg, ast.Name) and basis_arg.id == "_booked_basis")

    # the persisted row
    log_calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)
                 and n.func.id == "_log_grid_trade"]
    ok("the sale writes exactly one trade-history row", len(log_calls) == 1)
    if log_calls:
        entry_arg = log_calls[0].args[2]
        ok("the stored row's entry is the basis the pnl was priced at, so "
           "qty/entry/exit/pnl on one row agree",
           isinstance(entry_arg, ast.Name) and entry_arg.id == "_booked_basis")

    # the shadow block: fees_paid = gross_pnl - pnl, so a gross computed off a
    # different basis turns the recorded "fee" into the basis gap.
    gross = [n for n in ast.walk(fn)
             if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "gross_pnl"
                     for t in n.targets)]
    ok("the shadow gross uses the same basis, or its derived fee is the "
       "basis gap rather than a fee",
       bool(gross) and "_booked_basis" in ast.unparse(gross[0]))

    ok("_booked_basis is assigned before anything reads it",
       SRC.index("_booked_basis = sell_basis_for_slice")
       < SRC.index("_grid_slice_net_pnl(filled_qty, _booked_basis"))


# ------------------------------------------------ 3. the retracted promise
def test_the_false_promise_is_gone():
    ok("the design note no longer promises the basis never changes a "
       "recorded P&L",
       "never changes a\n# recorded P&L" not in SRC
       and "It never changes a" not in SRC)
    ok("and says instead that it prices the sale it authorises",
       "IT DOES PRICE THE SALE IT AUTHORISES" in SRC)


# --------------------------------------------------- 4. the real two rows
# Replayed through the production formula at the production fee shape for an
# adopted slice: no buy order was ever placed, so the sell leg alone is
# charged. The assertion is on the SIGN and on agreement with the gate, not
# on reproducing a venue fee to the cent.
def test_the_two_zec_rows_restate_positive():
    import crypto_grid_bot as g
    sell_leg = 0.0035          # real_maker_fee_rate, live
    rows = [
        # qty,          adoption mark, fill price,  what was booked
        (0.08036146,    1659.17,       1331.94,     -26.72),
        (0.37816667,    1650.61,       1330.02,     -123.21),
    ]
    true_basis = 1013.80
    booked_total = 0.0
    restated_total = 0.0
    for qty, mark, fill, booked in rows:
        old = g._grid_slice_net_pnl(qty, mark, fill, sell_leg)
        new = g._grid_slice_net_pnl(qty, true_basis, fill, sell_leg)
        ok(f"replaying the adoption mark reproduces the booked ${booked:.2f} "
           f"(got ${old:.2f})", abs(old - booked) < 0.30)
        ok(f"at what was really paid the same sale is a gain (${new:.2f})",
           new > 0)
        booked_total += old
        restated_total += new

    ok("the sign flips for the pair, not just for one row",
       booked_total < 0 < restated_total)
    # -149.93 booked against +143.28 real
    ok(f"the pair was mispriced by about $293 "
       f"(${restated_total - booked_total:.2f})",
       290.0 < (restated_total - booked_total) < 296.0)

    # The gate authorised these on the declared basis. Whatever the ledger
    # says, it must not contradict the decision's sign.
    for qty, mark, fill, _ in rows:
        basis_usd = qty * true_basis
        net = g._grid_slice_net_pnl(qty, true_basis, fill, sell_leg)
        pct = net / basis_usd
        ok(f"the booked outcome clears the parked floor the gate tested it "
           f"against ({pct*100:.2f}% vs {g.GRID_PARKED_MIN_NET_PCT*100:.1f}%)",
           pct >= g.GRID_PARKED_MIN_NET_PCT)


# ------------------------------------------------------- 5. the two books
def test_realized_splits_into_two_books():
    if True:
        import database
        import crypto_grid_bot as g
        from models import CryptoGridTradeHistory

        async def run():
            await database.init_db()
            async with database.get_session_factory()() as s:
                s.add_all([
                    # the grid's own book
                    CryptoGridTradeHistory(bot_name="crypto_grid_1",
                                           product_id="DOGE-USD", pnl=1.00,
                                           exit_reason="profit_target"),
                    CryptoGridTradeHistory(bot_name="crypto_grid_1",
                                           product_id="DOGE-USD", pnl=-0.40,
                                           exit_reason="stop_loss"),
                    CryptoGridTradeHistory(bot_name="crypto_grid_2",
                                           product_id="NEAR-USD", pnl=0.61,
                                           exit_reason="parked_sell"),
                    # a legacy row from before exit_reason existed
                    CryptoGridTradeHistory(bot_name="crypto_grid_2",
                                           product_id="NEAR-USD", pnl=0.29,
                                           exit_reason=None),
                    # inherited inventory resolving
                    CryptoGridTradeHistory(bot_name="crypto_grid_21",
                                           product_id="ZEC-USD", pnl=118.05,
                                           exit_reason=g.ADOPTED_EXIT_REASON),
                    CryptoGridTradeHistory(bot_name="crypto_grid_21",
                                           product_id="ZEC-USD", pnl=25.23,
                                           exit_reason=g.ADOPTED_EXIT_REASON),
                ])
                await s.commit()
            return await g.get_grid_trade_history(limit_recent=10)

        h = asyncio.run(run())

        ok("the blended cash total is still published unchanged",
           h["total_realized_pnl"] == 144.78)
        ok("the grid's own book is the four trips it chose both ends of",
           h["realized_own_trades"] == 4)
        ok("and sums only those four (+$1.50)",
           h["realized_own_usd"] == 1.50)
        ok("a legacy row with no exit_reason counts as the grid's own, not "
           "as inherited", h["realized_own_usd"] == 1.50)
        ok("the inherited book is named separately",
           h["realized_adopted_trades"] == 2
           and h["realized_adopted_usd"] == 143.28)
        ok("the two books add back to the total exactly - nothing is dropped "
           "and nothing is counted twice",
           round(h["realized_own_usd"] + h["realized_adopted_usd"], 2)
           == h["total_realized_pnl"])
        ok("own + adopted trade counts account for every closed row",
           h["realized_own_trades"] + h["realized_adopted_trades"]
           == h["total_trade_count"])

        # The point of the split: a large inherited exit must not move the
        # grid's own figure in EITHER direction. +$143 of inherited gain is
        # as misleading about the strategy as -$149 of phantom loss was.
        ok("rows already booked at the declared basis are not restated twice",
           h["realized_adopted_restated_usd"] is None
           or h["realized_adopted_restated_count"] == 0)

        ok("a $143 inherited gain leaves the grid's own +$1.50 untouched",
           h["realized_own_usd"] == 1.50 and h["total_realized_pnl"] > 100)


# ------------------------------------------- 6. the pages stopped blending
def test_the_pages_lead_with_the_grid_s_own_book():
    live = (REPO / "live_ops_dashboard.html").read_text()
    ok("Live Ops no longer claims realized cannot go negative",
       "realized cannot go negative" not in live)
    ok("the Taken leg names its two books",
       "takenSplit" in live and "realized_adopted_usd" in live)
    ok("the money panel leads with the grid's own realized figure",
       "The grid&#39;s own realized P&amp;L" in live
       or "The grid's own realized P&amp;L" in live)

    ft = (REPO / "family_tree_dashboard.html").read_text()
    ok("the P&L-by-coin header reads the own book, not every closed row",
       "realized_own_usd" in ft and "realized_own_trades" in ft)

    ok("the pages show the restated figure when the basis could price it",
       "realized_adopted_restated_usd" in live
       and "realized_adopted_restated_usd" in ft)
    ok("and say which basis the number in front of the reader came from",
       "priced at the adoption-day mark" in live
       and "priced at the adoption-day mark" in ft)
    ok("Live Ops still shows what was originally booked, so nothing is "
       "quietly replaced",
       "As originally booked" in live)

    router = (REPO / "routers" / "trading_dashboard.py").read_text()
    ok("the Live Ops headline passes the split through",
       "realized_adopted_trades" in router)
    ok("and an unavailable split is never backfilled with the blend",
       "never backfilled with the blended figure" in router)


# ------------------------------------------- 6b. the TRADED TODAY tile
# The tile the account owner circled. It read -$311.24 over "4 trades today"
# on a day the account took in $1,268.45 of cash and gained $297.78 against
# what was really paid. Every one of the four was an inherited ZEC slice.
def test_traded_today_reports_the_grid_s_own_trading():
    ft = (REPO / "family_tree_dashboard.html").read_text()
    i = ft.index("var el = document.getElementById('strip-today')")
    j = ft.index("var branches = (d && d.branches) || []", i)
    tile = ft[i:j]

    ok("the tile splits today's closes on exit_reason",
       "exit_reason !== 'adopted_exit'" in tile
       and "exit_reason === 'adopted_exit'" in tile)
    ok("the headline number is the grid's own trading",
       "var net = sumPnl(ownToday);" in tile)
    ok("the inherited closes are still shown, not dropped",
       "inheritedToday.length" in tile and "plus " in tile)
    ok("and are labelled as coin the grid did not buy",
       "the grid did not buy" in tile)
    ok("a day with only inherited closes says the grid placed none of its own",
       "the grid placed no round trip of its own today" in tile)
    ok("zero is not painted green - a flat day is flat",
       "net > 0 ? TV_GREEN" in tile)
    ok("the incomplete-window warning still wraps the whole line",
       "window starts inside today" in tile)
    # the regression itself: no bare sum over every close
    ok("no reduce over the unsplit list survives",
       "todays.reduce(" not in tile)


# ----------------------- 5b. the four rows written before the fix deployed
def test_pre_fix_rows_restate_on_read():
    # This runs against the same database the previous test seeded (the
    # module pins one DATABASE_URL before any import, deliberately - see the
    # note at the top). So it measures the DELTA these four rows make, not
    # an absolute, and an absolute assertion here would be reading the
    # fixture above as much as the code under test.
    import database
    import crypto_grid_bot as g
    from models import CryptoGridTradeHistory

    g.GRID_TRUE_COST_BASIS = g._parse_true_cost_basis("ZEC-USD:1013.80")

    # the four real rows, exactly as the live ledger holds them
    ROWS = [
        (0.08036146, 1659.17, 1331.94, -26.72),
        (0.37816667, 1650.61, 1330.02, -123.21),
        (0.37816667, 1650.61, 1329.95, -123.24),
        (0.11690454, 1650.61, 1330.17, -38.07),
    ]

    async def run():
        before = await g.get_grid_trade_history(limit_recent=10)
        async with database.get_session_factory()() as s:
            s.add_all([
                CryptoGridTradeHistory(
                    bot_name="crypto_grid_21", product_id="ZEC-USD",
                    qty=q, entry_price=e, exit_price=x, pnl=p,
                    exit_reason=g.ADOPTED_EXIT_REASON)
                for q, e, x, p in ROWS
            ])
            await s.commit()
        return before, await g.get_grid_trade_history(limit_recent=10)

    before, after = asyncio.run(run())

    d_booked = after["realized_adopted_usd"] - before["realized_adopted_usd"]
    # When nothing was restatable the restated figure is None - "nothing to
    # restate", not "$0.00" - and the honest baseline is then the booked sum,
    # because an un-restated row restates to itself.
    _base = (before["realized_adopted_restated_usd"]
             if before["realized_adopted_restated_usd"] is not None
             else before["realized_adopted_usd"])
    d_restated = after["realized_adopted_restated_usd"] - _base
    d_count = (after["realized_adopted_restated_count"]
               - before["realized_adopted_restated_count"])

    ok(f"all four rows are recognised as restatable (+{d_count})", d_count == 4)
    ok(f"as booked they add about -$311 (${d_booked:.2f})",
       abs(d_booked - -311.24) < 0.02)
    ok(f"restated at what was really paid they ADD money (${d_restated:+.2f})",
       d_restated > 0)
    ok("the sign flips - that is the whole point", d_booked < 0 < d_restated)
    ok(f"the correction is about $609 across the four "
       f"(${d_restated - d_booked:.2f})",
       605.0 < (d_restated - d_booked) < 613.0)
    ok("a row already booked at the declared basis is never restated twice",
       before["realized_adopted_restated_count"] == 0)

    # The stored rows must still read exactly what was booked. Scoped to the
    # four this test wrote - the fixture above seeded others.
    stored = {round(float(r["pnl"]), 2) for r in after["recent_trades"]
              if r.get("exit_reason") == g.ADOPTED_EXIT_REASON
              and r.get("exit_price") is not None}
    ok("the stored rows are untouched - restatement happens on READ",
       {p for _, _, _, p in ROWS} <= stored)
    ok("the grid's own book is not moved by any of it",
       after["realized_own_usd"] == before["realized_own_usd"] == 1.50)


# ------------------------- 6c. the open mark agrees with the gate that sells
def test_open_marks_use_the_same_basis_as_the_sell_gate():
    import ast as _ast
    tree = _ast.parse(SRC)
    fn = next((n for n in _ast.walk(tree)
               if isinstance(n, _ast.AsyncFunctionDef)
               and n.name == "get_grid_status"), None)
    ok("get_grid_status is where the page's marks are built", fn is not None)
    if fn is None:
        return
    body = _ast.unparse(fn)

    ok("the open mark is priced at the slice's sell basis, not its recorded "
       "entry",
       "_mark_basis = sell_basis_for_slice(s, product_id=b.product_id)" in body
       and "_grid_slice_net_pnl(s.qty, _mark_basis, current_price" in body)
    ok("the percentage uses that same basis, so the dollar and the percent "
       "cannot disagree",
       "cost_basis = s.qty * _mark_basis" in body)
    ok("the page says which basis each mark was taken against",
       "'marked_against'" in body or '"marked_against"' in body)
    ok("and flags when that is a declared basis rather than the recorded entry",
       "marked_against_is_declared" in body)

    # THE SAFETY LIMIT IS NOT LOOSENED AS A SIDE EFFECT.
    # _grid_branch_real_equity drives the live drawdown breaker and the
    # peak_equity ratchet. Marking a position higher would make the breaker
    # LESS likely to trip.
    eq = next((n for n in _ast.walk(tree)
               if isinstance(n, _ast.FunctionDef)
               and n.name == "_grid_branch_real_equity"), None)
    ok("the drawdown breaker's equity still measures against the recorded "
       "entry - a display fix never loosens a circuit breaker",
       eq is not None and "s.entry_price" in _ast.unparse(eq)
       and "sell_basis_for_slice" not in _ast.unparse(eq))
    ok("and the reason is written where the change was made",
       "THE DRAWDOWN BREAKER IS DELIBERATELY NOT CHANGED" in SRC)


# ------------------------------- 7. the tag still keeps it out of the record
def test_the_grid_s_record_still_excludes_inherited_exits():
    ok("get_grid_performance_metrics still excludes ADOPTED_EXIT_REASON",
       "!= ADOPTED_EXIT_REASON" in SRC)
    ok("an inherited gain is excluded for the same reason an inherited loss "
       "was - the grid did not choose the entry",
       "in either direction" in SRC)


if __name__ == "__main__":
    for t in (test_identity_everywhere_else,
              test_every_consumer_uses_the_booked_basis,
              test_the_false_promise_is_gone,
              test_the_two_zec_rows_restate_positive,
              test_realized_splits_into_two_books,
              test_pre_fix_rows_restate_on_read,
              test_the_pages_lead_with_the_grid_s_own_book,
              test_traded_today_reports_the_grid_s_own_trading,
              test_open_marks_use_the_same_basis_as_the_sell_gate,
              test_the_grid_s_record_still_excludes_inherited_exits):
        print(f"\n{t.__name__}")
        t()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
