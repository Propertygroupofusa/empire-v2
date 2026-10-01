// The headline described a dormant bot as though it were the account, and a
// failed fetch showed a bare dash with no reason. Both sent the owner looking
// for problems that were not where the screen pointed.
const fs = require('fs');
const SRC = fs.readFileSync('family_tree_dashboard.html', 'utf8');
let FAILS = [];
const ok = (label, cond, got) => {
  if (cond) console.log(`  PASS  ${label}`);
  else { FAILS.push(label); console.log(`  FAIL  ${label}${got !== undefined ? '   got: ' + got : ''}`); }
};

// Pull renderNarrative out and run it for real, rather than grepping prose.
const fn = SRC.slice(SRC.indexOf('function renderNarrative'),
                     SRC.indexOf('function renderRetiredTreeNote'));
let text = null;
const stub = {
  getElementById: () => ({ set textContent(v) { text = v; }, get textContent() { return text; } }),
};
const make = new Function('document', 'fmtUsd',
  fn + '; return renderNarrative;')(stub, (n) => '$' + Number(n).toFixed(2));

console.log('\n[1] a DORMANT tree says so, and points at the grid');
make({ branch_count: 2, total_allocated_usd: 0,
       branches: [{ product_id: 'BTC-USD', allocated_usd: 0, position: null },
                  { product_id: 'ETH-USD', allocated_usd: 0, position: null }] });
ok('it says dormant', /dormant/i.test(text), text);
ok('it does NOT claim the account tracks $0.00',
   !/tracking \$0\.00 total/.test(text), text);
ok('it names the grid as the thing actually trading', /GRID/.test(text), text);
ok('it does not print "BTC is the largest at $0.00"',
   !/largest at \$0\.00/.test(text), text);

console.log('\n[2] a LIVE tree still reports itself, and is labelled');
make({ branch_count: 3, total_allocated_usd: 1500.5,
       branches: [{ product_id: 'BTC-USD', allocated_usd: 900, position: {} },
                  { product_id: 'ETH-USD', allocated_usd: 600.5, position: null }] });
ok('it reports the real total', /1500\.50/.test(text), text);
ok('it says which bot it is about', /Family tree/.test(text), text);
ok('it counts only branches holding a position', /1 currently holding/.test(text), text);
ok('the largest is named', /BTC is the largest/.test(text), text);

console.log('\n[3] an empty tree still short-circuits');
make({ branch_count: 0, total_allocated_usd: 0, branches: [] });
ok('no branches -> the original message', /No branches yet/.test(text), text);

console.log('\n[4] a failed /grid-status says WHICH failure');
ok('the error is captured, not swallowed', /gridErr\s*=\s*\(e && e\.message\)/.test(SRC));
ok('the tile reads "unreadable" on an error', /'unreadable'/.test(SRC));
ok('...and "not reported" when the grid simply had no figure',
   /'not reported'/.test(SRC));
ok('the reason is attached to the element', /el\.title = dash \? _gridNote/.test(SRC));
ok('and logged for a desktop console', /console\.warn\('\[dashboard\] \/grid-status failed:'/.test(SRC));
ok('a gap is still never printed as a zero',
   !/stat-total'\)\.textContent = fmtUsd\(0\)/.test(SRC));

console.log('\n' + (FAILS.length ? `${FAILS.length} FAILED: ${FAILS}` : 'ALL PASS'));
process.exit(FAILS.length ? 1 : 0);
