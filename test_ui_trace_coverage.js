// Five of seven preview buttons reported nothing, and that cost three rounds.
//
// The account owner pressed a preview twice. /ui-trace read 0 events and
// /write-guard read 0 attempts - and because only previewGridReconcile and
// previewGridLevels emitted anything, those two zeros could not distinguish
//
//     "the click never left the browser"   from   "a different button"
//
// Three more holes were already there, each at the OUTCOME end of a path
// that was otherwise traced, so the codes sat on the server allowlist and
// had never once fired:
//
//     rec_preview_threw   previewGridReconcile's catch said nothing - on
//                         the very button being diagnosed, a thrown request
//                         read identically to one still in flight
//     rec_apply_ok        a successful reconcile left no positive mark
//     apply_threw         executeGridLevels' catch said nothing
//
// THE TWO HALVES MUST MATCH. ui_trace_endpoint drops any code not on
// _UI_TRACE_OK with {"recorded": false}, so a beacon added to the page
// without its name added to the server is silently inert - which looks
// exactly like the bug it was added to find.
const fs = require('fs');
const path = require('path');
const checks = [];
const ok = (l, c, d) => checks.push([d ? `${l}  -- ${d}` : l, !!c]);

const HTML = fs.readFileSync(path.join(__dirname, 'family_tree_dashboard.html'), 'utf8');
const PY = fs.readFileSync(path.join(__dirname, 'routers', 'trading_dashboard.py'), 'utf8');

