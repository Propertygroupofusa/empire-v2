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

// NO VERDICT HERE. The async blocks below are part of this suite, and an
// early "all checks passed" printed before they run is a pass count that
// can contradict the real outcome further down the same output. One
// suite, one verdict, at the end.

// ====================================================================
// A CHECKMARK IS A CLAIM AND HAS TO BE EARNED.
//
// Added after the apply path printed "Corrected 0 branch(es), clearing
// $0.00" behind a green tick. The account owner reported the reconcile
// done three times while the backing report never moved, because the
// button said it had worked. These tests run the real functions against
// a fake server and assert on what reaches the screen.
// ====================================================================
console.log('\n[9] zero applied is NOT reported as success');

const grab2 = (name) => {
  const m = src.match(new RegExp('(?:async )?function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n\\}'));
  if (!m) { console.log('FAIL: ' + name + ' not found'); process.exit(1); }
  return m[0];
};
const esc2 = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const runExec = (serverPayload) => {
  let out = '';
  const st = { set textContent(v) { out = v; }, get textContent() { return out; },
               set innerHTML(v) { out = v; }, get innerHTML() { return out; } };
  const plan = { set innerHTML(v) {}, get innerHTML() { return ''; } };
  const doc = { getElementById: id => (id === 'grid-reconcile-status' ? st
                                     : id === 'grid-reconcile-plan' ? plan : null) };
  const fn = new Function('document', 'postGuarded', '_gridReconcileUrl', 'escText',
    'fmtUsd', 'loadFleetReadiness', 'refresh',
    grab2('executeGridReconcile') + '; return executeGridReconcile;')(
    doc, async () => serverPayload, () => 'u', esc2,
    n => '$' + Number(n || 0).toFixed(2), () => {}, undefined);
  return fn().then(() => out);
};

runExec({ applied: [], cost_basis_removed_usd: 0,
          detail: 'no branch claims more coin than the wallet holds' }).then(out => {
  ok('an empty apply does NOT print a green tick', !out.includes('✅'), out.slice(0, 120));
  ok('it warns instead', out.includes('⚠'), out.slice(0, 120));
  ok('it says plainly that nothing changed', /Nothing was changed/.test(out), out.slice(0, 120));
  ok('it surfaces the server reason', /no branch claims more coin/.test(out), out.slice(0, 160));
  ok('it does not claim a corrected count', !/Corrected 0/.test(out), out.slice(0, 120));
  return runExec({ applied: [{ product_id: 'LINK-USD', units_removed: 0.1,
                               cost_basis_removed_usd: 12.5 }],
                   cost_basis_removed_usd: 12.5 });
}).then(out => {
  ok('a REAL apply still reports success', out.includes('✅'), out.slice(0, 120));
  ok('and names the branch count', /Corrected 1 branch/.test(out), out.slice(0, 120));

  // ---- the preview must not paper over a contradiction -------------
  console.log('\n[10] a preview that disagrees with the banner says so');
  const runPrev = (seenUnbacked, payload) => {
    let out = '';
    const st = { set textContent(v) { out = v; }, get textContent() { return out; },
                 set innerHTML(v) { out = v; }, get innerHTML() { return out; } };
    const plan = { set innerHTML(v) {}, get innerHTML() { return ''; } };
    const doc = { getElementById: id => (id === 'grid-reconcile-status' ? st
                                       : id === 'grid-reconcile-plan' ? plan : null) };
    const fn = new Function('document', 'postGuarded', '_gridReconcileUrl', 'escText',
      'fmtUsd', '_gridReconcileSeenUnbacked',
      grab2('previewGridReconcile') + '; return previewGridReconcile;')(
      doc, async () => payload, () => 'u', esc2,
      n => '$' + Number(n || 0).toFixed(2), seenUnbacked);
    return fn().then(() => out);
  };
  const noRows = { branches: [], detail: 'nothing to reconcile' };
  return runPrev(8, noRows).then(out => {
    ok('banner says 8 short, endpoint says none -> NOT a checkmark',
       !out.includes('✅'), out.slice(0, 140));
    ok('it names the disagreement', /disagree/i.test(out), out.slice(0, 140));
    ok('it quotes the count it saw', /8 short branch/.test(out), out.slice(0, 160));
    ok('it refuses to call the book clean', /UNRECONCILED/.test(out), out.slice(0, 200));
    return runPrev(0, noRows);
  }).then(out => {
    ok('banner agrees there is nothing short -> a tick is honest',
       out.includes('✅'), out.slice(0, 140));
    return runPrev(null, noRows);
  }).then(out => {
    ok('an unreadable banner does not manufacture a disagreement',
       out.includes('✅'), out.slice(0, 140));
  });
}).then(() => {
  console.log();
  if (fail) { console.log(`${fail} FAILED`); process.exit(1); }
  console.log('all checks passed');
}).catch(e => { console.log('THREW: ' + e.stack); process.exit(1); });
