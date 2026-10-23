#!/usr/bin/env python3
"""Stop the Bot Desktop the moment a task is done: close the browser, then stop the screen.

The desktop is OFF by default (`bot_desktop.auto_start: false`) and is started on demand — by the
viewer page on load, or by `bot_screen_start.py` in the human-step flow. This is the other half:
so that "on demand" does not quietly mean "left running until the idle timer". Run it as the LAST
step of any task that used the screen or a headed browser, and quote its numbers.

Why a helper and not two RPCs: `display.stop` stops Xvnc + Xfce but leaves a headed Chromium
orphaned on a dead socket (it holds ~100-340 tasks and its profile singleton, so the NEXT task
either fails to launch or attaches to a zombie). Closing the browser first is the order that
matters.

`agent-browser close` is not used: with the daemon already gone it re-spawns headless and fails
("Multiple targets are not supported in headless mode"), leaving the tree behind. The browser is
closed by signal, from the pid Chromium itself advertises in its profile.

Refuses while a human holds the lease — a takeover in progress is not idle time.

Usage:
    HERMES_HOME=/data/.hermes /opt/hermes/.venv/bin/python /opt/saga/bin/bot_screen_stop.py
        [--keep-screen] [--json]

Exit codes:
    0  browser (if any) closed and the screen stopped; prints pids before/after
    2  refused: a human holds the lease (hand back first)
    3  screen still running after the stop attempt
    4  cleanup failed: the browser survived, or runtime.stop() raised (see `error`)
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/hermes")

from tools.bot_desktop import browser, lease, runtime  # noqa: E402


def _read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def cgroup_pids() -> int | None:
    return _read_int("/sys/fs/cgroup/pids.current")


def _snapshot() -> dict[int, tuple[int, tuple[str, ...]]]:
    """pid -> (ppid, argv) for every process this uid can see."""
    procs: dict[int, tuple[int, tuple[str, ...]]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            # `stat` is `pid (comm) state ppid ...`; comm may contain spaces and parentheses.
            stat = (entry / "stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        try:
            argv = tuple(a.decode("utf-8", "replace")
                         for a in (entry / "cmdline").read_bytes().split(b"\0") if a)
        except OSError:
            continue
        procs[pid] = (ppid, argv)
    return procs


def _names_profile(argv: tuple[str, ...], profile: str) -> bool:
    """True when this argv names exactly this profile as its Chromium user-data-dir."""
    wanted = f"--user-data-dir={profile.rstrip('/')}"
    return any(arg.rstrip("/") == wanted for arg in argv)


def _is_chromium(argv: tuple[str, ...]) -> bool:
    return "chrom" in " ".join(argv).lower()


def chrome_pids() -> list[int]:
    """Pids of the Bot Desktop browser tree, and nothing else.

    Roots are the processes whose argv names THIS profile (`--user-data-dir=<profile>`); its
    renderer/zygote/utility children are reached by walking ppid down from those roots. The shared
    bundle path (`chrome-linux64/chrome`) is deliberately NOT matched: `PLAYWRIGHT_BROWSERS_PATH`
    makes every Playwright Chromium in this container use that same binary, so matching it would
    SIGTERM/SIGKILL another task's headless browser, the Meet bot or `hermes doctor --live`.
    """
    profile = str(browser.profile_dir())
    procs = _snapshot()
    roots = {pid for pid, (_ppid, argv) in procs.items()
             if _names_profile(argv, profile) and _is_chromium(argv)}
    if not roots:
        return []
    children: dict[int, list[int]] = {}
    for pid, (ppid, _argv) in procs.items():
        children.setdefault(ppid, []).append(pid)
    tree = set(roots)
    stack = list(roots)
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in tree:
                tree.add(child)
                stack.append(child)
    return sorted(tree)


def _alive(pids: list[int] | set[int]) -> list[int]:
    """Subset of `pids` that still exists AND still looks like a Chromium process.

    The argv re-check keeps a recycled pid (reaped while we waited) out of the SIGKILL pass.
    """
    procs = _snapshot()
    return sorted(pid for pid in pids
                  if pid in procs and _is_chromium(procs[pid][1]))


def _port_holder() -> int | None:
    """pid Chromium advertises for the shared profile (SingletonLock -> host-pid), if it is ours.

    SingletonLock outlives a crashed browser and its pid can be recycled by any later process, so
    the pid must be alive *and* still carry this profile's `--user-data-dir`.
    """
    profile = Path(str(browser.profile_dir()))
    try:
        target = os.readlink(profile / "SingletonLock")
        pid = int(target.rpartition("-")[2])
    except (OSError, ValueError):
        return None
    procs = _snapshot()
    if pid not in procs:
        return None
    _ppid, argv = procs[pid]
    if not (_is_chromium(argv) and _names_profile(argv, str(profile))):
        return None
    return pid


def stop_browser(timeout: float = 12.0) -> dict:
    tree = chrome_pids()
    holder = _port_holder()
    if holder and holder not in tree:
        tree.append(holder)
    if not tree:
        return {"closed": False, "killed": [], "reason": "no Chromium on this profile"}
    # Signal the whole recorded tree, not a fresh scan: once the root exits its children are
    # reparented to init and a second scan can no longer attribute them to this profile.
    for pid in tree:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline and _alive(tree):
        time.sleep(0.5)
    killed = []
    for pid in _alive(tree):
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except OSError:
            pass
    if killed:
        time.sleep(0.5)
    # Chromium's singleton files outlive a SIGKILL and would make the next launch think a browser
    # is up: clear them from the profile once nothing is left running.
    leftovers = []
    profile = Path(str(browser.profile_dir()))
    if not _alive(tree):
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort"):
            try:
                (profile / name).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                leftovers.append(f"{name}: {exc}")
    return {"closed": not _alive(tree), "killed": killed, "leftovers": leftovers,
            "pids_signalled": tree}


def clear_scratch_lock() -> bool:
    """Drop a gate lock whose owner died mid-flight (its browser is gone with it)."""
    path = runtime.state_dir() / "scratch-browser.lock"
    try:
        data = json.loads(path.read_text())
        pid = int(data.get("pid") or 0)
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return False  # owner alive; the gate's own disconnect hook will clear it
    except OSError:
        pass
    try:
        path.unlink()
        return True
    except OSError:
        return False


def main() -> int:
    as_json = "--json" in sys.argv
    keep_screen = "--keep-screen" in sys.argv

    if lease.human_holds():
        msg = "refused: a human holds the Bot Desktop lease — hand back first, then stop"
        print(json.dumps({"stopped": False, "reason": msg}) if as_json else msg)
        return 2

    # Refusal is a transition, not a read: `release(unless_human=True)` decides under the lease
    # file lock (the same idiom `display.stop` uses) and returns the resulting lease, so a takeover
    # that landed after the check above cannot have its browser killed under it. Re-checked here,
    # microseconds before the destructive part instead of 12 s earlier.
    if lease.release(unless_human=True).holder == lease.HUMAN:
        msg = "refused: a human holds the Bot Desktop lease — hand back first, then stop"
        print(json.dumps({"stopped": False, "reason": msg}) if as_json else msg)
        return 2

    before = cgroup_pids()
    browser_result = stop_browser()
    lock_cleared = clear_scratch_lock()
    screen_stopped = False
    error = None
    if not keep_screen:
        try:
            screen_stopped = runtime.stop()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            error = f"{type(exc).__name__}: {exc}"
    st = runtime.status().as_dict()
    after = cgroup_pids()
    out = {
        "browser": browser_result,
        "scratch_lock_cleared": lock_cleared,
        "screen_running_after": st.get("running"),
        "screen_stopped": not st.get("running"),
        "pids_before": before,
        "pids_after": after,
        "pids_delta": (after - before) if (before is not None and after is not None) else None,
        "error": error,
    }
    if as_json:
        print(json.dumps(out, indent=1, default=str))
    else:
        print(f"browser closed={browser_result['closed']} "
              f"killed={len(browser_result['killed'])} lock_cleared={lock_cleared}")
        print(f"screen running={st.get('running')} stopped={out['screen_stopped']}"
              + (f" error={error}" if error else ""))
        print(f"pids {before} -> {after} (delta {out['pids_delta']})")
    browser_ok = (browser_result.get("closed")
                  or browser_result.get("reason") == "no Chromium on this profile")
    if error or not browser_ok:
        return 4  # cleanup failed: a surviving Chromium must not read as success
    if keep_screen:
        return 0
    return 0 if out["screen_stopped"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
