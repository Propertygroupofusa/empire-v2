// The readiness panel's percentage must be coloured by the NUMBER's sign,
// not by the row's state. Before this, every LOCKED row printed red, so
// QNT's +41.39% (price is PAST its sell trigger) looked identical to
// PRIME's -12.97% (still has to climb that far). Opposite facts, same colour.
const fs = require('fs');
const src = fs.readFileSync('/home/user/empire-v2/family_tree_dashboard.html','utf8');

// Pull the real expression out of the shipped file - not a retyped copy.
const m = src.match(/const gapCol = ([\s\S]*?);\n/);
if (!m) { console.log('FAIL: gapCol not found in the page'); process.exit(1); }
const gapColFn = new Function('r', 'return ' + m[1].replace(/\n\s*/g,' ') + ';');

let fail = 0;
const ok = (label, cond, detail='') => {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}${cond||!detail ? '' : '   -> '+detail}`);
  if (!cond) fail++;
};

console.log('\n[1] sign decides the colour');
const cases = [
  ['QNT  +41.39 past trigger',  41.39, 'var(--green)'],
  ['PEPE  +2.33 past trigger',   2.33, 'var(--green)'],
  ['TIA   +1.42 past trigger',   1.42, 'var(--green)'],
  ['exactly 0',                  0,    'var(--green)'],
  ['SOL   -1.92 still to climb',-1.92, 'var(--red)'],
  ['ONDO  -0.27 still to climb',-0.27, 'var(--red)'],
  ['PRIME -12.97 still to climb',-12.97,'var(--red)'],
];
for (const [label, v, want] of cases) ok(label, gapColFn({gapPct:v}) === want, gapColFn({gapPct:v}));

console.log('\n[2] unreadable is never green');
const u = gapColFn({gapPct:null});
ok('null is not green', u !== 'var(--green)', u);
ok('null is not red either - it is its own verdict', u !== 'var(--red)', u);
ok('null reads gold (UNREADABLE)', u === 'var(--gold)', u);

console.log('\n[3] the number no longer inherits the row state');
const numCol = src.match(/text-align:right; font-size:0\.76em; color:\$\{(\w+)\}/);
ok('the percentage cell uses gapCol', numCol && numCol[1] === 'gapCol',
   numCol ? numCol[1] : 'cell not found');
ok('the STATE label still uses the state colour',
   /<span style="color:\$\{col\}; font-weight:700;">\$\{escText\(r\.state\)\}/.test(src));

console.log(`\n${fail === 0 ? 'ALL PASS' : fail + ' FAILED'}`);
process.exit(fail ? 1 : 0);
