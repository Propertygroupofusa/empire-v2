// The control that runs reconcile-slices from the page. The failure that
// matters is not a cosmetic one: this clears tracked cost basis and is NOT
// reversible, so the preview path must be incapable of applying, and the
// apply path must never fire without the plan having been shown first.
const fs = require('fs');
const src = fs.readFileSync('/home/user/empire-v2/family_tree_dashboard.html', 'utf8');

let fail = 0;
const ok = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}${cond || !detail ? '' : '   -> ' + detail}`);
  if (!cond) fail++;
};

const grab = (name) => {
  const m = src.match(new RegExp('(?:async )?function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n\\}'));
  if (!m) { console.log('FAIL: ' + name + ' not found'); process.exit(1); }
  return m[0];
};
const fnUrl = grab('_gridReconcileUrl');
const fnRender = grab('renderGridReconcile');
const fnPrev = grab('previewGridReconcile');
const fnExec = grab('executeGridReconcile');

// ---- the URL builder is the safety boundary -------------------------
const urlFn = new Function('API_BASE', fnUrl + '; return _gridReconcileUrl;')(
  'https://x/api/trading-dashboard');

console.log('\n[1] the preview URL cannot apply anything');
let u = urlFn(undefined, false);
ok('preview sends no dry_run flag at all (server default is true)',
   !/dry_run/.test(u), u);
ok('preview sends no accept_writeoff', !/accept_writeoff/.test(u), u);
ok('preview has no query string at all', !u.includes('?'), u);
ok('preview hits reconcile-slices', u.endsWith('/grid-status/reconcile-slices'), u);

console.log('\n[2] the apply URL carries BOTH required flags');
u = urlFn(undefined, true);
ok('dry_run=false present', /[?&]dry_run=false(&|$)/.test(u), u);
ok('accept_writeoff=true present', /[?&]accept_writeoff=true(&|$)/.test(u), u);
ok('no stray product_id', !/product_id/.test(u), u);

console.log('\n[3] one coin only');
u = urlFn('QNT-USD', false);
ok('product_id is sent and encoded', /\?product_id=QNT-USD$/.test(u), u);
ok('still no apply flags on a per-coin PREVIEW',
   !/dry_run|accept_writeoff/.test(u), u);
u = urlFn('QNT-USD', true);
ok('per-coin apply carries product_id and both flags',
   /product_id=QNT-USD/.test(u) && /dry_run=false/.test(u)
   && /accept_writeoff=true/.test(u), u);
ok('a product id with regex/url metacharacters is encoded, not injected',
   urlFn('A&b=c', false).includes('product_id=A%26b%3Dc'),
   urlFn('A&b=c', false));

// ---- the apply button only exists after a plan is rendered ----------
console.log('\n[4] apply is only reachable through a shown plan');
ok('executeGridReconcile is wired ONLY inside previewGridReconcile',
   fnPrev.includes('executeGridReconcile('),
   'preview does not offer it');
const callers = (src.match(/executeGridReconcile\(/g) || []).length;
ok('exactly two mentions: the definition and the one button in the plan',
   callers === 2, String(callers));
ok('renderGridReconcile offers ONLY the preview, never apply',
   fnRender.includes('previewGridReconcile()')
   && !fnRender.includes('executeGridReconcile'));
ok('the apply button text demands confirmation',
   /Confirm and correct for real/.test(fnPrev));

// ---- every write goes through the guarded helper --------------------
console.log('\n[5] it cannot forget the token header');
for (const [n, f] of [['preview', fnPrev], ['apply', fnExec]]) {
  ok(`${n} uses postGuarded`, f.includes('postGuarded('));
  ok(`${n} never calls fetch() directly`, !/\bfetch\(/.test(f));
  ok(`${n} never hardcodes a token header`, !/x-dashboard-token/.test(f));
}

// ---- unreadable backing is not "nothing to fix" --------------------
console.log('\n[6] the panel respects UNKNOWN');
function runRender(bk) {
  const el = { style: { display: '' }, innerHTML: '' };
  new Function('document', 'fmtUsd', 'bk',
    fnRender + '; return renderGridReconcile(bk);')(
    { getElementById: id => (id === 'grid-reconcile-wrap' ? el : null) },
    v => '$' + Number(v).toFixed(2), bk);
  return el;
}
for (const [label, bk] of [
  ['null', null],
  ['readable false', { readable: false, reason: 'wallet unreadable' }],
  ['readable false WITH a stale shortfall', { readable: false, not_in_wallet_usd: 1190.75, unbacked_branches: 10 }],
  ['nothing short', { readable: true, not_in_wallet_usd: 0, unbacked_branches: 0 }],
  ['zero branches but a dollar figure', { readable: true, not_in_wallet_usd: 5, unbacked_branches: 0 }],
]) {
  const x = runRender(bk);
  ok(`${label} -> hidden`, x.style.display === 'none' && x.innerHTML === '',
     x.style.display);
}
const live = runRender({ readable: true, not_in_wallet_usd: 1190.75,
                         unbacked_branches: 10, phantom_unrealized_usd: 62.19 });
ok('the live case shows', live.style.display === 'block');
ok('it shows the dollar shortfall', /\$1190\.75/.test(live.innerHTML));
ok('it shows the phantom gain', /\$62\.19/.test(live.innerHTML));
ok('it names the branch count', /\b10 branch/.test(live.innerHTML));
ok('it says it is not a loss', /not a loss/i.test(live.innerHTML));
ok('it offers preview, not apply',
   /previewGridReconcile\(\)/.test(live.innerHTML)
   && !/executeGridReconcile/.test(live.innerHTML));
ok('no NaN or undefined', !/NaN|undefined/.test(live.innerHTML));
const noPhantom = runRender({ readable: true, not_in_wallet_usd: 100,
                              unbacked_branches: 2, phantom_unrealized_usd: 0 });
ok('a zero phantom figure is omitted rather than printed as $0.00',
   !/\$0\.00/.test(noPhantom.innerHTML));

// ---- server text is escaped ---------------------------------------
console.log('\n[7] server text is escaped');
ok('the plan escapes product_id', /escText\(b\.product_id\)/.test(fnPrev));
ok('the plan escapes the server detail', /escText\(d\.detail/.test(fnPrev));
ok('skipped product ids are escaped', /escText\(x\.product_id\)/.test(fnPrev));

// ---- wiring --------------------------------------------------------
console.log('\n[8] wiring');
ok('the container exists', /id="grid-reconcile-wrap"/.test(src));
ok('it is fed d.backing', /renderGridReconcile\(d\.backing\)/.test(src));
const fnStart = src.indexOf('async function loadFleetReadiness()');
const body = src.slice(fnStart, src.indexOf('\nasync function ', fnStart + 10));
ok('the call rides the grid-status fetch the page already makes',
   body.includes('renderGridReconcile(d.backing)')
   && (body.match(/apiGet\('\/grid-status'\)/g) || []).length === 1);
ok('the panel adds no fetch of its own', !/apiGet|XMLHttpRequest/.test(fnRender));

console.log();
if (fail) { console.log(`${fail} FAILED`); process.exit(1); }
console.log('all checks passed');
