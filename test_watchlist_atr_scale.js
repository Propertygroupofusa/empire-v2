// Every fraction on the watchlist must be scaled the same way.
//
// WHAT WAS WRONG
//
// The server sends atr_pct as a FRACTION, like every other ratio in that
// payload: BLUR 0.008265 means 0.83%. The table rendered it with
//
//     c.atr_pct.toFixed(2) + '%'
//
// and no * 100, so 0.83% printed as "0.01%" - exactly 100x too small. Its
// neighbours in the same template literal both scale correctly:
//
//     (c.coin_return * 100).toFixed(2) + '%'
//     (c.alpha * 100).toFixed(2) + 'pp'
//
// so ATR was the only column of the three that was wrong, which is why it
// read 0.00% on nearly every row and looked like dead data rather than a
// formatting slip. The account owner reads that column to judge which
// coins are moving. A volatility column pinned at 0.00% says "nothing is
// volatile" about a market where plenty is.
//
// This checks the template text rather than running the page: the renderer
// is a string literal inside an onclick-driven function with no exported
// seam, and the defect is entirely in how the number is formatted.
const fs = require('fs');
const path = require('path');

const checks = [];
const ok = (label, cond, detail) =>
    checks.push([detail ? `${label}  -- ${detail}` : label, !!cond]);

const SRC = fs.readFileSync(
    path.join(__dirname, 'crypto_selection_backtest.html'), 'utf8');

// Pull the three sibling cells out of the row template.
const grab = (name) => {
    const re = new RegExp('c\\.' + name + '[^\\n]*', 'g');
    return (SRC.match(re) || []).join(' | ');
};

console.log('\n[1] ATR is scaled like its siblings');
const atr = grab('atr_pct');
ok('the ATR cell exists', atr.length > 0);
ok('ATR multiplies by 100 before toFixed',
   /c\.atr_pct\s*\*\s*100\s*\)\s*\.toFixed/.test(atr), atr.slice(0, 90));
ok('the unscaled form is gone',
   !/c\.atr_pct\.toFixed/.test(SRC));

console.log('\n[2] the siblings it has to agree with');
ok('coin_return scales by 100', /c\.coin_return \* 100\)\.toFixed/.test(SRC));
ok('alpha scales by 100', /c\.alpha \* 100\)\.toFixed/.test(SRC));

console.log('\n[3] the arithmetic, on the real values that exposed it');
// Live payload 2026-10-04: these are fractions, not percents.
const fmt = (v) => (v * 100).toFixed(2) + '%';
ok('BLUR 0.008265 renders as 0.83%, not 0.01%', fmt(0.008265426420863106) === '0.83%',
   fmt(0.008265426420863106));
ok('NEAR 0.004265 renders as 0.43%', fmt(0.004264808076306194) === '0.43%',
   fmt(0.004264808076306194));
ok('DOT 0.002139 renders as 0.21%', fmt(0.0021392617449664335) === '0.21%',
   fmt(0.0021392617449664335));
// The old formatter, for the record: it is what produced the dead column.
const oldFmt = (v) => v.toFixed(2) + '%';
ok('the OLD formatter really did flatten all three to 0.00/0.01%',
   ['0.01%', '0.00%', '0.00%'].join() ===
   [oldFmt(0.008265426420863106), oldFmt(0.004264808076306194),
    oldFmt(0.0021392617449664335)].join(),
   [oldFmt(0.008265426420863106), oldFmt(0.004264808076306194),
    oldFmt(0.0021392617449664335)].join());

const w = Math.max(...checks.map(c => c[0].length));
console.log();
for (const [l, p] of checks) console.log(`  [${p ? 'PASS' : 'FAIL'}] ${l.padEnd(w)}`);
const failed = checks.filter(c => !c[1]);
console.log(`\n  ${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);
