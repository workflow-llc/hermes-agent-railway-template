"""Startup shim for the SAGA browser gate.

Two copies of this file exist in the image on purpose:

  * `/opt/saga/browser-gate/sitecustomize.py` (with `PYTHONPATH=/opt/saga/browser-gate`);
  * one in **each interpreter's site-packages**, which the Dockerfile installs.

The second copy is the load-bearing one. The bundled launcher is not a console script: it does
`unset PYTHONPATH PYTHONHOME; export PYTHONPATH="$REPO:$SITE"` before `exec`ing python
(`/opt/hermes/.venv/bin/hermes:16-17`, and the same in `libexec/hermes*`), so a PYTHONPATH-only
install never reaches `sys.path` in the gateway, the dashboard or any `hermes` subcommand — the
gate would look installed while never being imported. `site` still imports `sitecustomize` from
site-packages at startup, which is why this file has to live there.

Import of this shim must never break an interpreter: every path is guarded.
"""

try:
    import saga_browser_gate

    saga_browser_gate.install_import_hooks()
except Exception:  # noqa: BLE001 - a startup hook must never take the interpreter down
    pass
