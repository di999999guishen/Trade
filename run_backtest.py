"""TradingAgents 回测: 如果在 7月17日 分析 3032.HK 会给出什么信号"""
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
date = "2026-07-17"  # 用前一天数据，模拟盘前决策

print(f"\n{'='*60}")
print(f"  [回测] 如果在 {date} 盘前用 TradingAgents 分析 {ticker}")
print(f"  (使用 {date} 之前的所有数据，不含当日行情)")
print(f"{'='*60}\n")

ta = TradingAgentsGraph(debug=True, config=config)
result, decision = ta.propagate(ticker, date)

print(f"\n{'='*60}")
print(f"  [{date} 盘前] 最终决策: {decision}")
print(f"{'='*60}")
