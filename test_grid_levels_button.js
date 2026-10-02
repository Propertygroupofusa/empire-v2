// The Levels control. The owner cannot authenticate from PowerShell, so
// this button is the only way the parked branches get unparked - and it
// writes num_levels on a live trading row. Two failures matter: a preview
// path that can apply, and a suggested count the server would refuse.
//
// The second is checked against branch_levels.py itself, by running the
// real planner on the same fixture - not against a re-implementation of
// its arithmetic in this file.
const fs = require('fs');
const { execFileSync } = require('child_process');
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
const fnSuggest = grab('_gridLevelsSuggest');
const fnRender  = grab('renderGridLevels');
const fnWanted  = grab('_gridLevelsWanted');
const fnPrev    = grab('previewGridLevels');
const fnShow    = grab('_gridLevelsShowPlan');
const fnExec    = grab('executeGridLevels');

// Assertions about what a function SENDS must read its code, not its
// comments - a comment saying "no dry_run is sent" otherwise fails a
// grep for dry_run. Strings keep their quotes, so URL/body checks still
// see what is really there.
const code = (t) => t.replace(/^\s*\/\/.*$/gm, '');

const CONSTS = (src.match(/const GRID_LEVELS_MIN = [^\n]*\n/) || [''])[0];
ok('the constants are declared', CONSTS.includes('GRID_LEVELS_MAX'), CONSTS);

const suggest = new Function(CONSTS + fnSuggest + '; return _gridLevelsSuggest;')();

// ---- the suggestion agrees with the real server planner -------------
console.log('\n[1] every suggested count survives branch_levels.plan');
const slice = (qty, px) => ({ qty, entry_price: px });
const fixtures = [
  // LINK and NEAR as measured: parked at 3/3 with real headroom.
  { name: 'LINK-like', b: { product_id: 'LINK-USD', bot_name: 'crypto_grid_9',
      allocated_usd: 137.87, num_levels: 3,
      slices: [slice(2, 11.0), slice(2, 10.5), slice(2, 10.0)] } },
  { name: 'NEAR-like', b: { product_id: 'NEAR-USD', bot_name: 'crypto_grid_11',
      allocated_usd: 183.30, num_levels: 3,
      slices: [slice(20, 2.4), slice(20, 2.3), slice(20, 2.2)] } },
  // Holds MORE slices than its own level count - the adoption shape.
  { name: 'over-full', b: { product_id: 'XRP-USD', bot_name: 'crypto_grid_3',
      allocated_usd: 600.0, num_levels: 3,
      slices: Array.from({length: 7}, () => slice(10, 2.0)) } },
  // Small allocation: the $5 slice floor bites before the rung count.
  { name: 'tiny alloc', b: { product_id: 'TINY-USD', bot_name: 'crypto_grid_90',
      allocated_usd: 12.0, num_levels: 2,
      slices: [slice(1, 5.0), slice(1, 5.0)] } },
  // Fully spent: room without money.
  { name: 'fully spent', b: { product_id: 'FULL-USD', bot_name: 'crypto_grid_91',
      allocated_usd: 100.0, num_levels: 4,
      slices: Array.from({length: 4}, () => slice(5, 5.0)) } },
];

const runPlan = (branch, want) => {
  const out = execFileSync('python3', ['-c', `
import json, sys
sys.path.insert(0, "/home/user/empire-v2")
import branch_levels
b, w = json.loads(sys.argv[1]), int(sys.argv[2])
print(json.dumps(branch_levels.plan(b, w)))
`, JSON.stringify(branch), String(want)], { encoding: 'utf8' });
  return JSON.parse(out);
};

for (const f of fixtures) {
  const s = suggest(f.b);
  if (s.want === null) {
    // A null suggestion is a claim that NO count works. Prove it: the
    // server must refuse, or buy nothing, at every count in range.
    let anyGood = null;
    for (let w = (f.b.slices.length + 1); w <= 20; w++) {
      const p = runPlan(f.b, w);
      if (p.ok === true && !p.buys_nothing_without_more_allocation) { anyGood = w; break; }
    }
    ok(`${f.name}: no count suggested, and none would have opened a rung`,
       anyGood === null, anyGood === null ? '' : `count ${anyGood} would have worked`);
    continue;
  }
  const p = runPlan(f.b, s.want);
  ok(`${f.name}: suggested ${s.want} -> server says ${p.status}`, p.ok === true, p.detail);
  ok(`${f.name}: suggested ${s.want} actually opens a rung`,
     Number(p.rungs_it_could_actually_open) >= 1,
     `rungs=${p.rungs_it_could_actually_open} ${p.detail || ''}`);
  ok(`${f.name}: suggestion is never below the open slices`,
     s.want >= f.b.slices.length, `want=${s.want} open=${f.b.slices.length}`);
  ok(`${f.name}: the slice stays above the $5 floor`,
     Number(p.slice_usd_after) >= 5.0, `slice=${p.slice_usd_after}`);
  // The smallest workable count: one lower must NOT open a rung.
  if (s.want > f.b.slices.length + 1) {
    const lower = runPlan(f.b, s.want - 1);
    ok(`${f.name}: ${s.want} is the SMALLEST count that works`,
       !(lower.ok === true && Number(lower.rungs_it_could_actually_open) >= 1),
       `count ${s.want - 1} also worked`);
  }
}

