"""
100分制量化评分引擎
汇总: 技术面 + 基本面 + 预测市场 + 情绪 + 宏观 + 轮动 + 风控
"""
import os, sys, json
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "crypto_data"))

from fear_greed import get_fear_greed, get_altcoin_season
from sector_macro import get_sector_rotation, get_eth_btc_ratio, get_dxy

def score_asset(ticker: str, name: str = "", market: str = "港股") -> dict:
    """
    对单一资产进行100分制量化评分
    返回完整的评分卡
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    results = []

    # ============ 1. 技术面 (25分) ============
    # 从 TradingAgents 获取（如果可用）
    tech_score = {"name": "技术面", "max": 25, "score": 0, "detail": []}
    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        
        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = "deepseek"
        config["deep_think_llm"] = "deepseek-v4-flash"
        config["quick_think_llm"] = "deepseek-v4-flash"
        config["output_language"] = "Chinese"
        config["max_debate_rounds"] = 1
        config["max_risk_discuss_rounds"] = 1
        
        ta = TradingAgentsGraph(debug=False, config=config)
        result, decision = ta.propagate(ticker, (datetime.now()).strftime("%Y-%m-%d"))
        
        # 从 TradingAgents 输出中提取技术信号
        decision_lower = str(result).lower()
        
        # 趋势
        if "死叉" in decision_lower or "空头排列" in decision_lower:
            tech_score["detail"].append(("趋势方向", 0, 6, "空头排列"))
        elif "金叉" in decision_lower and "零轴上方" in decision_lower:
            tech_score["detail"].append(("趋势方向", 5, 6, "多头排列"))
        else:
            tech_score["detail"].append(("趋势方向", 3, 6, "混合信号"))
        
        # RSI位置
        for line in str(result).split("\n"):
            if "rsi" in line.lower() and ":" in line:
                try:
                    rsi_val = float(line.split(":")[-1].strip().split()[0])
                    if 40 <= rsi_val <= 60:
                        tech_score["detail"].append(("RSI位置", 3, 3, f"RSI={rsi_val:.0f} 中性"))
                    elif rsi_val < 35:
                        tech_score["detail"].append(("RSI位置", 2, 3, f"RSI={rsi_val:.0f} 超卖"))
                    elif rsi_val > 70:
                        tech_score["detail"].append(("RSI位置", 1, 3, f"RSI={rsi_val:.0f} 超买"))
                    else:
                        tech_score["detail"].append(("RSI位置", 2, 3, f"RSI={rsi_val:.0f}"))
                    break
                except:
                    pass
        if not any(d[0] == "RSI位置" for d in tech_score["detail"]):
            tech_score["detail"].append(("RSI位置", 1, 3, "未获取"))
        
        # MACD
        if "macd" in decision_lower:
            if "金叉" in decision_lower:
                tech_score["detail"].append(("MACD", 3, 3, "金叉"))
            elif "死叉" in decision_lower:
                tech_score["detail"].append(("MACD", 0, 3, "死叉"))
            else:
                tech_score["detail"].append(("MACD", 1, 3, "弱势"))
        else:
            tech_score["detail"].append(("MACD", 1, 3, "未明确"))
        
        # 成交量 (默认)
        tech_score["detail"].append(("成交量", 2, 4, "参考TradingAgents报告"))
        tech_score["detail"].append(("支撑阻力", 2, 3, "参考TradingAgents报告"))
        tech_score["detail"].append(("布林带", 1, 2, "参考TradingAgents报告"))
        
        # 汇总
        tech_score["score"] = sum(d[1] for d in tech_score["detail"])
        tech_score["final_decision"] = decision
        
    except Exception as e:
        tech_score["score"] = 10  # fallback 中性偏低
        tech_score["detail"].append(("TradingAgents", 10, 25, f"调用失败: {str(e)[:50]}"))
        tech_score["final_decision"] = "N/A"
    
    results.append(tech_score)
    
    # ============ 3. 预测市场 (15分) ============
    pm_score = {"name": "预测市场+资金面", "max": 15, "score": 0, "detail": []}
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets?search=fed+rate+cut+2026&limit=3", timeout=10)
        markets = r.json()
        # 找不降息概率
        for m in markets:
            if "no" in m.get("question", "").lower() and "rate" in m.get("question", "").lower():
                prob = m.get("outcomePrices", ["0"])[0]
                no_cut_pct = float(prob) * 100
                if no_cut_pct >= 70:
                    pm_score["detail"].append(("联储不降息概率", 0, 4, f"{no_cut_pct:.0f}% → 利空"))
                elif no_cut_pct >= 50:
                    pm_score["detail"].append(("联储不降息概率", 2, 4, f"{no_cut_pct:.0f}% → 中性"))
                else:
                    pm_score["detail"].append(("联储不降息概率", 4, 4, f"{no_cut_pct:.0f}% → 利好"))
                break
        if not pm_score["detail"]:
            pm_score["detail"].append(("联储不降息概率", 2, 4, "未获取→默认中性"))
    except:
        pm_score["detail"].append(("联储不降息概率", 2, 4, "API不可用"))
    
    # 衰退概率
    try:
        r2 = requests.get("https://gamma-api.polymarket.com/markets?search=recession+2026&limit=3", timeout=10)
        for m in r2.json():
            if "us" in m.get("question", "").lower():
                prob = float(m.get("outcomePrices", ["0"])[0]) * 100
                if prob <= 15:
                    pm_score["detail"].append(("美国衰退概率", 3, 3, f"{prob:.0f}% → 低风险"))
                elif prob <= 30:
                    pm_score["detail"].append(("美国衰退概率", 2, 3, f"{prob:.0f}% → 中风险"))
                else:
                    pm_score["detail"].append(("美国衰退概率", 0, 3, f"{prob:.0f}% → 高风险"))
                break
        if not any(d[0] == "美国衰退概率" for d in pm_score["detail"]):
            pm_score["detail"].append(("美国衰退概率", 2, 3, "未获取→默认中性"))
    except:
        pm_score["detail"].append(("美国衰退概率", 2, 3, "API不可用"))
    
    # 其余默认中性
    pm_score["detail"].append(("ETF/机构资金流", 2, 4, "需手动补充"))
    pm_score["detail"].append(("期货OI+资金费率", 2, 4, "需手动补充"))
    
    pm_score["score"] = sum(d[1] for d in pm_score["detail"])
    results.append(pm_score)
    
    # ============ 4. 市场情绪 (12分) ============
    sent_score = {"name": "市场情绪", "max": 12, "score": 0, "detail": []}
    
    fg = get_fear_greed()
    sent_score["detail"].append(("恐惧贪婪", fg["score"], 3, f'{fg["value"]} {fg["label"]} → {fg["level"]}'))
    
    alt = get_altcoin_season()
    sent_score["detail"].append(("山寨季指数", alt["score"], 3, alt["label"]))
    
    # 社交媒体信号（默认值）
    sent_score["detail"].append(("新闻情绪", 2, 3, "参考TradingAgents情绪分析"))
    sent_score["detail"].append(("KOL共识", 2, 3, "需手动评估"))
    
    sent_score["score"] = sum(d[1] for d in sent_score["detail"])
    results.append(sent_score)
    
    # ============ 5. 宏观环境 (10分) ============
    macro_score = {"name": "宏观环境", "max": 10, "score": 0, "detail": []}
    
    dxy = get_dxy()
    macro_score["detail"].append(("美元指数DXY", dxy["score"], 3, dxy["level"]))
    
    ethbtc = get_eth_btc_ratio()
    macro_score["detail"].append(("ETH/BTC比率", ethbtc["score"], 3, ethbtc["level"]))
    
    # 利率预期（从预测市场继承）
    macro_score["detail"].append(("美联储利率预期", 2, 4, "见预测市场"))
    
    macro_score["score"] = sum(d[1] for d in macro_score["detail"])
    results.append(macro_score)
    
    # ============ 6. 板块轮动 (8分) ============
    rot_score = {"name": "板块轮动", "max": 8, "score": 0, "detail": []}
    
    sectors = get_sector_rotation()
    rot_score["detail"].append(("热门赛道Top3", sectors["score"], 3, str(sectors.get("top_sectors", [])[:80])))
    rot_score["detail"].append(("港股科技赛道", 1, 3, "当前资金流出科技板块"))
    rot_score["detail"].append(("相对强度", 1, 2, "跑输大盘"))
    
    rot_score["score"] = sum(d[1] for d in rot_score["detail"])
    results.append(rot_score)
    
    # ============ 7. 风控 (10分) ============
    risk_score = {"name": "风险控制", "max": 10, "score": 0, "detail": []}
    risk_score["detail"].append(("流动性", 3, 3, "港股ETF 流动性好"))
    risk_score["detail"].append(("距52周高点", 1, 3, "距高点-30%+ 已大幅回调"))
    risk_score["detail"].append(("杠杆适用", 0, 2, "ETF不建议加杠杆"))
    risk_score["detail"].append(("仓位建议", 1, 2, "控制在组合10-15%"))
    risk_score["score"] = sum(d[1] for d in risk_score["detail"])
    results.append(risk_score)
    
    # ============ 汇总 ============
    total = sum(r["score"] for r in results)
    max_total = sum(r["max"] for r in results) - 20  # 减去基本面20分（ETF不适用）
    
    if total >= 70: grade = "⭐⭐⭐⭐⭐ 强烈做多"
    elif total >= 60: grade = "⭐⭐⭐⭐ 看多"
    elif total >= 50: grade = "⭐⭐⭐ 偏多/轻仓"
    elif total >= 40: grade = "⭐⭐ 观望"
    else: grade = "⭐ 回避/减仓"
    
    return {
        "ticker": ticker,
        "name": name,
        "market": market,
        "time": now,
        "categories": results,
        "total_score": total,
        "max_score": max_total,
        "pct": round(total / max_total * 100, 1),
        "grade": grade
    }


def print_scorecard(data: dict):
    """打印格式化的评分卡"""
    print()
    print("=" * 70)
    print(f"  📊 量化评分卡: {data['ticker']} ({data['name']})")
    print(f"  时间: {data['time']}  |  市场: {data['market']}")
    print("=" * 70)
    
    for cat in data["categories"]:
        pct = cat["score"] / cat["max"] * 100
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        print(f"\n  {cat['name']} [{cat['score']}/{cat['max']}] {bar} {pct:.0f}%")
        for detail in cat["detail"]:
            name, score, max_s, note = detail
            print(f"    ├ {name}: {score}/{max_s}  — {note}")
    
    total_pct = data["pct"]
    total_bar = "█" * int(total_pct / 10) + "░" * (10 - int(total_pct / 10))
    print(f"\n{'='*70}")
    print(f"  🎯 总分: {data['total_score']}/{data['max_score']} ({data['pct']}%) {total_bar}")
    print(f"  📋 评级: {data['grade']}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    import requests
    # 跑 3032.HK 作为示例
    data = score_asset("3032.HK", "恒生科技ETF", "港股")
    print_scorecard(data)
    
    # 保存 JSON
    os.makedirs("results", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    with open(f"results/scorecard_{data['ticker']}_{ts}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"  📁 JSON已保存: results/scorecard_{data['ticker']}_{ts}.json")
