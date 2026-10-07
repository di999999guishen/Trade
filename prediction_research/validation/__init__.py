"""Validation / robustness layer for the ETF prediction pipeline.

These scripts previously lived outside the repo (session scratch dir) and rotted.
They are now version-controlled here so the selection-gate and window-robustness
checks stay runnable and reviewable.

Run via:  python -m prediction_research.validation <subcommand> [args]
Subcommands: window-scan, ic-analysis, constrained-backtest, skill-significance
"""
from __future__ import annotations

SUBCOMMANDS = {
    "window-scan": "window_scan",
    "ic-analysis": "ic_analysis",
    "constrained-backtest": "constrained_backtest",
    "skill-significance": "skill_significance",
}
