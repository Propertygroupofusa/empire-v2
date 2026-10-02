"""The chart canvas must not grow on redraw. It tripled, every 60 seconds.

WHAT THE OWNER SAW, for days: the PRICE panel as a tall white box with a
broken-image icon in the corner, while the header above it read the price
and the percentage correctly. The data was fine. The canvas was not.

THE BUG. fit() read the intended height with

    parseInt(cv.getAttribute('height'), 10)

and then, two lines later, assigned

    cv.height = Math.round(h * dpr)

canvas.height is a REFLECTED attribute - that assignment writes the same
attribute it had just read. So every redraw multiplied the height by the
device pixel ratio again. Measured in a real browser at dpr 3, from
height="300":

    redraw 1      900px
    redraw 2    2,700px
    redraw 3    8,100px
    redraw 4   24,300px   <- past Chrome's 16,384px per-side limit
    redraw 5   72,900px

advDraw() runs on a 60-second interval, so it crossed the limit about three
minutes in, and a canvas over that limit paints as a broken image.

WHY IT WAS MISSED REPEATEDLY, and this is the part worth keeping: at dpr 1
the multiplier is 1 and the canvas NEVER GROWS. Every desktop look and every
headless check ran at dpr 1 and showed a perfect chart. The bug existed only
on a high-density screen - which is the only place the owner ever looked.
A check that cannot see the failure is not a check.

So this test runs a REAL browser at dpr 3, redraws many more times than the
bug needed, and asserts the size is stable - and that the canvas still has
real pixels drawn on it afterwards.

Run: python3 test_canvas_does_not_grow.py
"""
import asyncio
import os
import sys

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "static", "advanced_charts.js"), encoding="utf-8").read()

PAGE_TMPL = """<canvas id="c" height="300" style="width:100%"></canvas>
<script>__MODULE__</script>
<script>
window.__sizes = function(n){
  const cv = document.getElementById('c');
  const out = [];
  // Drive the REAL shipped module, through its public draw entry point, so
  // this exercises fit() exactly as the dashboard does.
  const candles = [];
  let p = 100;
  for (let i=0;i<120;i++){
    p += (i % 7) - 3;
    candles.push({ time: 1700000000 + i*3600, low: p-2, high: p+2,
                   open: p-1, close: p+1, volume: 10 });
  }
  for (let i=0;i<n;i++){
    ADV.drawPrice(cv, candles);
    out.push({ call:i+1, attr: cv.getAttribute('height'),
               px: cv.height, w: cv.width, cssH: cv.style.height });
  }
  return out;
};
window.__painted = function(){
  const cv = document.getElementById('c');
  const g = cv.getContext('2d');
  const d = g.getImageData(0,0,cv.width,Math.min(cv.height,400)).data;
  let on = 0;
  for (let i=3;i<d.length;i+=4) if (d[i] !== 0) on++;
  return on;
};
</script>"""
PAGE = PAGE_TMPL.replace("__MODULE__", SRC)

checks = []
def ok(label, cond, detail=""):
    checks.append((label, bool(cond), detail))
def section(t):
    checks.append((t, None, ""))


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        for dpr in (3, 2, 1):
            pg = await b.new_page(viewport={"width": 412, "height": 900},
                                  device_scale_factor=dpr)
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            await pg.set_content(PAGE)
            sizes = await pg.evaluate("window.__sizes(12)")
            painted = await pg.evaluate("window.__painted()")
            section(f"[dpr {dpr}] 12 redraws, the way advDraw() does it every 60s")
            ok("no page error", not errs, "; ".join(errs)[:200])
            first, last = sizes[0], sizes[-1]
            ok("canvas pixel height is STABLE across redraws",
               len({s["px"] for s in sizes}) == 1,
               f"{first['px']} -> {last['px']}")
            ok("canvas pixel width is stable",
               len({s["w"] for s in sizes}) == 1,
               f"{first['w']} -> {last['w']}")
            ok("CSS height stays the intended 300px",
               all(s["cssH"] == "300px" for s in sizes), last["cssH"])
            ok("height is the intended 300 x dpr",
               last["px"] == 300 * dpr, f"{last['px']} vs {300*dpr}")
            ok(f"never exceeds Chrome's 16384px limit",
               all(s["px"] <= 16384 and s["w"] <= 16384 for s in sizes),
               f"max {max(max(s['px'], s['w']) for s in sizes)}")
            ok("and the chart actually PAINTED pixels",
               painted > 500, f"{painted} opaque pixels")
            await pg.close()
        await b.close()

asyncio.run(main())

print()
failed = 0
for label, res, detail in checks:
    if res is None:
        print(f"\n{label}")
    else:
        print(f"  {'PASS' if res else 'FAIL'}  {label}" + (f"   -> {detail}" if detail and not res else ""))
        failed += 0 if res else 1
total = sum(1 for _, r, _ in checks if r is not None)
print(f"\n{total - failed}/{total} checks passed")
print("ALL PASS" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
