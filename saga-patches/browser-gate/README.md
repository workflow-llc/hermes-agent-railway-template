# browser-gate — one Chromium tree per container, enforced in code

`PYTHONPATH=/opt/saga/browser-gate` (set in the Dockerfile) puts `sitecustomize.py` on the import
path of **every** Python process in the service. When a process imports Playwright, the meta-path
hook wraps `BrowserType.launch` and `launch_persistent_context` (sync and async) so that a launch
first passes the same admission decision the skills use:

| verdict | when | what the caller sees |
| -- | -- | -- |
| `allow` | nothing open, pid headroom | launch proceeds, and the tree is recorded in `<HERMES_HOME>/bot-desktop/scratch-browser.lock` until it disconnects |
| `reuse` | a headed browser is already open on the shared profile | `BrowserLaunchRefused` naming the DevTools port, so the caller attaches with `connect_over_cdp` instead of starting a second tree |
| `busy` | another gated browser is still alive | `BrowserLaunchRefused` — queue the work |
| `budget` | `pids.current + 340 + 40 > pids.max` | `BrowserLaunchRefused` — queue, or ask about the plan; do not change it |

Why it exists: the skill rule, `bot_browser_session.py` and Chromium's profile singleton all cover
the *sanctioned* paths. A throwaway `playwright.chromium.launch()` in a test script covers none of
them, and one Chromium tree is 114–341 cgroup tasks against a 1000 cap — on DEVS-1068 that is what
took the container to 980/1000 and produced real `fork: Resource temporarily unavailable` errors
inside Slack, cron and MCPs.

Check it without launching anything:

```bash
PYTHONPATH=/opt/saga/browser-gate python3 -m saga_browser_gate [--json]
```

`SAGA_BROWSER_GATE=off` in a process's environment bypasses it, deliberately and visibly.

Not covered, by design: agent-browser (the browser tool, already attaches over `--cdp`), the dock's
Browser icon, and any non-Python launcher. Those keep the previous layers.
