// The summary card at the top of the grid section. It led with realized +
// unrealized SUMMED, in red, labelled "Total profit" - and that figure was
// misread as a realised loss four times in one day while banked had never
// gone down. Banked now leads. These pin that, and pin that nothing was
// hidden to achieve it.
const fs = require('fs');
const src = fs.readFileSync('/home/user/empire-v2/family_tree_dashboard.html', 'utf8');
let fail = 0;
const ok = (l, c, d = '') => { console.log(`  ${c ? 'PASS' : 'FAIL'}  ${l}${c || !d ? '' : '   -> ' + d}`); if (!c) fail++; };

const m = src.match(/function renderGridRealizedTotalBanner\([^)]*\) \{[\s\S]*?\n\}/);
if (!m) { console.log('FAIL: renderGridRealizedTotalBanner not found'); process.exit(1); }
// Use the PAGE'S OWN fmtUsd, not a stub. A stub that formats differently
// tests the stub: mine used Math.abs and reported a failure the page does
// not have. The real one puts the sign inside the symbol - "$-441.13".
const fm = src.match(/function fmtUsd\(n\) \{[\s\S]*?\n\}/);
if (!fm) { console.log('FAIL: fmtUsd not found'); process.exit(1); }
const fmtUsd = new Function(fm[0] + '; return fmtUsd;')();
const fn = new Function('fmtUsd', m[0] + '; return renderGridRealizedTotalBanner;')(fmtUsd);

const HIST = { total_realized_pnl: 135.58, total_trade_count: 196, overall_win_rate: 88 };
const html = fn(HIST, { total_unrealized_net_usd: -576.71 });

console.log('\n[1] banked leads, in the big type');
const iBanked = html.indexOf('Banked');
const iOpen = html.indexOf('Still open');
const iSum = html.indexOf('added together');
ok('banked appears first', iBanked > -1 && iBanked < iOpen, `${iBanked} < ${iOpen}`);
ok('open mark comes second', iOpen > -1 && iOpen < iSum, `${iOpen} < ${iSum}`);
ok('the sum comes last', iSum > -1);
ok('banked is in the 1.25em headline',
   /font-size:1\.25em[^>]*>\s*🏆 Banked/.test(html.replace(/\n\s*/g, ' ')), html.slice(0, 200));

console.log('\n[2] NOTHING WAS HIDDEN to get there');
ok('the open-slice figure is still shown', html.includes('$-576.71'), 'missing the open mark');
ok('it is still rendered in red', /color:var\(--red\)[^>]*>\s*📉 Still open/.test(html.replace(/\n\s*/g, ' ')));
ok('the combined sum is still shown', html.includes('$-441.13'), 'missing the sum');
ok('the sum is labelled as unlike things, not as profit',
   /sum of two unlike things/.test(html));
ok('the card never calls the sum a total profit', !/Total profit/i.test(html));

console.log('\n[3] the figures are right');
ok('banked prints +$135.58', html.includes('+$135.58'));
ok('fmtUsd puts the sign inside the symbol', fmtUsd(-441.13) === '$-441.13', fmtUsd(-441.13));
ok('open prints $-576.71', html.includes('$-576.71'));
ok('sum prints $-441.13', html.includes('$-441.13'));
ok('trade count and win rate survive',
   html.includes('196 real completed trades') && html.includes('88% win rate'));

console.log('\n[4] an unreadable price leg says unknown, never zero');
const h2 = fn(HIST, { total_unrealized_net_usd: null });
ok('open mark reads unknown', /Still open[\s\S]{0,200}unknown right now/.test(h2.replace(/\n\s*/g, ' ')));
ok('the sum reads unknown too', /added together[\s\S]{0,160}unknown/.test(h2.replace(/\n\s*/g, ' ')));
ok('banked is still shown and still correct', h2.includes('+$135.58'));
ok('no zero is invented for the missing leg', !/Still open[\s\S]{0,120}\$0\.00/.test(h2.replace(/\n\s*/g, ' ')));

console.log('\n[5] the forced-close warning still fires on a real gap');
ok('warning present when banked is up but coin is down', /FORCED close/.test(html));
const h3 = fn(HIST, { total_unrealized_net_usd: 50 });
ok('no warning when nothing is underwater', !/FORCED close/.test(h3));

console.log('\n[6] a missing realized total still refuses to guess');
const h4 = fn({}, { total_unrealized_net_usd: -576.71 });
ok('says it could not load rather than printing 0', /Could not load/.test(h4));

console.log(fail ? `\n${fail} FAILURE(S)\n` : '\nALL PASS\n');
process.exit(fail ? 1 : 0);
