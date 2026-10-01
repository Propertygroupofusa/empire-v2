// ======================= FLEET READINESS =======================
// Why the fleet did or did not trade today, computed from the same
// numbers the bot uses.
//
// THE QUESTION THIS ANSWERS. On 2026-10-01 every one of the 23 branch
// coins swung more than its grid step, and exactly ONE trade closed.
// "Is it broken, is it the market, or is it waiting?" took an hour of
// ad-hoc queries to answer, and the answer expires in minutes. It
// belongs on the page.
//
// THE RULE, COPIED FROM THE BOT, NOT APPROXIMATED:
//
//     crypto_grid_bot.py:7050
//     _rise_hit = bool(slices and price >= branch.reference_price * (1 + grid_pct))
//
// Two details that an approximation gets wrong, and both change the
// answer:
//
//   * The reference is the BRANCH's reference_price, not each slice's
//     entry price. A first pass at this used slice entry prices and put
//     every branch in a different place.
//   * grid_pct is PER BRANCH and is not always 0.03 - live right now
//     BTC is 1.43%, ETH 1.78%, SOL 2.83%. A hardcoded 3% misreports
//     every one of them.
//
// Nothing here places, sizes or cancels an order. It reports distance.
// ===============================================================
const FLEET = (function () {
    'use strict';

    // A branch that cannot sell what it claims to hold is not "waiting" -
    // it is out of the game until its ledger is reconciled, and lumping
    // it in with the waiting ones hides the one thing that is fixable.
    function readiness(branches, unbackedAssets) {
        const locked = new Set((unbackedAssets || []).map(
            a => String(a).toUpperCase()));
        const out = [];
        for (const b of branches || []) {
            const pid = b.product_id || '';
            const asset = pid.split('-')[0].toUpperCase();
            const ref = Number(b.reference_price);
            const cur = Number(b.current_price);
            const gp = Number(b.grid_pct);
            const slices = (b.slices || []).length;
            const row = {
                bot: b.bot_name || '', product: pid, asset: asset,
                slices: slices,
                allocated: Number(b.allocated_usd) || 0,
                gridPct: isFinite(gp) ? gp : null,
                phantom: locked.has(asset),
                gapPct: null, state: null,
            };
            // UNREADABLE IS NOT ZERO AND NOT READY. A missing price must
            // never render as "at its trigger".
            if (!isFinite(ref) || !isFinite(cur) || !isFinite(gp)
                || ref <= 0 || cur <= 0) {
                row.state = 'UNREADABLE';
            } else {
                const trigger = ref * (1 + gp);
                row.trigger = trigger;
                row.gapPct = (cur / trigger - 1) * 100;
                row.state = row.phantom ? 'LOCKED'
                    : slices === 0 ? 'NO_SLICES'
                    : row.gapPct >= 0 ? 'READY'
                    : 'WAITING';
            }
            out.push(row);
        }
        return out;
    }

    function summarise(rows) {
        const s = {
            total: rows.length, ready: 0, waiting: 0, locked: 0,
            noSlices: 0, unreadable: 0,
            lockedUsd: 0, within1: 0, within3: 0, further: 0,
            // A branch past its trigger that CANNOT sell is the most
            // expensive row on the page: the move already happened and
            // was not harvested.
            lockedPastTrigger: 0, lockedPastTriggerUsd: 0,
        };
        for (const r of rows) {
            if (r.state === 'UNREADABLE') { s.unreadable++; continue; }
            if (r.state === 'LOCKED') {
                s.locked++; s.lockedUsd += r.allocated;
                if (r.gapPct !== null && r.gapPct >= 0) {
                    s.lockedPastTrigger++; s.lockedPastTriggerUsd += r.allocated;
                }
                continue;
            }
            if (r.state === 'NO_SLICES') { s.noSlices++; continue; }
            if (r.state === 'READY') { s.ready++; continue; }
            s.waiting++;
            if (r.gapPct >= -1) s.within1++;
            else if (r.gapPct >= -3) s.within3++;
            else s.further++;
        }
        return s;
    }

    // One sentence naming the binding constraint, so the page says what
    // is true today rather than leaving it to be inferred from a table.
    function verdict(s) {
        if (s.total === 0) return 'No branches to report on.';
        if (s.unreadable === s.total)
            return 'No branch could be read, so fleet readiness is UNKNOWN - '
                 + 'not idle, unknown.';
        if (s.lockedPastTrigger > 0)
            return `${s.lockedPastTrigger} branch(es) are PAST their sell `
                 + `trigger and cannot sell, holding $${s.lockedUsd.toFixed(2)}. `
                 + `The move already happened and was not harvested - this is `
                 + `the only item here that is fixable today.`;
        if (s.ready > 0)
            return `${s.ready} branch(es) are at or past their trigger and `
                 + `should sell on the next cycle.`;
        if (s.within1 > 0)
            return `Nothing is at its trigger yet; ${s.within1} branch(es) are `
                 + `within 1%. The fleet is waiting on price, not broken.`;
        return `No branch is near its trigger. Inventory sits above today's `
             + `price, so the grid is holding rather than selling into `
             + `weakness - which is the design, not a fault.`;
    }

    return { readiness, summarise, verdict };
})();
if (typeof window !== 'undefined') window.FLEET = FLEET;
if (typeof module !== 'undefined' && module.exports) module.exports = FLEET;
