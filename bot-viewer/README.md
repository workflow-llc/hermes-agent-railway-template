# bot-viewer — Bot Screen in a plain browser

A dashboard plugin that serves one page: **watch this gateway's Bot Desktop, Take over, Hand back**.
It exists because the RFB bridge and the lease already ship in Hermes, but the only viewer was the
Hermes Desktop app's Screen pane — and most of the team does not run Hermes Desktop.

    https://<hermes-host>/api/plugins/bot-viewer/page

## What it is (and is not)

* **No backend change.** The page is three static files plus a vendored noVNC (`assets/novnc`, core +
  pako, LGPL-2.1 per `assets/novnc/LICENSE.txt`) and a router that mounts them. It drives the routes
  Hermes already has:
  * `POST /api/auth/ws-ticket` — 30 s single-use gateway ticket (a browser cannot set headers on a
    WebSocket upgrade).
  * JSON-RPC over `/api/ws` — `display.observe` (ticket + server-minted `viewer_id`),
    `display.lease.acquire` / `display.lease.release` (Take over / Hand back), `display.start`.
  * `/api/display/ws?display_ticket=…` — raw RFB, spliced by `hermes_cli/web_routers/display.py` and
    input-gated by the lease's `RfbClientFilter`.
* **Not a dashboard tab.** `tab.hidden` is set; the page needs its own document so noVNC owns the
  canvas and so the URL is pasteable into Slack.
* **Auth is the dashboard's.** Every route under `/api/plugins/bot-viewer/` sits behind the same
  middleware as the rest of `/api/*` — no `public_paths` entry was added — and the two display
  endpoints keep their own checks (single-use ticket for RFB; gateway credential for RPC).
* **Watching is view-only.** The page sets `rfb.viewOnly = true` and only clears it after a successful
  `display.lease.acquire`. noVNC's flag is UX: the gateway drops input bytes from any viewer that does
  not hold the lease. Closing the tab releases the lease rather than locking the agent out.

## Install

Baked into the image by the Railway template's Dockerfile:

    COPY bot-viewer /opt/hermes/plugins/bot-viewer

That is the bundled-plugins root the dashboard already scans (`kanban`, `hermes-achievements`), so a
redeploy is the whole install — nothing on the volume is touched, and `/data/.hermes/plugins` (root
owned on this image) stays out of it.

## Verify

    python3 /data/.hermes/scripts/bot_screen_acceptance.py

Covers: the three surfaces refusing without credentials, the desktop stack being installed, the screen
starting (Xvnc + Xfce + `rfb.sock`), the real Chromium `--user-data-dir` and whether it is on the
volume, and pids/RAM before and after a headed session against the 1000-pid plan cap.
