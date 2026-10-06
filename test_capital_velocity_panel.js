// The velocity panel's job is to show a ranking WITHOUT becoming a buy
// list. These are the promises that keep it on the right side of that.
//
// Run as written: node test_capital_velocity_panel.js
const fs = require('fs');
const SRC = fs.readFileSync('family_tree_dashboard.html', 'utf8');
const fn = SRC.slice(SRC.indexOf('async function renderCapitalVelocity() {'));
const body = fn.slice(0, fn.indexOf('\nfunction renderGridOpportunityPanel'));

let fails = 0;
function ok(name, cond) {
    console.log((cond ? '  PASS  ' : '  FAIL  ') + name);
    if (!cond) fails++;
}
function section(n) { console.log('\n' + n); }

section('[1] the panel exists and is wired to the same poll as the census');
ok('it has a mount point', /id="capital-velocity-panel"/.test(SRC));
ok('it is rendered on the dashboard refresh',
   /renderAccountCensus\(\);\s*\n\s*renderCapitalVelocity\(\);/.test(SRC));
ok('it reads the capital-velocity endpoint and nothing else',
   /fetch\('\/api\/trading-dashboard\/grid-status\/capital-velocity'\)/.test(body));

section('[2] it cannot trade');
for (const bad of ['/orders', 'place_order', 'method:', 'POST', 'x-dashboard-token']) {
    ok(`no ${bad}`, !body.includes(bad));
}

section('[3] THE GATE is printed above the ranking, not under it');
const gateAt = body.indexOf('routing_allowed');
const tableAt = body.indexOf('<thead>');
ok('the routing verdict is read from the payload', gateAt > -1);
ok('and drawn BEFORE the ranked table - a caveat under a table of winners '
   + 'is a buy list with a footnote', gateAt < tableAt);
ok('when routing is not allowed the heading says so in plain words',
   /Not a buy list/.test(body));
ok('and says the ranking is not a reason to move money',
   /not a reason to move money/.test(body));

section('[4] a missing number is UNKNOWN, never a zero');
ok('the number formatter returns "unknown" for null/undefined',
   /\(v === null \|\| v === undefined\)[\s\S]{0,80}unknown/.test(body));
ok('deployable cash is not defaulted to 0', !/deployable_now_usd \|\| 0/.test(body));
ok('trapped capital is not defaulted to 0', !/trapped_usd \|\| 0/.test(body));
ok('server-side unknowns are surfaced, not swallowed',
   /d\.unknowns/.test(body));

section('[5] the two figures that must not be confused');
ok('deployable-now gets its own line', /can buy something right now/.test(body));
ok('and says plainly that claim is not money',
   /Claim is a name on a rung, not money/.test(body));
ok('thin branch samples are reported as withheld, not ranked',
   /too_few_cycles/.test(body) && /not evidence/.test(body));

section('[6] the CAPITAL ENGINE block leads with money, then rates');
const engineAt = body.indexOf('Verified deployable');
const rateAt = body.indexOf('$/capital-day');
ok('the buckets are drawn', engineAt > -1);
ok('and BEFORE the rates - every rate is meaningless if the money it '
   + 'describes cannot be spent', engineAt < rateAt);
for (const field of ['Reserved', 'In coin', 'Unresolved', 'Capital-days',
                     'Avg hold', 'Completed cycles', 'Recycle rate',
                     'Realized profit']) {
    ok(`the block shows ${field}`, body.includes(field));
}
ok('sell to buy is shown as a DISTRIBUTION, not one number',
   /p50_hours[\s\S]{0,120}p75_hours[\s\S]{0,120}p90_hours/.test(body));

section('[7] the never-deployable rule is reported either way');
ok('a violation is drawn in red and names what leaked',
   /deployable_violations[\s\S]{0,900}#ef4444/.test(body));
ok('and a clean read SAYS it was checked rather than staying silent',
   /checked this read, no leak/.test(body));
ok('the rule is stated in the words the owner used',
   /never buys, never routes, never counts as available/.test(body));

console.log(fails === 0 ? '\nALL PASS' : `\n${fails} FAILED`);
process.exit(fails === 0 ? 0 : 1);
