// Fleet readiness: why the fleet did or did not trade.
//
// The rule is copied from crypto_grid_bot.py:7050 -
//     price >= branch.reference_price * (1 + grid_pct)
// and the two ways an approximation of it goes wrong are both pinned
// here, because both were live defects in my own first pass:
//   1. using each SLICE's entry price instead of the BRANCH reference
//   2. hardcoding 3% when grid_pct is per-branch (BTC 1.43%, ETH 1.78%)
const path = require('path');
const F = require(path.join(__dirname, 'static', 'fleet_readiness.js'));

let fails = [];
const ok = (l, c, d) => { if (c) console.log('  PASS  ' + l);
  else { console.log('  FAIL  ' + l + (d ? '\n        ' + d : '')); fails.push(l); } };
const near = (a,b,e) => Math.abs(a-b) < (e||1e-9);

const br = o => Object.assign({
  bot_name:'b', product_id:'FOO-USD', reference_price:100, current_price:100,
  grid_pct:0.03, allocated_usd:100, slices:[{}]}, o);

console.log('\n[1] the trigger is reference x (1 + grid_pct)');
let r = F.readiness([br({reference_price:100, current_price:103, grid_pct:0.03})], [])[0];
ok('exactly at trigger is READY', r.state === 'READY' && near(r.gapPct, 0), JSON.stringify(r));
r = F.readiness([br({reference_price:100, current_price:102.99, grid_pct:0.03})], [])[0];
ok('a hair under is WAITING', r.state === 'WAITING' && r.gapPct < 0, String(r.gapPct));
r = F.readiness([br({reference_price:100, current_price:110, grid_pct:0.03})], [])[0];
ok('well past is READY', r.state === 'READY' && near(r.gapPct, 6.7961165, 1e-5), String(r.gapPct));

console.log('\n[2] grid_pct is PER BRANCH - hardcoding 3% misreports live branches');
// Live right now: BTC 1.43%, ETH 1.78%, SOL 2.83%.
const btc = F.readiness([br({product_id:'BTC-USD', reference_price:100,
                             current_price:102, grid_pct:0.0143})], [])[0];
ok('BTC at 1.43% step: 102 is PAST a 101.43 trigger',
   btc.state === 'READY', JSON.stringify(btc));
const hard = (100 * 1.03);   // what a hardcoded 3% would have used
ok('...and a hardcoded 3% would have called it WAITING',
   102 < hard, `${102} vs ${hard}`);
ok('the per-branch step is carried through', near(btc.gridPct, 0.0143));

console.log('\n[3] a phantom branch is LOCKED, never "waiting"');
r = F.readiness([br({product_id:'QNT-USD', reference_price:100,
                     current_price:175, allocated_usd:149.91})], ['QNT'])[0];
ok('locked, not ready', r.state === 'LOCKED', r.state);
ok('but its gap is still reported', r.gapPct > 0, String(r.gapPct));
const s = F.summarise([r]);
ok('counted as past-trigger-but-locked', s.lockedPastTrigger === 1);
ok('and its capital is counted', near(s.lockedUsd, 149.91));
ok('the verdict names it as the fixable item',
   /cannot sell/.test(F.verdict(s)) && /fixable/.test(F.verdict(s)), F.verdict(s));

console.log('\n[4] UNREADABLE is a third state, never "ready" and never 0');
for (const bad of [{current_price:null}, {reference_price:undefined},
                   {grid_pct:'x'}, {reference_price:0}, {current_price:NaN}]) {
  const row = F.readiness([br(bad)], [])[0];
  ok(`${JSON.stringify(bad)} -> UNREADABLE`, row.state === 'UNREADABLE',
     row.state + ' gap=' + row.gapPct);
  ok('   ...and gapPct stays null, not 0', row.gapPct === null);
}
ok('an all-unreadable fleet says UNKNOWN, not idle',
   /UNKNOWN/.test(F.verdict(F.summarise(F.readiness([br({current_price:null})], [])))));

console.log('\n[5] a branch with no slices has nothing to sell');
r = F.readiness([br({slices:[]})], [])[0];
ok('NO_SLICES, not WAITING', r.state === 'NO_SLICES', r.state);
ok('and it is not counted as waiting',
   F.summarise([r]).waiting === 0);

console.log('\n[6] a real payload, from a COMMITTED fixture');
// This used to read /tmp/gs2.json - a file outside the repo. It was a live
// snapshot when the assertions were written (23 branches, 8 locked,
// $987.22); a later poll overwrote it with a different fleet and the
// numbers stopped matching. On a fresh checkout the file is absent and the
// whole suite dies on readFileSync. A test whose fixture can be overwritten
// by unrelated work is not pinning anything.
const live = JSON.parse(require('fs').readFileSync(
  path.join(__dirname, 'fixtures', 'grid_status_sample.json'), 'utf8'));
// OWNED, not AVAILABLE - the same choice loadFleetReadiness makes.
const rows = F.readiness(live.branches,
  (live.backing_owned.unbacked || []).map(u => u.asset));
const S = F.summarise(rows);
ok('every branch in the fixture is classified',
   S.total === live.branches.length, `${S.total} of ${live.branches.length}`);
ok('every row got a state', rows.every(r => r.state));
// Derived from the fixture, not frozen: a recount cannot silently drift.
ok('the state counts add up to the total',
   S.ready + S.noProfit + S.waiting + S.locked + S.noSlices + S.unreadable === S.total,
   JSON.stringify(S));

