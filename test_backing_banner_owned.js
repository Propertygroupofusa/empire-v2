// The "claims coin the wallet does not hold" banner must read OWNED units.
//
// It said "5 branch(es) claim coin the wallet does not hold" over $743.71
// on 2026-10-05. Four of the five owned every unit they claimed - ALGO held
// 1347.646389 against a 492.70 claim. The banner was reading `backing`,
// which measures AVAILABLE units: correct for "can this branch sell right
// now", wrong for the existence claim in its own headline.
//
// The same misread in a buy gate refused four healthy branches for four
// hours on 2026-10-04. The server has published `backing_owned` beside
// `backing` ever since. This banner was still on the other one.
//
// Run: node test_backing_banner_owned.js
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, 'family_tree_dashboard.html'), 'utf8');

const checks = [];
const ok = (label, cond) => checks.push([label, !!cond]);

// --- the call site ---------------------------------------------------------
ok('the banner is handed backing_owned',
   /renderGridReconcile\(\s*d\.backing_owned\s*\|\|\s*null\s*,/.test(html));
ok('and the available block only as a second argument',
   /renderGridReconcile\(\s*d\.backing_owned[^)]*d\.backing\s*\|\|\s*null\s*\)/.test(html));
ok('it is NOT called with d.backing as the primary',
   !/renderGridReconcile\(\s*d\.backing\s*[,)]/.test(html));

// --- the function body -----------------------------------------------------
const start = html.indexOf('function renderGridReconcile(');
ok('the function is still there', start > 0);
const body = html.slice(start, start + 4200);
const code = body.split('\n').filter(l => !l.trim().startsWith('//')).join('\n');

ok('it takes the available block as a separate parameter',
   /function renderGridReconcile\(\s*bk\s*,\s*avail\s*\)/.test(code));
ok('the headline count comes from the primary block',
   /const n = Number\(bk\.unbacked_branches\)/.test(code));
ok('the headline dollars come from the primary block',
   /const short = Number\(bk\.not_in_wallet_usd\)/.test(code));

// Fail-closed: no silent fallback to the available block.
ok('an unreadable primary hides the banner',
   /if \(!bk \|\| bk\.readable !== true\) \{[\s\S]{0,120}display = 'none'/.test(code));
ok('it never falls back to the available block for the headline',
   !/bk\s*=\s*avail/.test(code) && !/avail\.not_in_wallet_usd[^}]*headline/i.test(code));

// --- the on-hold line ------------------------------------------------------
ok('the owned basis is stated to the reader', /units the account <strong>owns<\/strong>/.test(body));
ok('on-hold coin is shown separately when there is more of it',
   /Number\(avail\.unbacked_branches\) > Number\(n\)/.test(body));
ok('and is named as owned, not missing', /is owned and is not\s*\n?\s*missing/.test(body));
ok('it says reconcile will not touch it', /reconcile will not touch it/.test(body));
ok('the separate line renders nothing when there is no extra',
   /:\s*''\}/.test(body.slice(body.indexOf('Separately'))));
ok('the on-hold line guards on the available block being readable',
   /avail\.readable === true/.test(body));

const failed = checks.filter(([, c]) => !c);
checks.forEach(([l, c]) => console.log(`  ${c ? 'PASS' : 'FAIL'}  ${l}`));
console.log(`\n${checks.length - failed.length}/${checks.length} passed`);
process.exit(failed.length ? 1 : 0);
