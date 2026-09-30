// Storyloom UI acceptance driver — drives headless Chrome over CDP (Node >= 22 native WebSocket).
// Usage: node scripts/accept-ui.mjs [--base http://127.0.0.1:3100] [--cdp 9222] [--out /tmp/storyloom-accept]
import {writeFileSync, mkdirSync} from 'node:fs';

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf('--' + name); return i >= 0 ? args[i + 1] : dflt; };
const BASE = opt('base', 'http://127.0.0.1:3100');
const CDP = opt('cdp', '9222');
const OUT = opt('out', '/tmp/storyloom-accept');
mkdirSync(OUT, {recursive: true});

const report = {base: BASE, steps: [], failures: []};
const step = (name, data) => { report.steps.push({name, ...data}); console.log('•', name, JSON.stringify(data)); };
const fail = (name, why) => { report.failures.push({name, why}); console.error('✗', name, why); };

// --- minimal CDP client ---
async function connect(url) {
  const ws = new WebSocket(url);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pending = new Map();
  const listeners = [];
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) { const {res, rej} = pending.get(msg.id); pending.delete(msg.id); msg.error ? rej(new Error(msg.error.message)) : res(msg.result); }
    else if (msg.method) listeners.forEach(fn => fn(msg));
  };
  return {
    send: (method, params = {}) => new Promise((res, rej) => { const mid = ++id; pending.set(mid, {res, rej}); ws.send(JSON.stringify({id: mid, method, params})); }),
    on: fn => listeners.push(fn),
    close: () => ws.close(),
  };
}
async function evaluate(client, expression) {
  const r = await client.send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  if (r.exceptionDetails) throw new Error('page eval failed: ' + JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
}
async function shot(client, name) {
  const r = await client.send('Page.captureScreenshot', {format: 'png'});
  writeFileSync(`${OUT}/${name}.png`, Buffer.from(r.data, 'base64'));
  console.log('  📸', name);
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

// --- create a fresh tab ---
const tab = await (await fetch(`http://127.0.0.1:${CDP}/json/new?about:blank`, {method: 'PUT'})).json();
const c = await connect(tab.webSocketDebuggerUrl);
const consoleErrors = [];
c.on(msg => {
  if (msg.method === 'Runtime.exceptionThrown') consoleErrors.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
  if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') consoleErrors.push(msg.params.args.map(a => a.value ?? a.description).join(' ').slice(0, 300));
});
await c.send('Page.enable');
await c.send('Runtime.enable');
await c.send('Emulation.setDeviceMetricsOverride', {width: 1440, height: 900, deviceScaleFactor: 1, mobile: false});

async function goto(url) {
  const loaded = new Promise(res => { const h = m => { if (m.method === 'Page.loadEventFired') res(); }; c.on(h); });
  await c.send('Page.navigate', {url});
  await loaded;
  await sleep(1200); // hydration + fonts
}

// ============ 1. Reader empty state (first-launch acceptance) ============
await goto(BASE + '/');
const empty = await evaluate(c, `(() => {
  const q = s => document.querySelector(s);
  return {
    hero: !!q('.reader-browse-hero'),
    invite: (q('.reader-hero-copy p')?.textContent || '').slice(0, 40),
    emptyBox: !!q('.reader-empty'),
    emptyBtn: !!q('.reader-empty button'),
    toolbar: !!q('.reader-toolbar'),
    animationsAtRest: document.getAnimations().length,
    bodyText: document.body.innerText.slice(0, 0),
  };
})()`);
step('reader.empty-state', empty);
if (!empty.emptyBtn) fail('reader.empty-state', 'empty state has no import button');
if (empty.animationsAtRest > 0) fail('reader.empty-state.rest-animations', 'animations running at rest: ' + empty.animationsAtRest);
await shot(c, '01-reader-empty');

// empty-state import button readability (computed colors + contrast of the CTA)
const contrast = await evaluate(c, `(() => {
  const b = document.querySelector('.reader-empty button'); if (!b) return null;
  const cs = getComputedStyle(b);
  const lum = hex => { const m = hex.match(/\\d+\\.?\\d*/g).map(Number); const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); }; return .2126 * f(m[0]) + .7152 * f(m[1]) + .0722 * f(m[2]); };
  const l1 = lum(cs.color) + .05, l2 = lum(cs.backgroundColor) + .05;
  return {color: cs.color, bg: cs.backgroundColor, ratio: +(Math.max(l1, l2) / Math.min(l1, l2)).toFixed(2)};
})()`);
step('reader.empty-cta-contrast', contrast);
if (contrast && contrast.ratio < 3) fail('reader.empty-cta-contrast', 'CTA contrast ' + contrast.ratio);

// ============ 2. Import panel: open animation + error stability ============
const importFlow = await evaluate(c, `(async () => {
  const btn = [...document.querySelectorAll('button')].find(b => b.textContent.includes('导入第一篇微小说')) || [...document.querySelectorAll('button')].find(b => b.textContent.includes('导入'));
  if (!btn) return {found: false};
  const t0 = performance.now();
  btn.click();
  const panel = await new Promise(res => { const poll = () => { const p = document.querySelector('.story-import'); p ? res(p) : requestAnimationFrame(poll); }; poll(); });
  const openMs = +(performance.now() - t0).toFixed(1);
  const btnRect = btn.getBoundingClientRect();
  // submit invalid import to force an error line
  const ta = panel.querySelector('textarea');
  if (ta) { const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set; setter.call(ta, '   '); ta.dispatchEvent(new Event('input', {bubbles: true})); }
  const submit = [...panel.querySelectorAll('button')].find(b => !b.disabled && (b.textContent.includes('导入') || b.textContent.includes('开始')));
  let errMs = null;
  if (submit) {
    const e0 = performance.now();
    submit.click();
    await new Promise(res => { const poll = () => { const e = panel.querySelector('.feedback-line, .story-import [class*="feedback"], .story-import [class*="error"]'); e ? res(e) : (performance.now() - e0 > 3000 ? res(null) : requestAnimationFrame(poll)); }; poll(); });
    errMs = +(performance.now() - e0).toFixed(1);
  }
  const btnRectAfter = btn.getBoundingClientRect();
  return {found: true, openMs, errMs, buttonShift: +(Math.abs(btnRectAfter.top - btnRect.top)).toFixed(1)};
})()`);
step('reader.import-flow', importFlow);
if (importFlow.found) {
  if (importFlow.openMs > 300) fail('reader.import-open', 'panel took ' + importFlow.openMs + 'ms to appear');
  if (importFlow.buttonShift > 1) fail('reader.import-stability', 'toolbar button moved ' + importFlow.buttonShift + 'px when panel opened');
}
await shot(c, '02-import-panel');

// ============ 3. Reduced-motion walkthrough ============
await c.send('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'reduce'}]});
await goto(BASE + '/');
await evaluate(c, `(() => { const b = [...document.querySelectorAll('button')].find(b => b.textContent.includes('导入')); if (b) b.click(); })()`);
await sleep(400);
const rm = await evaluate(c, `({
  animations: document.getAnimations().length,
  running: document.getAnimations().filter(a => a.playState === 'running').length,
  panelVisible: !!document.querySelector('.story-import'),
})`);
step('reader.reduced-motion', rm);
if (rm.running > 0) fail('reader.reduced-motion', rm.running + ' animations still running under reduce');
if (!rm.panelVisible) fail('reader.reduced-motion', 'import panel did not open under RM');
await shot(c, '03-reduced-motion');
await c.send('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'no-preference'}]});

// ============ 4. Author workbench (no projects) ============
await goto(BASE + '/author');
const author = await evaluate(c, `(() => {
  const q = s => document.querySelector(s);
  const btns = [...document.querySelectorAll('button')].map(b => b.textContent.trim()).filter(Boolean).slice(0, 8);
  return {
    page: !!q('.author-page'),
    animationsAtRest: document.getAnimations().filter(a => a.playState === 'running').length,
    buttons: btns,
    hasCreate: btns.some(t => t.includes('新建') || t.includes('创建') || t.includes('开始')),
  };
})()`);
step('author.empty-state', author);
await shot(c, '04-author-empty');

// author RM
await c.send('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'reduce'}]});
await goto(BASE + '/author');
const authorRm = await evaluate(c, `document.getAnimations().filter(a => a.playState === 'running').length`);
step('author.reduced-motion', {running: authorRm});
if (authorRm > 0) fail('author.reduced-motion', authorRm + ' running under reduce');
await c.send('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'no-preference'}]});

// ============ 5. Mobile viewport ============
await c.send('Emulation.setDeviceMetricsOverride', {width: 390, height: 844, deviceScaleFactor: 2, mobile: true});
await c.send('Emulation.setTouchEmulationEnabled', {enabled: true});
await goto(BASE + '/');
await shot(c, '05-mobile-reader');
const mobile = await evaluate(c, `({
  horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
  animationsAtRest: document.getAnimations().filter(a => a.playState === 'running').length,
})`);
step('mobile.reader', mobile);
if (mobile.horizontalOverflow) fail('mobile.reader', 'horizontal overflow on 390px');
await goto(BASE + '/author');
await shot(c, '06-mobile-author');
const mobileA = await evaluate(c, `({horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1})`);
step('mobile.author', mobileA);
if (mobileA.horizontalOverflow) fail('mobile.author', 'horizontal overflow on 390px');

// ============ summary ============
step('console.errors', {count: consoleErrors.length, items: consoleErrors.slice(0, 5)});
if (consoleErrors.length) fail('console', consoleErrors.length + ' console errors/exceptions');
writeFileSync(`${OUT}/report.json`, JSON.stringify(report, null, 2));
console.log(report.failures.length ? `\nFAILURES: ${report.failures.length}` : '\nALL CHECKS PASSED');
c.close();
process.exit(report.failures.length ? 1 : 0);
