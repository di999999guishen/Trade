"""
新闻流统一入口 (Unified Runner)
===============================
一键运行四条流（财联社快讯 / 宏观 / ETF成分股 / 机构13F），生成 HTML + JSON 报告。

运行：
    python news_aggregator/run_flow.py            # 跑全部
    python news_aggregator/run_flow.py --cls      # 只跑财联社快讯
    python news_aggregator/run_flow.py --macro    # 只跑宏观
    python news_aggregator/run_flow.py --etf      # 只跑 ETF 成分股
    python news_aggregator/run_flow.py --inst     # 只跑机构 13F

输出：
    reports/news_flow_<date>.html
    reports/news_flow_<date>.json     （供 daily_predict 注入）

关于财联社流（2026-10-20 新增）：
    东财 stock_news_em 是**关键词历史检索**，不是实时流 —— 实测 10-20 当日
    煤炭 ETF 只有 5 条、最旧一条在 11 天前。财联社电报是 7×24 分钟级快讯，
    8 小时可抓 350 条，是当前唯一能提供「当天盘中/盘后信号」的源。
    它排在第 1 位跑，因为产出直接进 daily_predict 的上下文。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

_BASE = str(Path(__file__).resolve().parent)
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from flows.macro_flow import fetch_macro_flow               # noqa: E402
from flows.etf_constituents_flow import fetch_etf_constituent_flow  # noqa: E402
from flows.institutional_flow import fetch_institutional_flow      # noqa: E402
from flows.cls_telegraph_flow import fetch_cls_flow, load_holdings_context  # noqa: E402
from report_flow import build_report                               # noqa: E402

EMPTY_CLS = {"etfs": [], "global_news": [], "raw_count": 0, "matched": 0}


def run_all(macro=True, etf=True, inst=True, cls=True) -> tuple[str, str]:
    # 财联社先行：它是唯一提供「当天」信号的流，后面的流失败也不影响它落盘
    cls_data = EMPTY_CLS
    if cls:
        try:
            cons, names = load_holdings_context()
            cls_data = fetch_cls_flow(hours_back=8.0, constituents=cons,
                                      etf_names=names, verbose=True)
        except Exception as exc:  # 单条流失败不阻断整份报告
            print(f"[WARN] 财联社流失败，跳过: {type(exc).__name__}: {str(exc)[:120]}")
    macro_data = fetch_macro_flow() if macro else {"items": [], "fresh_items": [], "fresh_count": 0}
    etf_data = fetch_etf_constituent_flow() if etf else {"etfs": []}
    inst_data = fetch_institutional_flow() if inst else {"institutions": []}
    html, js = build_report(macro_data, etf_data, inst_data, cls_data)
    return html, js


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cls", action="store_true", help="只跑财联社快讯流")
    ap.add_argument("--macro", action="store_true", help="只跑宏观流")
    ap.add_argument("--etf", action="store_true", help="只跑 ETF 成分股流")
    ap.add_argument("--inst", action="store_true", help="只跑机构 13F 流")
    args = ap.parse_args(argv)

    # 没有任何 --xxx 则全跑
    any_flag = args.cls or args.macro or args.etf or args.inst
    cls = macro = etf = inst = (not any_flag)
    if any_flag:
        cls, macro, etf, inst = args.cls, args.macro, args.etf, args.inst

    print("=" * 60)
    print("   📡 独立新闻流聚合器")
    print(f"   时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    html, js = run_all(macro, etf, inst, cls)
    print(f"\n✅ HTML: {html}")
    print(f"✅ JSON: {js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
