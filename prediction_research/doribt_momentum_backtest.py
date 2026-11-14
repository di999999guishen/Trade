"""20 日 ETF 动量轮动最小回测（DoriBT 版）

链路：本地 cache CSV（新浪回填，data source 见 datasets/market_full）→ DoriBT 回测（A 股 ETF 规则内置）→ 年化/夏普/最大回撤。
策略：每 20 个交易日调仓，持有 20 日动量最强的 2 只等权；对比基准 = 510050 买入持有。
口径声明：
  - 价格为 cache CSV 原始收盘（未做分红复权调整，ETF 分红极少，影响有限但须知情）；
  - 回测期受 doribt china presets 上限约束：2022-09-01 ~ 2026-10-12；
  - 成本：佣金万三 + 最低 5 元（ETF 无印花税）；
  - 交易规则由 china_rules 自动处理（普通 ETF T+1、黄金 ETF T+0、跨境 ETF T+0、涨跌停阻断）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from doribt import (
    Backtest,
    Context,
    CorporateAction,
    Costs,
    Instrument,
    MarketData,
    PriceAdjustment,
    RunConfig,
    china_rules,
)

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "prediction_research" / "datasets" / "market_full"
OUT_DIR = ROOT / "prediction_research" / "runs"

# 标的池：symbol -> (交易所 profile, 涨跌停幅度)
POOL = {
    "510050": ("sse_equity_etf", 0.10),          # 上证50ETF 华夏
    "588000": ("sse_equity_etf", 0.20),          # 科创50ETF 华夏（20% 涨跌停）
    "515220": ("sse_equity_etf", 0.10),          # 煤炭ETF 国泰
    "518800": ("sse_gold_etf", 0.10),            # 黄金ETF 国泰（T+0）
    "513050": ("sse_cross_border_t0_etf", 0.10), # 中概互联网ETF 易方达（跨境 T+0）
    "159611": ("szse_equity_etf", 0.10),         # 电力ETF
    "159748": ("szse_equity_etf", 0.20),         # 创业板ETF（创业板 20% 涨跌停）
    "512170": ("szse_equity_etf", 0.10),         # 医疗ETF
    "589720": ("szse_equity_etf", 0.20),         # 创新药ETF（双创，20% 涨跌停）
}

START = "2022-09-01"
END = "2026-10-12"  # doribt china presets 硬上限
MOMENTUM_BARS = 20   # 动量回看
TOP_K = 2            # 持仓只数
REBALANCE_EVERY = 20 # 调仓间隔（交易日）
INITIAL_CASH = 1_000_000
RF_ANNUAL = 0.02     # 无风险利率（年化 2%，用于夏普）


def load_pool() -> tuple[list[dict], list[str], list, list]:
    """读 cache CSV → DoriBT records + 交易日历 + 拆分复权事件。

    新浪 cache 为未复权价；份额拆分/大额分红会造成价格断层（如 515220 2024-04-12
    约 1:2 拆分）。检测规则：当日收盘/前收盘 < 0.80 且 开盘/前收盘 < 0.90
    （开盘即大幅缺口，区别于跌停——跌停不会低开 10%）。
    晚上市标的（如 589720）在上市前补 unlisted bar（空价格零成交）以满足面板完整性。
    """
    records: list[dict] = []
    actions: list[CorporateAction] = []
    adjustments: list[PriceAdjustment] = []
    raw: dict[str, pd.DataFrame] = {}
    for sym, (_, limit) in POOL.items():
        f = CACHE_DIR / f"cache_{sym}.csv"
        if not f.exists():
            print(f"[warn] {sym} cache 缺失，跳过")
            continue
        df = pd.read_csv(f, parse_dates=["date"])
        df = df[(df["date"] >= START) & (df["date"] <= END)].sort_values("date").reset_index(drop=True)
        if len(df) < MOMENTUM_BARS + 5:
            print(f"[warn] {sym} 有效行数不足（{len(df)}），跳过")
            continue
        raw[sym] = df
    if not raw:
        raise SystemExit("池内无可用标的")
    calendar = sorted({d.strftime("%Y-%m-%d") for df in raw.values() for d in df["date"]})
    for sym, (_, limit) in POOL.items():
        if sym not in raw:
            continue
        df = raw[sym]
        present = set(df["date"].dt.strftime("%Y-%m-%d"))
        prev_close = df["close"].shift(1)  # 涨跌停价以前收盘为基准
        for day_str in calendar:
            if day_str not in present:
                # 未上市或数据缺失：unlisted bar（空价格、零成交、无涨跌停）
                records.append(
                    {
                        "session": day_str,
                        "symbol": sym,
                        "status": "unlisted",
                        "open": None,
                        "high": None,
                        "low": None,
                        "close": None,
                        "volume": 0,
                        "upper_limit": None,
                        "lower_limit": None,
                    }
                )
                continue
            row = df[df["date"] == day_str].iloc[0]
            c = round(float(row["close"]), 3)
            pc = prev_close[df.index[df["date"] == day_str][0]]
            if pd.notna(pc):
                pc = float(pc)
                # 拆分检测：开盘缺口 + 收盘仍深跌 ⇒ 除权事件，构造复权因子
                split_factor = None
                if float(row["open"]) / pc < 0.90 and c / pc < 0.80:
                    split_factor = round(float(row["open"]) / pc, 6)
                    ex_dt = row["date"]
                    ex_date = ex_dt.strftime("%Y-%m-%d")
                    # CorporateAction 校验要求 announced <= record_date < ex_date，取合理近似
                    rec_date = (ex_dt - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                    ann_date = (ex_dt - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
                    aid = f"{sym}_split_{ex_date}"
                    actions.append(
                        CorporateAction(
                            action_id=aid,
                            symbol=sym,
                            kind="distribution",
                            announced=ann_date,
                            record_date=rec_date,
                            ex_date=ex_date,
                            # 份额拆分 ≈ 送股：1 份变 (1+bonus) 份，价格 ÷ (1+bonus)
                            # 引擎账户层不自动派发（仅作复权触发壳），此处保证元数据自洽
                            bonus_per_share=round(1.0 / split_factor - 1.0, 4),
                            share_listing_date=ex_date,
                            share_credit_date=ex_date,
                            source=f"新浪 cache 拆分检测（前收 {pc} → 开 {row['open']}，factor={split_factor}）",
                        )
                    )
                    adjustments.append(
                        PriceAdjustment(action_id=aid, factor=split_factor, known_on=ex_date, source="新浪 cache 拆分检测")
                    )
                    print(f"[split] {sym} {ex_date}: factor={split_factor}（前收 {pc} → 开盘 {row['open']}）")
                # 涨跌停基准 = 复权到当日口径的前收（拆分日须同步缩放，否则 limit 远高于/低于当日 OHLC）
                limit_base = pc * split_factor if split_factor else pc
                upper = round(limit_base * (1 + limit), 3)
                lower = round(limit_base * (1 - limit), 3)
            else:  # 首日无前收盘：不给 limit（bars 校验允许显式缺失）
                upper = lower = None
            records.append(
                {
                    "session": day_str,
                    "symbol": sym,
                    "status": "trading",
                    "open": round(float(row["open"]), 3),
                    "high": round(float(row["high"]), 3),
                    "low": round(float(row["low"]), 3),
                    "close": c,
                    "volume": int(float(row["volume"])),
                    # 涨跌停价须对齐 ETF tick=0.001（4 位小数会触发 off tick 校验）
                    "upper_limit": upper,
                    "lower_limit": lower,
                }
            )
    return records, calendar, actions, adjustments


def build_data(records: list[dict], calendar: list[str], actions: list, adjustments: list) -> MarketData:
    profiles = {sym: prof for sym, (prof, _) in POOL.items() if any(r["symbol"] == sym for r in records)}
    return MarketData.from_records(
        records,
        calendar=calendar,
        instruments=[Instrument(symbol=s, kind="etf") for s in profiles],
        rules=china_rules(profiles, start=calendar[0], end=calendar[-1]),
        source="新浪 cache CSV（prediction_research/datasets/market_full），原始收盘 + 拆分复权",
        actions=actions,
        adjustments=adjustments,
    )


def make_strategy():
    state = {"last_rebalance": -1}

    def momentum_rotation(ctx: Context) -> None:
        i = ctx.bar_index
        if i < MOMENTUM_BARS or (i - state["last_rebalance"]) < REBALANCE_EVERY:
            return
        scores = {}
        for sym in POOL:
            # asof：使用「截至当前决策日已知」的复权价，防拆分前视
            hist = ctx.history(sym, bars=MOMENTUM_BARS + 1, adjustment="asof")
            if hist is None or len(hist) < MOMENTUM_BARS + 1:
                continue
            if not all(map(math.isfinite, hist)):  # 含未上市空价 bar，跳过
                continue
            if hist[0] <= 0:
                continue
            scores[sym] = hist[-1] / hist[0] - 1.0
        if not scores:
            return
        top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:TOP_K]
        weights = {s: round(1.0 / len(top), 4) for s, _ in top}
        ctx.target_weights(weights)
        state["last_rebalance"] = i
        state["last_pick"] = top

    return momentum_rotation


def make_benchmark(bench_sym: str):
    def buy_and_hold(ctx: Context) -> None:
        if ctx.bar_index == 0:
            ctx.target_weights({bench_sym: 0.95})

    return buy_and_hold


def metrics(equity: list[float]) -> dict:
    """年化 / 夏普 / 最大回撤（252 交易日年化，rf=2%）。"""
    n = len(equity)
    total = equity[-1] / equity[0] - 1.0
    years = n / 252.0
    ann = (equity[-1] / equity[0]) ** (1.0 / years) - 1.0 if years > 0 else float("nan")
    rets = [equity[i] / equity[i - 1] - 1.0 for i in range(1, n)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    std = math.sqrt(var)
    sharpe = ((mean * 252) - RF_ANNUAL) / (std * math.sqrt(252)) if std > 0 else float("nan")
    peak, mdd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1.0)
    return {
        "trading_days": n,
        "years": round(years, 2),
        "total_return": round(total, 4),
        "annualized": round(ann, 4),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(mdd, 4),
    }


def main() -> None:
    records, calendar, actions, adjustments = load_pool()
    print(f"records={len(records)} calendar={len(calendar)} 天（{calendar[0]} ~ {calendar[-1]}）")
    print(f"复权事件 {len(actions)} 起")
    data = build_data(records, calendar, actions, adjustments)

    costs = Costs(commission="0.0003", minimum_commission="5.00")
    config = RunConfig(initial_cash=INITIAL_CASH, costs=costs, backend="python")

    print("\n=== 策略：20 日动量轮动 top2 ===")
    res = Backtest(data, config=config).run(make_strategy())
    m = metrics(list(res.equity))
    print(json.dumps(m, ensure_ascii=False, indent=2))
    print("doribt total_return:", res.total_return, "| max_drawdown:", res.max_drawdown)
    print("fills:", len(res.fills))

    print("\n=== 基准：510050 买入持有 ===")
    res_b = Backtest(data, config=config).run(make_benchmark("510050"))
    m_b = metrics(list(res_b.equity))
    print(json.dumps(m_b, ensure_ascii=False, indent=2))

    # 调仓轨迹（从 fills 反推换仓）
    trades = [
        {"session": f.session if hasattr(f, "session") else str(getattr(f, "date", "")), "symbol": getattr(f, "symbol", ""), "side": getattr(f, "side", "")}
        for f in res.fills
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "run_date": "2026-10-23",
        "engine": "doribt 0.6.2",
        "period": {"start": calendar[0], "end": calendar[-1]},
        "pool": list(POOL.keys()),
        "strategy": {"type": "momentum_rotation", "momentum_bars": MOMENTUM_BARS, "top_k": TOP_K, "rebalance_every": REBALANCE_EVERY},
        "costs": {"commission": "0.0003", "minimum_commission": "5.00", "note": "ETF 无印花税"},
        "price_basis": "cache CSV 原始收盘，未复权",
        "strategy_metrics": m,
        "benchmark_510050_metrics": m_b,
        "strategy_fills": len(res.fills),
        "equity_tail": [round(x, 2) for x in list(res.equity)[-5:]],
    }
    out_path = OUT_DIR / "doribt_momentum_20d_backtest_20261023.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存: {out_path}")


if __name__ == "__main__":
    main()
