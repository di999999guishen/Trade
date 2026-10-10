"""WMI-free runtime environment labels.

``platform.platform()`` -- and ``platform.uname()`` behind it -- on Windows route
through ``platform.win32_ver()`` -> ``platform._wmi_query()``, i.e. a WMI call.
When the WMI service is degraded or blocked (common under sandboxing and
hardened enterprise images), that call can block for minutes. The cycle and
backfill code paths called ``platform.platform()`` on every run purely to record
a short environment label, so a single slow WMI query stalled the whole step and
made the test suite look like it had hung.

Reports only need a compact, human-readable environment label for auditability,
so we build it from interfaces that never touch WMI: ``sys`` and ``os.environ``.
"""

from __future__ import annotations

import os
import sys


def platform_label() -> str:
    """Return a compact OS/host label without any WMI or ``platform``-module call.

    Shape: ``<sys.platform>-<win major.minor.build>-<arch>-python<major.minor.micro>``
    with any unavailable component skipped.
    """
    parts: list[str] = [sys.platform]

    get_windows_version = getattr(sys, "getwindowsversion", None)
    if get_windows_version is not None:
        try:
            windows = get_windows_version()
            parts.append(f"win{windows.major}.{windows.minor}.{windows.build}")
        except Exception:  # pragma: no cover - defensive only
            pass

    architecture = os.environ.get("PROCESSOR_ARCHITECTURE") or os.environ.get("HOSTTYPE")
    if architecture:
        parts.append(str(architecture).lower())

    info = sys.version_info
    parts.append(f"python{info.major}.{info.minor}.{info.micro}")

    return "-".join(str(part) for part in parts if part)
