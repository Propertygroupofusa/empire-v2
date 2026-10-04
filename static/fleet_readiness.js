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
// THE TRIGGER IS ONLY HALF THE BOT'S RULE, AND THE OTHER HALF DECIDES.
//
// A rise trigger firing does NOT mean a sale happens. The bot then asks
// whether any open slice would net a real profit, and holds everything if
// none would - crypto_grid_bot.py, _pick_profitable_slice_to_sell:
//
//     for s in slices:
//         if _grid_slice_net_pnl(s.qty, s.entry_price, price, rate) > 0:
//             return s
//     return None          # caller skips selling entirely
//
// Modelling only the trigger made this panel promise sales that cannot
// happen. Measured live 2026-10-04 11:35Z: it showed "2 at or past trigger
// / should sell on the next cycle" for NEAR and ONDO, while the bot logged
//
//     real rise trigger fired ($0.4910 >= $0.4901) but no open slice would
//     net a real profit at this price - holding every slice
//
// ONDO's only slice was -8.98% net of fees and NEAR's two were -5.4% and
// -8.2%. Zero of the two "READY" branches could sell. The account owner
// was reading that panel to find out whether the fleet was about to earn.
//
// The profit test is NOT recomputed here. Each slice already carries
// unrealized_net_usd from the server, fee-adjusted and measured against
// `marked_against` - which is the DECLARED true cost basis on an adopted
// slice, not the adoption-day mark. Recomputing it in the browser would be
// an approximation of the exact thing this file's header forbids
// approximating, and would get every adopted slice wrong.
//
// Nothing here places, sizes or cancels an order. It reports distance.
// ===============================================================
const FLEET = (function () {
    'use strict';

    // A branch that cannot sell what it claims to hold is not "waiting" -
    // it is out of the game until its ledger is reconciled, and lumping
    // it in with the waiting ones hides the one thing that is fixable.
    // Does any open slice net a real profit at the current price?
    // true / false / null-for-unknown. Mirrors the bot's own loop: the
    // FIRST slice that nets anything at all is enough, no floor.
    function profitableSliceExists(slices) {
        let sawNumber = false;
        for (const s of slices || []) {
            const net = Number(s && s.unrealized_net_usd);
            if (!isFinite(net)) continue;
            sawNumber = true;
            if (net > 0) return true;
        }
        return sawNumber ? false : null;
    }

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
                // Tri-state on purpose. true = at least one slice nets a
                // real profit, false = none does and the bot will hold,
                // null = no slice carried a readable figure, so this pass
                // cannot assert either way and must not pretend to.
                row.sellable = profitableSliceExists(b.slices);
                row.state = row.phantom ? 'LOCKED'
                    : slices === 0 ? 'NO_SLICES'
                    : row.gapPct < 0 ? 'WAITING'
                    : row.sellable === false ? 'NO_PROFIT'
                    : 'READY';
            }
            out.push(row);
        }
        return out;
    }

    function summarise(rows) {
        const s = {
            total: rows.length, ready: 0, waiting: 0, locked: 0,
            noSlices: 0, unreadable: 0, noProfit: 0, noProfitUsd: 0,
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
            if (r.state === 'NO_PROFIT') {
                s.noProfit++; s.noProfitUsd += r.allocated; continue;
            }
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
            return `${s.ready} branch(es) are at or past their trigger AND `
                 + `hold a slice that nets a profit - those should sell on `
                 + `the next cycle.`;
        if (s.noProfit > 0)
            return `${s.noProfit} branch(es) reached their sell trigger, but `
                 + `not one open slice would net a profit after fees, so the `
                 + `bot is holding all of them - $${s.noProfitUsd.toFixed(2)} `
                 + `allocated. The price target was met; the purchase price `
                 + `is what is binding. This is the no-loss rule working, `
                 + `not a stall.`;
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