console.log('\n[6b] OWNED vs AVAILABLE, the error that cost four hours');
// backing (AVAILABLE) names five assets; backing_owned (OWNED) names one.
// SOL, LINK, ALGO and ACH own every unit they claim and are merely sitting
// under the fleet's own resting sell orders. Classifying those as LOCKED
// put $541.37 behind a red badge and, in the buy gate, refused four healthy
// branches for four hours.
const availNames = (live.backing.unbacked || []).map(u => u.asset).sort();
const ownedNames = (live.backing_owned.unbacked || []).map(u => u.asset).sort();
ok('the fixture really does contain the trap (the two lists differ)',
   availNames.length > ownedNames.length, `${availNames} vs ${ownedNames}`);
ok('only the genuinely-short asset is LOCKED',
   rows.filter(r => r.state === 'LOCKED').map(r => r.asset).sort().join(',')
     === ownedNames.join(','),
   rows.filter(r => r.state === 'LOCKED').map(r => r.asset).join(','));
for (const a of availNames.filter(x => !ownedNames.includes(x))) {
    const r = rows.find(x => x.asset === a);
    ok(`${a} is owned in full and is NOT locked`, r && r.state !== 'LOCKED',
       r && r.state);
}

console.log('\n[6c] A TRIGGER IS NOT A SALE - the bot\'s second gate');
// The panel used to call a branch READY on the price trigger alone and say
// "should sell on the next cycle". Live on 2026-10-04 it showed 2 such
// branches while the bot logged "real rise trigger fired ... but no open
// slice would net a real profit at this price - holding every slice".
// Neither could sell: ONDO's only slice was -8.98% net of fees.
const past = rows.filter(r => r.gapPct !== null && r.gapPct >= 0
                              && r.state !== 'LOCKED' && r.state !== 'NO_SLICES');
ok('the fixture has at least one branch past its trigger', past.length > 0,
   String(past.length));
for (const r of past) {
    const b = live.branches.find(x => x.product_id === r.product);
    const anyGreen = (b.slices || []).some(s => Number(s.unrealized_net_usd) > 0);
    ok(`${r.asset}: past trigger with ${anyGreen ? 'a' : 'NO'} profitable slice `
       + `-> ${anyGreen ? 'READY' : 'NO_PROFIT'}`,
       r.state === (anyGreen ? 'READY' : 'NO_PROFIT'), r.state);
}
ok('nothing is called READY unless a slice actually nets a profit',
   rows.filter(r => r.state === 'READY')
       .every(r => (live.branches.find(x => x.product_id === r.product).slices || [])
                     .some(s => Number(s.unrealized_net_usd) > 0)));
ok('a NO_PROFIT branch is not counted as waiting on price',
   S.waiting === rows.filter(r => r.state === 'WAITING').length);
ok('the verdict explains the hold rather than promising a sale',
   S.noProfit === 0 || /no-loss rule working/.test(F.verdict(S)), F.verdict(S));

console.log('\n[6d] the profit test is tri-state, never assumed');
const noFigures = F.readiness([{product_id:'X-USD', reference_price:100,
  current_price:103, grid_pct:0.03, slices:[{qty:1, entry_price:1}]}], [])[0];
ok('a slice with no net figure reads UNKNOWN, not false',
   noFigures.sellable === null, String(noFigures.sellable));
ok('...and an unknown does not get demoted to NO_PROFIT',
   noFigures.state === 'READY', noFigures.state);
const oneGreen = F.readiness([{product_id:'Y-USD', reference_price:100,
  current_price:103, grid_pct:0.03,
  slices:[{unrealized_net_usd:-5}, {unrealized_net_usd:0.01}]}], [])[0];
ok('one profitable slice among losers is enough, as in the bot',
   oneGreen.state === 'READY' && oneGreen.sellable === true, oneGreen.state);
const allRed = F.readiness([{product_id:'Z-USD', reference_price:100,
  current_price:103, grid_pct:0.03,
  slices:[{unrealized_net_usd:-5}, {unrealized_net_usd:-0.01}]}], [])[0];
ok('all-negative past the trigger is NO_PROFIT, not READY',
   allRed.state === 'NO_PROFIT' && allRed.sellable === false, allRed.state);
ok('exactly zero is not a profit (the bot tests > 0)',
   F.readiness([{product_id:'Q-USD', reference_price:100, current_price:103,
     grid_pct:0.03, slices:[{unrealized_net_usd:0}]}], [])[0].state === 'NO_PROFIT');

console.log('\n[7] it cannot move money');
// COMMENTS STRIPPED FIRST. The first version of this check matched the
// word "order" inside the module's own comment saying it places no
// order - a test failing on the sentence that documents the property it
// is testing. Reading control flow out of comment text is the same
// defect that bit the auto_trim_worker check.
let src = require('fs').readFileSync(
  path.join(__dirname,'static','fleet_readiness.js'),'utf8');
src = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
ok('the comment-stripper actually removed the banner',
   !src.includes('THE QUESTION THIS ANSWERS'), 'comments survived the strip');
for (const bad of ['fetch(','XMLHttpRequest','submit_order','place_order',
                   'POST','.post(','await '])
  ok(`no ${bad} in the executable code`, !src.includes(bad));

console.log('\n' + (fails.length ? fails.length + ' FAILED: ' + fails.join('; ') : 'ALL PASS'));
process.exit(fails.length ? 1 : 0);
