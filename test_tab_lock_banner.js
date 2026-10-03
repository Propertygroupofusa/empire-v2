const TRACE_STUB = 'var uiTrace=function(){};';
// The page-level lock banner, and the test gap that let the real bug live.
//
// Every write of one session failed with an EMPTY server log because
// postGuarded refuses before sending when sessionStorage has no token.
// An earlier simulation "proved" the page sent the request - but it had
// STUBBED postGuarded, so it exercised everything downstream of that
// refusal and nothing at it. These tests drive the REAL postGuarded.
const fs = require('fs');
const src = fs.readFileSync('/home/user/empire-v2/family_tree_dashboard.html', 'utf8');
let fail = 0;
const ok = (l, c, d='') => { console.log(`  ${c?'PASS':'FAIL'}  ${l}${c||!d?'':'   -> '+d}`); if(!c) fail++; };
const grab = n => {
  const m = src.match(new RegExp('(?:async )?function '+n+'\\([^)]*\\) \\{[\\s\\S]*?\\n\\}'));
  if(!m){console.log('MISSING '+n);process.exit(1);} return m[0];
};

console.log('\n[1] THE REAL postGuarded refuses before it sends');
let fetched = 0;
const makePG = (token) => new Function('getWriteToken','fetch','TOKEN_KEY',
  grab('postGuarded') + '; return postGuarded;')(
  () => token, async () => { fetched++; return {status:200, ok:true, json:async()=>({})}; }, 'k');
(async () => {
  fetched = 0;
  let threw = null;
  try { await makePG('')('https://x/api/trading-dashboard/grid-status/set-levels', {levels:{}}); }
  catch (e) { threw = e.message; }
  ok('an empty token THROWS', !!threw, threw);
  ok('and fetch is never called - nothing leaves the browser', fetched === 0, `fetch called ${fetched}x`);
  ok('the message names the tab, not the server', /tab is locked/i.test(threw||''), threw);
  ok('it states nothing was sent', /[Nn]othing was sent/.test(threw||''), threw);

  fetched = 0;
  await makePG('a-real-token')('https://x/p', {});
  ok('with a token present it DOES send', fetched === 1, `fetch called ${fetched}x`);

  // ---- the banner ------------------------------------------------
  console.log('\n[2] the page says so before any button is reached');
  const fn = grab('renderTabLock');
  const run = (tok, throws) => {
    let html='', disp='';
    const wrap = { set innerHTML(v){html=v;}, get innerHTML(){return html;},
                   style:{ set display(v){disp=v;}, get display(){return disp;} } };
    new Function('document','getWriteToken', TRACE_STUB + fn + '; return renderTabLock;')(
      { getElementById: id => (id === 'tab-lock-banner' ? wrap : null) },
      () => { if (throws) throw new Error('blocked'); return tok; })();
    return { html, disp };
  };
  let r = run('', false);
  ok('a locked tab shows the banner', r.disp === 'block' && /cannot save anything/.test(r.html));
  ok('it says buttons refuse BEFORE sending', /before sending/.test(r.html));
  ok('it warns the server log will be empty', /nothing appears in the logs/.test(r.html));
  ok('it says this is not a broken button', /is not\./.test(r.html), r.html.slice(0,120));
  ok('it explains the per-tab storage', /this browser tab only/.test(r.html));
  // The claim has to be ACCURATE, not just present. sessionStorage survives a
  // reload; it ends when the tab is closed. An earlier wording said phones
  // clear it whenever you switch apps, which overstates it.
  ok('it says the token SURVIVES a refresh', /survives a\s+refresh/.test(r.html), r.html);
  ok('it ties loss to the tab being CLOSED', /tab is closed/.test(r.html), r.html);
  ok('it does NOT claim switching apps clears it',
     !/every time you\s+switch apps/.test(r.html), 'overstated claim is back');
  // Match the control by NAME, not by a positional phrase that changes
  // every time the copy is reworded.
  ok('it points at the lock bar', /lock bar/.test(r.html), r.html.slice(0,160));
  ok('it covers EVERY button, not one panel',
     /Levels, Reconcile, all of them/.test(r.html));

  r = run('tok', false);
  ok('an unlocked tab hides it', r.disp === 'none' && r.html === '');

  r = run('', true);
  ok('an accessor that THROWS counts as locked, never as unlocked',
     r.disp === 'block', 'a private window must not read as able to send');

  console.log('\n[3] wiring');
  ok('the container exists', /id="tab-lock-banner"/.test(src));
  ok('it is above the alarm banner',
     src.indexOf('id="tab-lock-banner"') < src.indexOf('id="alarm-top-wrap"'));
  ok('it runs on every refresh pass', /renderTabLock\(\);\s*\n\s*renderOutOfReach/.test(src));
  ok('and the moment the token is set or cleared',
     /renderTokenBar\(\);[\s\S]{0,200}renderTabLock\(\)/.test(src));
  ok('the banner adds no fetch of its own', !/fetch\(|apiGet/.test(fn));

  console.log();
  if (fail) { console.log(`${fail} FAILED`); process.exit(1); }
  console.log('all checks passed');
})().catch(e => { console.log('THREW: '+e.stack); process.exit(1); });
