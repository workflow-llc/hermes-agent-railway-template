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
  * another gate-registered headed tree is still alive -> REFUSE (single global tree);
  * pids headroom below the reserve -> REFUSE (queue the work; a failed fork inside Hermes
    looks like an agent bug, not a capacity problem).

The one-session rule is about the *headed* Bot Desktop browser: `headless=True` launches skip the
reuse and single-tree verdicts (a headless tree has its own profile and cannot be pointed at the
window on screen) and are admitted on the pid budget alone. That keeps the shipped headless callers
— `hermes_cli/doctor_live.py`, `plugins/google_meet/meet_bot.py` — working. A
`launch_persistent_context` aimed at the Bot Desktop profile is treated as headed even with
`headless=True`, because it is trying to become the desktop's tree.

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
import uuid
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
    path = _lock_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # An unparsable lock is not automatically stale: a winner that has created the file but
        # whose payload we cannot read yet is a live reservation, and unlinking it would let us
        # both launch. Age decides — seconds old means busy, older means the writer died.
        try:
            if time.time() - path.stat().st_mtime < 5:
                return {"pid": 0, "started": "just created"}
        except OSError:
            return None
        return None
    if isinstance(data, dict) and _pid_alive(int(data.get("pid") or 0)):
        return data
    return None


def decide(*, headless: bool = False) -> tuple[str, str]:
    """(verdict, reason) for a browser launch starting right now.

    `headless` is not a loophole, it is a different question. Two shipped callers inside the image
    launch headless on purpose — `hermes_cli/doctor_live.py:72` (the doctor's live check) and
    `plugins/google_meet/meet_bot.py:476` (the Meet bot's default path) — and neither can "attach
    to the window that is on screen". A headless launch is its own tree with its own profile, so it
    never reuses the desktop's window; it only has to fit the pid budget.
    """
    if (os.environ.get("SAGA_BROWSER_GATE", "") or "").strip().lower() in ("off", "0", "false", "no"):
        return _OFF, "SAGA_BROWSER_GATE is off in this process — launch not gated"

    # A headless tree cannot be pointed at the headed desktop's window, so the reuse and
    # single-tree verdicts do not apply to it; the budget check below still does.
    port = None if headless else _shared_profile_port()
    if port:
        return _REUSE, (
            f"a headed browser is already open on the Bot Desktop profile (DevTools :{port}). Do not "
            f"launch a second tree: attach with playwright.chromium.connect_over_cdp('http://127.0.0.1:{port}') "
            "and reuse the window that is on screen, or wait for the open session to finish.")

    held = None if headless else _held_lock()
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

    if headless:
        return _ALLOW, f"headless launch fits the pid budget ({current}/{cap})"
    return _ALLOW, "no browser open and there is pid headroom for one tree"


class BrowserLaunchRefused(RuntimeError):
    """Raised instead of starting a second Chromium tree."""


def _reserve() -> str | None:
    """Take the gate lock atomically; return its token (None: the lock file is unusable).

    `decide()` followed by a plain `write_text()` was two steps with no lock, so two launchers
    landing at the same instant both saw ALLOW, both started a tree and the second silently
    overwrote the first's record — the exact concurrency this gate exists for. `O_CREAT|O_EXCL`
    makes the reservation itself the decision: one launcher creates the file, the loser gets
    EEXIST and is refused *before* it launches. A lock whose owner is dead is stale and is taken
    over, so a crash mid-flight does not wedge the gate.
    """
    path = _lock_path()
    token = uuid.uuid4().hex
    payload = json.dumps({"pid": os.getpid(),
                          "token": token,
                          "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "argv": sys.argv[:1],
                          "cmdline": " ".join(sys.argv)[:400]})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    for _ in range(3):
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(payload)
            # Content and name in ONE step: EEXIST now means another launcher won, never "the file
            # exists but is still empty", which _held_lock() would read as a stale lock and unlink
            # under the winner's feet.
            os.link(tmp, path)
        except FileExistsError:
            held = _held_lock()
            if held is None:
                # Owner is gone (crashed, SIGKILLed): take the stale lock over instead of wedging.
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            raise BrowserLaunchRefused(
                "Bot Desktop browser gate (busy): another launch registered the shared lock first "
                f"(pid {held.get('pid')}, started {held.get('started')}) — this container runs ONE "
                "browser tree at a time. Queue this work, or close that browser first. "
                "[override: SAGA_BROWSER_GATE=off]") from None
        except OSError:
            return None
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return token
    raise BrowserLaunchRefused(
        "Bot Desktop browser gate (busy): the shared lock could not be taken "
        "[override: SAGA_BROWSER_GATE=off]")


def _release(token: str | None) -> None:
    """Drop the lock only if it is still ours — a later tree's record must survive our exit."""
    if not token:
        return
    path = _lock_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.get("token") != token:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _attach_release(browser, token: str) -> None:
    """Clear our reservation when the tree we registered actually disconnects."""

    def _release_on_disconnect(*_a) -> None:
        _release(token)

    for event in ("disconnected", "disconnect"):
        try:
            browser.on(event, _release_on_disconnect)
            return
        except Exception:  # noqa: BLE001 - async browsers take a listener, not on()
            continue


def _shared_persistent_target(args, kwargs) -> bool:
    """True when launch_persistent_context is aimed at the Bot Desktop profile itself."""
    raw = kwargs.get("user_data_dir")
    if raw is None and args:
        raw = args[0]
    if not raw:
        return False
    try:
        candidate = Path(os.path.expanduser(str(raw)))
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return candidate.resolve() == _profile_dir().resolve()
    except OSError:
        return False


def _admit(args, kwargs, *, persistent: bool) -> tuple[str, str, bool]:
    """(verdict, reason, strict).

    strict = a headed tree, or a persistent context on the Bot Desktop profile: the cases where
    "reuse the window that is on screen" is a real remedy, so they also take the single-tree lock.
    A plainly headless launch is admitted on the pid budget alone.
    """
    headless = bool(kwargs.get("headless", True))  # Playwright's own default is headless
    strict = (not headless) or (persistent and _shared_persistent_target(args, kwargs))
    verdict, reason = decide(headless=not strict)
    return verdict, reason, strict


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

    def _wrap_sync(orig, persistent):
        def launch(self, *args, **kwargs):
            verdict, reason, strict = _admit(args, kwargs, persistent=persistent)
            if verdict not in (_ALLOW, _OFF):
                _refuse(verdict, reason)
            token = _reserve() if (strict and verdict == _ALLOW) else None
            try:
                result = orig(self, *args, **kwargs)
            except BaseException:
                _release(token)
                raise
            target = getattr(result, "browser", None) or result
            if token:
                _attach_release(target, token)
            return result
        launch.__name__ = getattr(orig, "__name__", "launch")
        launch.__doc__ = orig.__doc__
        return launch

    def _wrap_async(orig, persistent):
        async def launch(self, *args, **kwargs):
            verdict, reason, strict = _admit(args, kwargs, persistent=persistent)
            if verdict not in (_ALLOW, _OFF):
                _refuse(verdict, reason)
            token = _reserve() if (strict and verdict == _ALLOW) else None
            try:
                result = await orig(self, *args, **kwargs)
            except BaseException:
                _release(token)
                raise
            target = getattr(result, "browser", None) or result
            if token:
                _attach_release(target, token)
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
            new = wrapper(orig, name == "launch_persistent_context")
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
