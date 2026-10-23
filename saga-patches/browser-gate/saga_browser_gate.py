"""SAGA one-browser gate for the Bot Desktop — enforced in code, not only in skill text.

WHY THIS EXISTS
---------------
Three layers already hold the "one headed browser" cap: Chromium's singleton on the shared
`--user-data-dir`, agent-browser attaching with `--cdp` instead of launching, and the
`bot_browser_session.py` advice check the skills call. None of them covers a scratch script
doing `playwright.chromium.launch()` on its own profile: that is a fresh Chromium tree, and one
tree is 114-341 threads against a 1000 thread/process cap (`pids.current` counts threads). On
DEVS-1068 that is exactly what pushed the container to 980/1000 and produced real
`fork: Resource temporarily unavailable` errors, i.e. Slack, cron and MCP stalls caused by a
test script.

WHAT IT DOES
------------
Wraps `BrowserType.launch` / `launch_persistent_context` on both the sync and async
Playwright APIs. Every Python process that imports Playwright gets the wrapper, because
`sitecustomize.py` next to this file is on PYTHONPATH in the image. Before a launch is
allowed:

  * a headed browser already open on the shared Bot Desktop profile -> REFUSE and tell the
    caller to attach (`connect_over_cdp`) to the live one, never start a second tree;
  * another gate-registered browser is still alive -> REFUSE (single global tree);
  * pids headroom below the reserve -> REFUSE (queue the work; a failed fork inside Hermes
    looks like an agent bug, not a capacity problem).

An allowed launch registers itself in `bot-desktop/scratch-browser.lock` and clears the lock
when the browser disconnects, so the gate sees trees that Chromium's own singleton cannot.

ESCAPE HATCH
------------
`SAGA_BROWSER_GATE=off` in a process's environment disables it (deliberate, e.g. an image
build or a migration). Anything else — including unset — keeps the gate on.

NON-PYTHON LAUNCHERS
--------------------
agent-browser (the browser tool) and the dock's Browser icon never go through Playwright, so
they are unaffected: they already attach to a live instance via `--cdp` and are covered by
`bot_browser_session.py`. This module closes the *scratch Python* hole, not those paths.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

# Measured on this host (DEVS-1068): a headed Chromium at about:blank is 114 threads with the slim
# flag set and 180 with Playwright's defaults; a real page with a login form went to 341. Budget the
# worst case, since refusing early is cheap and a failed fork is not.
BROWSER_THREAD_ESTIMATE = 340
RESERVE = 40
LOCK_NAME = "scratch-browser.lock"

_ALLOW, _REUSE, _BUSY, _BUDGET, _OFF = "allow", "reuse", "busy", "budget", "off"


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "/data/.hermes")


def _lock_path() -> Path:
    return _hermes_home() / "bot-desktop" / LOCK_NAME


def _read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _profile_dir() -> Path:
    override = os.path.expanduser(os.environ.get("AGENT_BROWSER_PROFILE", "").strip())
    if override:
        return Path(override) if os.path.isabs(override) else _hermes_home() / override
    return _hermes_home() / "bot-desktop" / "browser-profile"


def _shared_profile_port() -> int | None:
    """DevTools port of a headed browser already running on the shared Bot Desktop profile.

    Deliberately stdlib-only: a scratch script runs the system interpreter, which has Playwright but
    not Hermes' venv packages, so importing `tools.bot_desktop` here would fail and silently wave the
    second tree through. Same two facts Chromium itself publishes: `DevToolsActivePort` holds the port
    and `SingletonLock` is a symlink to `host-pid`; both outlive a crashed browser, so the pid must be
    alive AND the port must accept a connection.
    """
    profile = _profile_dir()
    try:
        port_text = (profile / "DevToolsActivePort").read_text(encoding="utf-8-sig").splitlines()[0].strip()
        pid_text = os.readlink(profile / "SingletonLock").rpartition("-")[2]
    except (OSError, IndexError):
        return None
    if not (port_text.isdigit() and pid_text.isdigit()) or not _pid_alive(int(pid_text)):
        return None
    port = int(port_text)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return port
    except OSError:
        return None


def _held_lock() -> dict | None:
    try:
        data = json.loads(_lock_path().read_text())
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and _pid_alive(int(data.get("pid") or 0)):
        return data
    return None


def decide() -> tuple[str, str]:
    """(verdict, reason) for a browser launch starting right now."""
    if (os.environ.get("SAGA_BROWSER_GATE", "") or "").strip().lower() in ("off", "0", "false", "no"):
        return _OFF, "SAGA_BROWSER_GATE is off in this process — launch not gated"

    port = _shared_profile_port()
    if port:
        return _REUSE, (
            f"a headed browser is already open on the Bot Desktop profile (DevTools :{port}). Do not "
            f"launch a second tree: attach with playwright.chromium.connect_over_cdp('http://127.0.0.1:{port}') "
            "and reuse the window that is on screen, or wait for the open session to finish.")

    held = _held_lock()
    if held:
        return _BUSY, (
            f"another gated browser is alive (pid {held.get('pid')}, started {held.get('started')}) — this "
            "container runs ONE browser tree at a time. Queue this work, or close that browser first.")

    current = _read_int("/sys/fs/cgroup/pids.current")
    cap = _read_int("/sys/fs/cgroup/pids.max") or 1000
    if current is not None and current + BROWSER_THREAD_ESTIMATE + RESERVE > cap:
        return _BUDGET, (
            f"no pid headroom for a browser: {current}/{cap} threads/processes and one tree needs "
            f"~{BROWSER_THREAD_ESTIMATE}. Queue this task and re-run when the fleet is quieter; do not "
            "raise the Railway plan without asking.")

    return _ALLOW, "no browser open and there is pid headroom for one tree"


def _register(browser) -> None:
    """Record this tree so a second gated launch is refused, and clear it on disconnect."""
    path = _lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(),
                                    "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    "argv": sys.argv[:1]}))
    except OSError:
        return

    def _release(*_a) -> None:
        try:
            data = json.loads(path.read_text())
            if int(data.get("pid") or 0) == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass

    try:
        browser.on("disconnected", _release)
    except Exception:  # noqa: BLE001 - async browsers take a listener, not on()
        try:
            browser.on("disconnect", _release)
        except Exception:  # noqa: BLE001
            pass


class BrowserLaunchRefused(RuntimeError):
    """Raised instead of starting a second Chromium tree."""


def _refuse(verdict: str, reason: str) -> None:
    raise BrowserLaunchRefused(
        f"Bot Desktop browser gate ({verdict}): {reason} [override: SAGA_BROWSER_GATE=off]")


_installed = False


def install() -> bool:
    """Wrap Playwright's launch entry points once per process. False when Playwright is absent."""
    global _installed
    if _installed:
        return True
    try:
        import playwright.sync_api._generated as _sync_gen
        import playwright.async_api._generated as _async_gen
    except Exception:  # noqa: BLE001 - no Playwright in this interpreter
        return False

    def _wrap_sync(orig):
        def launch(self, *args, **kwargs):
            verdict, reason = decide()
            if verdict not in (_ALLOW, _OFF):
                _refuse(verdict, reason)
            result = orig(self, *args, **kwargs)
            target = getattr(result, "browser", None) or result
            _register(target)
            return result
        launch.__name__ = getattr(orig, "__name__", "launch")
        launch.__doc__ = orig.__doc__
        return launch

    def _wrap_async(orig):
        async def launch(self, *args, **kwargs):
            verdict, reason = decide()
            if verdict not in (_ALLOW, _OFF):
                _refuse(verdict, reason)
            result = await orig(self, *args, **kwargs)
            target = getattr(result, "browser", None) or result
            _register(target)
            return result
        launch.__name__ = getattr(orig, "__name__", "launch")
        launch.__doc__ = orig.__doc__
        return launch

    for module, wrapper in ((_sync_gen, _wrap_sync), (_async_gen, _wrap_async)):
        cls = getattr(module, "BrowserType", None)
        if cls is None:
            continue
        for name in ("launch", "launch_persistent_context"):
            orig = getattr(cls, name, None)
            if orig is None or getattr(orig, "_saga_gated", False):
                continue
            new = wrapper(orig)
            new._saga_gated = True  # type: ignore[attr-defined]
            setattr(cls, name, new)
    _installed = True
    return True


