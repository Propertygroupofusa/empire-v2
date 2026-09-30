// Extracts the SHIPPED horizonCaptureTier from family_tree_dashboard.html
// and checks it against the owner's own example numbers.
const fs = require('fs');
const src = fs.readFileSync(__dirname + '/family_tree_dashboard.html', 'utf8');
const m = src.match(/function horizonCaptureTier\(pct\) \{[\s\S]*?\n\}/);
if (!m) { console.log('FAIL: function not found'); process.exit(1); }
const horizonCaptureTier = new Function(m[0] + '; return horizonCaptureTier;')();
let fails = 0;
const check = (name, ok) => { console.log((ok ? 'PASS' : 'FAIL') + ': ' + name); if (!ok) fails++; };
const GREEN = '#10b981', ORANGE = '#f59e0b';
const cases = [
  ['30m', 10.3, 'developing', ORANGE], ['2h', 32.9, 'developing', ORANGE],
  ['6h', 55.9, 'qualified', GREEN], ['24h', 82.4, 'excellent', GREEN],
  ['72h', 100.0, 'complete', GREEN],
  ['boundary 49.9', 49.9, 'developing', ORANGE], ['boundary 50', 50, 'qualified', GREEN],
  ['boundary 79.9', 79.9, 'qualified', GREEN], ['boundary 80', 80, 'excellent', GREEN],
  ['boundary 99.9', 99.9, 'excellent', GREEN], ['0%', 0, 'developing', ORANGE],
];
for (const [name, pct, tier, color] of cases) {
  const t = horizonCaptureTier(pct);
  check(`${name} (${pct}%) -> ${tier}`, t.tier === tier && t.color === color);
}
check('2h rising 32.9 -> 50 turns green with no code change', horizonCaptureTier(50).color === GREEN);
check('null is unmeasured, not orange', horizonCaptureTier(null).tier === 'unmeasured' && horizonCaptureTier(null).color !== ORANGE);
check('undefined is unmeasured', horizonCaptureTier(undefined).tier === 'unmeasured');
check('labels', horizonCaptureTier(20).label === 'Developing' && horizonCaptureTier(60).label === 'Qualified'
  && horizonCaptureTier(85).label === 'Excellent trajectory' && horizonCaptureTier(100).label === 'Complete capture');
check('bar uses the tier colour, not an inline rule', /background:\$\{t\.color\}/.test(src) && !/v >= 50 \? '#10b981'/.test(src));
check('legend sentence present', src.includes('50%+ = qualified positive capture rate &mdash; moving toward excellence'));
console.log(fails ? `${fails} FAILED` : 'ALL PASS');
process.exit(fails ? 1 : 0);
