"""
新闻流 + TradingAgents 集成脚本（并发优化版 + 网络容错 + 可交易筛选）
流程: 新闻聚合 → 大事件提取 → 个股预测(并发) → 盘前简报
"""
import os, sys, json, time, logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# 路径设置
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, r"E:\Trade\stock_news_aggregator")
sys.path.insert(0, os.path.join(PROJECT_DIR, "news_aggregator"))

# 🔧 必须在所有 import 之前设置代理绕过（覆盖 WorkBuddy 注入的 env）
from retry_utils import setup_proxy_bypass, retry_on_network_error, RetryContext
setup_proxy_bypass()

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(PROJECT_DIR, "logs", "morning_briefing.log"), encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

# 并发控制
MAX_PARALLEL_STOCKS = 4  # 同时分析最多 4 只
MAX_PREDICT_STOCKS = 21  # 默认预测21只（14基础 + 7用户指定ETF）

# ── 默认观察列表 (全可交易：美股 + A股ETF，21只) ──────────────────────
# 用户画像: A股主板投资者，¥50,000总资产，可交易主板A股+ETF+美股
# 覆盖: 美股科技3只 + 港股映射ETF 3只 + A股宽基ETF 3只 + A股行业ETF 3只 + 贵金属 2只 + 用户指定ETF 7只
DEFAULT_WATCHLIST = [
    # ── 美股科技 (3只) ──
    {"code": "AAPL",  "name": "苹果",       "market": "美股", "type": "个股", "desc": "美股科技蓝筹"},
    {"code": "MSFT",  "name": "微软",       "market": "美股", "type": "个股", "desc": "美股AI/云"},
    {"code": "NVDA",  "name": "英伟达",     "market": "美股", "type": "个股", "desc": "美股AI芯片"},
    # ── 港股映射ETF (3只) ──
    {"code": "513130", "name": "恒生科技ETF",  "market": "A股", "type": "ETF", "desc": "港股科技龙头"},
    {"code": "159920", "name": "恒生ETF",      "market": "A股", "type": "ETF", "desc": "港股大盘基准"},
    {"code": "159892", "name": "恒生医药ETF",  "market": "A股", "type": "ETF", "desc": "港股医药板块"},
    # ── A股宽基ETF (3只) — 大盘/创业板/科创 ──
    {"code": "510050", "name": "上证50ETF",    "market": "A股", "type": "ETF", "desc": "A股大盘蓝筹(上证50)"},
    {"code": "159915", "name": "创业板ETF",    "market": "A股", "type": "ETF", "desc": "A股成长股(创业板)"},
    {"code": "588000", "name": "科创50ETF",    "market": "A股", "type": "ETF", "desc": "A股硬科技(科创板)"},
    # ── A股行业ETF (3只) — 消费/食品饮料/医药 ──
    {"code": "159928", "name": "消费ETF",      "market": "A股", "type": "ETF", "desc": "A股消费龙头"},
    {"code": "515170", "name": "食品饮料ETF",  "market": "A股", "type": "ETF", "desc": "A股食品饮料(白酒+乳业)"},
    {"code": "512010", "name": "医药ETF",      "market": "A股", "type": "ETF", "desc": "A股医药龙头"},
    # ── 贵金属 (2只) — 黄金/白银 ──
    {"code": "518880", "name": "黄金ETF",      "market": "A股", "type": "ETF", "desc": "黄金现货(华安黄金ETF)"},
    {"code": "159937", "name": "白银基金",      "market": "A股", "type": "ETF", "desc": "白银现货(博时白银LOF)"},
    # ── 用户指定ETF (7只) — 有色金属/电力/上证/创新药/新能源/医疗 ──
    {"code": "512400", "name": "有色金属ETF",   "market": "A股", "type": "ETF", "desc": "有色金属板块(南方基金)"},
    {"code": "159611", "name": "电力ETF",       "market": "A股", "type": "ETF", "desc": "电力公用事业(景顺长城)"},
    {"code": "510160", "name": "上证指数ETF",   "market": "A股", "type": "ETF", "desc": "上证综合指数(南方基金)"},
    {"code": "589720", "name": "科创创新药ETF", "market": "A股", "type": "ETF", "desc": "科创板创新药(国泰基金)"},
    {"code": "159748", "name": "创新药ETF",     "market": "A股", "type": "ETF", "desc": "创新药板块(富国基金)"},
    {"code": "516090", "name": "新能源ETF",     "market": "A股", "type": "ETF", "desc": "新能源产业(易方达)"},
    {"code": "512170", "name": "医疗ETF",       "market": "A股", "type": "ETF", "desc": "医疗健康板块(华宝基金)"},
]

