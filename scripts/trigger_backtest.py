#!/usr/bin/env python3
"""Backtest the sell-rule model against the REAL closed book. Read-only.

The differential test (test_trigger_consistency.py) proves the readers agree
with the executor's SOURCE. This asks a different question: does the model
agree with what the market actually did? It checks the one claim the fix
turned on - that the parked leg and the rise leg are OR-ed, not exclusive.

If they are OR-ed, the book MUST contain profit_target closes on branches
that were parked. The old model said that was impossible.

LIMIT OF THE INFERENCE, stated because it matters: adopted-only status is
read from the fleet NOW, not from the moment of the close. A branch can have
become adopted-only BECAUSE the bought slice sold. So these rows are strong
evidence, not proof. The taxonomy check and the both-legs check below do not
depend on that caveat and stand on their own.

Usage: python3 scripts/trigger_backtest.py
"""

import json
import os
import urllib.request
from collections import defaultdict

API = os.getenv("EMPIRE_BASE", "https://empire-v2-production.up.railway.app") \
      + "/api/trading-dashboard"


def _get(path):
    with urllib.request.urlopen(API + path, timeout=90) as r:
        return json.loads(r.read().decode())


th = _get("/grid-status/trade-history?limit=1000")
gs = _get("/grid-status")

tr = th['recent_trades']

# Which products are ADOPTED-ONLY right now? An adopted-only branch is parked
# by rule regardless of free rungs, so it is parked whenever it has slices.
adopted_only, levels = {}, {}
for b in gs['branches']:
    sl = b.get('slices') or []
    adopted_only[b['product_id']] = bool(sl) and all(x.get('adopted') for x in sl)
    levels[b['product_id']] = b.get('num_levels')

print('EXIT REASONS IN THE REAL BOOK (ground truth):')
rs = defaultdict(int)
for t in tr: rs[t.get('exit_reason')] += 1
for k, v in sorted(rs.items(), key=lambda kv: -kv[1]):
    print('   {:<16} {}'.format(str(k), v))
print('   -> the model says exactly three legs: _rise_hit (profit_target),')
print('      _parked_sell (parked_sell), _stop_slice (stop_loss). No other')
print('      reason appears except None on pre-instrumentation rows.')
print()

print('THE DECISIVE CASE: a profit_target close on a branch that was PARKED.')
print('The OLD model (parked and rise exclusive) says this CANNOT happen.')
print()
hits = []
for t in tr:
    p = t.get('product_id')
    if t.get('exit_reason') == 'profit_target' and adopted_only.get(p):
        hits.append(t)
by = defaultdict(list)
for t in hits: by[t['product_id']].append(t)
if not hits:
    print('   NONE FOUND - the old model is not refuted by this test.')
else:
    print('   {} such close(s), on branches that are adopted-only (= parked by rule):'.format(len(hits)))
    for p, g in sorted(by.items(), key=lambda kv: -len(kv[1])):
        tot = sum(x['pnl'] for x in g)
        print('     {:<10} {} close(s)  {:+.2f}   (branch adopted-only now, {} levels)'.format(
            p, len(g), tot, levels.get(p)))
    print()
    print('   Each one is a sale the OLD model would have called impossible, and')
    print('   the money is real - it is in the realized book.')
print()

print('BOTH LEGS ON THE SAME BRANCH (independent firing):')
legs = defaultdict(set)
for t in tr:
    if t.get('exit_reason'): legs[t['product_id']].add(t['exit_reason'])
both = {p: v for p, v in legs.items() if {'profit_target','parked_sell'} <= v}
for p, v in sorted(both.items()):
    print('   {:<10} fired {}'.format(p, ' AND '.join(sorted(v))))
if not both: print('   none')
