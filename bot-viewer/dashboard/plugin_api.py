"""Bot Screen viewer page: a plain-browser noVNC client for THIS gateway's Bot Desktop.

Why a plugin and not a core route: the RFB bridge (``/api/display/ws``) and the lease RPCs already
ship in Hermes; what is missing server-side is nothing at all. What was missing is a page a human
can open in a normal browser — Hermes Desktop has the pane, but most of the team does not run it.
So this is a *static asset mount plus one HTML file*, served from the dashboard origin that already
carries the basic-auth gate, and it talks to the same two endpoints the Desktop pane uses:

* ``POST /api/auth/ws-ticket`` → mint a 30 s single-use gateway ticket (browser cannot set headers
  on a WebSocket upgrade).
* JSON-RPC over ``/api/ws``: ``display.observe`` (ticket + server-minted viewer id),
  ``display.lease.acquire`` / ``display.lease.release`` (Take over / Hand back), ``display.start``.
* ``/api/display/ws?display_ticket=…`` → raw RFB, spliced by the gateway and input-gated by the
  lease's ``RfbClientFilter``.

No backend change: every route under ``/api/plugins/bot-viewer/`` goes through the same dashboard
auth middleware as the rest of ``/api/*`` (no ``public_paths`` entry), and the two display endpoints
carry their own checks. Nothing here reads or writes the lease; the page only asks the gateway.

Asset paths are resolved under this file's directory with a realpath containment check: the page is
served to authenticated humans only, but a traversal bug would widen that to any file this process
can read (``.env``, vault), so the guard is explicit rather than implicit.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()

_HERE = Path(__file__).resolve().parent
_ASSETS = _HERE / "assets"

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


def _serve(path: Path, *, cache: str = "no-cache") -> FileResponse:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        path,
        media_type=_MIME.get(path.suffix.lower(), "application/octet-stream"),
        headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff",
                 # The page owns its own noVNC copy; nothing external is loaded.
                 "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                                            "connect-src 'self' ws: wss:; style-src 'self' 'unsafe-inline'"},
    )


@router.get("/page")
async def viewer_page() -> FileResponse:
    """The viewer. Open it in any browser; it starts view-only and never takes the lease by itself."""
    return _serve(_HERE / "index.html")


@router.get("/app.js")
async def viewer_js() -> FileResponse:
    return _serve(_HERE / "app.js")


@router.get("/style.css")
async def viewer_css() -> FileResponse:
    return _serve(_HERE / "style.css")


@router.get("/assets/{relpath:path}")
async def viewer_asset(relpath: str) -> FileResponse:
    """noVNC, vendored under ``assets/novnc`` (core + pako), served from this origin only."""
    target = (_ASSETS / relpath).resolve()
    try:
        target.relative_to(_ASSETS)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    return _serve(target, cache="public, max-age=86400")
