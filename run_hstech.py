"""TradingAgents - 恒生科技 ETF (3032.HK) 分析脚本"""
import os, sys
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
date = "2026-07-18"

print(f"\n{'='*60}")
print(f"  TradingAgents 分析: {ticker} (恒生科技 ETF)")
print(f"  日期: {date}  |  LLM: {config['llm_provider']}")
print(f"  深度思考: {config['deep_think_llm']}  |  快速思考: {config['quick_think_llm']}")
print(f"{'='*60}\n")

ta = TradingAgentsGraph(debug=True, config=config)
result, decision = ta.propagate(ticker, date)

print(f"\n{'='*60}")
print(f"  最终决策: {decision}")
print(f"{'='*60}\n")
print(f"完整报告:\n{result}")
