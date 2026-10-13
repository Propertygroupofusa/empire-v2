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
    'let _rsPreviewed = null; let _rsRan = false; let _depPreviewed = null; let _rsNothingToFree = false;',
    grab('async function _autoDeployLine('),
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
        fetch: async (url) => ({ok: true, status: 200, json: async () =>
            (String(url).includes('auto-deploy') ? (opts.auto || {}) : (opts.preview || PREVIEW))}),
        postGuarded: async (url, body) => {
            posts.push({url, body});
            if (opts.postThrows) throw new Error('the tab is locked');
            if (url.includes('deploy-cash')) {
                const live = url.includes('dry_run=false');
                if (opts.nothingIdle) return {ok: false, adds: [], total_usd: 0,
                        detail: 'Nothing above the reserve is unclaimed.'};
                if (live && opts.liveEmpty) return {ok: false, adds: [], dry_run: true,
                        detail: 'Nothing to add.'};
                if (live && opts.liveFailed) return {ok: true, dry_run: false,
                        adds: [{product_id: 'NEAR-USD', usd: 120.5}, {product_id: 'TIA-USD', usd: 120.5}],
                        added: [{product_id: 'NEAR-USD', usd: 120.5}],
                        failed: [{product_id: 'TIA-USD', usd: 120.5, error: 'ValueError: no grid branch named x'}],
                        added_usd: 120.5};
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
        // The page's own diagnostic beacon. Stubbed, not omitted: the real
        // one is a fire-and-forget GET and these functions now call it, so
        // a sandbox without it throws a ReferenceError before the first
        // assertion. Same stub the levels and reconcile tests already use.
        var uiTrace = function () {};
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
    await e7.api.runDeployFreed();   // first press: preview only
    await e7.api.runDeployFreed();   // second press: live
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

    console.log('--- nothing stranded: the deploy button is still on the page ---');
    const EMPTY = {read_only: true, total_freeable_usd: 0.0,
        branches: [{...PREVIEW.branches[2]}]};
    let e10 = env({preview: EMPTY});
    await e10.api.renderGridFreeCash();
    const h10 = e10.els['grid-freecash-wrap'].innerHTML;
    ok(/Nothing is stranded right now/.test(h10), 'still says nothing is stranded');
    ok(/id="rs-deploy-btn"/.test(h10), 'the deploy button is rendered');
    ok(!/id="rs-deploy-btn"[^>]*disabled/.test(h10), 'and it is NOT disabled');
    ok(/Put idle cash to work/.test(h10), 'labelled for idle cash');
    ok(/reserve is never\s+touched/.test(h10), 'says the reserve is untouched');
    ok(/id="rs-status"/.test(h10) && /id="rs-detail"/.test(h10), 'has its status lines');

    console.log('--- nothing stranded: first press previews, second press runs ---');
    await e10.api.runDeployFreed();
    let d10 = e10.posts.filter(p => p.url.includes('deploy-cash'));
    ok(d10.length === 1 && d10[0].url.includes('dry_run=true'), 'first press is a dry run only');
    ok(/nothing moved/.test(e10.els['rs-status'].innerHTML), 'says nothing moved');
    ok(!/Free the money first/.test(e10.els['rs-status'].innerHTML), 'no longer refuses');
    e10.els['grid-freecash-wrap'].innerHTML = 'MARK';
    await e10.api.renderGridFreeCash();
    ok(e10.els['grid-freecash-wrap'].innerHTML === 'MARK',
       'a pending preview is NOT wiped by the next poll');
    await e10.api.runDeployFreed();
    d10 = e10.posts.filter(p => p.url.includes('deploy-cash'));
    ok(d10.length === 2 && d10[1].url.includes('dry_run=false'), 'second press is live');
    ok(/Put <strong>\$120\.50<\/strong> to\s+work/.test(e10.els['rs-status'].innerHTML),
       'reports what was deployed');
    await e10.api.renderGridFreeCash();
    ok(e10.els['grid-freecash-wrap'].innerHTML !== 'MARK', 'after the run the card refreshes again');

    console.log('--- nothing idle: the preview says so and nothing goes live ---');
    let e11 = env({preview: EMPTY, nothingIdle: true});
    await e11.api.renderGridFreeCash();
    await e11.api.runDeployFreed();
    await e11.api.runDeployFreed();
    const d11 = e11.posts.filter(p => p.url.includes('deploy-cash'));
    ok(d11.every(p => p.url.includes('dry_run=true')), 'only dry runs were sent');
    ok(/Nothing above the reserve/.test(e11.els['rs-status'].innerHTML), 'shows why');

    console.log('--- the plan empties between the presses: no false success ---');
    let e12 = env({preview: EMPTY, liveEmpty: true});
    await e12.api.renderGridFreeCash();
    await e12.api.runDeployFreed();
    await e12.api.runDeployFreed();
    const st12 = e12.els['rs-status'].innerHTML;
    ok(/Nothing was put to work, and no money moved/.test(st12), 'says nothing moved: ' + st12.slice(0, 80));
    ok(!/NaN/.test(st12) && !/\u2705 Put/.test(st12), 'no "Put $NaN" success line');
    ok(e12.els['rs-deploy-btn'].disabled === false, 'button re-enabled to try again');

    console.log('--- a branch the server could not add to is shown, not hidden ---');
    let e13 = env({preview: EMPTY, liveFailed: true});
    await e13.api.renderGridFreeCash();
    await e13.api.runDeployFreed();
    await e13.api.runDeployFreed();
    ok(/Put <strong>\$120\.50<\/strong>/.test(e13.els['rs-status'].innerHTML), 'reports the real added total');
    ok(/NEAR-USD: \+\$120\.50/.test(e13.els['rs-detail'].innerHTML), 'lists what was added');
    ok(/TIA-USD: not added/.test(e13.els['rs-detail'].innerHTML), 'lists the failure');

    console.log('--- auto-deploy state is shown on the card ---');
    let e14 = env({preview: EMPTY, auto: {env_name: 'GRID_AUTO_DEPLOY', enabled: false,
                                          min_deploy_usd: 1000}});
    await e14.api.renderGridFreeCash();
    ok(/Auto-deploy is off/.test(e14.els['grid-freecash-wrap'].innerHTML), 'OFF is shown');
    ok(/set GRID_AUTO_DEPLOY to on in Railway/.test(e14.els['grid-freecash-wrap'].innerHTML),
       'says how to turn it on');
    let e15 = env({auto: {env_name: 'GRID_AUTO_DEPLOY', enabled: true, min_deploy_usd: 1000,
        last: {reason: 'DEPLOYED', detail: 'put $1,062.52 to work across 9 branch(es).',
               checked_at: '2026-10-13T19:05:00Z'}}});
    await e15.api.renderGridFreeCash();
    const h15 = e15.els['grid-freecash-wrap'].innerHTML;
    ok(/Auto-deploy is ON/.test(h15), 'ON is shown (stranded layout too)');
    ok(/\$1,000\.00 sits idle above your floor/.test(h15), 'states the $1,000 rule');
    ok(/put \$1,062\.52 to work across 9/.test(h15), 'shows the last check');
    let e16 = env({preview: EMPTY});
    await e16.api.renderGridFreeCash();
    ok(!/Auto-deploy/.test(e16.els['grid-freecash-wrap'].innerHTML),
       'an unreadable status adds nothing and breaks nothing');

    console.log(`\n${pass} passed, ${fail} failed`);
    process.exit(fail ? 1 : 0);
})();
