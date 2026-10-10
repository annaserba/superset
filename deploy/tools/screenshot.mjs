#!/usr/bin/env node
/**
 * Screenshot a Superset URL through headless Chrome (CDP).
 *
 * Usage:
 *   node tools/screenshot.mjs <url> <out.png> [--user admin] [--password admin]
 *                             [--width 1600] [--height 1000] [--wait 12000]
 *                             [--headed]
 *
 * Logs in through the standard login form, waits for the page to settle, dumps
 * console errors and every HTTP response that is not 2xx/3xx, and writes a PNG.
 * Exits non-zero when the page reported errors.
 */
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const CHROME = process.env.CHROME_PATH
  || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

const argv = process.argv.slice(2);
const positional = argv.filter((a) => !a.startsWith('--'));
const flag = (name, fallback) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : fallback;
};
const has = (name) => argv.includes(`--${name}`);

const target = positional[0];
const outPath = positional[1];
if (!target || !outPath) {
  console.error('usage: screenshot.mjs <url> <out.png> [--user u] [--password p]');
  process.exit(2);
}
const user = flag('user', 'admin');
const password = flag('password', 'admin');
const width = Number(flag('width', 1600));
const height = Number(flag('height', 1000));
const settle = Number(flag('wait', 12000));
const port = Number(flag('port', 9333));

const profile = mkdtempSync(join(tmpdir(), 'superset-shot-'));
const chromeArgs = [
  has('headed') ? '--headless=false' : '--headless=new',
  '--disable-gpu',
  '--no-first-run',
  '--no-default-browser-check',
  '--hide-scrollbars',
  `--user-data-dir=${profile}`,
  `--remote-debugging-port=${port}`,
  `--window-size=${width},${height}`,
  target.startsWith('http') ? 'about:blank' : target,
];
const chrome = spawn(CHROME, chromeArgs, { stdio: 'ignore' });

const base = new URL(target).origin;
let ws;
let nextId = 1;
const pending = new Map();
const consoleErrors = [];
const badResponses = [];

const send = (method, params = {}, sessionId) =>
  new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });

// Wait for the DevTools endpoint to come up.
let version;
for (let attempt = 0; attempt < 60; attempt++) {
  try {
    version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
    break;
  } catch {
    await sleep(250);
  }
}
if (!version) {
  console.error('Chrome DevTools endpoint did not start');
  chrome.kill();
  process.exit(1);
}

ws = new WebSocket(version.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve, { once: true });
  ws.addEventListener('error', reject, { once: true });
});

let sessionId;
ws.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
    return;
  }
  if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
    consoleErrors.push(message.params.args.map((a) => a.value ?? a.description).join(' '));
  }
  if (message.method === 'Runtime.exceptionThrown') {
    consoleErrors.push(message.params.exceptionDetails.text
      + ' ' + (message.params.exceptionDetails.exception?.description ?? ''));
  }
  if (message.method === 'Network.responseReceived') {
    const { status, url } = message.params.response;
    if (status >= 400 && !url.includes('favicon') && !url.includes('service-worker')) {
      badResponses.push(`${status} ${url}`);
    }
  }
});

const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
({ sessionId } = await send('Target.attachToTarget', { targetId, flatten: true }));
const call = (method, params) => send(method, params, sessionId);
await call('Page.enable');
await call('Runtime.enable');
await call('Network.enable');
await call('Emulation.setDeviceMetricsOverride', {
  width, height, deviceScaleFactor: 1, mobile: false,
});

const evaluate = async (expression) => {
  const result = await call('Runtime.evaluate', {
    expression, awaitPromise: true, returnByValue: true,
  });
  return result.result?.value;
};

const goto = async (url) => {
  await call('Page.navigate', { url });
  for (let attempt = 0; attempt < 80; attempt++) {
    await sleep(250);
    if (await evaluate('document.readyState === "complete"')) return;
  }
};

// 1. log in
await goto(`${base}/login/`);
// React needs a moment to hydrate the form; wait for the inputs to exist.
let hasForm = false;
for (let attempt = 0; attempt < 40 && !hasForm; attempt++) {
  await sleep(250);
  hasForm = await evaluate('!!document.querySelector(\'input#username, input[name="username"]\')');
}
if (!hasForm) {
  console.error('login form never rendered');
  ws.close();
  chrome.kill();
  process.exit(1);
}
await evaluate(`(() => {
  const set = (el, value) => {
    const setter = Object.getOwnPropertyDescriptor(el.constructor.prototype, 'value').set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const form = document.querySelector('form');
  // The Ant Design inputs carry an id but no name attribute.
  set(form.querySelector('input#username, input[name="username"]'), ${JSON.stringify(user)});
  set(form.querySelector('input#password, input[name="password"]'), ${JSON.stringify(password)});
  form.requestSubmit();
  return true;
})()`);
let loggedIn = false;
for (let attempt = 0; attempt < 40 && !loggedIn; attempt++) {
  await sleep(250);
  loggedIn = await evaluate('!location.pathname.startsWith("/login")');
}
console.log(`login: ${loggedIn ? 'ok' : 'FAILED'} (at ${await evaluate('location.pathname')})`);

// 2. open the requested page and let it settle
await goto(target);
await sleep(settle);

// 3. report what the page saw
const summary = await evaluate(`JSON.stringify({
  title: document.title,
  path: location.pathname,
  h1: (document.querySelector('h1') || {}).innerText || '',
  tiles: document.querySelectorAll('[data-test="chart-container"], .dashboard-component-chart-holder').length,
  errors: (document.body.innerText.match(/error|exception|forbidden|not found/gi) || []).slice(0, 8),
  text: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 400),
})`);
console.log('page:', summary);
if (consoleErrors.length) {
  console.log('console errors:');
  for (const line of consoleErrors.slice(0, 10)) console.log('  -', line.slice(0, 200));
}
if (badResponses.length) {
  console.log('failed responses:');
  for (const line of [...new Set(badResponses)].slice(0, 10)) console.log('  -', line);
}

// 4. screenshot
mkdirSync(dirname(outPath), { recursive: true });
const shot = await call('Page.captureScreenshot', {
  format: 'png', captureBeyondViewport: true,
});
writeFileSync(outPath, Buffer.from(shot.data, 'base64'));
console.log(`screenshot written: ${outPath}`);

ws.close();
chrome.kill();
await sleep(500);
process.exit(consoleErrors.length || badResponses.length || !loggedIn ? 1 : 0);