def install_screen_hook() -> bool:
    """Make "started on demand" true in code now that `bot_desktop.auto_start` is OFF.

    `bot_desktop.auto_start` is the only thing that made a headed browser start a missing screen
    (`tools/browser_tool_session.py::_ensure_screen_for_headed_chromium` ->
    `runtime.ensure_started_for_tool`), and it also reads as "start a desktop at boot", which is why
    it is disabled. With it off, a headed browse on a cold gateway finds no DISPLAY and Chromium
    fails with "cannot open display": the screen must be started by the thing that needs it.

    This wraps `runtime.ensure_started_for_tool` so the tool boundary starts the screen whenever it
    is missing, whatever the config says, and leaves every other placement decision (refused /
    sandbox) exactly as upstream made it. Idempotent.
    """
    try:
        if "/opt/hermes" not in sys.path:
            sys.path.insert(0, "/opt/hermes")
        from tools.bot_desktop import runtime as _runtime
    except Exception:  # noqa: BLE001 - not a Hermes host
        return False

    original = getattr(_runtime, "ensure_started_for_tool", None)
    if original is None or getattr(original, "_saga_on_demand", False):
        return bool(original)

    def ensure_started_for_tool() -> None:
        original()
        try:
            if _runtime.published_env().get("DISPLAY"):
                _runtime.touch_activity()
                return
        except Exception:  # noqa: BLE001
            return
        try:
            _runtime.start()
        except Exception:  # noqa: BLE001 - the tool's own "no display" diagnosis is the right message
            pass

    ensure_started_for_tool._saga_on_demand = True  # type: ignore[attr-defined]
    _runtime.ensure_started_for_tool = ensure_started_for_tool
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verdict, reason = decide()
    if "--json" in argv:
        print(json.dumps({"verdict": verdict, "reason": reason,
                          "pids_current": _read_int("/sys/fs/cgroup/pids.current"),
                          "pids_max": _read_int("/sys/fs/cgroup/pids.max"),
                          "lock": str(_lock_path()), "held": _held_lock()}, indent=1))
    else:
        print(f"browser gate: {verdict} — {reason}")
    return 0 if verdict in (_ALLOW, _OFF) else (11 if verdict == _BUDGET else 10)


if __name__ == "__main__":
    raise SystemExit(main())
