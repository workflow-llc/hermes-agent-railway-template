"""Installed on PYTHONPATH in the Hermes image (see Dockerfile): routes every Playwright browser
launch in every Python process through the SAGA one-browser gate.

Imported by `site` at interpreter start, so it must be cheap and must never raise. The patching
itself happens only when a process actually imports Playwright, via a meta-path hook that runs
*after* the Playwright modules finish executing.

Two designs were tried and one discarded: wrapping `builtins.__import__` re-enters during
Playwright's own nested imports (it imports asyncio and its bindings mid-flight) and broke the
import with `NameError: name 'base_events' is not defined`. A loader post-hook leaves the import
machinery alone, which is why this file uses one.

Any *other* sitecustomize that would otherwise be shadowed (Debian images install the apport hook
in /usr/lib/python3*/sitecustomize.py) is re-imported here, guarded: a second sitecustomize
failing must not break the interpreter.
"""

import importlib.abc
import importlib.util
import os
import sys

_TARGET_MODULES = frozenset({
    "playwright.sync_api",
    "playwright.async_api",
    "playwright.sync_api._generated",
    "playwright.async_api._generated",
    # The on-demand screen start lives on the Hermes side, not Playwright's: the wrapper goes in when
    # the runtime module is imported (see saga_browser_gate.install_screen_hook).
    "tools.bot_desktop.runtime",
})


def _chain_other_sitecustomize():
    here = os.path.dirname(os.path.abspath(__file__))
    for entry in list(sys.path):
        try:
            if os.path.abspath(entry or ".") == here:
                continue
            candidate = os.path.join(entry, "sitecustomize.py")
            if not os.path.isfile(candidate):
                continue
            spec = importlib.util.spec_from_file_location("_saga_chained_sitecustomize", candidate)
            if spec and spec.loader:
                spec.loader.exec_module(importlib.util.module_from_spec(spec))
                return
        except Exception:  # noqa: BLE001
            continue


class _AfterImportLoader(importlib.abc.Loader):
    """Delegates to the real loader, then runs the gate installer."""

    def __init__(self, inner):
        self._inner = inner

    def create_module(self, spec):
        create = getattr(self._inner, "create_module", None)
        return create(spec) if create else None

    def exec_module(self, module):
        self._inner.exec_module(module)
        try:
            import saga_browser_gate
            if module.__name__ == "tools.bot_desktop.runtime":
                saga_browser_gate.install_screen_hook()
            else:
                saga_browser_gate.install()
        except Exception:  # noqa: BLE001 - never break the caller's import
            pass


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self):
        self._busy = False

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _TARGET_MODULES or self._busy:
            return None
        self._busy = True
        try:
            for finder in list(sys.meta_path):
                if finder is self:
                    continue
                find_spec = getattr(finder, "find_spec", None)
                if find_spec is None:
                    continue
                try:
                    spec = find_spec(fullname, path, target)
                except Exception:  # noqa: BLE001
                    continue
                if spec is None or spec.loader is None:
                    continue
                if isinstance(spec.loader, _AfterImportLoader):
                    return spec
                spec.loader = _AfterImportLoader(spec.loader)
                return spec
        finally:
            self._busy = False
        return None


def _install_finder():
    if any(isinstance(f, _Finder) for f in sys.meta_path):
        return
    # Insert early: after the builtin/frozen importers, before path-based finders matter — position
    # only affects which finder answers first, and this one delegates.
    sys.meta_path.insert(0, _Finder())


_chain_other_sitecustomize()
try:
    _install_finder()
except Exception:  # noqa: BLE001
    pass
