"""Static audit figures; synthetic and real-source labels stay visible."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def plot_report(payload, path, bps, synthetic):
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), constrained_layout=True)
    for strategy in ("buy_hold_spy", "buy_hold_qqq", "s1_etf_momentum", "s2_multi_factor"):
        key = f"{strategy}_{bps:g}bps"
        frame = pd.DataFrame(payload["results"][key]["nav"])
        days = pd.to_datetime(frame.session)
        axes[0, 0].plot(days, frame.nav / frame.nav.iloc[0], label=strategy)
        axes[0, 1].plot(days, 1 - frame.nav / frame.nav.cummax(), label=strategy)
        axes[1, 0].plot(days, 1 - frame.cash_weight, label=strategy)
    axes[0, 0].set_title("Net normalized wealth")
    axes[0, 1].set_title("Drawdown (positive loss)")
    axes[1, 0].set_title("Risk-asset weight")
    selected = {key: value for key, value in payload["metrics"].items() if key.startswith(("s1_", "s2_"))}
    axes[1, 1].bar(range(len(selected)), [m["total_cost_usd"] for m in selected.values()])
    axes[1, 1].set_xticks(range(len(selected)), [key.replace("_etf_momentum", "").replace("_multi_factor", "") for key in selected], rotation=30, ha="right")
    axes[1, 1].set_title("All-in transaction cost (USD)")
    axes[0, 0].legend(fontsize=8)
    for ax in axes.flat:
        ax.grid(alpha=.2)
    fig.suptitle("SYNTHETIC FIXTURE — NOT INVESTMENT EVIDENCE" if synthetic else "RESEARCH ONLY — total-return proxy, fixed ETF pool")
    fig.savefig(path, dpi=130)
    plt.close(fig)
