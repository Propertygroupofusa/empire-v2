// ======================= ADVANCED CHARTS =======================
// Candlesticks + SMA/EMA, RSI(14), MACD(12,26,9).
//
// The indicator math is PURE and separated from the drawing, and hangs
// off window.ADV so it can be driven by a test runner outside a browser.
// These numbers get looked at before money moves, so "the line looked
// about right" is not a check: test_advanced_charts.js drives these
// against hand-computed values.
//
// Nothing in this file places, sizes, cancels or even reads an order.
// ===============================================================
const ADV = (function () {
    'use strict';

    // ---- indicator math (pure) ----

    // Simple moving average. Index i holds the mean of the p values
    // ENDING at i; everything before the first full window is null, not
    // 0 - a zero would draw a line across the bottom of the chart and
    // read as a real price. A gap is not a zero.
    function sma(values, p) {
        const out = new Array(values.length).fill(null);
        if (!(p > 0) || values.length < p) return out;
        let run = 0;
        for (let i = 0; i < values.length; i++) {
            run += values[i];
            if (i >= p) run -= values[i - p];
            if (i >= p - 1) out[i] = run / p;
        }
        return out;
    }

    // Exponential moving average, seeded with the SMA of the first p
    // values - the standard seed. Seeding with values[0] instead makes
    // the first dozen points wrong in a way that is invisible on a chart.
    function ema(values, p) {
        const out = new Array(values.length).fill(null);
        if (!(p > 0) || values.length < p) return out;
        const k = 2 / (p + 1);
        let seed = 0;
        for (let i = 0; i < p; i++) seed += values[i];
        let prev = seed / p;
        out[p - 1] = prev;
        for (let i = p; i < values.length; i++) {
            prev = values[i] * k + prev * (1 - k);
            out[i] = prev;
        }
        return out;
    }

    // Wilder's RSI, which is what charting packages mean by "RSI 14":
    // the averages are smoothed, not re-meaned over a sliding window.
    //
    // The all-gains case is the one worth stating: when every change in
    // the window is up, avgLoss is 0 and RS divides by zero. RSI is
    // defined as 100 there, so it is returned explicitly rather than
    // left to produce Infinity and then NaN through the scaling.
    function rsi(closes, p) {
        p = p || 14;
        const out = new Array(closes.length).fill(null);
        if (closes.length <= p) return out;
        let g = 0, l = 0;
        for (let i = 1; i <= p; i++) {
            const d = closes[i] - closes[i - 1];
            if (d >= 0) g += d; else l -= d;
        }
        let ag = g / p, al = l / p;
        out[p] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
        for (let i = p + 1; i < closes.length; i++) {
            const d = closes[i] - closes[i - 1];
            ag = (ag * (p - 1) + (d > 0 ? d : 0)) / p;
            al = (al * (p - 1) + (d < 0 ? -d : 0)) / p;
            out[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
        }
        return out;
    }

    // MACD line = EMA(fast) - EMA(slow). The signal is an EMA of the
    // MACD line, and it must be computed over the MACD line's REAL
    // values only: feeding the leading nulls in as zeros drags the
    // signal toward zero for its whole first window and puts the
    // crossovers in the wrong place.
    function macd(closes, fast, slow, sig) {
        fast = fast || 12; slow = slow || 26; sig = sig || 9;
        const ef = ema(closes, fast), es = ema(closes, slow);
        const line = closes.map((_, i) =>
            (ef[i] === null || es[i] === null) ? null : ef[i] - es[i]);
        const firstReal = line.findIndex(v => v !== null);
        const signal = new Array(closes.length).fill(null);
        const hist = new Array(closes.length).fill(null);
        if (firstReal !== -1) {
            const dense = line.slice(firstReal);
            const se = ema(dense, sig);
            for (let i = 0; i < se.length; i++) {
                if (se[i] === null) continue;
                signal[firstReal + i] = se[i];
                hist[firstReal + i] = line[firstReal + i] - se[i];
            }
        }
        return { line: line, signal: signal, hist: hist };
    }

    // Coinbase returns [time, low, high, open, close, volume], NEWEST
    // FIRST. Charting it in that order draws time backwards, so the
    // reversal happens here, once, rather than in each drawing function.
    function parseCandles(rows) {
        if (!Array.isArray(rows)) return [];
        return rows
            .filter(r => Array.isArray(r) && r.length >= 6 && r.every(n => typeof n === 'number' && isFinite(n)))
            .map(r => ({ t: r[0], low: r[1], high: r[2], open: r[3], close: r[4], vol: r[5] }))
            .sort((a, b) => a.t - b.t);
    }

    // ---- drawing ----

    const CSS = n => getComputedStyle(document.documentElement)
        .getPropertyValue(n).trim() || '#888';

    // Canvas is sized in CSS pixels but drawn at device resolution, or
    // every hairline is a grey smear on a phone.
    function fit(cv) {
        const dpr = window.devicePixelRatio || 1;
        const w = Math.max(1, cv.clientWidth);
        const h = Math.max(1, parseInt(cv.getAttribute('height'), 10) || 150);
        cv.style.height = h + 'px';
        cv.width = Math.round(w * dpr);
        cv.height = Math.round(h * dpr);
        const c = cv.getContext('2d');
        c.setTransform(dpr, 0, 0, dpr, 0, 0);
        c.clearRect(0, 0, w, h);
        return { c: c, w: w, h: h };
    }

    const PAD = { l: 6, r: 58, t: 8, b: 16 };

    function extent(series) {
        let lo = Infinity, hi = -Infinity;
        series.forEach(arr => arr.forEach(v => {
            if (v === null || v === undefined || !isFinite(v)) return;
            if (v < lo) lo = v;
            if (v > hi) hi = v;
        }));
        if (!isFinite(lo) || !isFinite(hi)) return null;
        if (lo === hi) { lo -= 1; hi += 1; }
        return { lo: lo, hi: hi };
    }

    // `lines` is either a count of evenly spaced gridlines, or an explicit
    // array of values. RSI's meaningful levels are 30 and 70, which no
    // even division lands on - drawing quartiles there put the labels at
    // 75 and 25 right beside the 70/30 dashes and made the chart read
    // wrong at a glance.
    function grid(c, w, h, lo, hi, fmt, lines) {
        c.strokeStyle = CSS('--border');
        c.fillStyle = CSS('--text-dim');
        c.lineWidth = 1;
        c.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace';
        c.textBaseline = 'middle';
        const n = (typeof lines === 'number') ? lines : 5;
        const vals = Array.isArray(lines) ? lines
            : Array.from({length: n + 1}, (_, i) => hi - (hi - lo) * i / n);
        vals.forEach(v => {
            const y = Math.round(yOf(v, lo, hi, h)) + 0.5;
            c.beginPath();
            c.moveTo(PAD.l, y);
            c.lineTo(w - PAD.r, y);
            c.stroke();
            c.fillText(fmt(v), w - PAD.r + 6, y);
        });
    }

    const yOf = (v, lo, hi, h) =>
        PAD.t + (h - PAD.t - PAD.b) * (1 - (v - lo) / (hi - lo));

    function line(c, vals, lo, hi, w, h, colour, width) {
        const n = vals.length;
        const step = (w - PAD.l - PAD.r) / Math.max(1, n - 1);
        c.strokeStyle = colour;
        c.lineWidth = width || 2;
        c.lineJoin = 'round';
        c.beginPath();
        let open = false;
        for (let i = 0; i < n; i++) {
            const v = vals[i];
            if (v === null || v === undefined || !isFinite(v)) { open = false; continue; }
            const x = PAD.l + step * i, y = yOf(v, lo, hi, h);
            if (!open) { c.moveTo(x, y); open = true; } else { c.lineTo(x, y); }
        }
        c.stroke();
    }

    function money(v) {
        const a = Math.abs(v);
        if (a >= 1000) return '$' + v.toFixed(0);
        if (a >= 1) return '$' + v.toFixed(2);
        return '$' + v.toFixed(a >= 0.01 ? 4 : 6);
    }

    function drawPrice(cv, candles) {
        const f = fit(cv); if (!f) return;
        const c = f.c, w = f.w, h = f.h;
        const closes = candles.map(d => d.close);
        const s = sma(closes, 50), e = ema(closes, 50);
        const ex = extent([candles.map(d => d.high), candles.map(d => d.low), s, e]);
        if (!ex) return;
        const pad = (ex.hi - ex.lo) * 0.06;
        const lo = ex.lo - pad, hi = ex.hi + pad;
        grid(c, w, h, lo, hi, money, 5);

        const step = (w - PAD.l - PAD.r) / Math.max(1, candles.length);
        // A 2px gap between bodies, and never thinner than one hairline,
        // so a 300-candle view stays a chart instead of a block of ink.
        const bw = Math.max(1, Math.min(9, step - 2));
        const green = CSS('--green'), red = CSS('--red');
        candles.forEach((d, i) => {
            const x = PAD.l + step * i + step / 2;
            const up = d.close >= d.open;
            c.strokeStyle = c.fillStyle = up ? green : red;
            c.lineWidth = 1;
            c.beginPath();                       // the wick
            c.moveTo(Math.round(x) + 0.5, yOf(d.high, lo, hi, h));
            c.lineTo(Math.round(x) + 0.5, yOf(d.low, lo, hi, h));
            c.stroke();
            const yo = yOf(d.open, lo, hi, h), yc = yOf(d.close, lo, hi, h);
            c.fillRect(x - bw / 2, Math.min(yo, yc),
                       bw, Math.max(1, Math.abs(yc - yo)));   // the body
        });
        line(c, s, lo, hi, w, h, CSS('--blue'), 1.6);
        line(c, e, lo, hi, w, h, CSS('--gold'), 1.6);
    }

    function drawRsi(cv, candles) {
        const f = fit(cv); if (!f) return;
        const c = f.c, w = f.w, h = f.h;
        const v = rsi(candles.map(d => d.close), 14);
        const lo = 0, hi = 100;
        grid(c, w, h, lo, hi, n => n.toFixed(0), [100, 70, 50, 30, 0]);
        // 70 and 30 are the only levels anyone reads off this chart.
        c.setLineDash([3, 3]);
        [[70, CSS('--red')], [30, CSS('--green')]].forEach(([lvl, col]) => {
            const y = Math.round(yOf(lvl, lo, hi, h)) + 0.5;
            c.strokeStyle = col; c.lineWidth = 1;
            c.beginPath(); c.moveTo(PAD.l, y); c.lineTo(w - PAD.r, y); c.stroke();
        });
        c.setLineDash([]);
        line(c, v, lo, hi, w, h, CSS('--blue'), 2);
        return v[v.length - 1];
    }

    function drawMacd(cv, candles) {
        const f = fit(cv); if (!f) return;
        const c = f.c, w = f.w, h = f.h;
        const m = macd(candles.map(d => d.close), 12, 26, 9);
        const ex = extent([m.line, m.signal, m.hist]);
        if (!ex) return;
        // Zero must be ON the chart - a MACD panel that crops the zero
        // line hides the crossing, which is the only thing it is for.
        const span = Math.max(Math.abs(ex.lo), Math.abs(ex.hi)) * 1.08 || 1;
        const lo = -span, hi = span;
        const fmt = n => (Math.abs(n) >= 10 ? n.toFixed(0) : n.toFixed(2));
        grid(c, w, h, lo, hi, fmt, [hi, hi / 2, 0, lo / 2, lo]);

        const step = (w - PAD.l - PAD.r) / Math.max(1, m.hist.length);
        const bw = Math.max(1, Math.min(6, step - 2));
        const zero = yOf(0, lo, hi, h);
        const green = CSS('--green'), red = CSS('--red');
        m.hist.forEach((v, i) => {
            if (v === null || !isFinite(v)) return;
            const x = PAD.l + step * i + step / 2;
            const y = yOf(v, lo, hi, h);
            c.fillStyle = v >= 0 ? green : red;
            c.fillRect(x - bw / 2, Math.min(y, zero), bw, Math.max(1, Math.abs(zero - y)));
        });
        c.strokeStyle = CSS('--border-bright'); c.lineWidth = 1;
        c.beginPath(); c.moveTo(PAD.l, Math.round(zero) + 0.5);
        c.lineTo(w - PAD.r, Math.round(zero) + 0.5); c.stroke();
        line(c, m.line, lo, hi, w, h, CSS('--blue'), 2);
        line(c, m.signal, lo, hi, w, h, CSS('--gold'), 1.6);
    }

    return { sma: sma, ema: ema, rsi: rsi, macd: macd,
             parseCandles: parseCandles, money: money,
             drawPrice: drawPrice, drawRsi: drawRsi, drawMacd: drawMacd };
})();
if (typeof window !== 'undefined') window.ADV = ADV;
if (typeof module !== 'undefined' && module.exports) module.exports = ADV;
