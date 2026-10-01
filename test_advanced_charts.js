// Indicator math for the Advanced Charts panel, against hand-computed
// values rather than "the line looked about right".
//
// These are the four indicators a person reads before deciding to buy or
// sell. Every one of them has a quiet failure mode that still draws a
// plausible curve:
//   - SMA/EMA emitting 0 instead of null before the first full window,
//     which draws a line along the bottom of the chart and reads as a
//     real price;
//   - EMA seeded with values[0] instead of the SMA of the first window,
//     which is wrong for its first dozen points and looks fine;
//   - RSI dividing by zero on an all-up window;
//   - MACD's signal computed over the leading nulls as zeros, which
//     drags it toward zero and puts every crossover in the wrong place.
// A chart hides all four. A number does not.
const path = require('path');
const A = require(path.join(__dirname, 'static', 'advanced_charts.js'));

let fails = [];
const ok = (label, cond, detail) => {
    if (cond) console.log('  PASS  ' + label);
    else { console.log('  FAIL  ' + label + (detail ? '\n        ' + detail : '')); fails.push(label); }
};
const near = (a, b, eps) => a !== null && b !== null && Math.abs(a - b) < (eps || 1e-9);

console.log('\n[1] SMA');
// mean(1..5)=3, mean(2..6)=4, mean(3..7)=5
const s = A.sma([1,2,3,4,5,6,7], 5);
ok('leading slots are null, NOT zero', s.slice(0,4).every(v => v === null), JSON.stringify(s));
ok('first window = 3', near(s[4], 3), String(s[4]));
ok('slides to 4 then 5', near(s[5], 4) && near(s[6], 5), JSON.stringify(s.slice(5)));
ok('shorter input than the window is all null',
   A.sma([1,2], 5).every(v => v === null));

console.log('\n[2] EMA');
// Seed = mean(1..5) = 3. k = 2/6 = 1/3.
// i=5: 6/3 + 3*2/3 = 2 + 2 = 4
// i=6: 7/3 + 4*2/3 = 2.3333 + 2.6667 = 5
const e = A.ema([1,2,3,4,5,6,7], 5);
ok('leading slots are null', e.slice(0,4).every(v => v === null));
ok('seeded with the SMA of the first window (3), not values[0] (1)',
   near(e[4], 3), String(e[4]));
ok('i=5 is 4', near(e[5], 4, 1e-9), String(e[5]));
ok('i=6 is 5', near(e[6], 5, 1e-9), String(e[6]));
// A flat series must stay flat - a bad seed shows up here as drift.
const flat = A.ema(new Array(40).fill(42), 12);
ok('a flat series stays exactly flat (no seed drift)',
   flat.slice(11).every(v => near(v, 42, 1e-9)), String(flat[39]));

console.log('\n[3] RSI');
// Every change up => avgLoss 0 => RS divides by zero. RSI is 100 there.
const up = A.rsi(Array.from({length: 40}, (_, i) => 100 + i), 14);
ok('all-gains window returns exactly 100, not NaN/Infinity',
   up[39] === 100, String(up[39]));
ok('and every value is finite', up.slice(14).every(v => isFinite(v)));
const down = A.rsi(Array.from({length: 40}, (_, i) => 100 - i), 14);
ok('all-losses window returns 0', near(down[39], 0, 1e-9), String(down[39]));
// Wilder's own published worked example. This is the check that matters:
// a sliding re-mean, a wrong seed, or arithmetic smoothing all still
// produce a curve between 0 and 100 that looks entirely normal on a
// chart, and all three miss these numbers.
//
// (An earlier draft of this test asserted only "the value moves after
// the seed" against a series I had typed an extra leading close into.
// It failed - correctly - because closes[14] and closes[15] are both
// 46.28, so a zero change leaves the gain/loss RATIO untouched and the
// RSI genuinely does not move. The assertion was wrong, not the code.)
const WILDER = [44.3389,44.0902,44.1497,43.6124,44.3278,44.8264,45.0955,
                45.4245,45.8433,46.0826,45.8931,46.0328,45.6140,46.2820,
                46.2820,46.0028,46.0328,46.4116,46.2240,45.6453,46.2110,
                46.2525,45.7120,46.4418,45.7817,45.3514,44.0274,44.1731,
                44.2278,44.5713];
const band = A.rsi(WILDER, 14);
ok('matches the published first RSI of 70.5327', near(band[14], 70.5327, 5e-4), String(band[14]));
ok('matches the published second RSI of 66.3186', near(band[15], 66.3186, 5e-4), String(band[15]));
ok('matches the published third RSI of 66.5498', near(band[16], 66.5498, 5e-4), String(band[16]));
ok('a real series stays inside 0..100',
   band.slice(14).every(v => v >= 0 && v <= 100), JSON.stringify(band.slice(14,18)));
