"""TradingAgents - 预测 3032.HK 明日走势 (截至 2026-07-20 最新数据)"""
import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = os.getenv("TRADINGAGENTS_LLM_PROVIDER", "deepseek")
config["deep_think_llm"] = os.getenv("TRADINGAGENTS_DEEP_THINK_LLM", "deepseek-v4-pro")
config["quick_think_llm"] = os.getenv("TRADINGAGENTS_QUICK_THINK_LLM", "deepseek-v4-flash")
config["output_language"] = "Chinese"
config["max_debate_rounds"] = 1
config["max_risk_discuss_rounds"] = 1

ticker = "3032.HK"
date = "2026-07-21"  # 分析明日走势

print(f"\n{'='*60}")
print(f"  明日预测: {ticker} | 分析日期: {date}")
print(f"  使用截至今天的所有数据，预测明日方向")
print(f"{'='*60}\n")

ta = TradingAgentsGraph(debug=True, config=config)
result, decision = ta.propagate(ticker, date)

print(f"\n{'='*60}")
print(f"  明日方向预测: {decision}")
print(f"{'='*60}")
