"""Descriptive daily factor diagnostics; all outcomes stay registered."""
import numpy as np


def factor_diagnostics(features, samples, seed=42):
    table = features.join(samples.set_index("sample_id")[["session", "symbol", "return_value"]], how="inner")
    records = {}
    for name in features.columns:
        daily = []
        spread = []
        for _, group in table.groupby("session", sort=True):
            pair = group[[name, "return_value"]].dropna()
            # Two assets cannot support meaningful cross-sectional diagnostics.
            if len(pair) < 3 or pair[name].nunique() < 2 or pair.return_value.nunique() < 2:
                continue
            daily.append(float(pair[name].rank().corr(pair.return_value.rank())))
            order = pair.sort_values(name, kind="stable")
            count = max(1, len(order) // 3)
            spread.append(float(order.return_value.iloc[-count:].mean() - order.return_value.iloc[:count].mean()))
        values = np.array(daily)
        sensitivity = {}
        for block in (5, 20):
            if len(values) < 3 * block:
                sensitivity[str(block)] = None
                continue
            rng = np.random.default_rng(seed)
            means = []
            for _ in range(200):
                starts = rng.integers(0, len(values) - block + 1, size=int(np.ceil(len(values) / block)))
                draw = np.concatenate([values[i:i+block] for i in starts])[:len(values)]
                means.append(float(draw.mean()))
            sensitivity[str(block)] = [float(x) for x in np.quantile(means, [.025, .975])]
        records[name] = {"coverage": float(table[name].notna().mean()) if len(table) else None,
                         "daily_rank_ic": daily, "independent_time_unit": "session_not_asset_times_day",
                         "ic_mean": float(values.mean()) if len(values) else None,
                         "ic_std": float(values.std(ddof=1)) if len(values) > 1 else None,
                         "top_minus_bottom_gross_label_return": float(np.mean(spread)) if spread else None,
                         "block_bootstrap_mean_ic_intervals": sensitivity,
                         "status": "descriptive_only" if daily else "insufficient_cross_section"}
    return {"factors": records, "factor_selection_performed": False,
            "transaction_costs": "not_in_label_spread_use_strategy_ledger", "seed": seed}