// ---- a gap is not a zero -------------------------------------------
console.log('\n[2] an unpriced slice is UNKNOWN, never zero');
let u = suggest({ product_id: 'X-USD', allocated_usd: 100, num_levels: 2,
                  slices: [slice(1, 5), { qty: 1, entry_price: null }] });
ok('unpriced slice reports unknown', u.unknown === true, JSON.stringify(u));
ok('unpriced slice suggests no count', u.want === null, JSON.stringify(u));
u = suggest({ product_id: 'X-USD', allocated_usd: 0, num_levels: 2, slices: [] });
ok('no allocation suggests no count', u.want === null && u.unknown === false,
   JSON.stringify(u));

// ---- only changed boxes are sent -----------------------------------
console.log('\n[3] _gridLevelsWanted sends only real changes');
const mkEl = (product, current, value) => ({
  getAttribute: k => ({ 'data-product': product, 'data-current': String(current) })[k],
  value,
});
const els = [
  mkEl('LINK-USD', 3, '6'),    // changed  -> sent
  mkEl('NEAR-USD', 3, '3'),    // same     -> dropped
  mkEl('XRP-USD', 3, ''),      // blank    -> dropped
  mkEl('SHIB-USD', 3, '4.5'),  // fraction -> dropped
  mkEl('HBAR-USD', 3, 'abc'),  // junk     -> dropped
  mkEl('BCH-USD', 3, ' 8 '),   // padded   -> sent
];
const wanted = new Function('document', fnWanted + '; return _gridLevelsWanted;')({
  querySelectorAll: () => ({ forEach: cb => els.forEach(cb) }),
})();
ok('a changed count is sent', wanted['LINK-USD'] === 6, JSON.stringify(wanted));
ok('whitespace is tolerated', wanted['BCH-USD'] === 8, JSON.stringify(wanted));
ok('an unchanged count is NOT sent', !('NEAR-USD' in wanted), JSON.stringify(wanted));
ok('a blank box is NOT sent', !('XRP-USD' in wanted), JSON.stringify(wanted));
ok('a fraction is NOT sent', !('SHIB-USD' in wanted), JSON.stringify(wanted));
ok('junk is NOT sent', !('HBAR-USD' in wanted), JSON.stringify(wanted));
ok('exactly the two real changes are sent', Object.keys(wanted).length === 2,
   JSON.stringify(wanted));

// ---- the preview cannot apply --------------------------------------
console.log('\n[4] the preview path is incapable of applying');
ok('preview hits set-levels', /set-levels/.test(fnPrev));
ok('preview sends NO dry_run flag at all (server default is true)',
   !/dry_run/.test(code(fnPrev)), 'dry_run appears in previewGridLevels code');
