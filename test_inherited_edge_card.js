// The maker-only card must not drop the inherited exits it stopped counting.
//
// realized_edge.current now excludes adopted_exit rows, which is correct -
// a position the grid never opened says nothing about what its step earns.
// But the money is real. On 2026-10-05 that was four ZEC exits at -$311.24,
// enough to make the pooled book read -2.07% while the grid own 122 cycles
// were about +1.67%. Hiding it would swap one misleading number for another.
//
// Run: node test_inherited_edge_card.js
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, 'family_tree_dashboard.html'), 'utf8');

const checks = [];
const ok = (label, cond) => checks.push([label, !!cond]);

// Pull the card body so a match elsewhere in the file cannot pass for one here.
const start = html.indexOf('const re = data.realized_edge;');
ok('the realized-edge card is still there', start > 0);
const body = html.slice(start, start + 9000);

ok('the card reads the inherited cohort', body.includes('re.current_inherited'));
ok('it guards on there BEING one', /re\.current_inherited\s*&&\s*re\.current_inherited\.trades/.test(body));
ok('it prints the inherited dollars', body.includes('re.current_inherited.net_usd'));
ok('it says the figure above excludes them', /[Nn]ot counted above/.test(body));
ok('it names them as positions the grid did not open',
   /did not open/.test(body));
ok('it says a GAIN would be excluded too, not just a loss',
   /gain would be excluded/i.test(body));
ok('it still offers the pooled book', body.includes('re.current_including_inherited'));

// Absence must render as nothing, never as a zero-dollar banner.
const inheritedBlock = body.slice(body.indexOf('re.current_inherited'));
ok('with no inherited exits it renders an empty string',
   /:\s*''\s*\)\s*\+/.test(inheritedBlock.slice(0, 2600)));

// The headline figure must still be the grid's own.
//
// Anchored on the expression that RENDERS the number, not on the string
// appearing anywhere in the card: re.current.net_pct also sits in the
// colour ternary one line above, so a bare includes() passed a mutant
// that swapped the displayed value to the pooled cohort.
const headline = body.slice(0, body.indexOf('realized, CURRENT config'));
const shown = headline.slice(headline.lastIndexOf('</b>') > 0
    ? headline.lastIndexOf('${re.current.trades ?') : 0);
ok('the displayed percentage is re.current.net_pct',
   /\$\{re\.current\.trades \? \(re\.current\.net_pct >= 0[^}]*re\.current\.net_pct\.toFixed\(2\)/.test(headline));
ok('the displayed percentage is NOT the pooled cohort',
   !headline.includes('current_including_inherited'));
ok('and the cycle count beside it is the grid own count',
   /\$\{re\.current\.trades\} completed cycle/.test(body));

// Sign handling: the dollar line must not print "+-$311.24".
ok('negative dollars print one sign, not two',
   body.includes("Math.abs(re.current_inherited.net_usd)"));
ok('and the sign is chosen from the value',
   /net_usd\s*>=\s*0\s*\?\s*'\+'\s*:\s*'-'/.test(body));

const failed = checks.filter(([, c]) => !c);
checks.forEach(([l, c]) => console.log(`  ${c ? 'PASS' : 'FAIL'}  ${l}`));
console.log(`\n${checks.length - failed.length}/${checks.length} passed`);
process.exit(failed.length ? 1 : 0);
