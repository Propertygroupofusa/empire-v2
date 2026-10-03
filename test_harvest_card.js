// The harvest card must actually RUN and render the right things.
//
// It is extracted from the page and executed with the page's OWN fmtUsd and
// escText - a stubbed formatter would test the stub, which is how an earlier
// test in this project reported a failure the page did not have.
const fs = require('fs');
const html = fs.readFileSync('family_tree_dashboard.html', 'utf8');

function extract(name) {
    const start = html.indexOf('function ' + name + '(');
    if (start < 0) throw new Error('not found: ' + name);
    let i = html.indexOf('{', start), depth = 0;
    for (let j = i; j < html.length; j++) {
        if (html[j] === '{') depth++;
        else if (html[j] === '}') { depth--; if (!depth) return html.slice(start, j + 1); }
    }
    throw new Error('unbalanced: ' + name);
}
function extractAsync(name) {
    const start = html.indexOf('async function ' + name + '(');
    if (start < 0) throw new Error('not found: ' + name);
    let i = html.indexOf('{', start), depth = 0;
    for (let j = i; j < html.length; j++) {
        if (html[j] === '{') depth++;
        else if (html[j] === '}') { depth--; if (!depth) return html.slice(start, j + 1); }
    }
    throw new Error('unbalanced: ' + name);
}

const src = extract('fmtUsd') + '\n' + extract('escText') + '\n'
          + extractAsync('renderGridHarvest');

let fails = 0, passes = 0;
function ok(cond, msg) {
    if (cond) { passes++; } else { fails++; console.log('  FAIL: ' + msg); }
}

// The real shape the live endpoint returns, verified against production.
const LIVE = {
    branches: [
        {bot_name: 'crypto_grid_14', product_id: 'QNT-USD', allocated_usd: 160.65,
         realised_total: 1.19, baseline: 1.19, earned_since_baseline: 0.0,
         harvest_usd: 0.0, why_not: '$0.00 earned since baseline, under the $10.00 minimum'},
        {bot_name: 'crypto_grid_12', product_id: 'BCH-USD', allocated_usd: 171.29,
         realised_total: 0.58, baseline: 0.58, earned_since_baseline: 0.0,
         harvest_usd: 0.0, why_not: 'holds 3 open slice(s); withdraw needs a flat branch'},
    ],
    total_harvest_usd: 0.0, ready: false, unwatched_branches: 0,
    loop_has_run: true, read_only: true, detail: 'Nothing was withdrawn.'
};

function run(payload, {fetchThrows = false, httpStatus = 200} = {}) {
    const el = {style: {}, innerHTML: ''};
    const ctx = {
        API_BASE: 'https://x/api',
        document: {getElementById: (id) => id === 'grid-harvest-wrap' ? el : null},
        fetch: async () => {
            if (fetchThrows) throw new Error('network down');
            return {ok: httpStatus === 200, status: httpStatus,
                    json: async () => payload};
        },
        console,
    };
    const fn = new Function('ctx', `
        const {API_BASE, document, fetch, console} = ctx;
        ${src}
        return renderGridHarvest();
    `);
    return fn(ctx).then(() => el);
}

(async () => {
    console.log('--- renders against the real live payload ---');
    let el = await run(LIVE);
    ok(el.style.display === 'block', 'card is visible');
    ok(el.innerHTML.length > 400, 'card rendered substantial HTML, got ' + el.innerHTML.length);
    ok(/Profit harvest/.test(el.innerHTML), 'has a title');
    ok(/watching 2 of 2 branches/.test(el.innerHTML), 'reports watched count');
    ok(!/has not completed a pass/.test(el.innerHTML), 'does not claim the loop is dead');
    ok(/\$0\.00 ready to come off the table/.test(el.innerHTML), 'leads with $0.00 ready');

    console.log('--- the anti-panic line is present and unmissable ---');
    ok(/TOTAL ALLOCATED \(GRID\) goes DOWN/.test(el.innerHTML),
       'warns allocated will fall');
    ok(/banked, not lost/.test(el.innerHTML), 'says banked not lost');
    ok(/never reduced by a\s+harvest/.test(el.innerHTML),
       'says realised is never reduced');

    console.log('--- a branch holding coin is excluded from the flat list ---');
    ok(!/BCH-USD/.test(el.innerHTML),
       'BCH holds 3 slices so it must NOT be listed as flat');
    ok(/QNT-USD/.test(el.innerHTML), 'QNT is flat and must be listed');

    console.log('--- shows money when there is money ---');
    el = await run({...LIVE, total_harvest_usd: 46.5, ready: true,
        branches: [{...LIVE.branches[0], harvest_usd: 46.5,
                    earned_since_baseline: 46.5, why_not: null}]});
    ok(/\$46\.50 ready to come off the table/.test(el.innerHTML), 'headline amount');
    ok(/take \$46\.50/.test(el.innerHTML), 'per-branch take shown');
    ok(/2e8b57/.test(el.innerHTML), 'headline is green when positive');

    console.log('--- an un-run loop is reported as such, not as $0 ---');
    el = await run({branches: [{bot_name: 'b', product_id: 'A-USD',
        allocated_usd: 100, realised_total: 5, baseline: null,
        earned_since_baseline: null, harvest_usd: 0,
        why_not: 'no baseline yet - the harvest has not seen this branch'}],
        total_harvest_usd: 0, unwatched_branches: 1, loop_has_run: false});
    ok(/has not completed a pass/.test(el.innerHTML), 'flags the un-run loop');
    ok(/d9534f/.test(el.innerHTML), 'flags it in red');
    ok(!/A-USD/.test(el.innerHTML),
       'a branch with no baseline must not be listed as harvestable');

    console.log('--- a dead endpoint says so instead of looking empty ---');
    el = await run(LIVE, {fetchThrows: true});
    ok(el.style.display === 'block', 'still visible on error');
    ok(/status unavailable/.test(el.innerHTML), 'says status unavailable');
    ok(/network down/.test(el.innerHTML), 'surfaces the real error');
    ok(/display problem only/.test(el.innerHTML),
       'reassures that money is unaffected');
    ok(!/ready to come off the table/.test(el.innerHTML),
       'must NOT show a $0.00 figure it could not read');

    el = await run(LIVE, {httpStatus: 500});
    ok(/status unavailable/.test(el.innerHTML), 'HTTP 500 is an error too');
    ok(/HTTP 500/.test(el.innerHTML), 'names the status code');

    console.log('--- no token is used, so a locked tab still works ---');
    ok(!/postGuarded|x-dashboard-token|empire_write_token/.test(src),
       'the card must not touch the write-token path');
    ok(/cache: *'no-store'/.test(src), 'reads fresh, not cached');

    console.log(`\n${passes} passed, ${fails} failed`);
    process.exit(fails ? 1 : 0);
})();
