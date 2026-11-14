"""A 回补脚本：把被历史门限（RC1）剔除的 ETF 的日线 K 线补回来（"回补后再评"）。

根因：粗筛从全市场 ETF 板选股，下游 LLM 只读 datasets/market_full 里回补过 K 线的标的。
新纳入筛选池的标的缓存行数只有 2~26 行 → 注入历史为空 → 兜底成机械 Hold（约占 top10 的 40%）。

本脚本用 etf_data_source.refresh_pool（多源降级：eastmoney/sina/tencent，零额外算力）把缺失
标的的 K 线写回缓存目录。回补后重跑 run-agents 即可让这些标的重新进入增持评估。

用法：
  python backfill_klines.py                  # 自动取最近粗筛 deferred_for_backfill 的标的
  python backfill_klines.py --top-n 10       # 对最近粗筛 top10 强制回补（含已满足门限的）
  python backfill_klines.py --symbols 158037 158023 158043
需要网络（行情源）。失败会逐只列出原因。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from prediction_research.config import load_config, market_data_dir
from prediction_research.screening import latest_screen
from prediction_research.tradingagents_adapter import build_plan
from prediction_research import etf_data_source


def main() -> None:
    ap = argparse.ArgumentParser(description="回补被历史门限剔除的 ETF 日线 K 线")
    ap.add_argument("--config", default=None, help="配置文件路径（默认 config/research.json）")
    ap.add_argument("--symbols", nargs="*", default=None, help="显式指定代码列表")
    ap.add_argument("--top-n", type=int, default=None, help="对最近粗筛 top_n 强制回补")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cache_dir = market_data_dir(cfg)

    codes = args.symbols
    if not codes:
        plan = build_plan(cfg, args.top_n or int(cfg["workflow"]["agent_top_n"]))
        deferred = plan.get("deferred_for_backfill", [])
        codes = [d["symbol"] for d in deferred]
        if not codes and args.top_n is None:
            print("没有需要回补的标的（最近批次所有候选都已满足历史门限）。")
            return

    print(f"回补 {len(codes)} 只标的的日线 -> {cache_dir}")
    report = etf_data_source.refresh_pool(codes, cache_dir=str(cache_dir), min_rows=20)
    ok = [c for c, r in report.items() if r.get("ok")]
    fail = [c for c, r in report.items() if not r.get("ok")]
    print(f"成功 {len(ok)} 只：{ok}")
    if fail:
        print(f"失败 {len(fail)} 只：")
        for c in fail:
            print("   ", c, report[c].get("error"))
    print("\n回补完成。下一步：重新运行 run-agents（会再次 build_plan，此时这些标的已满足历史门限，将进入增持评估）。")


if __name__ == "__main__":
    main()
