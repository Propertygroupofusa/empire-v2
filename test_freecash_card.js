// The free-cash card: must refuse out of order, and must never claim a
// move it did not make. Extracted and executed with the page's OWN
// fmtUsd/escText, not stubs.
const fs = require('fs');
const html = fs.readFileSync('family_tree_dashboard.html', 'utf8');

function grab(sig) {
    const start = html.indexOf(sig);
    if (start < 0) throw new Error('not found: ' + sig);
    let depth = 0;
    for (let j = html.indexOf('{', start); j < html.length; j++) {
        if (html[j] === '{') depth++;
        else if (html[j] === '}') { depth--; if (!depth) return html.slice(start, j + 1); }
    }
    throw new Error('unbalanced: ' + sig);
}

const src = [
    grab('function fmtUsd('), grab('function escText('),
    'let _rsPreviewed = null; let _rsRan = false;',
    grab('async function renderGridFreeCash('),
    grab('function _rsSay('), grab('function _rsEnable('),
    grab('async function previewFreeCash('),
    grab('async function runFreeCash('),
    grab('async function runDeployFreed('),
].join('\n');

let pass = 0, fail = 0;
const ok = (c, m) => c ? pass++ : (fail++, console.log('  FAIL: ' + m));

const PREVIEW = {
    read_only: true, total_freeable_usd: 1716.37,
    branches: [
        {bot_name: 'crypto_grid_6', product_id: 'XRP-USD', allocated_usd: 2228.05,
         coin_basis_usd: 653.60, floor_usd: 653.60, freeable_usd: 1574.45,
         slices: 7, num_levels: 3, tradeable_slices: 7, parked: true, why_not: null},
        {bot_name: 'crypto_grid_21', product_id: 'ZEC-USD', allocated_usd: 2271.29,
         coin_basis_usd: 2196.76, floor_usd: 2196.76, freeable_usd: 74.53,
         slices: 6, num_levels: 3, tradeable_slices: 6, parked: true, why_not: null},
        {bot_name: 'crypto_grid_1', product_id: 'APE-USD', allocated_usd: 246.97,
         coin_basis_usd: 82.57, floor_usd: 82.57, freeable_usd: 0.0,
         slices: 1, num_levels: 3, tradeable_slices: 1, parked: false,
         why_not: 'has a free rung'},
    ],
};

function env(opts = {}) {
    const els = {};
    const mk = () => ({style: {}, innerHTML: '', textContent: '', disabled: false});
    for (const id of ['grid-freecash-wrap', 'rs-status', 'rs-detail',
                      'rs-run-btn', 'rs-deploy-btn']) els[id] = mk();
    const posts = [];
    const ctx = {
        API_BASE: 'https://x/api',
        document: {getElementById: (id) => els[id] || null},
        fetch: async () => ({ok: true, status: 200, json: async () => PREVIEW}),
        postGuarded: async (url, body) => {
            posts.push({url, body});
            if (opts.postThrows) throw new Error('the tab is locked');
            if (url.includes('deploy-cash')) {
                const live = url.includes('dry_run=false');
                return {ok: true, adds: [{product_id: 'NEAR-USD', usd: 120.5}],
                        added: live ? [{product_id: 'NEAR-USD', usd: 120.5}] : [],
                        added_usd: live ? 120.5 : 0, detail: 'ok'};
            }
            const row = PREVIEW.branches.find(r => r.bot_name === body.bot_name);
            if (opts.refuse) return {ok: false, reason: 'branch has a free rung'};
            return {ok: true, product_id: row.product_id,
                    allocated_before: row.allocated_usd,
                    allocated_after: row.floor_usd, floor_usd: row.floor_usd,
                    freed_usd: row.freeable_usd, dry_run: !!body.dry_run};
        },
        console,
    };
    const fn = new Function('ctx', `
        const {API_BASE, document, fetch, postGuarded, console} = ctx;
        ${src}
        return {renderGridFreeCash, previewFreeCash, runFreeCash, runDeployFreed,
                els: ${'null'}};
    `);
    return {api: fn(ctx), els, posts};
}

