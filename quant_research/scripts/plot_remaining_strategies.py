"""Descriptive plots from frozen S6/S7 outputs; never select or refit parameters."""
import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from quant_research.artifacts import file_hash, publication, run_id, verify_files, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    root = args.run.resolve()
    verify_files(root, json.loads((root / "manifest.json").read_text(encoding="utf-8")))
    identifier = run_id("s6s7_report")
    with publication(root.parent, identifier) as stage:
        fig, axes = plt.subplots(1, 2, figsize=(13, 4.7), constrained_layout=True)
        for variant, label in [("spy_ief_residual", "SPY + IEF residual"),
                               ("spy_residual", "SPY residual"), ("raw_reversal", "Raw reversal")]:
            nav = pd.read_parquet(root / f"{variant}_5bps_borrow0.02_nav.parquet")
            axes[0].plot(pd.to_datetime(nav.session), nav.nav/nav.nav.iloc[0], label=label, lw=1.3)
        axes[0].axhline(1, color="gray", linestyle="--", label="Cash: zero interest")
        axes[0].set(title="S6: hypothetical always-borrowable scenarios", ylabel="NAV / initial capital",
                    xlabel="5 bps per traded notional; assumed 2% annual borrow fee")
        scenarios = pd.DataFrame(json.loads((root / "s7_scenarios.json").read_text(encoding="utf-8")))
        for variant, rows in scenarios.groupby("variant"):
            axes[1].plot(rows.spot_end, rows.net_pnl, marker=".", label=variant.replace("_", " "))
        axes[1].plot([60, 140], [-4000, 4000], color="gray", linestyle="--", label="100 shares only")
        axes[1].axhline(0, color="gray", lw=.5)
        axes[1].set(title="S7: invented expiry examples, not backtests", ylabel="Total P&L (USD)",
                    xlabel="Terminal underlying price (entry = $100)")
        for ax in axes:
            ax.grid(alpha=.2)
            ax.legend(fontsize=8)
        fig.savefig(stage / "comparison.png", dpi=150)
        plt.close(fig)
        write_json(stage / "manifest.json", {"source_run": str(root),
                                               "source_manifest_hash": file_hash(root / "manifest.json"),
                                               "plot_script_hash": file_hash(__file__),
                                               "files": {"comparison.png": file_hash(stage / "comparison.png")}})
    print(root.parent / identifier)


if __name__ == "__main__":
    main()
