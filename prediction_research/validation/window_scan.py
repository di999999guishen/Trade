"""扫描各 ETF 池等权净值，定位真实的下行 / 震荡窗口。

为什么先做这一步
----------------
上一轮回测覆盖 2022-07 ~ 2026-09，commodity 池 8 只里有 3 只是黄金，
整段窗口恰好是黄金单边牛市，买入持有 +86%，任何主动操作都在跟"躺赢"比。
这种环境下得出的结论（"模型跑输"）无法区分是模型无用还是环境偏袒。

本脚本不依赖模型，只做纯价格分析，回答：
    - 这段历史里，哪些区间是真正的下行 / 震荡？
    - 每个窗口的长度、跌幅、是否具备回测所需的最小样本量？
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # .../prediction_research/validation/<file> -> TradingAgents
sys.path.insert(0, str(REPO))

TRADING_DAYS = 252


def load_prices(symbols: list[str], dirs) -> dict[str, dict[str, float]]:
    """symbol -> date -> close"""
    from prediction_research.data import find_cache

    out: dict[str, dict[str, float]] = {}
    for sym in symbols:
        try:
            snap = find_cache(sym, dirs)
        except (FileNotFoundError, ValueError):
            continue
        rows = {}
        for bar in snap.bars:
            d = bar.trading_date.isoformat() if hasattr(bar.trading_date, "isoformat") else str(bar.trading_date)
            rows[d] = bar.close
        out[sym] = rows
    return out


def equal_weight_nav(prices: dict[str, dict[str, float]]) -> list[tuple[str, float]]:
    """等权持有全部成分（按各自首个有效日起逐步纳入），返回 (date, nav)。"""
    all_dates = sorted({d for rows in prices.values() for d in rows})
    holdings: dict[str, float] = {}   # sym -> entry price
    nav: list[tuple[str, float]] = []
    for d in all_dates:
        for sym, rows in prices.items():
            if sym not in holdings and d in rows:
                holdings[sym] = rows[d]
        if not holdings:
            continue
        val = statistics.fmean(
            rows[d] / holdings[sym]
            for sym, rows in prices.items()
            if sym in holdings and d in rows
        )
        nav.append((d, val))
    return nav


def max_drawdown_window(nav: list[tuple[str, float]]) -> tuple[str, str, float]:
    """返回最大回撤的 (起点, 终点, 跌幅)。"""
    peak_i, peak_v = 0, nav[0][1]
    best = (nav[0][0], nav[0][0], 0.0)
    for i, (d, v) in enumerate(nav):
        if v > peak_v:
            peak_i, peak_v = i, v
        dd = v / peak_v - 1
        if dd < best[2]:
            best = (nav[peak_i][0], d, dd)
    return best


def scan_windows(nav: list[tuple[str, float]], min_days: int = 120) -> list[dict]:
    """滑动窗口，枚举 n 个交易日内的收益，挑出最差与最接近零的窗口。"""
    spans = [60, 120, 250, 500]
    found: list[dict] = []
    n = len(nav)
    for span in spans:
        if n <= span + min_days:
            continue
        records = []
        for i in range(0, n - span):
            j = i + span
            ret = nav[j][1] / nav[i][1] - 1
            records.append((ret, nav[i][0], nav[j][0], i, j))
        if not records:
            continue
        records.sort(key=lambda r: r[0])
        worst = records[0]
        flattest = min(records, key=lambda r: abs(r[0]))
        for tag, rec in (("worst", worst), ("flattest", flattest)):
            i, j = rec[3], rec[4]
            seg = nav[i:j + 1]
            step = [seg[k][1] / seg[k - 1][1] - 1 for k in range(1, len(seg))]
            found.append({
                "tag": tag, "span_days": span,
                "start": rec[1], "end": rec[2],
                "total_return": round(rec[0], 6),
                "annual_vol": round(statistics.pstdev(step) * (TRADING_DAYS ** 0.5), 6) if len(step) > 1 else None,
                "bars": len(seg),
            })
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universes", nargs="+", default=["commodity", "cached"])
    ap.add_argument("--out", default="window_scan.json")
    ap.add_argument("--data-dir", default=None,
                    help="覆盖 market_data_dir；用于读 datasets/market_full 全历史缓存")
    args = ap.parse_args()

    from prediction_research.config import load_config
    from prediction_research.data import configured_data_dirs

    cfg = load_config()
    if args.data_dir:
        cfg["market_data_dir"] = args.data_dir
    dirs = configured_data_dirs(cfg)
    report: dict = {}

    for uni in args.universes:
        symbols = [str(i["symbol"]) for i in cfg["universes"][uni] if i.get("symbol")]
        prices = load_prices(symbols, dirs)
        nav = equal_weight_nav(prices)
        if len(nav) < 200:
            report[uni] = {"ok": False, "error": f"nav too short: {len(nav)}"}
            continue
        mdd = max_drawdown_window(nav)
        mdd_rng = [d for d, _ in nav]
        report[uni] = {
            "ok": True,
            "members": len(prices),
            "nav_start": nav[0][0], "nav_end": nav[-1][0], "nav_bars": len(nav),
            "full_period_return": round(nav[-1][1] / nav[0][1] - 1, 6),
            "max_drawdown": {"start": mdd[0], "end": mdd[1], "depth": round(mdd[2], 6)},
            "windows": scan_windows(nav),
        }

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
