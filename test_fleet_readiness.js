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

console.log('\n[6] the real fleet, from the live payload');
const live = JSON.parse(require('fs').readFileSync('/tmp/gs2.json','utf8'));
const rows = F.readiness(live.branches, (live.backing.unbacked||[]).map(u=>u.asset));
const S = F.summarise(rows);
ok('all 23 branches classified', S.total === 23, String(S.total));
ok('8 locked', S.locked === 8, String(S.locked));
ok('locked capital is $987.22', near(S.lockedUsd, 987.22, 0.01), String(S.lockedUsd));
ok('nothing is ready', S.ready === 0, String(S.ready));
ok('every row got a state', rows.every(r => r.state));
ok('QNT is locked AND past trigger',
   rows.some(r => r.asset === 'QNT' && r.state === 'LOCKED' && r.gapPct > 60),
   JSON.stringify(rows.find(r => r.asset === 'QNT')));

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
