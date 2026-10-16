"""
新闻流统一入口 (Unified Runner)
===============================
一键运行三条流（宏观 / ETF成分股 / 机构13F），生成 HTML + JSON 报告。

运行：
    python news_aggregator/run_flow.py            # 跑全部
    python news_aggregator/run_flow.py --macro    # 只跑宏观
    python news_aggregator/run_flow.py --etf      # 只跑 ETF 成分股
    python news_aggregator/run_flow.py --inst     # 只跑机构 13F

输出：
    reports/news_flow_<date>.html
    reports/news_flow_<date>.json     （供 daily_predict 注入）
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
from report_flow import build_report                               # noqa: E402


def run_all(macro=True, etf=True, inst=True) -> tuple[str, str]:
    macro_data = fetch_macro_flow() if macro else {"items": [], "fresh_items": [], "fresh_count": 0}
    etf_data = fetch_etf_constituent_flow() if etf else {"etfs": []}
    inst_data = fetch_institutional_flow() if inst else {"institutions": []}
    html, js = build_report(macro_data, etf_data, inst_data)
    return html, js


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--macro", action="store_true", help="只跑宏观流")
    ap.add_argument("--etf", action="store_true", help="只跑 ETF 成分股流")
    ap.add_argument("--inst", action="store_true", help="只跑机构 13F 流")
    args = ap.parse_args(argv)

    # 没有任何 --xxx 则全跑
    any_flag = args.macro or args.etf or args.inst
    macro = etf = inst = (not any_flag)
    if any_flag:
        macro, etf, inst = args.macro, args.etf, args.inst

    print("=" * 60)
    print("   📡 独立新闻流聚合器")
    print(f"   时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    html, js = run_all(macro, etf, inst)
    print(f"\n✅ HTML: {html}")
    print(f"✅ JSON: {js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
