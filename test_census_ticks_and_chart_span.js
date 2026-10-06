// Two display fixes the account owner asked for in one sitting, and the
// promises each one has to keep.
//
//  1. The advanced-chart header read "-0.77% over 350 candles" beside a
//     chart whose visible slope was up. The number was first-candle to
//     last-candle across 14.5 days; the chart's right-hand third was
//     +1.01% over three days. Both true, and the label named neither
//     span, so the only available reading was "the price is down".
//
//  2. The account-census table refetches every 15 seconds and really
//     does move - three live reads 13 seconds apart gave $10,038.56,
//     $10,039.65 and $10,008.67 - but it was drawn as a flat table with
//     no memory of the previous reading, so nothing could be coloured
//     and the owner read the stillness as a dead feed.
//
// Run as written: node test_census_ticks_and_chart_span.js
const fs = require('fs');
const SRC = fs.readFileSync('family_tree_dashboard.html', 'utf8');

let fails = 0;
function ok(name, cond) {
    console.log((cond ? '  PASS  ' : '  FAIL  ') + name);
    if (!cond) fails++;
}
function section(n) { console.log('\n' + n); }

// ---- the helper, lifted out of the page and actually run ----
const m = SRC.match(/function censusMove\(now, before\) \{[\s\S]*?\n\}/);
if (!m) { console.log('  FAIL  censusMove is not in the page'); process.exit(1); }
const censusMove = new Function('return ' + m[0])();

section('[1] a first reading has nothing to compare against');
ok('an undefined previous colours nothing', censusMove(100, undefined) === '');
ok('a null previous colours nothing', censusMove(100, null) === '');
ok('an unreadable now colours nothing', censusMove(NaN, 100) === '');
ok('an unreadable previous colours nothing', censusMove(100, NaN) === '');
ok('an infinite reading colours nothing', censusMove(Infinity, 100) === '');
ok('a previous of ZERO is a reading, not an unknown',
   censusMove(5, 0).includes('var(--green)'));

section('[2] a still reading is not an up tick');
ok('an identical value colours nothing', censusMove(100, 100) === '');
ok('sub-cent drift is held still so the table cannot strobe',
   censusMove(100.004, 100) === '');
ok('a cent of real movement does show', censusMove(100.01, 100) !== '');

section('[3] direction is carried by BOTH colour and caret');
const up = censusMove(100.50, 100), dn = censusMove(99.50, 100);
ok('a rise is green', up.includes('var(--green)') && !up.includes('var(--red)'));
ok('a rise carries the up caret', up.includes('▲'));
ok('a fall is red', dn.includes('var(--red)') && !dn.includes('var(--green)'));
ok('a fall carries the down caret', dn.includes('▼'));
ok('a fall prints a POSITIVE size beside its caret',
   dn.includes('0.50') && !dn.includes('-0.50'));
ok('the real 21:11Z drop reads red at its real size',
   censusMove(10008.67, 10039.65).includes('var(--red)') &&
   censusMove(10008.67, 10039.65).includes('30.98'));

section('[4] the baseline is read before the draw and written after it');
const fn = SRC.slice(SRC.indexOf('async function renderAccountCensus() {'));
const body = fn.slice(0, fn.indexOf('\nfunction renderGridOpportunityPanel'));
ok('the previous reading is taken from CENSUS_PREV',
   /const prev = CENSUS_PREV \|\| \{\}/.test(body));
ok('it is read BEFORE the markup is built',
   body.indexOf('const prev = CENSUS_PREV') < body.indexOf('let h ='));
ok('CENSUS_PREV is replaced only AFTER innerHTML is set, so a render that '
   + 'threw cannot leave a baseline nothing was drawn from',
   body.indexOf('el.innerHTML = h;') < body.lastIndexOf('CENSUS_PREV = {'));
ok('the per-asset baseline is keyed by asset, not by row position',
   /byAsset: Object\.fromEntries\(\(d\.holdings \|\| \[\]\)\.map\(r => \[r\.asset, r\.usd\]\)\)/
     .test(body));

section('[5] every figure the owner watches carries its move');
ok('the Coinbase total does', /censusMove\(d\.total_usd, prev\.total\)/.test(body));
ok('cash does', /censusMove\(d\.cash_usd, prev\.cash\)/.test(body));
ok('coin does', /censusMove\(d\.coin_usd, prev\.coin\)/.test(body));
ok('and every asset row does',
   /censusMove\(r\.usd, prevAsset\[r\.asset\]\)/.test(body));

section('[6] this is a DISPLAY fix - it must not reach the venue');
ok('the panel still only reads account-census',
   /fetch\('\/api\/trading-dashboard\/account-census'\)/.test(body));
ok('it places no order', !/\/orders|side:|place_order/.test(body));
ok('the helper itself does no I/O',
   !/fetch|XMLHttpRequest|localStorage/.test(m[0]));

section('[7] the chart header names the span it measured');
const adv = SRC.slice(SRC.indexOf('const chg = first.close'),
                      SRC.indexOf('const rr = document.getElementById(\'adv-rsi-read\')'));
ok('the span comes from the candle TIMESTAMPS, not count x granularity',
   /const spanDays = \(last\.t - first\.t\) \/ 86400;/.test(adv));
ok('the label says what the percentage is measured over',
   /over the last \$\{spanTxt\}/.test(adv));
ok('the candle count is kept, as data volume rather than as the span',
   /\$\{candles\.length\} candles/.test(adv));
ok('a window under a day is stated in hours, not "0.3 days"',
   /spanDays < 1 \? `\$\{\(spanDays \* 24\)\.toFixed\(1\)\} hours`/.test(adv));
ok('the percentage arithmetic itself is UNCHANGED - first close to last',
   /\(last\.close - first\.close\) \/ first\.close \* 100/.test(adv));

// the same arithmetic the page runs, exercised
const spanOf = (t0, t1) => { const d = (t1 - t0) / 86400;
    return d < 1 ? `${(d * 24).toFixed(1)} hours` : `${d.toFixed(1)} days`; };
ok('350 hourly candles read as 14.5 days, which is what BTC showed',
   spanOf(0, 349 * 3600) === '14.5 days');
ok('a six-hour window reads in hours', spanOf(0, 6 * 3600) === '6.0 hours');

console.log(fails === 0 ? '\nALL PASS' : `\n${fails} FAILED`);
process.exit(fails === 0 ? 0 : 1);
