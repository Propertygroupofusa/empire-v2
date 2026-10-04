// A setting you changed that is not in effect looked exactly like one that was.
//
// env_float and env_int fall back to the built-in default when a value cannot
// be parsed, record it, and carry on. That fail-soft is deliberate: a bad
// value must not kill the import, which is how one pasted "240 -> 3600" took
// the whole fleet offline on 4 October.
//
// But the record was only readable by fetching /module-health by hand. Live
// the same afternoon:
//
//   GRID_PARKED_MIN_NET_PCT = "1.0%"
//       -> could not convert string to float: '1.0%'
//       -> running with the 0.010 default
//       -> and NOTHING on any page said so
//
// The account owner believed he had changed a setting he had not, and found
// out only because the fleet happened to restart while someone was looking.
const fs = require('fs');
const path = require('path');
const checks = [];
const ok = (l, c, d) => checks.push([d ? `${l}  -- ${d}` : l, !!c]);
const HTML = fs.readFileSync(path.join(__dirname, 'family_tree_dashboard.html'), 'utf8');

function grab(name) {
    const i = HTML.indexOf(`async function ${name}(`);
    if (i < 0) return null;
    let j = HTML.indexOf('{', i), d = 0, k;
    for (k = j; k < HTML.length; k++) {
        if (HTML[k] === '{') d++;
        else if (HTML[k] === '}') { d--; if (!d) break; }
    }
    return HTML.slice(i, k + 1);
}

console.log('\n[1] the banner exists and is wired');
ok('the slot is on the page', /id="env-rejected-banner"/.test(HTML));
ok('it sits with the lock banner, not buried',
   HTML.indexOf('id="env-rejected-banner"') - HTML.indexOf('id="tab-lock-banner"') < 200);
const src = grab('renderEnvRejected');
ok('the renderer was found (not a vacuous pass)', !!src && src.length > 600,
   src ? `${src.length} chars` : 'MISSING');
ok('it is called on load beside renderTabLock', /renderTabLock\(\);\s*\n\s*renderEnvRejected\(\);/.test(HTML));
ok('it reads the real endpoint', /apiGet\('\/module-health'/.test(src || ''));

// Exercise the real function against a DOM stub.
const el = { innerHTML: '', style: { display: '' } };
let served = null, shouldThrow = false;
const make = new Function('document', 'apiGet', 'escText',
    `${src}\nreturn renderEnvRejected;`);
const fn = make(
    { getElementById: id => (id === 'env-rejected-banner' ? el : null) },
    async () => { if (shouldThrow) throw new Error('boom'); return served; },
    t => String(t).replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c])));

const REJECTED = {
    count: 1,
    note: '1 environment variable(s) could not be parsed and are NOT in effect;',
    fallbacks: [{ variable: 'GRID_PARKED_MIN_NET_PCT', bad_value: '1.0%',
                  expected: 'float', running_with_default: 0.01,
                  error: "could not convert string to float: '1.0%'" }],
};

async function run(payload, thr) {
    served = payload; shouldThrow = !!thr;
    el.innerHTML = ''; el.style.display = '';
    await fn();
    return { html: el.innerHTML, shown: el.style.display !== 'none' };
}

(async () => {
    console.log('\n[2] the live rejection is named in full');
    const r = await run({ env_fallbacks: REJECTED });
    ok('the banner is shown', r.shown);
    ok('it names the variable', r.html.includes('GRID_PARKED_MIN_NET_PCT'));
    ok('it shows what was SET', r.html.includes('1.0%'));
    ok('it shows what is RUNNING instead', r.html.includes('0.01'));
    ok('it gives the parser\'s own reason', r.html.includes('could not convert'));
    ok('it says a restart is needed', /takes a restart to apply/.test(r.html));
    ok('it says digits only', /digits and a decimal point only/.test(r.html));

    console.log('\n[3] it stays quiet when there is nothing to say');
    const clean = await run({ env_fallbacks: { count: 0, fallbacks: [], note: 'All numeric environment variables parsed cleanly.' } });
    ok('no banner when every variable parsed', !clean.shown && clean.html === '');

    console.log('\n[4] a failed read is NOT an all-clear, and never throws');
    const thrown = await run(null, true);
    ok('a thrown fetch hides the banner rather than claiming clean',
       !thrown.shown && thrown.html === '');
    const missing = await run({});
    ok('a payload with no env_fallbacks key says nothing', !missing.shown);
    const nullish = await run(null);
    ok('a null payload says nothing', !nullish.shown);

    console.log('\n[5] it cannot move money and cannot be injected into');
    ok('no fetch in the renderer beyond apiGet', !/fetch\(/.test(src));
    ok('no POST', !/POST/.test(src));
    ok('no write token is touched', !/writeHeaders|getWriteToken/.test(src));
    const evil = await run({ env_fallbacks: { count: 1, note: '<img src=x onerror=1>',
        fallbacks: [{ variable: '<script>bad</script>', bad_value: '<b>x</b>',
                      running_with_default: 1, error: '<i>e</i>' }] } });
    ok('every field is escaped', !/<script>bad<\/script>/.test(evil.html)
       && !/<img src=x/.test(evil.html) && evil.html.includes('&lt;script&gt;'));

    const w = Math.max(...checks.map(c => c[0].length));
    console.log();
    for (const [l, p] of checks) console.log(`  [${p ? 'PASS' : 'FAIL'}] ${l.padEnd(w)}`);
    const bad = checks.filter(c => !c[1]);
    console.log(`\n  ${checks.length - bad.length}/${checks.length} checks passed`);
    if (bad.length) process.exit(1);
})();