from news_aggregator.fetcher import fetch_all_news, classify_news
from news_aggregator.reporter import generate_report


# ── 可交易性检查 + ETF 推荐映射 ──────────────────────────────────────────

def is_tradable(code: str, market: str) -> tuple[bool, str]:
    """判断股票是否用户可交易。

    用户画像: A股主板投资者，股龄<2年，无科创板/创业板/北交所/港股通权限。

    Returns: (可交易, 不可交易原因或空字符串)
    """
    if market == "港股":
        return False, "无港股通权限(需50万资产+2年经验)"
    if market == "A股":
        if code.startswith("688"):
            return False, "科创板(需50万+2年)"
        if code.startswith("30") and len(code) == 6:
            return False, "创业板(需2年股龄)"
        if code.startswith("8") and len(code) >= 4:
            return False, "北交所(需额外权限)"
    return True, ""


# ETF 推荐映射：港股/限制板块 → 可购买的A股ETF
ETF_RECOMMENDATIONS = {
    # 港股龙头 → A股ETF
    "00700": [  # 腾讯
        {"name": "中概互联网ETF", "code": "513050"},
        {"name": "港股通互联网ETF", "code": "159792"},
    ],
    "09988": [  # 阿里巴巴
        {"name": "中概互联网ETF", "code": "513050"},
        {"name": "恒生互联网ETF", "code": "513330"},
    ],
    "01810": [  # 小米
        {"name": "消费电子ETF", "code": "159732"},
        {"name": "港股通科技ETF", "code": "513860"},
    ],
    "03690": [  # 美团
        {"name": "港股通消费ETF", "code": "513960"},
        {"name": "恒生科技ETF", "code": "513130"},
    ],
    "09618": [  # 京东
        {"name": "中概互联网ETF", "code": "513050"},
    ],
    "09999": [  # 网易
        {"name": "中概互联网ETF", "code": "513050"},
        {"name": "游戏ETF", "code": "159869"},
    ],
    "09888": [  # 百度
        {"name": "中概互联网ETF", "code": "513050"},
    ],
    # 港股ETF
    "03032": [  # 恒生科技ETF(港股) → A股恒生科技ETF
        {"name": "恒生科技ETF", "code": "513130"},
        {"name": "港股通科技ETF", "code": "513860"},
    ],
    "02800": [  # 盈富基金 → A股恒生ETF
        {"name": "恒生ETF", "code": "159920"},
    ],
    # 科创板 → A股替代
    "688981": [  # 中芯国际
        {"name": "芯片ETF", "code": "159995"},
        {"name": "半导体ETF", "code": "512480"},
    ],
    # 创业板权重 → A股替代
    "300750": [  # 宁德时代 → 可通过创业板ETF参与
        {"name": "创业板ETF", "code": "159915"},
        {"name": "新能源车ETF", "code": "515030"},
    ],
}


def get_etf_recommendation(code: str, name: str) -> str:
    """为不可交易股票推荐可购买的A股ETF。"""
    etfs = ETF_RECOMMENDATIONS.get(code, [])
    if etfs:
        parts = [f"{e['name']}({e['code']})" for e in etfs[:2]]
        return " | ".join(parts)
    # 通用推荐
    if name and "科技" in name:
        return "科技ETF(515000) | 科创50ETF(588000)"
    if name and ("消费" in name or "零售" in name):
        return "消费ETF(159928) | 食品饮料ETF(515170)"
    if name and ("医药" in name or "医疗" in name):
        return "医药ETF(512010) | 医疗ETF(512170)"
    if name and ("金融" in name or "银行" in name or "保险" in name):
        return "金融ETF(510230) | 证券ETF(512880)"
    if name and ("能源" in name or "新能源" in name or "电池" in name):
        return "新能源ETF(516160) | 碳中和ETF(159790)"
    return "沪深300ETF(510300) | A50ETF(159601)"


# TradingAgents 预测 (轻量版) — 带重试
def to_yfinance_ticker(code: str, market: str) -> str:
    """转换代码为 yfinance 格式。ETF按交易所区分散户(SH=5开头, SZ=1开头)。"""
    if market == "A股":
        if code.startswith(("6", "5")): return f"{code}.SS"  # 上海（主板60+ETF51/58/56）
        else: return f"{code}.SZ"  # 深圳（主板00/30+ETF15）
    elif market == "港股": return f"{code}.HK"
    else: return code  # 美股直接用


