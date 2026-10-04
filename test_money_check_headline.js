// "Nothing is sitting still" was printed over money that was sitting still.
//
// The money-check headline chose between two sentences by counting
// findings that carry an `action`:
//
//     actionable.length ? "N things worth doing" : "Nothing is sitting
//                                                  still - every dollar
//                                                  that can be working, is."
//
// But crypto_grid_bot.money_check() raises four findings with
// action: null that each say, in their own detail text, that money is
// NOT working:
//
//   paused_with_capital  "N paused branch(es) still hold $X ... A paused
//                         branch never buys, so that capital is idle by
//                         choice."            (no severity key at all)
//   nothing_deployed     "Every branch is flat, so nothing is invested in
//                         any coin right now."              severity warn
//   cash_overdrawn       "Branch reserves exceed the wallet by $X."
//                                                            severity bad
//   cash_unknown         "The real Coinbase balance could not be read, so
//                         idle cash cannot be judged."       severity warn
//
// Every one of them produced the green all-clear. money_check's own
// source comment on cash_overdrawn records that an earlier version of
// the SERVER "ran this through the 'committed' branch, which printed a
// calm green tick over it" - the finding was fixed there, and the
// headline above it went on doing exactly that.
//
// severity decides now. 'ok' is the only all-clear, and a finding with no
// severity has not claimed to be one, so absence fails closed.
const fs = require('fs');
const path = require('path');
const checks = [];
const ok = (l, c, d) => checks.push([d ? `${l}  -- ${d}` : l, !!c]);

const SRC = fs.readFileSync(
    path.join(__dirname, 'family_tree_dashboard.html'), 'utf8');

// ── extract the two real functions, so this tests the page, not a copy ──
function grab(name) {
    const start = SRC.indexOf(`function ${name}(`);
    if (start < 0) return null;
    // Brace-match from the first { after the signature.
    let i = SRC.indexOf('{', start), depth = 0;
    for (let j = i; j < SRC.length; j++) {
        if (SRC[j] === '{') depth++;
        else if (SRC[j] === '}') { depth--; if (!depth) return SRC.slice(start, j + 1); }
    }
    return null;
}
const renderSrc = grab('renderMoneyCheck');
const iconSrc = grab('moneyIcon');

console.log('\n[1] both functions were actually located');
ok('renderMoneyCheck source found (not a vacuous pass)',
   !!renderSrc && renderSrc.length > 500, renderSrc ? `${renderSrc.length} chars` : 'MISSING');
ok('moneyIcon source found', !!iconSrc && iconSrc.includes('severity'));

// ── a DOM stub just wide enough ──
const el = { innerHTML: '' };
const sandbox = {
    document: { getElementById: (id) => (id === 'money-check-wrap' ? el : null) },
    fmtSignedUsd: (v) => (v == null ? '—' : (v < 0 ? '-$' : '$') + Math.abs(v).toFixed(2)),
    SAFE_MONEY_ACTIONS: { '/grid-status/reanchor-flat-branches':
        { label: 'Re-anchor', safety: 'no order is placed' } },
};
const make = new Function('document', 'fmtSignedUsd', 'SAFE_MONEY_ACTIONS',
    `${iconSrc}\n${renderSrc}\nreturn renderMoneyCheck;`);
const renderMoneyCheck = make(sandbox.document, sandbox.fmtSignedUsd,
                              sandbox.SAFE_MONEY_ACTIONS);

const GREEN = 'Nothing is sitting still';
const NOBUTTON = 'that no button here can fix';
const NOTMEASURED = 'Nothing was measured';
const THREW = 'Could not render the money check';

function render(findings) {
    el.innerHTML = '';
    renderMoneyCheck({ findings, deployed_usd: 0, free_cash_usd: 0,
                       allocated_usd: 0, reserve_usd: 0 });
    return el.innerHTML;
}

// The four real findings, copied from crypto_grid_bot.money_check().
const PAUSED = { kind: 'paused_with_capital', usd: 1960.06, action: null,
    detail: '3 paused branch(es) still hold $1,960.06. A paused branch never buys, '
          + 'so that capital is idle by choice.',
    basis: 'allocated_usd on branches with active = false' };            // no severity
const FLAT = { kind: 'nothing_deployed', usd: 5021.46, severity: 'warn', action: null,
    detail: 'Every branch is flat, so nothing is invested in any coin right now.',
    basis: 'branches holding zero open slices hold zero coin' };
