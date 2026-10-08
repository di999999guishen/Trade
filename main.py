"""Command-line entry point for a complete TradingAgents prediction run.

Usage:
    python main.py 510050.SS --date 2026-09-04 --reports-dir results --quiet

Ticker is resolved against the domestic (akshare/eastmoney) data sources first,
so A股/港股/ETF 标的不会撞 Yahoo/Reddit/StockTwits/FRED 限流。
"""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

# 🔧 代理绕过（必须在所有网络 import 之前）
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from retry_utils import setup_proxy_bypass

    setup_proxy_bypass()
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a trading prediction.")
    parser.add_argument("ticker", nargs="?", default="510050.SS")
    parser.add_argument(
        "--date",
        dest="trade_date",
        default=date.today().isoformat(),
        help="Analysis date in YYYY-MM-DD format (default: today).",
    )
    parser.add_argument(
        "--reports-dir",
        default="results",
        help="Directory where the complete Markdown report tree is saved.",
    )
    parser.add_argument("--quiet", action="store_true", help="Disable agent trace output.")
    parser.add_argument("--screen-evidence", type=Path, help="Frozen ETF evidence JSON from the research screen.")
    return parser.parse_args()


def main() -> int:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    try:
        date.fromisoformat(args.trade_date)
    except ValueError:
        print(f"Invalid --date value: {args.trade_date!r}; expected YYYY-MM-DD.", file=sys.stderr)
        return 2

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = os.getenv("TRADINGAGENTS_LLM_PROVIDER", "deepseek")
    config["deep_think_llm"] = os.getenv("TRADINGAGENTS_DEEP_THINK_LLM", "deepseek-v4-flash")
    config["quick_think_llm"] = os.getenv("TRADINGAGENTS_QUICK_THINK_LLM", "deepseek-v4-flash")
    config["output_language"] = "Chinese"
    config["max_debate_rounds"] = 1
    config["max_risk_discuss_rounds"] = 1
    # 🔒 全程国内/不限流数据源（禁用 yfinance/Reddit/StockTwits/FRED）
    config["data_vendors"] = {
        "core_stock_apis": "akshare",
        "technical_indicators": "akshare",
        "fundamental_data": "eastmoney",
        "news_data": "eastmoney",
        "macro_data": "akshare_macro",
        "prediction_markets": "polymarket",
    }

    if args.screen_evidence:
        from tradingagents.evidence_context import render_screen_evidence
        payload = json.loads(args.screen_evidence.read_text(encoding="utf-8"))
        config["screen_evidence_context"] = render_screen_evidence(payload, args.ticker, args.trade_date)

    graph = TradingAgentsGraph(debug=not args.quiet, config=config)
    final_state, decision = graph.propagate(args.ticker, args.trade_date)

    report_root = Path(args.reports_dir) / f"{args.ticker}_{args.trade_date}"
    saved_path = graph.save_reports(final_state, args.ticker, save_path=report_root)
    print(f"\nFinal decision: {decision}")
    print(f"Reports saved to: {saved_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
