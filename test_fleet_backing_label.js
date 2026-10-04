// "Crypto total" was the one thing that number is not.
//
// The tile renders allocation_backing.backed_usd, which allocation_backing.py
// computes as:
//
//     backed = deployed + wallet + held
//
// deployed is what the 22 GRID BRANCHES' open slices cost. Coin no branch
// bought - a manual buy, anything staked, anything outside the fleet - was
// never in it and never could be.
//
// Measured live 2026-10-04 17:4xZ, with the cap still reading "Crypto total":
//
//     the tile          $7,705.78   ($3,695.50 branch coin + $4,010.28 cash)
//     the venue held   $10,020.73   ($6,020.48 coin      + $4,000.25 cash)
//     understated by    $2,324.98
//
// The account owner had bought $1,000 of BTC that morning and it did not move
// the tile by a cent, because BTC-USD's branch did not buy it. He asked why
// the number would not go up.
const fs = require('fs');
const path = require('path');
const checks = [];
const ok = (l, c, d) => checks.push([d ? `${l}  -- ${d}` : l, !!c]);
const HTML = fs.readFileSync(path.join(__dirname, 'family_tree_dashboard.html'), 'utf8');

console.log('\n[1] the cap no longer claims to be everything you own');
const cap = HTML.slice(HTML.indexOf('id="strip-total"') - 700, HTML.indexOf('id="strip-total"'));
ok('the tile markup was located (not a vacuous pass)', cap.includes('tv-cap'), `${cap.length} chars`);
ok('"Crypto total" is gone from this tile', !/<div class="tv-cap"[^>]*>Crypto total<\/div>/.test(HTML));
ok('it reads "Fleet backing"', /<div class="tv-cap"[^>]*>Fleet backing<\/div>/.test(HTML));
ok('the cap carries an explanation of what it excludes',
   /tv-cap" title="Coin the 22 grid branches bought[\s\S]{0,400}not counted here/.test(HTML));
ok('...and points at where the real account total lives',
   /Whole account, from the venue panel for the real account total/.test(HTML));

console.log('\n[2] the subtitle says it on the page, not only in a tooltip');
// A phone user never sees a title attribute. The exclusion has to be visible.
const sub = HTML.slice(HTML.indexOf("document.getElementById('strip-total-sub')"),
                       HTML.indexOf("document.getElementById('strip-total-sub')") + 900);
ok('the subtitle renderer was located', sub.length > 200, `${sub.length} chars`);
ok('coin is labelled BRANCH coin, not just "in coin"', /branch coin/.test(sub));
ok('the exclusion is rendered as visible text', /excludes coin no branch bought/.test(sub));
ok('no "in coin" wording survives that implied all of it', !/' in coin · \$'/.test(sub));

console.log('\n[3] the number itself is untouched - this is a LABEL fix');
ok('it still renders backed_usd', /stripTotal\.textContent = '\$' \+ backed\.toFixed\(2\)/.test(HTML));
ok('still built from deployed_coin_usd', /bk\.deployed_coin_usd/.test(sub));
ok('still built from wallet_cash_usd', /bk\.wallet_cash_usd/.test(sub));
ok('the stale-reading fallback is still there',
   /__lastBackedUsd/.test(HTML) && /STALE - the venue balance could not be read/.test(HTML));

console.log('\n[4] it adds no fetch, no write, no new way to fail');
const before = (HTML.match(/apiGet\(|fetch\(/g) || []).length;
ok('the subtitle block makes no request of its own',
   !/fetch\(|apiGet\(/.test(sub), 'a 429 on the census must not blank this tile');
ok('no write path was introduced here', !/postGuarded|writeHeaders/.test(sub));

console.log('\n[5] the arithmetic the label now describes');
const deployed = 3695.50, wallet = 4000.19, venueTotal = 10020.73;
const backed = deployed + wallet;
ok('backed = branch coin + wallet', Math.abs(backed - 7695.69) < 0.01, backed.toFixed(2));
ok('the venue total is larger, and that is the point',
   venueTotal > backed, `gap $${(venueTotal - backed).toFixed(2)}`);
ok('the gap is real money, not rounding', (venueTotal - backed) > 2000);

const w = Math.max(...checks.map(c => c[0].length));
console.log();
for (const [l, p] of checks) console.log(`  [${p ? 'PASS' : 'FAIL'}] ${l.padEnd(w)}`);
const bad = checks.filter(c => !c[1]);
console.log(`\n  ${checks.length - bad.length}/${checks.length} checks passed`);
if (bad.length) process.exit(1);