// ---- every code the page emits, and every code the server admits ----
const emitted = new Set();
for (const m of HTML.matchAll(/uiTrace\(\s*(?:[^,'")]*\?\s*)?'([a-z0-9_]+)'/g)) emitted.add(m[1]);
for (const m of HTML.matchAll(/uiTrace\([^)]*:\s*'([a-z0-9_]+)'/g)) emitted.add(m[1]);

const i0 = PY.indexOf('_UI_TRACE_OK = {');
const allowed = new Set(
    [...PY.slice(i0, PY.indexOf('\n}', i0)).matchAll(/"([a-z0-9_]+)"/g)].map(m => m[1]));

console.log('\n[1] both files were actually read (not a vacuous pass)');
ok('the page yielded codes', emitted.size > 40, `${emitted.size} emitted`);
ok('the allowlist was located', allowed.size > 40, `${allowed.size} allowed`);

console.log('\n[2] the two halves match exactly - an unlisted code is dropped');
const orphanPage = [...emitted].filter(c => !allowed.has(c)).sort();
const orphanSrv = [...allowed].filter(c => !emitted.has(c)).sort();
ok('no beacon fires a code the server would drop', orphanPage.length === 0,
   orphanPage.join(',') || 'none');
ok('no allowlisted code is dead', orphanSrv.length === 0, orphanSrv.join(',') || 'none');

console.log('\n[3] the three codes that had never fired now do');
for (const c of ['rec_preview_threw', 'rec_apply_ok', 'apply_threw']) {
    ok(`${c} is emitted somewhere`, emitted.has(c));
}

// ---- which functions send a write, and whether they report ----
function bodies() {
    const out = {};
    for (const m of HTML.matchAll(/(?:async\s+)?function\s+([A-Za-z0-9_]+)\s*\(/g)) {
        const i = m.index;
        let j = HTML.indexOf('{', i), d = 0, k;
        for (k = j; k < HTML.length; k++) {
            if (HTML[k] === '{') d++;
            else if (HTML[k] === '}') { d--; if (!d) break; }
        }
        out[m[1]] = HTML.slice(i, k + 1);
    }
    return out;
}
const B = bodies();
const TARGETS = ['previewSale', 'executeSale', 'previewFreeCash', 'runFreeCash',
                 'runDeployFreed', 'previewGridRotation', 'runGridRotation',
                 'previewConsolidate', 'executeConsolidate',
                 'previewReconcile', 'executeReconcile',
                 'previewGridReconcile', 'executeGridReconcile',
                 'previewGridLevels', 'executeGridLevels'];

console.log('\n[4] every control in scope reports entry, send, outcome and failure');
for (const fn of TARGETS) {
    const b = B[fn];
    if (!b) { ok(`${fn}: function found`, false, 'MISSING'); continue; }
    const codes = [...b.matchAll(/uiTrace\(\s*(?:[^,'")]*\?\s*)?'([a-z0-9_]+)'/g)].map(m => m[1]);
    const has = sfx => codes.some(c => c.endsWith(sfx));
    ok(`${fn}: entry beacon`, has('_enter'), codes.length + ' total');
    ok(`${fn}: send beacon`, has('_sending'));
    ok(`${fn}: outcome beacon`, has('_ok') || has('_nothing'));
    ok(`${fn}: failure beacon`, has('_threw') || /catch/.test(b) === false);
}

console.log('\n[5] no write path still dereferences a panel node outside its try');
// previewSale, previewFreeCash, previewConsolidate and previewReconcile each
// read getElementById(...).innerHTML before entering the try, so a missing
// panel threw an uncaught TypeError and left no trace at all.
for (const fn of ['previewSale', 'previewFreeCash', 'previewConsolidate', 'previewReconcile']) {
    const b = B[fn] || '';
    const head = b.slice(0, b.indexOf('try {'));
    ok(`${fn}: guards its nodes before using them`,
       /if \(!\w+(\s*\|\|\s*!\w+)?\)\s*\{[^}]*uiTrace\('[a-z0-9_]+_no_nodes'/.test(head)
       || /if \(!\w+\) \{ uiTrace\('[a-z0-9_]+_no_nodes'\); return; \}/.test(head));
}

console.log('\n[6] a dismissed confirm() is told apart from a failed write');
for (const fn of ['executeReconcile', 'executeConsolidate', 'runGridRotation']) {
    ok(`${fn}: records the cancel`, /uiTrace\('[a-z0-9_]+_(cancelled|no_ticket)'\)/.test(B[fn] || ''));
}

console.log('\n[7] every n is a whole number - the endpoint types it as int');
// uiTrace's n reaches `n: int = None`. A fractional value is rejected with
// 422 and the event is LOST, which is the opposite of a diagnostic.
// Only CALL SITES - uiTrace's own `function uiTrace(code, n)` signature and
// its internal use of n are not beacons, and matching them made this check
// fail on the helper rather than on any caller.
const args = [...HTML.matchAll(/uiTrace\(\s*(?:[^,'")]*\?\s*)?'[a-z0-9_]+'\s*,\s*([^;]+?)\);/g)]
    .map(m => m[1].trim());
ok('second arguments were found', args.length >= 10, `${args.length} found`);
// Whitespace-insensitive: the pre-existing rec_no_nodes call is written
// (st?1:0)+(planWrap?2:0) with no spaces and is perfectly integer-safe.
const SAFE = [/\.length\b/, /^Math\.round\(/, /^Number\([^)]*\)\s*\|\|\s*0$/,
              /^\d+$/,
              /^\(\s*\w+\s*\?\s*1\s*:\s*0\s*\)\s*\+\s*\(\s*\w+\s*\?\s*2\s*:\s*0\s*\)$/];
const unsafe = args.filter(a => !SAFE.some(re => re.test(a)));
ok('no beacon can pass a fraction', unsafe.length === 0, unsafe.join(' | ') || 'none');

console.log('\n[8] the buffer was raised to hold a full session');
const m = PY.match(/_UI_TRACE_MAX = (\d+)/);
ok('_UI_TRACE_MAX was found', !!m, m ? m[1] : 'MISSING');
ok('it is large enough for the new volume', m && Number(m[1]) >= 120, m && m[1]);
ok('the beacon still cannot record free text',
   /if code not in _UI_TRACE_OK:/.test(PY) && /"code not on the allowlist"/.test(PY));
ok('it is still a GET, so no token is ever involved',
   /@router\.get\("\/ui-trace"\)/.test(PY));

const w = Math.max(...checks.map(c => c[0].length));
console.log();
for (const [l, p] of checks) console.log(`  [${p ? 'PASS' : 'FAIL'}] ${l.padEnd(w)}`);
const bad = checks.filter(c => !c[1]);
console.log(`\n  ${checks.length - bad.length}/${checks.length} checks passed`);
if (bad.length) process.exit(1);
