// A flawless record displayed as the worst possible profit factor.
//
// get_grid_performance_metrics() in crypto_grid_bot.py is explicit:
//
//     NONE, NEVER ZERO, when there is nothing to measure. "No completed
//     trades" and "a 0% win rate" are different claims and must not
//     render alike ... profit_factor is likewise None when there are no
//     losses at all: the ratio is undefined, and 1.0 would assert the
//     strategy exactly broke even, which is the opposite of a flawless
//     record.
//
// Two layers then threw that away. The router collapsed each None to 0
// "to keep the old numeric shape", and the panel did `|| 0` on top. So a
// grid with 196 wins and no losses would have shown:
//
//     Win Rate: 0.0%   Profit Factor: 0.00x   Expectancy: $0.00/trade
//
// which is not a weaker version of the truth, it is its opposite.
const fs = require('fs');
const path = require('path');
const checks = [];
const ok = (l, c, d) => checks.push([d ? `${l}  -- ${d}` : l, !!c]);

const HTML = fs.readFileSync(path.join(__dirname, 'family_tree_dashboard.html'), 'utf8');
const PY = fs.readFileSync(path.join(__dirname, 'routers', 'trading_dashboard.py'), 'utf8');

console.log('\n[1] the router no longer collapses None to a number');
ok('the win_rate "or 0" fallback is gone',
   !/win_rate = win_rate if win_rate is not None else \(/.test(PY));
ok('the profit_factor 0.0 fallback is gone',
   !/profit_factor = profit_factor if profit_factor is not None else 0\.0/.test(PY));
ok('win_rate is emitted as None when unmeasured',
   /"win_rate": round\(win_rate, 1\) if win_rate is not None else None/.test(PY));
ok('profit_factor is emitted as None when undefined',
   /"profit_factor": round\(profit_factor, 2\) if profit_factor is not None else None/.test(PY));
ok('expectancy no longer ends in "or 0"',
   !/\(rolling_expectancy or \{\}\)\.get\("expectancy"\) or 0/.test(PY));
ok('expectancy is emitted as None when unmeasured',
   /"expectancy_per_trade": round\(_expectancy, 2\) if _expectancy is not None else None/.test(PY));

console.log('\n[2] the grid fallback for win_rate is kept, not dropped');
ok('branch totals still fill win_rate when the grid cannot',
   /if win_rate is None and total_trades > 0:/.test(PY)
   && /win_rate = total_wins \/ total_trades \* 100/.test(PY));

console.log('\n[3] the panel renders the three stats through one guard');
const block = HTML.slice(HTML.indexOf("document.getElementById('scale-win-rate')") - 900,
                         HTML.indexOf("scale-expectancy'") + 300);
ok('the block was actually located (not a vacuous pass)',
   block.includes('scale-win-rate') && block.includes('scale-expectancy'),
   `${block.length} chars`);
ok('no "|| 0" survives on win_rate', !/metrics\.win_rate \|\| 0/.test(HTML));
ok('no "|| 0" survives on profit_factor', !/metrics\.profit_factor \|\| 0/.test(HTML));
ok('no "|| 0" survives on expectancy', !/metrics\.expectancy_per_trade \|\| 0/.test(HTML));

// Exercise the real guard, lifted from the page.
const m = HTML.match(/const stat = \(v, fn\) => \([^;]+\);/);
ok('the stat() guard was found in the page', !!m, m ? m[0].length + ' chars' : 'MISSING');
const stat = new Function(`${m[0]}\nreturn stat;`)();

console.log('\n[4] what each state now shows');
ok('null win rate -> em-dash', stat(null, v => `${v.toFixed(1)}%`) === '—');
ok('null profit factor -> em-dash', stat(null, v => `${v.toFixed(2)}x`) === '—');
ok('undefined -> em-dash', stat(undefined, v => String(v)) === '—');
ok('a real 87.8% still prints', stat(87.8, v => `${v.toFixed(1)}%`) === '87.8%');
ok('a real 7.64x still prints', stat(7.64, v => `${v.toFixed(2)}x`) === '7.64x');
ok('a real $0.69 still prints', stat(0.6917, v => `$${v.toFixed(2)}/trade`) === '$0.69/trade');
ok('a genuine ZERO is still shown as zero, not hidden',
   stat(0, v => `${v.toFixed(1)}%`) === '0.0%');
ok('Infinity is not printed as a number', stat(Infinity, v => String(v)) === '—');
ok('NaN is not printed as a number', stat(NaN, v => String(v)) === '—');

const w = Math.max(...checks.map(c => c[0].length));
console.log();
for (const [l, p] of checks) console.log(`  [${p ? 'PASS' : 'FAIL'}] ${l.padEnd(w)}`);
const bad = checks.filter(c => !c[1]);
console.log(`\n  ${checks.length - bad.length}/${checks.length} checks passed`);
if (bad.length) process.exit(1);
