"""WMI-free process guard for CPython's ``platform`` probes.

Why this exists
---------------
On Windows ``platform.machine()`` resolves through ``uname()`` -> ``win32_ver()``
-> ``_win32_ver()`` -> ``_wmi_query()``, and ``_wmi_query`` issues a **real WMI
query** (``_wmi.exec_query(...)``).  It only short-circuits when the module
global ``platform._wmi`` is falsy::

    def _wmi_query(table, *keys):
        global _wmi
        if not _wmi:
            raise OSError("not supported")
        ...

Now note ``pandas/compat/_constants.py`` calls ``platform.machine()`` **at import
time**::

    WASM = (sys.platform == "emscripten") or (platform.machine() in ["wasm32", "wasm64"])

So on a host whose WMI service is degraded -- observed on this machine: WMI
queries block for *minutes* under sandboxing / hardened enterprise images --
**any** ``import pandas``, and therefore ``import yfinance`` and most of this
project, can stall for minutes.  ``tasklist`` hangs too, which is how we first
noticed.  This is what made the test suite look hung at ~68% and what turned a
single cycle/backfill step into an apparent freeze.

Fix
---
Clear ``platform._wmi`` once, as early as possible.  Every subsequent
``_wmi_query`` then raises ``OSError`` immediately, ``_win32_ver`` falls back to
the registry / ``sys.getwindowsversion()``, and ``machine()`` falls back to
``os.environ['PROCESSOR_ARCHITECTURE']``.  Nothing this project reads depends on
the WMI path -- ``prediction_research.runtime_env.platform_label()`` was already
rewritten to avoid it -- so no label is lost; the multi-minute stall just
disappears.

Set ``WMI_GUARD=0`` to opt out (when you genuinely need WMI-backed values).
"""

from __future__ import annotations

import os
import platform

_OPT_OUT = {"0", "false", "no", "off"}


def disable_wmi() -> bool:
    """Neutralise WMI-backed ``platform`` probes.

    Returns ``True`` when an active ``_wmi`` handle was just cleared, ``False``
    when the guard is opted out, already applied, or unavailable on this
    platform.  Safe to call any number of times.
    """
    if os.environ.get("WMI_GUARD", "1").strip().lower() in _OPT_OUT:
        return False
    # ``platform._wmi`` only exists on Windows builds that ship the ``_wmi``
    # extension; elsewhere there is nothing to neutralise.
    if not hasattr(platform, "_wmi"):
        return False
    was_active = getattr(platform, "_wmi", None) is not None
    platform._wmi = None
    return was_active


# The import-time side effect is the whole point: any entry point that does
# ``import wmi_guard`` is protected before it imports pandas/yfinance.  The call
# is idempotent, so repeated imports stay harmless.
disable_wmi()
