"""D：缠论-lite 买点 vs LLM 增持 vs 规则闸门 —— top10 历史前向胜率对照。

回答用户的核心问题：「市面上有没有零算力、面向国内 ETF、且比你现在更准的开源方案？」
本脚本用你自己的 ETF 缓存数据做实证，而不是信第三方 headline 准确率。

三方信号对照口径：
  1. 缠论买点（chanlun_buy）   —— 对每只 top10 ETF 做**全历史滚动回测**：遍历其日线，
        凡缠论给出买点的 K 线，记录持有 horizon（5/20 交易日）后的前向收益与上涨概率。
        这是与数据新鲜度无关的、可立即量化的真实胜率。
  2. LLM 增持（llm_overweight）—— 最近一次多智体决策日(trade_date)的 Overweight/Buy 评级，
        前向收益需 K 线覆盖决策日后 N 日；数据滞后时显示「无前向数据」。
  3. 规则闸门增持（rule_gate_overweight）—— C 的 rule_gate_rating 同上单点评估。

输出 runs/chanlun_compare_<ts>.json 并打印对照表。

注意：chanlun_lite 现已内置**高保真后端 czsc**（纯 Python 缠论引擎，完整实现笔/线段/中枢/背驰/买卖点）。
czsc 不可用时自动回退到内置 lite 近似（分型-笔-反转）。`signal()` 接口不变，本脚本无需改动。
PyPI 同名 `chanlun` 包已改为 Rust 核心、仅提供 cp312/cp39 预编译 wheel，在 Python 3.13 上无法安装，
故以 czsc 作为等效高保真后端（详见报告「高保真引擎切换」一节）。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from prediction_research.config import load_config, market_data_dir
from prediction_research.screening import latest_screen
from prediction_research.data import load_cache
from prediction_research.features import build_samples
from prediction_research.chanlun_lite import signal as chanlun_signal
from prediction_research.tradingagents_adapter import rule_gate_rating, decision_semantics
from prediction_research.store import connect


def _read_raw_bars(symbol: str, cfg: dict) -> list[dict]:
    """直接读 cache CSV 全部行（不强制 >=61），用于缠论信号；找不到返回 []。"""
    dirs = [market_data_dir(cfg)]
    legacy = cfg.get("legacy_cache_dir")
    if legacy:
        dirs.append(Path(cfg["_project_dir"]) / legacy)
    bars: list[dict] = []
    for d in dirs:
        f = d / f"cache_{symbol}.csv"
        if not f.exists():
            continue
        with f.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    bars.append({"date": row["date"], "open": float(row["open"]),
                                 "high": float(row["high"]), "low": float(row["low"]),
                                 "close": float(row["close"]), "volume": float(row.get("volume", 0) or 0)})
                except (ValueError, KeyError):
                    continue
        if bars:
            break
    return bars


def _llm_rating_from_db(db_path: Path, ticker: str, trade_date: str):
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT decision_text FROM agent_analyses WHERE ticker=? AND analysis_date=? "
            "AND status='ok' AND decision_text IS NOT NULL ORDER BY created_at_utc DESC LIMIT 1",
            (ticker, trade_date),
        ).fetchone()
    if not row:
        return None
    return decision_semantics(row[0])["rating"]


def _forward_at_trade(symbol: str, cfg: dict, trade_date: str) -> dict:
    """单点前向收益（用于 LLM/规则闸门在 trade_date 的评估）。"""
    try:
        snapshot = load_cache(symbol, market_data_dir(cfg))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    out: dict = {}
    for h in cfg["horizons"]:
        sample = next((s for s in build_samples(snapshot, int(h)) if s.feature_date.isoformat() == trade_date), None)
        if sample and sample.target_return is not None:
            out[str(h)] = {"ret": round(sample.target_return, 4), "up": int(sample.target_up)}
    return out


def _chanlun_backtest(bars: list[dict], horizons: list[int]) -> dict:
    """缠论买点全历史滚动回测：遍历日线，买点处记录 horizon 日后前向收益。"""
    n = len(bars)
    if n < 10:
        return {}
    result: dict[str, dict] = {str(h): {"samples": 0, "wins": 0, "sret": 0.0} for h in horizons}
    for i in range(5, n):
        window = bars[: i + 1]
        sig = chanlun_signal(window)["signal"]
        if sig != "buy":
            continue
        entry = bars[i + 1]["open"] if i + 1 < n else None
        if entry in (None, 0):
            continue
        for h in horizons:
            j = i + 1 + h
            if j >= n:
                continue
            ret = bars[j]["close"] / entry - 1.0
            bucket = result[str(h)]
            bucket["samples"] += 1
            bucket["wins"] += 1 if ret > 0 else 0
            bucket["sret"] += ret
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="缠论-lite vs LLM vs 规则闸门 前向胜率对照")
    ap.add_argument("--config", default=None)
    ap.add_argument("--top-n", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    top_n = args.top_n or int(cfg["workflow"]["agent_top_n"])
    horizons = [int(h) for h in cfg["horizons"]]
    overheat = float(cfg.get("workflow", {}).get("overheat_threshold_pct",
                      cfg.get("etf_market", {}).get("screen", {}).get("overheat_threshold_pct", 7.0)))
    allow_divergence = bool(cfg.get("workflow", {}).get("rule_gate_allow_divergence", False))
    gate_min_amount = float(cfg.get("workflow", {}).get("rule_gate_min_amount", 50_000_000.0))
    gate_chanlun_weight = float(cfg.get("workflow", {}).get("rule_gate_chanlun_weight", 0.0))
    gate_min_confirm = float(cfg.get("workflow", {}).get("rule_gate_min_confirm", 1.0))
    screen_path, screen = latest_screen(cfg)
    selected = screen["selected"][:top_n]
    epochs = [int(r["quote_epoch"]) for r in selected if r.get("quote_epoch")]
    trade_date = datetime.fromtimestamp(max(epochs), timezone(timedelta(hours=8))).date().isoformat()
    db_path = (Path(cfg["_project_dir"]) / cfg["state_db"]).resolve()

    rows: list[dict] = []
    cl_backtest_acc: dict[str, dict] = {str(h): {"samples": 0, "wins": 0, "sret": 0.0} for h in horizons}
    for r in selected:
        sym = r["symbol"]
        ticker = sym + (".SS" if r["exchange"] == "SSE" else ".SZ")
        raw = _read_raw_bars(sym, cfg)
        cl_signal_trade = chanlun_signal(raw).get("signal", "neutral") if raw else "neutral"
        llm = _llm_rating_from_db(db_path, ticker, trade_date)
        candidate = {
            "screen_evidence": {"change_pct": r.get("change_pct"), "main_net_inflow_pct": r.get("main_net_inflow_pct"),
                                "amount": r.get("amount")},
            "order_divergence": r.get("order_divergence", {"status": "missing_data", "signal": "no_data"}),
            "chanlun_signal": {"signal": cl_signal_trade},
        }
        gate = rule_gate_rating(candidate, llm, overheat, allow_divergence,
                                gate_min_amount, gate_chanlun_weight, gate_min_confirm)
        fwd = _forward_at_trade(sym, cfg, trade_date)
        cl_bt = _chanlun_backtest(raw, horizons)
        for h in horizons:
            b = cl_bt.get(str(h))
            if b:
                acc = cl_backtest_acc[str(h)]
                acc["samples"] += b["samples"]
                acc["wins"] += b["wins"]
                acc["sret"] += b["sret"]
        rows.append({"symbol": sym, "name": r.get("name"), "ticker": ticker,
                     "chanlun_signal_at_trade": cl_signal_trade, "llm_rating": llm,
                     "rule_gated_rating": gate["rule_gated_rating"], "rule_override": gate["rule_override"],
                     "rule_reason": gate["reason"], "forward_at_trade": fwd,
                     "chanlun_backtest": {str(h): {"samples": cl_bt.get(str(h), {}).get("samples", 0)} for h in horizons}})

    def _summarize(acc):
        s = acc["samples"]
        return {"triggered_samples": s, "win_rate": (round(acc["wins"] / s, 4) if s else None),
                "mean_return": (round(acc["sret"] / s, 4) if s else None)}

    # 单点 LLM / 规则闸门聚合
    def _point_agg(trigger, label):
        wins = tot = 0
        sret = 0.0
        for row in rows:
            if not trigger(row):
                continue
            for fv in row["forward_at_trade"].values():
                tot += 1
                wins += fv["up"]
                sret += fv["ret"]
        return {"label": label, "triggered_symbols": sum(1 for row in rows if trigger(row)),
                "horizon_samples": tot, "win_rate": (round(wins / tot, 4) if tot else None),
                "mean_return": (round(sret / tot, 4) if tot else None)}

    comparison = {
        "chanlun_buy_backtest": {str(h): {"label": f"缠论买点·回测 H{h}", **_summarize(cl_backtest_acc[str(h)])} for h in horizons},
        "llm_overweight": _point_agg(lambda r: r["llm_rating"] in ("Overweight", "Buy"), "LLM增持·单点"),
        "rule_gate_overweight": _point_agg(lambda r: r["rule_gated_rating"] == "Overweight", "规则闸门增持·单点"),
    }

    out = {"trade_date": trade_date, "top_n": top_n, "horizons": horizons, "overheat_threshold": overheat,
           "rows": rows, "comparison": comparison,
           "generated_at_utc": datetime.now(timezone.utc).isoformat(),
           "note": "chanlun_buy 为全历史滚动回测胜率；llm/rule_gate 为最近决策日单点，需 K 线覆盖决策日后 N 日才有前向数据。"}
    runs = (Path(cfg["_project_dir"]) / cfg["runs_dir"]).resolve()
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"chanlun_compare_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"== 缠论-lite 对照（trade_date={trade_date}, top_n={top_n}）==")
    print("-- 缠论买点：全历史滚动回测 --")
    for h in horizons:
        v = comparison["chanlun_buy_backtest"][str(h)]
        wr = f"{round(v['win_rate']*100,1)}%" if v["win_rate"] is not None else "-"
        mr = f"{round(v['mean_return']*100,2)}%" if v["mean_return"] is not None else "-"
        print(f"  H{h:<3} 样本={v['triggered_samples']:>5}  胜率={wr:>8}  均值收益={mr}")
    print("-- LLM增持 / 规则闸门：最近决策日单点（前向数据滞后则显示 -）--")
    for key in ("llm_overweight", "rule_gate_overweight"):
        v = comparison[key]
        wr = f"{round(v['win_rate']*100,1)}%" if v["win_rate"] is not None else "-"
        mr = f"{round(v['mean_return']*100,2)}%" if v["mean_return"] is not None else "-"
        print(f"  {v['label']:<16} 触发标的={v['triggered_symbols']:>3}  前向样本={v['horizon_samples']:>4}  胜率={wr:>8}  均值收益={mr}")
    print(f"\n明细写入：{path}")
    print("\n逐标的（决策日信号）：")
    for row in rows:
        llm_s = str(row["llm_rating"]) if row["llm_rating"] else "None"
        fwd_s = " / ".join(f"H{h}:{'+' if fv['up'] else '-'}{round(fv['ret']*100,1)}%" for h, fv in row["forward_at_trade"].items()) or "无前向数据"
        bt = " / ".join(f"买点样本={row['chanlun_backtest'][str(h)]['samples']}" for h in horizons)
        print(f"  {row['symbol']} {str(row['name'])[:10]:<10} 缠论={row['chanlun_signal_at_trade']:<7} "
              f"LLM={llm_s:<10} 闸门={row['rule_gated_rating']:<10} | 缠论{bt} | 前向={fwd_s}")


if __name__ == "__main__":
    main()
