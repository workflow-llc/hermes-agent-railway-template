// SAGA Bot Screen — plain-browser viewer for this profile's Bot Desktop.
//
// Two sockets, both same-origin and both behind the dashboard's own auth:
//
//   1. /api/ws          the gateway JSON-RPC channel (`display.observe` mints the ticket and the
//                       server-side viewer id, `display.lease.*` is Take over / Hand back).
//   2. /api/display/ws  the raw RFB stream, redeemed with that single-use ticket.
//
// Watching never takes control: the frame opens read-only, noVNC's viewOnly stays on, and the
// bridge in the gateway drops input bytes from a viewer that does not hold the lease. Input
// starts flowing only after an explicit Take over.
import RFB from './assets/novnc/core/rfb.js';

const $ = (id) => document.getElementById(id);
const screenEl = $('screen');

let gw = null;
let nextId = 0;
const inflight = new Map();
let viewerId = null;
let rfb = null;
let holding = false;
let startPending = false;
let observing = false;

const MAX_RETRIES = 10;
let retries = 0;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// `display.observe` refuses with this when no screen is up (`methods_display.py`), which is the
// signal to start one rather than an error to hand the human.
const NOT_RUNNING = /not running|call display\.start/i;

function say(text, kind = '') {
  const el = $('status');
  el.textContent = text;
  el.dataset.kind = kind;
}

function setMode() {
  $('takeover').disabled = !rfb || holding || !viewerId;
  $('handback').disabled = !holding;
  const badge = $('mode');
  badge.textContent = holding ? 'You have control' : 'View only';
  badge.dataset.holding = holding ? '1' : '0';
}

function gatewayUrl() {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}/api/ws`;
}

function onFrame(ev) {
  let frame;
  try {
    frame = JSON.parse(ev.data);
  } catch {
    return;
  }
  if (frame.id !== undefined && inflight.has(frame.id)) {
    const { resolve, reject } = inflight.get(frame.id);
    inflight.delete(frame.id);
    if (frame.error) reject(new Error(frame.error.message || JSON.stringify(frame.error)));
    else resolve(frame.result);
    return;
  }
  // Server-pushed events: a lease transition that happened elsewhere (someone else took over,
  // or the agent's screen was started/stopped) is reflected in the badge.
  if (frame.method === 'event' && frame.params) {
    const payload = frame.params.payload || {};
    if (frame.params.type === 'display.lease' && payload.lease) {
      const holder = payload.lease.holder;
      if (holder !== 'human') {
        holding = false;
        if (rfb) rfb.viewOnly = true;
        setMode();
      }
    }
    if (frame.params.type === 'display.status') {
      const running = !!payload.running;
      $('start').hidden = running;
    }
  }
}

async function openGateway() {
  const res = await fetch('/api/auth/ws-ticket', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
  });
  if (!res.ok) throw new Error(`gateway ticket refused (HTTP ${res.status})`);
  const { ticket } = await res.json();
  return await new Promise((resolve, reject) => {
    const ws = new WebSocket(`${gatewayUrl()}?ticket=${encodeURIComponent(ticket)}`);
    let opened = false;
    ws.onopen = () => {
      opened = true;
      resolve(ws);
    };
    ws.onmessage = onFrame;
    ws.onerror = () => {
      if (!opened) reject(new Error('gateway websocket failed'));
    };
    ws.onclose = (e) => {
      if (!opened) {
        reject(new Error(`gateway closed (${e.code})`));
        return;
      }
      if (gw === ws) {
        gw = null;
        say('gateway connection lost — reload to reconnect', 'warn');
      }
    };
  });
}

async function rpc(method, params = {}) {
  if (!gw) gw = await openGateway();
  const id = ++nextId;
  return await new Promise((resolve, reject) => {
    inflight.set(id, { resolve, reject });
    gw.send(JSON.stringify({ jsonrpc: '2.0', id, method, params }));
    setTimeout(() => {
      if (inflight.has(id)) {
        inflight.delete(id);
        reject(new Error(`${method} timed out`));
      }
    }, 20000);
  });
}

// The vendored client is the KasmVNC fork of noVNC, whose constructor is
// `new RFB(targetEl, touchInputEl, urlOrChannel, options)` — FOUR arguments, not upstream's three.
// `touchInput` must be a real DOM element: `Keyboard`'s constructor immediately writes
// `this._touchInput.value`, so passing the URL string there throws
// `Cannot create property 'value' on string 'wss://…'` before the socket is ever opened.
function keyboardInput() {
  let el = document.getElementById('noVNC_keyboardinput');
  if (!el) {
    el = document.createElement('input');
    el.id = 'noVNC_keyboardinput';   // the client's own IME hook queries this exact id
    el.type = 'text';
    el.autocomplete = 'off';
    el.autocapitalize = 'off';
    el.spellcheck = false;
    el.setAttribute('aria-hidden', 'true');
    el.setAttribute('tabindex', '-1');
    el.style.cssText = 'position:fixed;left:-1000px;top:0;width:1px;height:1px;opacity:0;';
    document.body.appendChild(el);
  }
  return el;
}

function connectRfb(obs) {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = new URL(`${scheme}//${location.host}${obs.path}`);
  url.searchParams.set('display_ticket', obs.ticket);

  // Build the replacement BEFORE tearing the old one down: the superseded instance's disconnect
  // handler then sees `rfb !== self` and stays quiet, instead of reporting "screen stopped" and
  // un-hiding Start on the fresh stream.
  const previous = rfb;
  // Both sockets are same-origin and the bridge accepts without a subprotocol, so `wsProtocols: []`
  // is deliberate: requesting `binary` would fail the handshake.
  const self = new RFB(screenEl, keyboardInput(), url.toString(), { shared: true, wsProtocols: [] });
  rfb = self;
  if (previous) {
    try {
      previous.disconnect();
    } catch {
      /* already gone */
    }
  }
  self.viewOnly = true; // watch-only until Take over, and the bridge enforces it server-side
  self.scaleViewport = true;
  self.resizeSession = false;
  self.background = '#0b0d10';
  say('connecting to the screen…');
  setMode();

  self.addEventListener('connect', () => {
    if (rfb !== self) return;
    retries = 0;
    say(holding ? 'connected — you have control' : 'watching (view only)', 'ok');
    setMode();
  });
  self.addEventListener('disconnect', (e) => {
    if (rfb !== self) return; // a superseded instance closing: not this page's state
    const code = e.detail && e.detail.code;
    holding = false;
    rfb = null;
    setMode();
    if (code === 4000) {
      say('someone else took control — back to view only, reconnecting…', 'warn');
    } else if (code === 4001 || code === 1000) {
      // Stopped on purpose: 4001 is the bridge's "screen is gone", 1000 is the teardown a stop
      // under an attached viewer produces (`bot_screen_stop.py`, or the idle timer). Reconnecting
      // here restarted the desktop two seconds later, so a tab left open — including a backgrounded
      // or sleeping one — defeated `idle_stop_minutes` and kept Xvnc alive. Hand the decision back:
      // "screen stopped" plus a Start button. Start-on-page-load is unchanged.
      say('screen stopped — press Start screen to watch again', 'warn');
      $('start').hidden = false;
      return;
    } else {
      say('stream ended — reconnecting…', 'warn');
    }
    // Network drops and evictions are retried through observe(), which starts the screen again if
    // it is down; the cap stops an endless restart loop when the desktop cannot come up at all.
    retries += 1;
    if (retries > MAX_RETRIES) {
      say('the screen keeps dropping — press Start screen to retry', 'err');
      $('start').hidden = false;
      return;
    }
    setTimeout(() => observe().catch(fail), 1500);
  });
}

