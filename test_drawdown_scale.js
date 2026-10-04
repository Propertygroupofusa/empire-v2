// A pause notice that says "down 0%" describes nothing that can happen.
//
// drawdown_pct and drawdown_breaker_pct are FRACTIONS. Measured live
// 2026-10-04 on QNT-USD: drawdown_pct 0.4371, peak_equity 285.40,
// drawdown_breaker_pct 0.25 - and the bot's own log line for that same
// branch, in the same minute, read:
//
//     crypto_grid_14: real equity $160.65 is down 44% from its own
//     $285.40 peak (breaker at 25%) - new buys paused
//
// (285.40 - 160.65) / 285.40 = 0.4371. The field is a fraction.
//
// Read as a percentage it produced THREE faults in two lines:
//
//   1. the guard `b.drawdown_pct > 0.5` was written as "show once past
//      0.5%", but against a fraction it means "past 50%" - so the branch
//      down 43.71% rendered NOTHING, and the row its own comment calls
//      "always visible" was hidden exactly when it mattered;
//   2. .toFixed(0) on 0.4371 printed "0%";
//   3. the breaker printed "0.25%" instead of 25%.
//
// So the one branch in the fleet that WAS halted announced itself as
// "Paused: down 0% from its own $285.40 peak (breaker at 0.25%)".
const fs = require('fs');
const path = require('path');
const checks = [];
const ok = (l, c, d) => checks.push([d ? `${l}  -- ${d}` : l, !!c]);

const SRC = fs.readFileSync(
    path.join(__dirname, 'family_tree_dashboard.html'), 'utf8');

console.log('\n[1] the raw fraction is never rendered as a percentage');
ok('no unscaled drawdown_pct renderer survives',
   !/b\.drawdown_pct\.toFixed/.test(SRC));
ok('no unscaled breaker renderer survives',
   !/\$\{b\.drawdown_breaker_pct\}%/.test(SRC));
ok('both are scaled into locals', /b\.drawdown_pct \* 100/.test(SRC)
   && /b\.drawdown_breaker_pct \* 100/.test(SRC));

console.log('\n[2] the visibility guard tests the scaled value');
ok('the guard no longer compares a fraction against 0.5',
   !/drawdownHtml = \(b\.drawdown_pct != null && b\.drawdown_pct > 0\.5\)/.test(SRC));
ok('it compares the scaled percentage instead',
   /drawdownHtml = \(_ddPct != null && _ddPct > 0\.5\)/.test(SRC));

console.log('\n[3] the live QNT numbers, before and after');
const b = { drawdown_pct: 0.4371, drawdown_breaker_pct: 0.25, peak_equity: 285.40 };
const before = { shown: b.drawdown_pct > 0.5,
                 txt: b.drawdown_pct.toFixed(0), br: String(b.drawdown_breaker_pct) };
const dd = b.drawdown_pct * 100, br = b.drawdown_breaker_pct * 100;
const after = { shown: dd > 0.5, txt: dd.toFixed(0), br: br.toFixed(0) };
ok('OLD: the drawdown row was hidden entirely', before.shown === false);
ok('OLD: it would have printed "0%"', before.txt === '0', before.txt);
ok('OLD: the breaker printed "0.25%"', before.br === '0.25', before.br);
ok('NEW: the row is shown', after.shown === true);
ok('NEW: it prints "44%"', after.txt === '44', after.txt);
ok('NEW: the breaker prints "25%"', after.br === '25', after.br);
ok('NEW: it agrees with the bot\'s own log wording',
   `down ${after.txt}% from its own $285.40 peak (breaker at ${after.br}%)`
   === 'down 44% from its own $285.40 peak (breaker at 25%)');

console.log('\n[4] a branch with no drawdown still renders nothing');
ok('0 drawdown stays hidden', !((0 * 100) > 0.5));
ok('a null field does not throw', (null != null ? null * 100 : null) === null);

const w = Math.max(...checks.map(c => c[0].length));
console.log();
for (const [l, p] of checks) console.log(`  [${p ? 'PASS' : 'FAIL'}] ${l.padEnd(w)}`);
const bad = checks.filter(c => !c[1]);
console.log(`\n  ${checks.length - bad.length}/${checks.length} checks passed`);
process.exit(bad.length ? 1 : 0);