const OVERDRAWN = { kind: 'cash_overdrawn', usd: -104.55, severity: 'bad', action: null,
    detail: 'Branch reserves exceed the wallet by $104.55.', basis: 'wallet vs reserves' };
const UNKNOWN = { kind: 'cash_unknown', usd: null, severity: 'warn', action: null,
    detail: 'The real Coinbase balance could not be read, so idle cash cannot be judged.',
    basis: 'unknown cash is never treated as deployable' };
const DEPLOYED = { kind: 'deployed', usd: 10663.80, severity: 'ok', action: null,
    detail: '$10,663.80 is genuinely in coin.', basis: 'entry_price x qty' };
const COMMITTED = { kind: 'cash_committed', usd: 0, severity: 'ok', action: null,
    detail: 'The free cash is the reserve.', basis: 'GRID_CASH_RESERVE_USD' };
const STALE = { kind: 'stale_reference', usd: null,
    action: '/grid-status/reanchor-flat-branches',
    detail: '4 flat branch(es) measure from a stale reference.',
    basis: 'live price against each stored reference' };

console.log('\n[2] nothing threw - the catch block would hide every failure below');
for (const [name, f] of [['paused', [PAUSED]], ['flat', [FLAT]],
                         ['overdrawn', [OVERDRAWN]], ['unknown', [UNKNOWN]],
                         ['all ok', [DEPLOYED, COMMITTED]], ['empty', []],
                         ['missing', undefined]]) {
    ok(`${name}: rendered without throwing`, !render(f).includes(THREW));
}

console.log('\n[3] the four action-less findings no longer read as an all-clear');
for (const [name, f] of [['paused_with_capital', PAUSED], ['nothing_deployed', FLAT],
                         ['cash_overdrawn', OVERDRAWN], ['cash_unknown', UNKNOWN]]) {
    const html = render([f]);
    ok(`${name}: does NOT claim "${GREEN}"`, !html.includes(GREEN));
    ok(`${name}: says so instead`, html.includes(NOBUTTON));
}

console.log('\n[4] no findings at all is not an all-clear either');
ok('an empty list says nothing was measured',
   render([]).includes(NOTMEASURED) && !render([]).includes(GREEN));
ok('a missing findings key says the same',
   render(undefined).includes(NOTMEASURED) && !render(undefined).includes(GREEN));
ok('a non-array findings value says the same',
   render({}).includes(NOTMEASURED) && !render({}).includes(GREEN));

console.log('\n[5] the green sentence is still reachable - this is not a blanket ban');
const allOk = render([DEPLOYED, COMMITTED]);
ok('every finding severity ok -> the green all-clear', allOk.includes(GREEN));
ok('and it does not also print the warning', !allOk.includes(NOBUTTON));

console.log('\n[6] an actionable finding still wins the headline');
const act = render([STALE, PAUSED]);
ok('"1 thing worth doing right now" is shown',
   act.includes('1 thing worth doing right now'));
ok('the unresolved card is still listed below it',
   act.includes('paused with capital'));
ok('the green claim is not made alongside it', !act.includes(GREEN));

console.log('\n[7] moneyIcon no longer ticks a finding with no severity');
const icon = make(sandbox.document, sandbox.fmtSignedUsd, sandbox.SAFE_MONEY_ACTIONS)
    && new Function(`${iconSrc}\nreturn moneyIcon;`)();
ok('no severity -> not a green tick', icon(PAUSED, false) !== '✅',
   JSON.stringify(icon(PAUSED, false)));
ok("severity 'ok' -> still a green tick", icon(DEPLOYED, false) === '✅');
ok("severity 'warn' -> info", icon(FLAT, false) === 'ℹ️');
ok("severity 'bad' -> warning", icon(OVERDRAWN, false) === '⚠️');
ok('an actionable finding -> the bolt', icon(STALE, true) === '⚡');

const w = Math.max(...checks.map(c => c[0].length));
console.log();
for (const [l, p] of checks) console.log(`  [${p ? 'PASS' : 'FAIL'}] ${l.padEnd(w)}`);
const bad = checks.filter(c => !c[1]);
console.log(`\n  ${checks.length - bad.length}/${checks.length} checks passed`);
if (bad.length) process.exit(1);
