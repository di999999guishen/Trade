"""Pytest bootstrap for the repository root.

Importing this module applies the WMI guard *before* pytest collects any test
module -- and therefore before any ``import pandas`` -- so a degraded WMI service
can no longer stall the suite.  See ``wmi_guard`` for the full rationale.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import wmi_guard  # noqa: E402,F401  (import side effect: disables WMI-backed platform probes)
