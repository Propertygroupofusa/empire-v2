// What the grid bot is waiting for, drawn on the price chart and written out.
//
// The owner, 2026-10-14: "I don't have a grid like this that's showing me what
// the bots are doing ... how the bots are literally deciding what they're going
// to do with my money." The levels are computed from the numbers the bot
// decides with, using the rules read from crypto_grid_bot.py:
//   BUY   when price <= reference * (1 - step) and a rung is free
//   SELL  when price >= reference * (1 + step), the OLDEST rung that nets
//         >= 0.15% after both fee legs; none clears -> it waits
//   PARKED when every rung is used, a rung netting >= 1% sells
//   never below cost
// Real figures from the live fleet on 2026-10-14 are used so a rule that drifts
// from the bot shows up as a failing number here, not as a plausible picture.
const path = require('path');
const A = require(path.join(__dirname, 'static', 'advanced_charts.js'));

let fails = [];
const ok = (label, cond, detail) => {
    if (cond) console.log('  PASS  ' + label);
    else { console.log('  FAIL  ' + label + (detail ? '\n        ' + detail : '')); fails.push(label); }
};
const near = (a, b, eps) => a !== null && b !== null && Math.abs(a - b) < (eps || 1e-6);

// BTC, live 14 Oct 2026: two rungs of three used, last fill 81,629.57.
const btc = {
    product_id: 'BTC-USD', current_price: 81639.89, reference_price: 81629.57,
    grid_pct: 0.012604496128065525, num_levels: 3, allocated_usd: 125.85,
    slices: [
        { entry_price: 84149.61, unrealized_net_pct: -0.0367, opened_at: '2026-10-07T02:02:30Z' },
        { entry_price: 81629.57, unrealized_net_pct: -0.0069, opened_at: '2026-10-14T16:09:00Z' },
    ],
};

console.log('\n[1] the levels are the bot\'s own arithmetic');
const L = A.botLevels(btc);
ok('reads fine', L && L.ok === true);
ok('buy level = reference * (1 - step)', near(L.buyAt, 81629.57 * (1 - 0.012604496128065525), 1e-6), String(L.buyAt));
ok('sell level = reference * (1 + step)', near(L.sellAt, 81629.57 * (1 + 0.012604496128065525), 1e-6), String(L.sellAt));
ok('2 of 3 rungs used, not full', L.open === 2 && L.full === false);
ok('no "all rungs used" sell line while a rung is free', L.parkedAt === null);

console.log('\n[2] the sell names the first rung that clears fees at that price, oldest first');
// sellAt ~ 82,658.4. The oldest rung (84,149.61) would be a loss there, so it is skipped;
// the 81,629.57 rung nets 1.2604% - 0.7% = +0.5604%.
const sell = L.story.find(t => t.indexOf('It sells one rung') === 0) || '';
ok('story names the 81,629.57 rung, not the older underwater one', sell.indexOf('$81630') !== -1 || sell.indexOf('81630') !== -1, sell);
ok('and states the profit it would bank', /bank \+0\.5\d%/.test(sell), sell);

console.log('\n[3] when no rung would clear fees, it says it keeps waiting');
const waiting = A.botLevels(Object.assign({}, btc, {
    slices: [{ entry_price: 84149.61, unrealized_net_pct: -0.0367, opened_at: '2026-10-07T02:02:30Z' }] }));
const w = waiting.story.find(t => t.indexOf('It sells one rung') === 0) || '';
ok('no rung clears -> "keeps waiting"', w.indexOf('keeps waiting') !== -1, w);

console.log('\n[4] a flat branch buys on a drop from its tracked high');
const flat = A.botLevels(Object.assign({}, btc, { slices: [] }));
ok('no sell line when nothing is held', flat.sellAt === null && !flat.lines.some(l => l.kind === 'sell'));
ok('buy line present', flat.lines.some(l => l.kind === 'buy'));
ok('says it holds nothing', flat.story.some(t => t.indexOf('Holding nothing here') === 0));

console.log('\n[5] a full branch does not buy, and may sell a +1% rung (the parked route)');
const full = A.botLevels(Object.assign({}, btc, { num_levels: 2 }));
ok('full branch has no buy line', full.buyAt === null && !full.lines.some(l => l.kind === 'buy'));
ok('says it will not buy more', full.story.some(t => t.indexOf('Every rung is used') === 0));
// parked trigger: lowest entry * (1 + 1% + 0.7%)
ok('parked level = lowest entry * 1.017', near(full.parkedAt, 81629.57 * 1.017, 1e-6), String(full.parkedAt));
ok('parked story promises +1.00% profit', full.story.some(t => t.indexOf('+1.00% profit') !== -1));

console.log('\n[6] it never promises a sale below cost');
ok('every story ends with the never-below-cost line',
   [L, waiting, flat, full].every(x => x.story[x.story.length - 1].indexOf('never sells below cost') !== -1));

console.log('\n[7] rungs held are listed oldest first with their current result');
const held = L.story.find(t => t.indexOf('Rungs held') === 0) || '';
ok('oldest rung first', held.indexOf('84150') !== -1 && held.indexOf('84150') < held.indexOf('81630'), held);
ok('with its result in plain signed percent', held.indexOf('−3.67% now') !== -1, held);

console.log('\n[8] unreadable input is said, never drawn as a guess');
const bad = A.botLevels({ product_id: 'X', current_price: null, reference_price: 1, grid_pct: 0.02, num_levels: 3, slices: [] });
ok('not ok, no lines', bad.ok === false && bad.lines.length === 0);
ok('says so', /could not be read/.test(bad.story[0]));
ok('no branch -> null, not a crash', A.botLevels(null) === null);

console.log('\n[9] lines far from the price are left off the chart (they stay in the story)');
const vis = A.visibleLines([
    { v: 100, kind: 'buy', label: 'a' }, { v: 119, kind: 'sell', label: 'b' },
    { v: 121, kind: 'sell', label: 'c' }, { v: 79, kind: 'held', label: 'd' }], 100);
ok('within 20% kept, beyond dropped', vis.map(l => l.label).join('') === 'ab', JSON.stringify(vis));

console.log('\n[9b] rungs bought at nearly the same price are one line, so ten rungs do not stack ten labels');
const many = A.botLevels(Object.assign({}, btc, { num_levels: 10, slices: [
    { entry_price: 1.54, unrealized_net_pct: -0.14, opened_at: '2026-10-07T01:00:00Z' },
    { entry_price: 1.5395, unrealized_net_pct: -0.14, opened_at: '2026-10-07T02:00:00Z' },
    { entry_price: 1.5388, unrealized_net_pct: -0.14, opened_at: '2026-10-07T03:00:00Z' },
    { entry_price: 1.50, unrealized_net_pct: -0.12, opened_at: '2026-10-08T03:00:00Z' }] }));
const heldLines = many.lines.filter(l => l.kind === 'held');
ok('three near-equal rungs collapse to one line, the distant one stays separate', heldLines.length === 2, JSON.stringify(heldLines));
ok('the grouped line says how many', heldLines[0].label.indexOf('\u00d73') !== -1, heldLines[0].label);
ok('the written story still lists every rung it can fit', many.story.some(t => t.indexOf('Rungs held') === 0));

console.log('\n[10] nothing here can act');
const src = require('fs').readFileSync(path.join(__dirname, 'static', 'advanced_charts.js'), 'utf8');
ok('no network or order call in the chart module',
   !/fetch\(|XMLHttpRequest|postGuarded|\/api\//.test(src));

console.log(fails.length ? '\n' + fails.length + ' FAILED' : '\nALL PASS');
process.exit(fails.length ? 1 : 0);
