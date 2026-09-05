"""快速预测 - 专注美股/港股大事件个股"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_aggregator"))
from dotenv import load_dotenv
load_dotenv(".env")
from news_aggregator.fetcher import fetch_all_news, classify_news
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from collections import Counter

all_news = fetch_all_news()
major, _ = classify_news(all_news)

# 只取美股和港股
stock_counter = Counter()
for e in major:
    if e["market"] in ["美股", "港股"]:
        ticker = f"{e['stock_code']}.HK" if e["market"] == "港股" else e["stock_code"]
        stock_counter[ticker] += 1

top2 = stock_counter.most_common(2)
if top2:
    ticker = top2[0][0]
    name = ticker.replace(".HK","")
    count = top2[0][1]
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = "deepseek"
    config["deep_think_llm"] = "deepseek-v4-flash"
    config["quick_think_llm"] = "deepseek-v4-flash"
    config["output_language"] = "Chinese"
    config["max_debate_rounds"] = 1
    
    print(f"\n{'='*50}")
    print(f"  预测: {ticker} - {count}条大事件")
    print(f"{'='*50}")
    ta = TradingAgentsGraph(debug=False, config=config)
    _, decision = ta.propagate(ticker, "2026-07-21")
    print(f"\n  🎯 决策: {decision}")
else:
    print("没有足够的大事件美股/港股")
