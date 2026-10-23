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
// Kasm's RFB wants a touch-input element; a detached node keeps its gesture handlers off the page.
const touchEl = document.createElement('div');

let gw = null;
let nextId = 0;
const inflight = new Map();
let viewerId = null;
let rfb = null;
let holding = false;
let startPending = false;

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

function connectRfb(obs) {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = new URL(`${scheme}//${location.host}${obs.path}`);
  url.searchParams.set('display_ticket', obs.ticket);

  if (rfb) {
    try {
      rfb.disconnect();
    } catch {
      /* already gone */
    }
  }
  // The vendored noVNC is Kasm's fork: (target, touchInput, urlOrChannel, options),
  // not upstream noVNC 1.3's (target, url, options). Passing the URL second made the
  // constructor treat the string as the touch element and throw.
  rfb = new RFB(screenEl, touchEl, url.toString(), { shared: true, wsProtocols: ['binary'] });
  rfb.viewOnly = true; // watch-only until Take over, and the bridge enforces it server-side
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.background = '#0b0d10';
  say('connecting to the screen…');
  setMode();

  rfb.addEventListener('connect', () => {
    say(holding ? 'connected — you have control' : 'watching (view only)', 'ok');
    setMode();
  });
  rfb.addEventListener('disconnect', (e) => {
    const code = e.detail && e.detail.code;
    holding = false;
    rfb = null;
    setMode();
    if (code === 4000) {
      say('someone else took control — back to view only, reconnecting…', 'warn');
    } else if (code === 4001) {
      say('the bot screen stopped', 'warn');
      $('start').hidden = false;
      return;
    } else {
      say('stream ended — reconnecting…', 'warn');
    }
    setTimeout(() => observe().catch(fail), 1500);
  });
}

function fail(err) {
  say(err && err.message ? err.message : String(err), 'err');
}

async function observe() {
  let obs = await rpc('display.observe', {});
  viewerId = obs.viewer_id;
  if (!obs.running) {
    // Opening the viewer IS the request for a screen: start it and wait, rather than showing a
    // dead button nobody presses. display.start is idempotent, and the idle-stop timer re-arms.
    $('start').hidden = false;
    say('starting the bot screen…');
    try {
      await rpc('display.start', {});
    } catch (err) {
      say('could not start the screen', 'warn');
      setMode();
      return;
    }
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 500));
      obs = await rpc('display.observe', {});
      viewerId = obs.viewer_id;
      if (obs.running) break;
    }
    if (!obs.running) {
      say('the screen did not come up — press Start to retry', 'warn');
      setMode();
      return;
    }
  }
  $('start').hidden = true;
  connectRfb(obs);
}

async function startScreen() {
  if (startPending) return;
  startPending = true;
  say('starting the screen…');
  try {
    await rpc('display.start', {});
    startPending = false;
    await observe();
  } catch (err) {
    startPending = false;
    fail(err);
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