@retry_on_network_error(max_retries=3, base_delay=5.0, max_delay=30.0)
def _run_trading_agent(ticker: str, name: str, analysis_date: str):
    """内部：调用 TradingAgents，支持自动重试"""
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.default_config import DEFAULT_CONFIG

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = os.getenv("TRADINGAGENTS_LLM_PROVIDER", "deepseek")
    config["deep_think_llm"] = "deepseek-v4-flash"
    config["quick_think_llm"] = "deepseek-v4-flash"
    config["output_language"] = "Chinese"
    config["max_debate_rounds"] = 1
    config["max_risk_discuss_rounds"] = 1

    ta = TradingAgentsGraph(debug=False, config=config)
    _, decision = ta.propagate(ticker, analysis_date)
    return decision


def predict_stock(ticker: str, name: str) -> dict:
    """对单只个股运行 TradingAgents 预测，带网络重试"""
    analysis_date = datetime.now().strftime("%Y-%m-%d")
    max_attempts = 3

    for attempt in range(max_attempts):
        try:
            decision = _run_trading_agent(ticker, name, analysis_date)
            return {"ticker": ticker, "name": name, "decision": decision, "status": "ok"}
        except Exception as e:
            if attempt < max_attempts - 1 and any(
                kw in str(e).lower() for kw in ["timeout", "connect", "refused", "reset", "eof", "ssl", "network", "proxy"]
            ):
                delay = 5 * (2 ** attempt)
                logger.warning(f"[{name}] 网络错误，{delay}s后第{attempt+2}次尝试: {str(e)[:80]}")
                time.sleep(delay)
            else:
                logger.error(f"[{name}] 预测失败（已重试{attempt}次）: {str(e)[:120]}")
                return {"ticker": ticker, "name": name, "decision": f"预测失败: {str(e)[:80]}", "status": "error"}