ok('leading slots are null', band.slice(0,14).every(v => v === null));

console.log('\n[4] MACD');
const closes = Array.from({length: 120}, (_, i) => 100 + Math.sin(i / 7) * 9);
const m = A.macd(closes, 12, 26, 9);
ok('line is null until the slow EMA exists (index 25)',
   m.line.slice(0, 25).every(v => v === null) && m.line[25] !== null);
ok('signal starts 8 bars after the line, not at index 0',
   m.signal.slice(0, 33).every(v => v === null) && m.signal[33] !== null,
   'first signal at ' + m.signal.findIndex(v => v !== null));
ok('hist === line - signal everywhere both exist',
   m.line.every((v, i) => (v === null || m.signal[i] === null)
       ? true : near(m.hist[i], v - m.signal[i], 1e-9)));
// THE BUG THIS CATCHES: feeding line's leading nulls to ema() as zeros.
// That seeds the signal near zero and holds it there, so for a series
// whose MACD line is consistently positive the signal would sit far
// below it and every crossover would land in the wrong place.
const rising = Array.from({length: 120}, (_, i) => 100 + i);
const mr = A.macd(rising, 12, 26, 9);
const i0 = mr.signal.findIndex(v => v !== null);
ok('on a steady ramp the signal tracks the line, it does not sit at zero',
   Math.abs(mr.signal[i0] - mr.line[i0]) < Math.abs(mr.line[i0]) * 0.5,
   'line=' + mr.line[i0].toFixed(4) + ' signal=' + mr.signal[i0].toFixed(4));
ok('a flat series gives a MACD of 0',
   near(A.macd(new Array(120).fill(50), 12, 26, 9).line[119], 0, 1e-9));

console.log('\n[5] Coinbase candle parsing');
// Coinbase answers [time, low, high, open, close, volume], NEWEST FIRST.
const raw = [[300, 1, 4, 2, 3, 10], [100, 1, 5, 2, 4, 20], [200, 2, 6, 3, 5, 30]];
const cs = A.parseCandles(raw);
ok('sorted oldest-first so time runs left to right',
   cs.map(d => d.t).join(',') === '100,200,300', cs.map(d => d.t).join(','));
ok('low/high/open/close land in the right fields (not OHLC order)',
   cs[0].low === 1 && cs[0].high === 5 && cs[0].open === 2 && cs[0].close === 4,
   JSON.stringify(cs[0]));
ok('a malformed row is dropped, not turned into zeros',
   A.parseCandles([[1,2,3,4,5,6], [1,2,null,4,5,6], 'nope']).length === 1);
ok('a non-array answer yields no candles, and does not throw',
   A.parseCandles({detail: 'Not Found'}).length === 0);

console.log('\n[6] price formatting keeps small coins readable');
ok('a four-figure price loses the cents', A.money(83456.7) === '$83457', A.money(83456.7));
ok('a dollar price keeps cents', A.money(2.5) === '$2.50', A.money(2.5));
ok('a sub-cent coin keeps enough digits to differ',
   A.money(0.000123) === '$0.000123', A.money(0.000123));

console.log('\n[7] the page runs the file this test drives');
// The whole point of serving the math from /static is that the browser
// and this test load the SAME bytes. An inlined second copy in the HTML
// would drift from the tested one with nothing failing, which is how a
// chart ends up disagreeing with its own test suite.
const fs = require('fs');
const html = fs.readFileSync(path.join(__dirname, 'family_tree_dashboard.html'), 'utf8');
ok('the dashboard loads /static/advanced_charts.js',
   html.includes('src="/static/advanced_charts.js"'));
ok('the file it loads is the one under test',
   fs.existsSync(path.join(__dirname, 'static', 'advanced_charts.js')));
ok('the math is NOT also inlined into the page (no second copy to drift)',
   !html.includes('function parseCandles(') && !html.includes("Wilder's RSI"));
ok('the panel is initialised on load', html.includes('initAdvancedCharts();'));
ok('the charts never call a trading endpoint',
   !/advFetchCandles[\s\S]{0,400}(close-branch|reconcile|spread-evenly|free-locked)/.test(html));

console.log('\n' + (fails.length ? fails.length + ' FAILED: ' + fails.join('; ') : 'ALL PASS'));
process.exit(fails.length ? 1 : 0);