ok('preview sends only the levels map', /\{levels\}/.test(fnPrev));
ok('preview goes through the write guard', /postGuarded\(/.test(fnPrev));
ok('preview refuses to send an empty map',
   /keys\.length/.test(fnPrev) && /Nothing to preview/.test(fnPrev));

console.log('\n[5] the apply path carries dry_run=false and nothing else');
ok('apply sends dry_run: false', /dry_run:\s*false/.test(fnExec));
ok('apply goes through the write guard', /postGuarded\(/.test(fnExec));
ok('apply hits set-levels', /set-levels/.test(fnExec));

// ---- apply cannot fire without a shown plan ------------------------
console.log('\n[6] apply cannot send a count no plan was shown for');
ok('apply takes no caller-supplied map', /async function executeGridLevels\(\)/.test(fnExec));
ok('apply reads the pending map set by the preview',
   /_gridLevelsPending/.test(fnExec) && /_gridLevelsPending/.test(fnShow));
ok('the pending map is cleared at the top of every plan render',
   /const plans = d\.plans \|\| \[\];\s*\n\s*_gridLevelsPending = null;/.test(fnShow));
ok('it is only set when something came back READY',
   /if \(ready\.length\) _gridLevelsPending = levels;/.test(fnShow));
ok('it is cleared after a successful apply',
   /_gridLevelsPending = null;/.test(fnExec));
ok('apply bails out when nothing is pending', /no previewed plan/.test(fnExec));

// prove the bail-out by running it with nothing pending
let sentCount = 0, statusText = '';
const stEl = { set textContent(v) { statusText = v; }, get textContent() { return statusText; },
               set innerHTML(v) { statusText = v; } };
new Function('document', 'postGuarded', 'API_BASE', '_gridLevelsPending',
  fnExec + '; return executeGridLevels;')(
  { getElementById: id => (id === 'grid-levels-status' ? stEl : null) },
  async () => { sentCount++; return {}; },
  'https://x/api/trading-dashboard',
  null,
)();
ok('with nothing pending, apply sends no request at all', sentCount === 0,
   `sent ${sentCount}`);
ok('and it says so', /no previewed plan/.test(statusText), statusText);

// ---- a product id never reaches an onclick attribute ---------------
console.log('\n[7] server text is escaped, and ids stay out of markup');
ok('the plan escapes product_id', /escText\(p\.product_id/.test(fnShow));
ok('the plan escapes the server detail', /escText\(d\.detail/.test(fnShow));
ok('per-branch detail text is escaped', /escText\(p\.detail\)/.test(fnShow));
ok('missing ids are escaped', /d\.missing\.map\(escText\)/.test(fnShow));
ok('applied ids are escaped', /escText\(a\.product_id\)/.test(fnExec));
ok('the confirm button interpolates NO data into its onclick',
   /onclick="executeGridLevels\(\)"/.test(fnShow)
   && !/onclick='executeGridLevels\(\$\{/.test(fnShow));
ok('the row escapes the product id into the input', /data-product="\$\{escText\(pid\)\}"/.test(fnRender));

// ---- the panel only claims what it can see -------------------------
console.log('\n[8] the panel shows parked branches and only parked branches');
const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const mkWrap = () => { let html = '', disp = ''; return {
  set innerHTML(v) { html = v; }, get innerHTML() { return html; },
  style: { set display(v) { disp = v; }, get display() { return disp; } },
  get _disp() { return disp; } }; };
const runRender = branches => {
  const wrap = mkWrap();
  new Function('document', 'escText', 'fmtUsd', CONSTS + fnSuggest + fnRender
    + '; return renderGridLevels;')(
    { getElementById: () => wrap }, esc, n => '$' + Number(n).toFixed(2))(branches);
  return wrap;
};
let w = runRender([]);
ok('no branches -> the panel is hidden', w._disp === 'none' && w.innerHTML === '');
w = runRender([{ product_id: 'A-USD', num_levels: 5, allocated_usd: 100,
                 slices: [slice(1, 5), slice(1, 5)] }]);
ok('a branch with room is NOT called parked', w._disp === 'none', w.innerHTML.slice(0, 80));
w = runRender([
  { product_id: 'LINK-USD', num_levels: 3, allocated_usd: 137.87,
    slices: [slice(2, 11), slice(2, 10.5), slice(2, 10)] },
  { product_id: 'A-USD', num_levels: 9, allocated_usd: 100, slices: [slice(1, 5)] },
]);
ok('the parked branch is listed', w.innerHTML.includes('LINK-USD'));
ok('the unparked branch is NOT listed', !w.innerHTML.includes('A-USD'));
ok('it counts 1 parked, not 2', /1 branch\(es\) are parked/.test(w.innerHTML),
   (w.innerHTML.match(/\d+ branch\(es\) are parked/) || [''])[0]);
ok('the row offers an input', /class="grid-levels-in"/.test(w.innerHTML));
ok('the preview button is the only one rendered at rest',
   (w.innerHTML.match(/onclick="/g) || []).length === 1);
ok('it says no order is placed', /no order is placed/i.test(w.innerHTML));
ok('it names num_levels as the only thing changed',
   /num_levels and nothing else/.test(w.innerHTML));
w = runRender([{ product_id: 'Q-USD', num_levels: 2, allocated_usd: 100,
                 slices: [slice(1, 5), { qty: 1, entry_price: null }] }]);
ok('an unpriced slice renders UNKNOWN, not $0.00', /UNKNOWN/.test(w.innerHTML));
ok('and leaves the input empty rather than guessing',
   /value=""/.test(w.innerHTML), (w.innerHTML.match(/value="[^"]*"/) || [''])[0]);

// ---- wiring --------------------------------------------------------
console.log('\n[9] wiring');
ok('the container exists', /id="grid-levels-wrap"/.test(src));
ok('it is fed the branches', /renderGridLevels\(d\.branches \|\| \[\]\)/.test(src));
const fnStart = src.indexOf('async function loadFleetReadiness()');
const body = src.slice(fnStart, src.indexOf('\nasync function ', fnStart + 10));
ok('the call rides the grid-status fetch the page already makes',
   body.includes('renderGridLevels(d.branches || [])')
   && (body.match(/apiGet\('\/grid-status'\)/g) || []).length === 1);
ok('the panel adds no fetch of its own', !/apiGet|XMLHttpRequest|fetch\(/.test(fnRender));
ok('the panel touches no threshold or other setting',
   !/allocated_usd\s*=|num_levels\s*=|GRID_CASH_RESERVE|stop_loss/.test(fnRender));

console.log();
if (fail) { console.log(`${fail} FAILED`); process.exit(1); }
console.log('all checks passed');
