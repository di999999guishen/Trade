# -*- coding: utf-8 -*-
"""159208（航空航天ETF万家）月口径（20 交易日）单独分析。

与 main.py 的差异（仅此两处，均不改上游源码）：
1. config["holding_period_days"] = 20 —— 结算/反思/回测口径对齐月度；
2. config["screen_evidence_context"] 注入月度 horizon 指令 —— graph 的
   _instrument_step 会把它拼进 instrument_context，每个分析师都能看到
   （trading_graph.py:300-307 注释："appended ... so every analyst sees it"）。
其余（国内源链、wmi_guard、代理旁路、.env LLM 档）与 main.py 完全一致。
"""
import json
import os
import sys
from datetime import date
from pathlib import Path

# 🔧 WMI 守卫（必须在 import pandas 之前）
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import wmi_guard  # noqa: F401
except Exception:
    pass

try:
    from retry_utils import setup_proxy_bypass

    setup_proxy_bypass()
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

TICKER = "159208.SZ"  # 深交所后缀必带：裸 6 位代码会被路由到 Sina US 通道（实测失败）
TRADE_DATE = "2026-10-13"
HORIZON_DAYS = 20  # ≈ 一个自然月

MONTHLY_HORIZON_NOTE = (
    "\nMonthly horizon directive (this run's governing timeframe, overrides any default "
    "intraday or single-week framing):\n"
    f"- Decision horizon: {HORIZON_DAYS} trading days (approximately one calendar month). "
    "Every rating (Buy / Overweight / Hold / Underweight / Sell) must be justified "
    "explicitly against the outlook over the next 20 trading days -- not a single "
    "session, not a multi-quarter view.\n"
    "- Evidence weighting: the most recent 20 trading days of price action and fund "
    "flows carry the most weight; older data is background only.\n"
    "- Fundamentals: reference the most recent reporting period (monthly-to-quarterly "
    "cadence) rather than 'the past week'; for an ETF, analyze the underlying "
    "holdings' exposure, not corporate financials of the fund itself.\n"
    "- Price structure on a one-month horizon: support_levels / resistance_levels / "
    "invalidation_price / stop-loss must be absolute price levels relevant over the "
    "next 20 trading days, and scale_out_ladder rungs should span the month "
    "(2-4 rungs, each with an absolute trigger_price and close_pct, summing to <=100). "
    "Leave fields null rather than inventing numbers when the evidence is missing.\n"
)


def main() -> int:
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
    # 月度口径（本脚本唯一的结构性差异）
    config["holding_period_days"] = HORIZON_DAYS
    config["screen_evidence_context"] = MONTHLY_HORIZON_NOTE

    # 🔴 国内源补丁（同 main.py:90-101）：get_insider_transactions 注册东财实现
    from tradingagents.dataflows import router as _dataflow_router
    from tradingagents.dataflows.eastmoney_data import get_insider_transactions_eastmoney

    _dataflow_router.VENDOR_METHODS.setdefault("get_insider_transactions", {})[
        "eastmoney"
    ] = get_insider_transactions_eastmoney
    config["tool_vendors"] = {"get_insider_transactions": "eastmoney"}

    print(f"[run] ticker={TICKER} date={TRADE_DATE} horizon={HORIZON_DAYS}d "
          f"llm={config['deep_think_llm']}", flush=True)
    graph = TradingAgentsGraph(debug=False, config=config)
    final_state, decision = graph.propagate(TICKER, TRADE_DATE)

    report_root = Path(__file__).resolve().parent / "prediction_research" / "runs" / "tradingagents"
    save_path = report_root / f"{TICKER.split('.')[0]}_{TRADE_DATE}"
    save_path.mkdir(parents=True, exist_ok=True)
    saved = graph.save_reports(final_state, TICKER, save_path=save_path)
    print(f"\nFinal decision: {decision}")
    print(f"Reports saved to: {Path(saved).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
