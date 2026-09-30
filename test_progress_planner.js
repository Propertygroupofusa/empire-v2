// The 4-year planner and milestone ladder, run from the SHIPPED page code.
const fs = require('fs');
let fails = 0;
const check = (n, c, d) => { console.log((c ? 'PASS ' : 'FAIL ') + n + (c ? '' : '  [' + d + ']')); if (!c) fails++; };
const seg = f => { const h = fs.readFileSync(__dirname + '/' + f, 'utf8');
  return h.slice(h.indexOf('const LADDER_MILESTONES'), h.indexOf('async function loadCombinedProgress')); };
const fam = seg('family_tree_dashboard.html'), alp = seg('alpaca_dashboard.html');
const pick = (src, name) => { const i = src.indexOf('function ' + name + '(');
  const rest = src.slice(i); const j = rest.indexOf('\n}\n'); return rest.slice(0, j + 2); };
for (const fn of ['renderMilestoneLadder', 'plannerYearsTo', 'plannerMonthlyFor', 'renderFourYearPlanner', 'plannerUpdateOut'])
  check('both dashboards ship the same ' + fn, pick(fam, fn) === pick(alp, fn));
eval(pick(fam, 'plannerYearsTo')); eval(pick(fam, 'plannerMonthlyFor'));
const start = 9723.91, goal = 1e6;
for (const r of [0.118, 0.30, 0.50, 1.00]) {
  const m = plannerMonthlyFor(goal, start, r, 48);
  check(`${r * 100}%/yr: solved monthly cash reaches $1M in exactly 4.0 years`, plannerYearsTo(goal, start, r, m) === 4, m);
}
const noCash = Math.pow(goal / start, 1 / 4) - 1;
check('no-added-cash rate reaches $1M in 4.0 years', plannerYearsTo(goal, start, noCash, 0) === 4);
check('a yearly rate means what it says after 12 months',
  Math.abs(plannerYearsTo(2000, 1000, 1.0, 0) - 1) < 1e-9, plannerYearsTo(2000, 1000, 1.0, 0));
check('zero return with no cash never arrives (null, not a number)', plannerYearsTo(goal, start, 0, 0) === null);
check('more cash never takes longer', plannerYearsTo(goal, start, 0.3, 5000) >= plannerYearsTo(goal, start, 0.3, 10000));
// ladder
const el = { innerHTML: '' };
global.document = { getElementById: () => el };
global.fmtUsd = v => '$' + Number(v).toFixed(2);
eval(fam.slice(fam.indexOf('const LADDER_MILESTONES'), fam.indexOf('function renderGauge(')));
renderMilestoneLadder('x', 9723.91, 'c');
check('next milestone is $10k, $276.09 away', el.innerHTML.includes('ladder-row next') && el.innerHTML.includes('$276.09 to go'));
check('bar shows 97% of the way to $10k', el.innerHTML.includes('97% of the way to $10k'));
renderMilestoneLadder('x', 30000, 'c');
check('$10k and $25k marked done at $30k', (el.innerHTML.match(/ladder-row done/g) || []).length === 2);
renderMilestoneLadder('x', null, 'unreadable');
check('unknown equity shows a dash, no position, no fake zero',
  el.innerHTML.includes('&mdash;') && !el.innerHTML.includes('ladder-row done') && !el.innerHTML.includes('ladder-row next'));
console.log(fails ? fails + ' FAILED' : 'ALL PASS'); process.exit(fails ? 1 : 0);