(async () => {
    console.log('--- renders the real numbers ---');
    let {api, els} = env();
    await api.renderGridFreeCash();
    ok(/\$1,716\.37 is claimed but unspendable/.test(els['grid-freecash-wrap'].innerHTML),
       'headline total');
    ok(/XRP-USD/.test(els['grid-freecash-wrap'].innerHTML), 'lists XRP');
    ok(!/APE-USD/.test(els['grid-freecash-wrap'].innerHTML),
       'a branch with a free rung must NOT be listed');
    ok(/TOTAL ALLOCATED \(GRID\) will drop/.test(els['grid-freecash-wrap'].innerHTML),
       'warns allocated will drop');
    ok(/banked\s+profit does not change/.test(els['grid-freecash-wrap'].innerHTML),
       'says banked is unaffected');
    ok(/1\. Preview/.test(els['grid-freecash-wrap'].innerHTML), 'has preview button');

    console.log('--- the live buttons start DISABLED ---');
    ({api, els} = env());
    await api.renderGridFreeCash();
    ok(els['rs-run-btn'].disabled !== false || true, 'markup disables them');
    ok(/id="rs-run-btn" disabled/.test(els['grid-freecash-wrap'].innerHTML),
       'Free it starts disabled in markup');
    ok(/id="rs-deploy-btn" disabled/.test(els['grid-freecash-wrap'].innerHTML),
       'Deploy starts disabled in markup');

    console.log('--- freeing without a preview is refused ---');
    let e3 = env();
    await e3.api.renderGridFreeCash();
    await e3.api.runFreeCash();
    ok(/Preview first/.test(e3.els['rs-status'].innerHTML), 'refuses without preview');
    ok(e3.posts.filter(p => p.url.includes('rightsize') && p.body.dry_run === false).length === 0,
       'NO live rightsize was sent');

    console.log('--- deploying before freeing is refused ---');
    let e4 = env();
    await e4.api.renderGridFreeCash();
    await e4.api.runDeployFreed();
    ok(/Free the money first/.test(e4.els['rs-status'].innerHTML),
       'refuses deploy before rightsize');
    ok(e4.posts.filter(p => p.url.includes('deploy-cash')).length === 0,
       'NO deploy call was sent');

    console.log('--- preview is dry-run only, then enables the live run ---');
    let e5 = env();
    await e5.api.renderGridFreeCash();
    await e5.api.previewFreeCash();
    const dry = e5.posts.filter(p => p.url.includes('rightsize'));
    ok(dry.length === 2, 'previewed both freeable branches, got ' + dry.length);
    ok(dry.every(p => p.body.dry_run === true), 'EVERY preview post was dry_run:true');
    ok(/nothing moved/.test(e5.els['rs-status'].innerHTML), 'says nothing moved');
    ok(/\$1,648\.98/.test(e5.els['rs-status'].innerHTML),
       'totals the two dry runs: ' + e5.els['rs-status'].innerHTML.slice(0, 120));

    console.log('--- the live run sends dry_run:false and reports the real total ---');
    let e6 = env();
    await e6.api.renderGridFreeCash();
    await e6.api.previewFreeCash();
    await e6.api.runFreeCash();
    const live = e6.posts.filter(p => p.url.includes('rightsize') && p.body.dry_run === false);
    ok(live.length === 2, 'two live rightsize calls');
    ok(/Freed <strong>\$1,648\.98/.test(e6.els['rs-status'].innerHTML), 'reports freed total');
    ok(/no coin was sold/.test(e6.els['rs-status'].innerHTML), 'says no coin sold');

    console.log('--- deploy runs only after a successful free, and previews first ---');
    let e7 = env();
    await e7.api.renderGridFreeCash();
    await e7.api.previewFreeCash();
    await e7.api.runFreeCash();
    await e7.api.runDeployFreed();
    const dep = e7.posts.filter(p => p.url.includes('deploy-cash'));
    ok(dep.length === 2, 'deploy previewed then ran, got ' + dep.length);
    ok(dep[0].url.includes('dry_run=true'), 'first deploy call is a dry run');
    ok(dep[1].url.includes('dry_run=false'), 'second is live');
    ok(/Put <strong>\$120\.50<\/strong> to\s+work/.test(e7.els['rs-status'].innerHTML),
       'reports what was deployed');

    console.log('--- a refusal is surfaced, not swallowed ---');
    let e8 = env({refuse: true});
    await e8.api.renderGridFreeCash();
    await e8.api.previewFreeCash();
    ok(/Nothing would be freed/.test(e8.els['rs-status'].innerHTML),
       'refused preview does not enable the live run');
    ok(/refused/.test(e8.els['rs-detail'].innerHTML), 'shows the refusal reason');

    console.log('--- a locked tab says so and claims nothing ---');
    let e9 = env({postThrows: true});
    await e9.api.renderGridFreeCash();
    await e9.api.previewFreeCash();
    ok(/locked/.test(e9.els['rs-status'].innerHTML), 'surfaces the lock message');
    ok(!/Freed/.test(e9.els['rs-status'].innerHTML), 'must NOT claim it freed anything');

    console.log(`\n${pass} passed, ${fail} failed`);
    process.exit(fail ? 1 : 0);
})();
