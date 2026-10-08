"""带真实市场约束的连续净值回测，对比项目现有「cohort 配对」口径的差距。

为什么需要这个
--------------
flow_backtest.py 自己在 payload 里写明了两点局限：

    "interpretation": "重叠的独立等权 cohort；不是连续组合净值，不报告年化收益或 Sharpe"
    "cost_limitations": "统一往返费率假设，不保证涨跌停成交，未模拟市场冲击"

本脚本把同一批 walk-forward 预测喂进一个真正的逐日模拟组合，
补齐：连续净值 / 年化 / Sharpe / 最大回撤 / 换手，并逐层叠加
交易成本、滑点、涨跌停不可成交三种摩擦，量化「理想回测」到底高估了多少。

标签口径与 features.py 保持一致：
    entry  = bars[idx+1].open      （次日开盘）
    exit   = bars[idx+horizon].close（第 horizon 日收盘）

用法
----
    python constrained_backtest.py --universe commodity --horizon 20 --top-k 3
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # .../prediction_research/validation/<file> -> TradingAgents
sys.path.insert(0, str(REPO))

ETF_NAMES = {
    "518880": "黄金ETF", "159934": "黄金ETF易方达", "159937": "黄金ETF博时",
    "159985": "豆粕ETF", "159980": "有色ETF", "159981": "能源化工ETF",
    "501018": "南方原油LOF", "162411": "华宝油气LOF",
    "510300": "沪深300ETF", "510500": "中证500ETF", "510050": "上证50ETF",
    "159915": "创业板ETF", "588000": "科创50ETF", "512880": "证券ETF",
    "512660": "军工ETF", "512170": "医疗ETF", "512800": "银行ETF",
    "159995": "芯片ETF", "516670": "畜牧ETF", "159593": "A50ETF",
    "515220": "煤炭ETF",
}

LIMIT_PCT = 0.098      # 涨跌停判定阈值（主板 ETF/LOF 为 10%）
TRADING_DAYS = 252


# --------------------------------------------------------------------------
# 组合模拟器
# --------------------------------------------------------------------------

class PortfolioSim:
    """逐日模拟，按 friction 档位决定是否计入摩擦。

    friction 由若干开关组成，逐层叠加以便分离各自的损耗：
        cost      往返手续费（bps，单边一半）
        slippage  单边滑点（bps）
        limits    涨跌停不可成交
    """

    def __init__(self, initial_cash: float = 1_000_000.0, horizon: int = 20,
                 cost_bps: float = 0.0, slippage_bps: float = 0.0,
                 limits: bool = False):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.horizon = horizon
        self.cost_bps = cost_bps
        self.slippage_bps = slippage_bps
        self.limits = limits
        self.positions: dict[str, dict] = {}
        self.equity_curve: list[tuple[str, float]] = []
        self.trades = 0
        self.blocked_buys = 0
        self.blocked_sells = 0

    # -- helpers -----------------------------------------------------------

    def _apply_slip_buy(self, price: float) -> float:
        return price * (1 + self.slippage_bps / 10_000)

    def _apply_slip_sell(self, price: float) -> float:
        return price * (1 - self.slippage_bps / 10_000)

    def _cash_after_cost(self, notional: float) -> float:
        return notional * (1 - self.cost_bps / 2 / 10_000)   # 单边

    # -- core --------------------------------------------------------------

    def sell(self, symbol: str, open_price: float, prev_close: float, day: str) -> None:
        pos = self.positions.pop(symbol)
        if self.limits and prev_close and open_price / prev_close - 1 <= -LIMIT_PCT:
            # 跌停，卖不掉 —— 持仓原样放回
            self.positions[symbol] = pos
            self.blocked_sells += 1
            return
        px = self._apply_slip_sell(open_price)
        proceeds = self._cash_after_cost(pos["shares"] * px)
        self.cash += proceeds
        self.trades += 1

    def buy(self, symbol: str, target_value: float, open_price: float,
            prev_close: float, day: str) -> bool:
        if self.limits and prev_close and open_price / prev_close - 1 >= LIMIT_PCT:
            self.blocked_buys += 1
            return False
        if target_value <= 0 or self.cash <= 0:
            return False
        px = self._apply_slip_buy(open_price)
        if px <= 0:
            return False
        notional = min(target_value, self.cash)
        shares = notional / px
        if shares <= 0:
            return False
        cost = shares * px * (self.cost_bps / 2 / 10_000)
        if shares * px + cost > self.cash:
            shares = (self.cash / (1 + self.cost_bps / 2 / 10_000)) / px
        self.cash -= shares * px * (1 + self.cost_bps / 2 / 10_000)
        self.positions[symbol] = {"shares": shares, "entry": px, "day": day, "held": 0}
        self.trades += 1
        return True

    def mark(self, day: str, prices: dict[str, float]) -> None:
        equity = self.cash
        for sym, pos in self.positions.items():
            equity += pos["shares"] * prices.get(sym, pos["entry"])
        self.equity_curve.append((day, equity))

    # -- reporting ---------------------------------------------------------

    def stats(self) -> dict:
        series = [e for _, e in self.equity_curve]
        if len(series) < 2:
            return {"rows": len(series)}
        rets = [series[i] / series[i - 1] - 1 for i in range(1, len(series))]
        total = series[-1] / series[0] - 1
        years = len(series) / TRADING_DAYS
        cagr = (series[-1] / series[0]) ** (1 / years) - 1 if years > 0 else 0.0
        vol = statistics.pstdev(rets) * math.sqrt(TRADING_DAYS) if len(rets) > 1 else 0.0
        sharpe = (cagr / vol) if vol > 0 else 0.0
        # 最大回撤
        peak, mdd = series[0], 0.0
        for v in series:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        return {
            "final_equity": round(series[-1], 2),
            "total_return": round(total, 6),
            "cagr": round(cagr, 6),
            "annual_vol": round(vol, 6),
            "sharpe": round(sharpe, 4),
            "max_drawdown": round(mdd, 6),
            "days": len(series),
            "trades": self.trades,
            "blocked_buys": self.blocked_buys,
            "blocked_sells": self.blocked_sells,
        }


# --------------------------------------------------------------------------
# 数据 / 信号准备
# --------------------------------------------------------------------------

def load_price_table(snapshots: dict[str, object]) -> dict[str, dict[str, dict]]:
    """symbol -> date -> {open, high, low, close}"""
    table: dict[str, dict[str, dict]] = {}
    for sym, snap in snapshots.items():
        if snap is None:
            continue
        rows: dict[str, dict] = {}
        prev_close = None
        for bar in snap.bars:
            d = bar.trading_date.isoformat() if hasattr(bar.trading_date, "isoformat") else str(bar.trading_date)
            rows[d] = {
                "open": bar.open, "high": bar.high, "low": bar.low,
                "close": bar.close, "prev_close": prev_close,
            }
            prev_close = bar.close
        table[sym] = rows
    return table


def run_strategy(
    signal_by_date: dict[str, list[tuple[str, float]]],
    price_table: dict[str, dict[str, dict]],
    top_k: int,
    **sim_kwargs,
) -> PortfolioSim:
    """signal_by_date: feature_date -> [(symbol, probability_up), ...] 已排序候选。

    决策日 t 用 feature_date=t 的信号，**次日开盘**成交；持仓满 horizon 后于
    次日开盘卖出。所有时间序列严格按交易日推进，杜绝前视。
    """
    sim = PortfolioSim(**sim_kwargs)
    horizon = sim.horizon
    all_dates = sorted({d for rows in price_table.values() for d in rows})

    for i, day in enumerate(all_dates):
        # 1) 到期卖出（今日开盘）
        matured = [s for s, p in sim.positions.items() if p["held"] >= horizon]
        for sym in matured:
            row = price_table.get(sym, {}).get(day)
            if row:
                sim.sell(sym, row["open"], row["prev_close"], day)

        # 2) 用前一交易日(feature_date=prev)的信号，今日开盘买入
        prev_date = all_dates[i - 1] if i > 0 else None
        signals = signal_by_date.get(prev_date) if prev_date else None
        if signals:
            picks = [s for s in signals[:top_k] if s[0] not in sim.positions]
            slots = max(0, top_k - len(sim.positions))
            if picks and slots > 0:
                row0 = price_table.get(picks[0][0], {}).get(day)
                # 等权：目标市值 = 总资产 / top_k
                total_equity = sim.cash + sum(
                    p["shares"] * price_table.get(s, {}).get(day, {}).get("close", p["entry"])
                    for s, p in sim.positions.items()
                )
                target = total_equity / max(top_k, 1)
                for sym, _prob in picks[:slots]:
                    row = price_table.get(sym, {}).get(day)
                    if row:
                        sim.buy(sym, target, row["open"], row["prev_close"], day)

        # 3) 持仓天数推进 + 收盘估值
        for p in sim.positions.values():
            p["held"] += 1
        close_prices = {
            sym: rows[day]["close"] for sym, rows in price_table.items() if day in rows
        }
        sim.mark(day, close_prices)

    return sim


def run_timing_strategy(
    signal_by_date: dict[str, list[tuple[str, float]]],
    price_table: dict[str, dict[str, dict]],
    threshold: float = 0.5,
    **sim_kwargs,
) -> PortfolioSim:
    """择时模式：用**整个池子的平均概率**决定满仓/空仓，不做个股挑选。

    存在理由：commodity h20 实测 Brier skill 显著为正但 IC≈0.027(ICIR 0.05)，
    说明模型学到的是「市场整体方向」而非「个股排序」。若该推断成立，
    按平均概率做择时应优于按 top-k 做选股。本函数即为该推断的检验。
    """
    sim = PortfolioSim(**sim_kwargs)
    all_dates = sorted({d for rows in price_table.values() for d in rows})
    symbols = list(price_table.keys())

    for i, day in enumerate(all_dates):
        prev_date = all_dates[i - 1] if i > 0 else None
        signals = signal_by_date.get(prev_date) if prev_date else None
        want_full = False
        if signals:
            avg_prob = statistics.fmean(p for _s, p in signals)
            want_full = avg_prob > threshold

        if not want_full:
            for sym in list(sim.positions):
                row = price_table.get(sym, {}).get(day)
                if row:
                    sim.sell(sym, row["open"], row["prev_close"], day)
        else:
            target_each = (sim.cash + sum(
                p["shares"] * price_table.get(s, {}).get(day, {}).get("close", p["entry"])
                for s, p in sim.positions.items()
            )) / max(len(symbols), 1)
            for sym in symbols:
                if sym in sim.positions:
                    continue
                row = price_table.get(sym, {}).get(day)
                if row:
                    sim.buy(sym, target_each, row["open"], row["prev_close"], day)

        sim.mark(day, {
            s: r[day]["close"] for s, r in price_table.items() if day in r
        })
    return sim


def filter_price_table(table: dict[str, dict[str, dict]],
                       start: str | None, end: str | None) -> dict[str, dict[str, dict]]:
    """按窗口截断价格表。第一行保留其原始 prev_close，涨跌停判定仍然正确。"""
    if not start and not end:
        return table
    out: dict[str, dict[str, dict]] = {}
    for sym, rows in table.items():
        keep = {
            d: v for d, v in rows.items()
            if (not start or d >= start) and (not end or d <= end)
        }
        if keep:
            out[sym] = keep
    return out


def filter_objects(items: list, attr: str, start: str | None, end: str | None) -> list:
    """按属性日期过滤一组对象（Sample / PredictionResult 通用）。"""
    if not start and not end:
        return items
    kept = []
    for x in items:
        d = getattr(x, attr)
        d = d.isoformat() if hasattr(d, "isoformat") else str(d)
        if start and d < start:
            continue
        if end and d > end:
            continue
        kept.append(x)
    return kept


WS_PATH = Path(__file__).with_name("window_scan.json")
PRESET_WINDOWS: dict[str, dict] = {
    # 全部由 `window_scan.py --data-dir datasets/market_full` 在全历史
    # （commodity 2012-2026 / cached 2005-2026）等权净值上实测得出，不是拍脑袋选的。
    #
    # 硬性约束：model.min_train_dates = 252，窗口内 feature_date 少于它会
    # 导致 walk_forward 产出 0 fold（策略空仓），脚本已设 FATAL 守卫。
    # 因此所有预设一律取 500 交易日级窗口。
    "cached_down_500":      {"start": "2010-11-11", "end": "2012-11-28"},  # -56.87% 长熊
    "cached_flat_500":      {"start": "2023-05-16", "end": "2025-06-10"},  # -0.03%  横盘
    "cached_down_2008":     {"start": "2007-10-18", "end": "2008-10-27"},  # -69.79% 危机
    "commodity_down_500":   {"start": "2013-05-23", "end": "2015-06-23"},  # -34.25%
    "commodity_flat_500":   {"start": "2016-11-25", "end": "2018-12-12"},  # +0.01%  横盘
}


def run_buy_and_hold(price_table: dict[str, dict[str, dict]]) -> PortfolioSim:
    """基准：期初等权买入持有，不调仓。"""
    sim = PortfolioSim()
    all_dates = sorted({d for rows in price_table.values() for d in rows})
    symbols = list(price_table.keys())
    target = sim.initial_cash / max(len(symbols), 1)
    bought = set()

    for day in all_dates:
        for sym in symbols:
            row = price_table.get(sym, {}).get(day)
            if not row:
                continue
            if sym not in bought and sym not in sim.positions:
                if sim.buy(sym, target, row["open"], row["prev_close"], day):
                    bought.add(sym)
        sim.mark(day, {
            s: r[day]["close"] for s, r in price_table.items() if day in r
        })
    return sim


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="cached")
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--window", choices=sorted(PRESET_WINDOWS), default=None,
                    help="预设下行/震荡窗口；与 --start/--end 二选一")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default="constrained_backtest.json")
    ap.add_argument("--data-dir", default=None,
                    help="覆盖 market_data_dir；用于读 datasets/market_full 全历史缓存")
    ap.add_argument("--no-slippage", action="store_true",
                    help="仅在诊断摩擦构成时关闭滑点；关闭后的结果不可用于决策")
    args = ap.parse_args()

    from prediction_research.config import load_config, resolve_project_path
    from prediction_research.data import configured_data_dirs, find_cache
    from prediction_research.evaluation import (
        walk_forward, selection_metrics, signal_verdict,
        excess_metrics, decision_report, DEFAULT_SLIPPAGE_BPS,
    )
    from prediction_research.features import build_samples

    if args.window:
        args.start = PRESET_WINDOWS[args.window]["start"]
        args.end = PRESET_WINDOWS[args.window]["end"]

    cfg = load_config()
    if args.data_dir:
        cfg["market_data_dir"] = args.data_dir
    symbols = [
        str(item["symbol"]) for item in cfg["universes"][args.universe]
        if item.get("symbol")
    ]
    dirs = configured_data_dirs(cfg)

    snapshots, missing = {}, []
    for sym in symbols:
        try:
            snapshots[sym] = find_cache(sym, dirs)
        except (FileNotFoundError, ValueError):
            snapshots[sym] = None
            missing.append(sym)

    win = f"{args.start or 'begin'} ~ {args.end or 'latest'}"
    print(f"universe={args.universe} members={len(symbols)} missing={missing} "
          f"window={win}", flush=True)

    # --- walk-forward 生成样本外概率（限定在窗口内） ---
    all_samples = []
    for sym in symbols:
        if snapshots[sym] is None:
            continue
        all_samples.extend(build_samples(snapshots[sym], args.horizon))
    all_samples = filter_objects(all_samples, "feature_date", args.start, args.end)
    print(f"samples={len(all_samples)}", flush=True)
    predictions, folds = walk_forward(all_samples, cfg["model"])
    print(f"predictions={len(predictions)} folds={len(folds)}", flush=True)

    signal_by_date: dict[str, list[tuple[str, float]]] = {}
    for p in predictions:
        key = p.feature_date.isoformat() if hasattr(p.feature_date, "isoformat") else str(p.feature_date)
        signal_by_date.setdefault(key, []).append((p.symbol, p.probability_up))
    for key in signal_by_date:
        signal_by_date[key].sort(key=lambda x: -x[1])

    if not signal_by_date:
        # 没有信号 => 所有策略等价于空仓。在下跌窗口里空仓必然"跑赢"基准，
        # 这个"跑赢"毫无信息量，必须中止而不是输出误导性的漂亮表格。
        raise SystemExit(
            "\n[FATAL] 窗口内没生成任何预测 -> 策略全程空仓，"
            "在下跌市里空仓会伪装成'跑赢基准'。\n"
            f"  window = {win}\n"
            f"  samples = {len(all_samples)}   folds = {len(folds)}\n"
            f"  常见原因: 窗口内 feature_date 数 < model.min_train_dates"
            f"（当前 {cfg['model']['min_train_dates']}）。\n"
            "  处理: 放宽窗口，或加 --data-dir datasets/market_full 用全历史缓存。\n"
        )

    price_table = load_price_table({k: v for k, v in snapshots.items() if v})
    price_table = filter_price_table(price_table, args.start, args.end)

    # --- 选股能力闸门：用 ICIR 判定，不用 Brier ---
    sel_metrics = selection_metrics(predictions)
    verdict = signal_verdict(sel_metrics)
    print(f"\n[选股闸门] ICIR={verdict['icir']} (门槛 {verdict['icir_gate']}) "
          f"-> {verdict['usages']['stock_picking']}", flush=True)

    slip = 0.0 if args.no_slippage else DEFAULT_SLIPPAGE_BPS
    common = dict(horizon=args.horizon)
    variants = {
        "A_ideal":      dict(**common),
        "B_cost_only":  dict(**common, cost_bps=10),
        "C_cost_slip":  dict(**common, cost_bps=10, slippage_bps=slip),
        "D_full":       dict(**common, cost_bps=10, slippage_bps=slip, limits=True),
    }

    modes: dict[str, dict[str, dict]] = {}
    sel_key = f"stock_picking_top{args.top_k}"
    modes[sel_key] = {
        n: run_strategy(signal_by_date, price_table, args.top_k, **kw).stats()
        for n, kw in variants.items()
    }
    print(f"  {sel_key} done", flush=True)

    for thr in (0.50, 0.55):
        key = f"market_timing_thr{thr}"
        modes[key] = {
            n: run_timing_strategy(signal_by_date, price_table, threshold=thr, **kw).stats()
            for n, kw in variants.items()
        }
        print(f"  {key} done", flush=True)

    bench = run_buy_and_hold(price_table)
    bench_stats = bench.stats()
    print(f"  buy_hold: {bench_stats}", flush=True)

    # --- 强制超额口径 ---
    excess = {
        m: {n: excess_metrics(s, bench_stats) for n, s in vs.items()}
        for m, vs in modes.items()
    }

    ranking = [
        {
            "mode": m, "variant": n,
            "geometric_excess": e["geometric_excess"],
            "strategy_return": e["strategy_total_return"],
            "benchmark_return": e["benchmark_total_return"],
            "sharpe_excess": e["sharpe_excess"],
            "beats_benchmark": e["beats_benchmark"],
        }
        for m, vs in excess.items() for n, e in vs.items()
    ]
    ranking.sort(key=lambda r: -(r["geometric_excess"] if r["geometric_excess"] is not None else -9))

    best_mode = modes[ranking[0]["mode"]][ranking[0]["variant"]]
    decision = decision_report(predictions, best_mode, bench_stats)

    result = {
        "config": vars(args), "universe": symbols, "missing": missing,
        "window": {"name": args.window, "start": args.start, "end": args.end},
        "predictions": len(predictions), "folds": len(folds),
        "signal_dates": len(signal_by_date),
        "slippage_bps": slip,
        "selection_metrics": sel_metrics,
        "signal_verdict": verdict,
        "recommended_usage": "market_timing" if not verdict["selection_usable"] else "both",
        "modes": modes,
        "benchmark_buy_hold": bench_stats,
        "excess_vs_buy_hold": excess,
        "ranking_by_excess": ranking,
        "decision": decision["decision"],
        "decision_note": decision["note"],
    }

    print(f"\n=== 按超额收益排序（决策口径：相对买入持有） ===")
    print(f"{'模式':<26}{'摩擦档':<14}{'策略收益':>10}{'基准收益':>10}{'几何超额':>11}{'判定':>8}")
    for r in ranking:
        tag = "跑赢" if r["beats_benchmark"] else "跑输"
        print(f"{r['mode']:<26}{r['variant']:<14}"
              f"{r['strategy_return']*100:>9.2f}%{r['benchmark_return']*100:>9.2f}%"
              f"{r['geometric_excess']*100:>10.2f}%{tag:>8}")

    out = Path(args.out)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten -> {out}")


if __name__ == "__main__":
    main()
