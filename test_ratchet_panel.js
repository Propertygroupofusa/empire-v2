// Runs the SHIPPED renderGridProfitRatchet, extracted verbatim from the page.
const fs = require('fs');
const html = fs.readFileSync(__dirname + '/family_tree_dashboard.html', 'utf8');
const start = html.indexOf('function renderGridProfitRatchet(');
const end = html.indexOf('async function toggleGridProfitRatchet(');
if (start < 0 || end < 0) throw new Error('render function not found');
const el = { innerHTML: '' };
global.document = { getElementById: () => el };
global.fmtUsd = (v) => '$' + Number(v).toFixed(2);
eval(html.slice(start, end));
let fails = 0;
const check = (n, c) => { console.log((c ? 'PASS ' : 'FAIL ') + n); if (!c) fails++; };
const r = { armed: true, tier: 1, locked_usd: 42.0, realized_total: 90.0, trading_capital: 8488.62,
            floor: 8482.62, progress_usd: 6.0, next_tier_size: 84.83, seed_credited: true,
            tiers_pending_reconciliation: 0, last_reconcile: { ok: true, findings: [] } };
renderGridProfitRatchet({ profit_ratchet: r, total_unrealized_net_usd: -491.0 });
for (const l of ['LOCKED PROFIT', 'REALIZED AVAILABLE', 'OPEN P&amp;L', 'TRADING CAPITAL', 'TOTAL EQUITY'])
  check('shows ' + l, el.innerHTML.includes(l) || el.innerHTML.includes(l.replace('&amp;', '&')));
check('realized available = realized - locked', el.innerHTML.includes('$48.00'));
check('total equity = trading capital + locked + open', el.innerHTML.includes('$8039.62'));
check('locked guarantee stated precisely', el.innerHTML.includes('never decreases from trading losses'));
renderGridProfitRatchet({ profit_ratchet: r, total_unrealized_net_usd: null });
check('unpriced open P&L reads unknown, not $0', /OPEN P&(amp;)?L<\/span><span><strong>unknown/.test(el.innerHTML));
check('equity is unknown when open P&L is', /TOTAL EQUITY<\/span><span><strong>unknown/.test(el.innerHTML));
renderGridProfitRatchet({ profit_ratchet: { ...r, last_reconcile: { ok: false, findings: ['QNT-USD SHORT'] },
                                            tiers_pending_reconciliation: 1 }, total_unrealized_net_usd: 0 });
check('dirty ledger is shown with the finding', el.innerHTML.includes('does not match') && el.innerHTML.includes('QNT-USD SHORT'));
check('pending tier is shown', el.innerHTML.includes('waiting on reconciliation'));
renderGridProfitRatchet({ profit_ratchet: { armed: false } });
check('off state renders', el.innerHTML.includes('OFF'));
console.log(fails ? fails + ' FAILED' : 'ALL PASS'); process.exit(fails ? 1 : 0);