function fail(err) {
  say(err && err.message ? err.message : String(err), 'err');
}

async function observe() {
  if (observing) return;
  observing = true;
  try {
    const obs = await ensureObserved();
    viewerId = obs.viewer_id;
    $('start').hidden = true;
    connectRfb(obs);
  } finally {
    observing = false;
  }
}

// `bot_desktop.auto_start` is a tool-boundary hook and an idle screen is stopped after
// `bot_desktop.idle_stop_minutes`, so a viewer link opened cold (or left open over lunch) finds
// nothing running. Opening the page therefore starts the screen and waits for it, instead of
// printing "call display.start first" and giving up.
async function ensureObserved() {
  // `display.start` blocks server-side for up to 15 s (runtime.start(wait_seconds=15), under
  // start.lock), so calling it on every 2 s retry meant 30 overlapping bring-ups and a worst case
  // of minutes. Ask once, then poll observe() — which is cheap and is the real readiness signal.
  let started = false;
  for (let attempt = 0; attempt < 30; attempt++) {
    try {
      return await rpc('display.observe', {});
    } catch (err) {
      const msg = String((err && err.message) || err);
      if (!NOT_RUNNING.test(msg)) throw err;
      if (!started) {
        started = true;
        say('starting the bot screen…');
        try {
          await rpc('display.start', {}); // idempotent: a no-op while it is already starting/up
        } catch (startErr) {
          if (!/already|running|starting/i.test(String(startErr.message))) throw startErr;
        }
      } else {
        say('waiting for the screen to come up…');
      }
      await sleep(2000);
    }
  }
  throw new Error('the bot screen did not start — check the display log');
}

async function startScreen() {
  if (startPending) return;
  startPending = true;
  retries = 0;
  say('starting the screen…');
  try {
    await observe();
  } catch (err) {
    fail(err);
    $('start').hidden = false;
  } finally {
    startPending = false;
  }
}

async function takeOver() {
  try {
    await rpc('display.lease.acquire', { viewer_id: viewerId, reason: 'browser viewer' });
    holding = true;
    if (rfb) rfb.viewOnly = false;
    say('you have control — click into the screen, then Hand back when done', 'ok');
    setMode();
  } catch (err) {
    fail(err);
  }
}

async function handBack() {
  try {
    await rpc('display.lease.release', { viewer_id: viewerId });
    holding = false;
    if (rfb) rfb.viewOnly = true;
    say('handed back — watching (view only)', 'ok');
    setMode();
  } catch (err) {
    fail(err);
  }
}

$('takeover').addEventListener('click', takeOver);
$('handback').addEventListener('click', handBack);
$('start').addEventListener('click', startScreen);
window.addEventListener('beforeunload', () => {
  // Closing the page hands control back rather than leaving the agent locked out of its screen.
  if (holding && gw && gw.readyState === 1) {
    gw.send(JSON.stringify({ jsonrpc: '2.0', id: ++nextId, method: 'display.lease.release',
      params: { viewer_id: viewerId } }));
  }
});

setMode();
observe().catch(fail);
