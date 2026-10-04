// The control that runs the idle rotation from the page. It moves real
// allocation, so the failure that matters is a Run that fires without the
// plan having been seen, or twice on one plan. Both are asserted below by
// executing the page's own functions - not by reading them.
const fs = require('fs');
const src = fs.readFileSync('/home/user/empire-v2/family_tree_dashboard.html', 'utf8');

let fail = 0;
const ok = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}${cond || !detail ? '' : '   -> ' + detail}`);
  if (!cond) fail++;
};
const grab = (name) => {
  const m = src.match(new RegExp('(?:async )?function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n\\}'));
  if (!m) { console.log('FAIL: ' + name + ' not found'); process.exit(1); }
  return m[0];
};

// --- a tiny DOM and the page helpers the handlers lean on -------------
function harness({ previewReply, executeReply, confirmReturns = true }) {
  const nodes = {};
  const mk = (id) => (nodes[id] = { id, textContent: '', innerHTML: '',
                                    style: {} });
  ['grid-rotation-wrap', 'grid-rotation-status', 'grid-rotation-plan'].forEach(mk);
  const sent = [];
  const ctx = {
    document: { getElementById: (id) => nodes[id] || null },
    window: { confirm: () => confirmReturns },
    API_BASE: 'https://x/api/trading-dashboard',
    fmtUsd: (v) => '$' + Number(v || 0).toFixed(2),
    escText: (s) => String(s == null ? '' : s),
    postGuarded: async (url) => {
      sent.push(url);
      if (url.includes('/rotation/preview')) {
        if (previewReply instanceof Error) throw previewReply;
        return previewReply;
      }
      if (executeReply instanceof Error) throw executeReply;
      return executeReply;
    },
    loadGridStatus: undefined,
  };
  const body = `
    // The page's own diagnostic beacon. Stubbed, not omitted: the real one
    // is a fire-and-forget GET and these functions now call it, so a
    // sandbox without it throws a ReferenceError before the first
    // assertion. Same stub the levels and reconcile tests already use.
    var uiTrace = function () {};
    let _rotTicket = null, _rotPreviewed = null;
    ${grab('renderGridRotation')}
    ${grab('previewGridRotation')}
    ${grab('runGridRotation')}
    return { renderGridRotation, previewGridRotation, runGridRotation,
             ticket: () => _rotTicket };`;
  const fns = new Function('document', 'window', 'API_BASE', 'fmtUsd', 'escText',
                           'postGuarded', 'loadGridStatus', body)(
    ctx.document, ctx.window, ctx.API_BASE, ctx.fmtUsd, ctx.escText,
    ctx.postGuarded, ctx.loadGridStatus);
  return { fns, nodes, sent };
}

const FLAT = [
  { product_id: 'JASMY-USD', allocated_usd: 193.95, num_levels: 3, slices: [] },
  { product_id: 'QNT-USD', allocated_usd: 160.65, num_levels: 3, slices: [] },
  { product_id: 'TIA-USD', allocated_usd: 15.00, num_levels: 3, slices: [] },
  { product_id: 'APE-USD', allocated_usd: 247.0, num_levels: 3, slices: [{}] },
];
const READY = { ready: true, balances: true, would_withdraw_usd: 324.60,
                would_place_usd: 324.60, detail: 'PREVIEW ONLY',
                sources: [{ product_id: 'JASMY-USD', release_usd: 178.95, why: 'flat' }],
                targets: [{ product_id: 'APE-USD', add_usd: 324.60, dip_depth: 0.12 }] };

async function main() {
  console.log('\n[1] the card only appears when there is something to move');
  let h = harness({});
  h.fns.renderGridRotation(FLAT);
  ok('card shown when flat branches hold movable cash',
     h.nodes['grid-rotation-wrap'].style.display === 'block');
  ok('headline shows the movable total, not the allocation',
     h.nodes['grid-rotation-wrap'].innerHTML.includes('$324.60'),
     h.nodes['grid-rotation-wrap'].innerHTML.slice(0, 160));
  h = harness({});
  h.fns.renderGridRotation([{ product_id: 'A', allocated_usd: 500, num_levels: 3, slices: [{}] }]);
  ok('card hidden when no branch is flat',
     h.nodes['grid-rotation-wrap'].style.display === 'none');
  h = harness({});
  h.fns.renderGridRotation([{ product_id: 'A', allocated_usd: 20, num_levels: 3, slices: [] }]);
  ok('card hidden when under the $10 minimum after the $15 keep-alive',
     h.nodes['grid-rotation-wrap'].style.display === 'none');

  console.log('\n[2] the card itself offers NO way to run');
  h = harness({});
  h.fns.renderGridRotation(FLAT);
  ok('no Run control before a preview',
     !/runGridRotation/.test(h.nodes['grid-rotation-wrap'].innerHTML));
  ok('the only control is Preview',
     /previewGridRotation/.test(h.nodes['grid-rotation-wrap'].innerHTML));

  console.log('\n[3] preview moves nothing and mints a ticket');
  h = harness({ previewReply: READY });
  await h.fns.previewGridRotation();
  ok('exactly one request, to the preview endpoint', h.sent.length === 1
     && h.sent[0].endsWith('/grid-status/rotation/preview'), h.sent.join(' | '));
  ok('no ticket is sent to preview', !/ticket=/.test(h.sent[0]), h.sent[0]);
  ok('no confirm is sent to preview', !/confirm=/.test(h.sent[0]), h.sent[0]);
  ok('a ticket was minted for the run', !!h.fns.ticket(), String(h.fns.ticket()));
  ok('Run now offered, carrying the previewed amount',
     /runGridRotation/.test(h.nodes['grid-rotation-plan'].innerHTML)
     && h.nodes['grid-rotation-plan'].innerHTML.includes('$324.60'));

  console.log('\n[4] a plan that is not runnable never offers Run');
  h = harness({ previewReply: { ready: false, why_not: 'nothing to move' } });
  await h.fns.previewGridRotation();
  ok('NOT READY -> no Run button',
     !/runGridRotation/.test(h.nodes['grid-rotation-plan'].innerHTML));
  ok('NOT READY -> no ticket minted', h.fns.ticket() === null);
  h = harness({ previewReply: { ready: true, balances: false,
                                would_withdraw_usd: 300, would_place_usd: 250 } });
  await h.fns.previewGridRotation();
  ok('UNBALANCED -> no Run button',
     !/runGridRotation/.test(h.nodes['grid-rotation-plan'].innerHTML));
  ok('UNBALANCED -> no ticket minted', h.fns.ticket() === null);
  ok('UNBALANCED -> says so', /does not balance/.test(h.nodes['grid-rotation-status'].innerHTML));

  console.log('\n[5] Run cannot fire without a preview');
  h = harness({ executeReply: { ran: true } });
  await h.fns.runGridRotation();
  ok('nothing was sent', h.sent.length === 0, h.sent.join(' | '));
  ok('it says to preview first', /Preview it first/.test(h.nodes['grid-rotation-status'].textContent));

  console.log('\n[6] Run sends the minted ticket and confirm=true');
  h = harness({ previewReply: READY, executeReply: { ran: true, added_usd: 324.60,
                added: [{}], withdrawn: [{}], detail: 'moved' } });
  await h.fns.previewGridRotation();
  const tkt = h.fns.ticket();
  await h.fns.runGridRotation();
  const exec = h.sent[1] || '';
  ok('second request is the execute endpoint',
     exec.includes('/grid-status/rotation/execute'), exec);
  ok('carries the ticket the preview minted',
     exec.includes('ticket=' + encodeURIComponent(tkt)), exec);
  ok('carries confirm=true', /[?&]confirm=true(&|$)/.test(exec), exec);
  ok('reports what moved', /Moved \$324\.60/.test(h.nodes['grid-rotation-status'].innerHTML));

  console.log('\n[7] a second tap re-sends the SAME ticket, so the server refuses it');
  await h.fns.runGridRotation();
  ok('still the same ticket - cannot mint a fresh one-shot by tapping again',
     (h.sent[2] || '').includes('ticket=' + encodeURIComponent(tkt)), h.sent[2]);

  console.log('\n[8] declining the confirmation sends nothing');
  h = harness({ previewReply: READY, executeReply: { ran: true }, confirmReturns: false });
  await h.fns.previewGridRotation();
  await h.fns.runGridRotation();
  ok('only the preview was ever sent', h.sent.length === 1, h.sent.join(' | '));
  ok('it says it was cancelled', /Cancelled/.test(h.nodes['grid-rotation-status'].textContent));

  console.log(fail ? `\n${fail} FAILURE(S)\n` : '\nALL PASS\n');
  process.exit(fail ? 1 : 0);
}
main();
