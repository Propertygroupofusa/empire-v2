// The out-of-reach banner must never print $0.00 for an amount nobody
// has measured. The whole point of the module behind it is that a staked
// balance is invisible to the trading key, so the amount is UNKNOWN - and
// a banner that renders UNKNOWN as a dollar figure, or hides itself
// because the figure is missing, reinstates exactly the blindness it was
// added to end: on 2026-10-02 the Coinbase app showed $13,912.19 of
// crypto while every total on this page was built from $8,135.00.
const fs = require('fs');
const path = '/home/user/empire-v2/family_tree_dashboard.html';
const src = fs.readFileSync(path, 'utf8');

let fail = 0;
const ok = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}${cond || !detail ? '' : '   -> ' + detail}`);
  if (!cond) fail++;
};

// Pull the shipped function out of the real file, not a retyped copy.
const m = src.match(/function renderOutOfReach\(oor\) \{[\s\S]*?\n\}/);
if (!m) { console.log('FAIL: renderOutOfReach not found in the page'); process.exit(1); }

// A DOM stub just large enough for the renderer, so what is asserted is
// the real innerHTML the browser would get.
function run(oor) {
  const el = { style: { display: '' }, innerHTML: '' };
  const fn = new Function('document', 'escText', 'fmtUsd', 'oor',
    m[0] + '; return renderOutOfReach(oor);');
  fn({ getElementById: id => (id === 'out-of-reach-banner' ? el : null) },
     s => String(s == null ? '' : s).replace(/[&<>"]/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])),
     v => '$' + Number(v).toLocaleString('en-US',
        { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
     oor);
  return el;
}

const LIVE = {
  readable: true,
  verdict: 'UNKNOWN',
  out_of_reach_usd: null,
  detail: '5 branch(es) run on a currency whose trading balance is empty '
        + 'or dust: ETH, PEPE, PRIME, QNT, TIA. How much crypto sits '
        + 'outside the trading balance is UNKNOWN.',
  empty_trading_balance: [
    { asset: 'ETH', confirmed_zero: false }, { asset: 'TIA', confirmed_zero: true },
    { asset: 'PRIME', confirmed_zero: true },
  ],
};

console.log('\n[1] UNKNOWN is shown as UNKNOWN, never as a number');
let r = run(LIVE);
ok('the banner is shown', r.style.display === 'block', r.style.display);
ok('it prints the word UNKNOWN', /UNKNOWN/.test(r.innerHTML));
ok('it does NOT print $0.00', !/\$0\.00/.test(r.innerHTML));
ok('it does not print $NaN', !/NaN/.test(r.innerHTML));
ok('it does not print "undefined"', !/undefined/.test(r.innerHTML));
ok('it says "not zero" in so many words', /not zero/.test(r.innerHTML));
ok('it names the empty-balance assets', /ETH/.test(r.innerHTML)
   && /TIA/.test(r.innerHTML) && /PRIME/.test(r.innerHTML));
ok('it reports the count', /\b3\b/.test(r.innerHTML));
ok('it carries the server detail', /outside the trading balance/.test(r.innerHTML));

console.log('\n[2] a declared figure is shown as money, with its share');
r = run(Object.assign({}, LIVE, { verdict: 'DECLARED',
  out_of_reach_usd: 4093.14, out_of_reach_share_pct: 28.65,
  detail: '$4,093.14 of crypto is held outside the trading balance.' }));
ok('the banner is shown', r.style.display === 'block');
ok('the dollar figure appears', /\$4,093\.14/.test(r.innerHTML));
ok('the share appears', /28\.6/.test(r.innerHTML));
ok('the word UNKNOWN is gone', !/UNKNOWN/.test(r.innerHTML));
ok('no NaN, no undefined',
   !/NaN|undefined/.test(r.innerHTML), r.innerHTML.slice(0, 120));

console.log('\n[3] unreadable hides - it is not a finding, and not a zero');
for (const [label, payload] of [
  ['null', null], ['undefined', undefined],
  ['readable false', { readable: false, reason: 'balances unreadable', out_of_reach_usd: null }],
  ['empty object', {}],
  ['readable missing', { verdict: 'UNKNOWN', out_of_reach_usd: null }],
]) {
  const x = run(payload);
  ok(`${label} -> hidden`, x.style.display === 'none' && x.innerHTML === '',
     x.style.display + ' / ' + x.innerHTML.slice(0, 60));
}

console.log('\n[4] an explicit "nothing out of reach" hides, but only when clean');
let x = run({ readable: true, verdict: 'DECLARED', out_of_reach_usd: 0,
              empty_trading_balance: [], detail: 'none declared' });
ok('declared 0 with no empty branches -> hidden', x.style.display === 'none');
x = run({ readable: true, verdict: 'DECLARED', out_of_reach_usd: 0,
          empty_trading_balance: [{ asset: 'ETH', confirmed_zero: false }],
          detail: 'none declared' });
ok('declared 0 but a branch IS on an empty balance -> still shown',
   x.style.display === 'block' && /ETH/.test(x.innerHTML));
x = run({ readable: true, verdict: 'UNKNOWN', out_of_reach_usd: null,
          empty_trading_balance: [], detail: 'nothing empty' });
ok('UNKNOWN with no empty branches -> still shown (the amount is the finding)',
   x.style.display === 'block' && /UNKNOWN/.test(x.innerHTML));

console.log('\n[5] it cannot be fed markup');
x = run({ readable: true, verdict: 'UNKNOWN', out_of_reach_usd: null,
          detail: '<img src=x onerror=alert(1)>',
          empty_trading_balance: [{ asset: '<script>bad</script>' }] });
ok('the detail is escaped', !/<img/.test(x.innerHTML) && /&lt;img/.test(x.innerHTML));
ok('the asset name is escaped',
   !/<script>bad/.test(x.innerHTML) && /&lt;script&gt;bad/.test(x.innerHTML));

console.log('\n[6] it is wired to the payload the page already fetches');
ok('renderOutOfReach is called', /renderOutOfReach\(d\.out_of_reach\)/.test(src));
ok('it reads grid-status, which the page already requests',
   /renderOutOfReach\(d\.out_of_reach\)/.test(src)
   && src.indexOf("apiGet('/grid-status')") <
      src.indexOf('renderOutOfReach(d.out_of_reach)'));
ok('the container exists in the markup',
   /id="out-of-reach-banner"/.test(src));
// The banner must ride the request the page already makes. Asserted by
// LOCATION, not by a count: the call has to sit inside the function that
// already awaited /grid-status, with no fetch of its own between them.
const fnStart = src.indexOf('async function loadFleetReadiness()');
const fnEnd = src.indexOf('\nasync function ', fnStart + 10);
const body = src.slice(fnStart, fnEnd === -1 ? src.length : fnEnd);
ok('the call sits inside loadFleetReadiness',
   fnStart !== -1 && body.includes('renderOutOfReach(d.out_of_reach)'));
ok('that function awaits /grid-status exactly once',
   (body.match(/apiGet\('\/grid-status'\)/g) || []).length === 1);
ok('the banner adds no fetch, apiGet or XHR of its own',
   !/apiGet|fetch\(|XMLHttpRequest/.test(m[0]));
ok('and no out-of-reach endpoint is requested anywhere on the page',
   !/apiGet\(['\"][^'\"]*out-of-reach/.test(src));

console.log();
if (fail) { console.log(`${fail} FAILED`); process.exit(1); }
console.log('all checks passed');
