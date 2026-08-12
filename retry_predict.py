"""
TradingAgents 预测重试 — 针对 morning_briefing 中失败的个股
"""
import os, sys, json
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

# 重试失败的 5 只股票
STOCKS = [
    {"ticker": "AAPL",          "name": "苹果",   "market": "美股"},
    {"ticker": "000858.SZ",     "name": "五粮液",  "market": "A股"},
    {"ticker": "600519.SS",     "name": "贵州茅台", "market": "A股"},
    {"ticker": "NVDA",          "name": "英伟达",  "market": "美股"},
    {"ticker": "601899.SS",     "name": "紫金矿业", "market": "A股"},
]

now = datetime.now()
analysis_date = (now).strftime("%Y-%m-%d")

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = os.getenv("TRADINGAGENTS_LLM_PROVIDER", "deepseek")
config["deep_think_llm"] = "deepseek-v4-flash"
config["quick_think_llm"] = "deepseek-v4-flash"
config["output_language"] = "Chinese"
config["max_debate_rounds"] = 1
config["max_risk_discuss_rounds"] = 1

print(f"\n{'='*60}")
print(f"  🔄 AI 预测重试")
print(f"  📅 分析日期: {analysis_date}")
print(f"  📊 目标: {len(STOCKS)} 只股票")
print(f"{'='*60}\n")

predictions = []
for i, stock in enumerate(STOCKS, 1):
    print(f"[{i}/{len(STOCKS)}] 分析 {stock['market']} {stock['name']}({stock['ticker']})...", flush=True)
    try:
        ta = TradingAgentsGraph(debug=False, config=config)
        _, decision = ta.propagate(stock["ticker"], analysis_date)
        status = "ok"
        print(f"     → {decision}", flush=True)
    except Exception as e:
        decision = f"预测失败: {str(e)[:100]}"
        status = "error"
        print(f"     → ❌ {decision}", flush=True)

    predictions.append({
        "ticker": stock["ticker"],
        "name": stock["name"],
        "market": stock["market"],
        "decision": decision,
        "status": status,
    })

# 更新 briefing JSON
briefing_path = os.path.join(PROJECT_DIR, "news_aggregator", "reports", f"briefing_{now.strftime('%Y%m%d')}.json")
if os.path.exists(briefing_path):
    with open(briefing_path, "r", encoding="utf-8") as f:
        briefing = json.load(f)
    briefing["predictions"] = predictions
    briefing["predicted_stocks"] = len(predictions)
    briefing["retry_time"] = now.strftime("%H:%M:%S")
    with open(briefing_path, "w", encoding="utf-8") as f:
        json.dump(briefing, f, ensure_ascii=False, indent=2)

print(f"\n{'='*60}")
print(f"  ✅ 重试完成")
print(f"  📊 成功: {sum(1 for p in predictions if p['status'] == 'ok')}/{len(predictions)}")
print(f"  💾 摘要: {briefing_path}")
print(f"{'='*60}")