def main():
    now = datetime.now()
    print("=" * 70)
    print(f"  盘前新闻流 + AI 预测系统")
    print(f"  时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # ===== Phase 1: 新闻采集（带重试） =====
    print("\n[Phase 1/3] 新闻采集...")
    all_news = {}
    major_events, normal_news = [], []

    max_news_attempts = 3
    for attempt in range(max_news_attempts):
        try:
            all_news = fetch_all_news()
            major_events, normal_news = classify_news(all_news)
            break
        except Exception as e:
            if attempt < max_news_attempts - 1:
                delay = 10 * (2 ** attempt)
                logger.warning(f"新闻采集失败（第{attempt+1}次），{delay}s后重试: {str(e)[:80]}")
                time.sleep(delay)
            else:
                logger.error(f"新闻采集完全失败: {str(e)[:200]}")
                print(f"  ❌ 新闻采集失败，跳过本阶段: {str(e)[:80]}")

    total = sum(len(v) for v in all_news.values())
    print(f"\n  采集完成:")
    print(f"     总新闻: {total} 条")
    print(f"     重大事件: {len(major_events)} 条")
    for market in ["港股"]:
        print(f"     {market}: {len(all_news.get(market, []))} 条")

    # ===== Phase 2: 生成新闻 HTML 报告 =====
    print("\n[Phase 2/3] 生成报告...")
    report_path = generate_report(all_news, major_events, normal_news,
                                   output_dir=os.path.join(PROJECT_DIR, "news_aggregator", "reports"))
    print(f"  ✅ 新闻报告: {report_path}")

    # ===== Phase 3: AI 预测 — 默认观察列表（新闻作为上下文参考） =====
    USER_BUDGET = 50000

    # ── Polymarket 黄金预测信号 (加入新闻流) ──
    print(f"\n[Phase 3/3] AI 预测 — 默认观察列表 + 预测市场信号")
    print(f"  🔮 获取 Polymarket 黄金预测市场数据...")
    gold_poly_signal = ""
    try:
        from tradingagents.dataflows.polymarket import get_prediction_markets
        gold_poly_signal = get_prediction_markets("gold price", limit=6)
        # 提取关键行展示
        short_lines = []
        for line in gold_poly_signal.split("\n"):
            if line.startswith("- **"):
                parts = line.replace("- **", "").split("** — ")
                if len(parts) >= 2:
                    short_lines.append(f"    {parts[0]} → {parts[1][:80]}")
        if short_lines:
            print(f"  📡 Polymarket 黄金预测:")
            for sl in short_lines[:5]:
                print(sl)
        else:
            print(f"  ⚠️ Polymarket 无可用黄金预测")
    except Exception as e:
        print(f"  ⚠️ Polymarket 获取失败: {str(e)[:60]}")
        gold_poly_signal = f"Polymarket data unavailable: {e}"

    # 从新闻事件中提取相关 ETF/美股关键词，为观察列表补充事件上下文
    news_keywords = set()
    for event in major_events:
        title = event.get("title", "") + event.get("stock_name", "")
        news_keywords.update([event.get("stock_code", ""), event.get("stock_name", "")])

    # 为观察列表每只标的匹配新闻上下文
    predict_list = []
    for item in DEFAULT_WATCHLIST:
        entry = dict(item)
        # 匹配相关新闻
        related = [e for e in major_events if
                   item["code"] in e.get("title", "") or item["name"] in e.get("title", "")
                   or item["desc"] in e.get("title", "")]
        entry["event_title"] = related[0]["title"][:60] if related else f"定期观察: {item['desc']}"
        entry["tradable"] = True  # 全部可交易
        entry["untradable_reason"] = ""
        predict_list.append(entry)

    print(f"  📋 观察列表: {len(DEFAULT_WATCHLIST)} 只 | 全部预测 (最多{MAX_PARALLEL_STOCKS}只并行)")
    print(f"  💰 账户总资产: ¥{USER_BUDGET:,} | 🌍 美股3 | 🇭🇰 HK映射ETF 3 | 🇨🇳 A股宽基ETF 3 | 🏭 A股行业ETF 3 | 🥇 贵金属 2 | 📌 用户指定ETF 7")
    print(f"  📰 新闻事件: {len(major_events)}条 → 作为分析参考上下文\n")

    # 并发预测：所有标的都在同一线程池中竞争
    predictions = []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_STOCKS) as executor:
        future_map = {}
        for item in predict_list[:MAX_PREDICT_STOCKS]:
            ticker = to_yfinance_ticker(item["code"], item["market"])
            tag = "🇺🇸" if item["market"] == "美股" else "📊"
            label = f"{tag} {item['name']}({ticker}) [{item['desc']}]"
            future = executor.submit(predict_stock, ticker, item["name"])
            future_map[future] = {**item, "ticker": ticker, "label": label}

        for future in as_completed(future_map):
            item = future_map[future]
            result = future.result()
            result["market"] = item["market"]
            result["event"] = item.get("event_title", "")
            result["type"] = item.get("type", "")
            result["desc"] = item.get("desc", "")
            result["tradable"] = True
            predictions.append(result)

            status_icon = "✅" if result["status"] == "ok" else "⚠️"
            print(f"  {status_icon} {item['label']}")
            print(f"     → {result['decision'][:80]}")

    # 按决策严重程度排序
    severity = {"SELL": 0, "Underweight": 1, "HOLD": 2, "BUY": 3, "Overweight": 3}
    predictions.sort(key=lambda p: severity.get(
        next((k for k in severity if k.lower() in p["decision"].lower()), "HOLD"), 99
    ))

    # ===== 生成盘前简报 =====
    print(f"\n{'='*70}")
    print(f"  今日盘前简报 — ETF+美股组合")
    print(f"{'='*70}")

    print(f"\n{'─'*50}")
    print(f"  预测市场信号 (Polymarket)")
    print(f"{'─'*50}")
    if gold_poly_signal:
        for line in gold_poly_signal.split("\n"):
            if line.startswith("- **"):
                print(f"  {line.replace('- **', '🔮 ')}")
            elif line and not line.startswith("##"):
                pass  # skip header lines in console
        print()
    else:
        print(f"  ⚠️ Polymarket 黄金预测数据暂时不可用\n")

    print(f"\n{'─'*50}")
    print(f"  默认观察列表预测 | 账户资产: ¥{USER_BUDGET:,} | 全部可交易")
    print(f"{'─'*50}")

    for p in predictions:
        decision_icon_map = {"SELL": "🔴", "Underweight": "🟠", "HOLD": "🟡", "BUY": "🟢", "Overweight": "🟢"}
        icon = "⚪"
        for k, v in decision_icon_map.items():
            if k.lower() in p["decision"].lower():
                icon = v; break

        market_tag = "🇺🇸" if p["market"] == "美股" else "📊"
        type_tag = p.get("type", "")
        print(f"  {icon} {market_tag} [{type_tag}] {p['name']}({p['ticker']}) — {p.get('desc', '')}")
        print(f"     📰 {p['event'][:70]}")
        print(f"     决策: {p['decision']}")
        print()

    print(f"  完整新闻报告: {report_path}")
    print(f"  详细预测日志: results/daily/")
    print(f"{'='*70}")

    # 保存摘要
    summary = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "news_total": total,
        "major_events": len(major_events),
        "predicted_stocks": len(predictions),
        "predictions": predictions,
        "polymarket_gold": gold_poly_signal,
    }
    os.makedirs(os.path.join(PROJECT_DIR, "news_aggregator", "reports"), exist_ok=True)
    summary_path = os.path.join(PROJECT_DIR, "news_aggregator", "reports", f"briefing_{now.strftime('%Y%m%d')}.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    return summary


if __name__ == "__main__":
    main()
