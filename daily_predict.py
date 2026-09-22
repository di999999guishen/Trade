"""TradingAgents 每日盘前预测 — 自动化脚本（网络容错版）
自动取最新交易日数据，分析个股走势方向，网络中断自动重试
"""
import os, sys, json, time, logging
from datetime import datetime
from daily_artifacts import new_run_id, save_daily_result

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 🔧 强制代理绕过（必须在所有 import 之前）
from retry_utils import setup_proxy_bypass, retry_on_network_error
setup_proxy_bypass()

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

# 日志配置
os.makedirs(os.path.join(PROJECT_DIR, "logs"), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(PROJECT_DIR, "logs", "daily_predict.log"), encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

# 确保日志目录存在
os.makedirs(os.path.join(PROJECT_DIR, "logs"), exist_ok=True)

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

# ---- 自动日期 ----
today = datetime.now()
run_id = new_run_id(today)
# 盘前（早 7:30 运行）：预测当日（today）走势，而非次日
analysis_date = today.strftime("%Y-%m-%d")

# ---- 分析标的 ----
# 默认只跑 A 股标的（走新浪财经数据源，最省 API / 不受 Yahoo 限流拖慢）：
#   510050.SS = 上证50ETF；515220.SS = 煤炭ETF国泰
# 可用环境变量 DAILY_TICKER 以逗号分隔覆盖，如 "3032.HK,510050.SS"
tickers = os.getenv("DAILY_TICKER", "510050.SS,515220.SS").split(",")
tickers = [t.strip() for t in tickers if t.strip()]

# ---- 配置 ----
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = os.getenv("TRADINGAGENTS_LLM_PROVIDER", "deepseek")
config["deep_think_llm"] = os.getenv("TRADINGAGENTS_DEEP_THINK_LLM", "deepseek-v4-flash")
config["quick_think_llm"] = os.getenv("TRADINGAGENTS_QUICK_THINK_LLM", "deepseek-v4-flash")
config["output_language"] = "Chinese"
config["max_debate_rounds"] = 1
config["max_risk_discuss_rounds"] = 1

# 🔒 全程国内/不限流数据源（禁用 yfinance/Reddit/StockTwits/FRED 等限流源）
# 东方财富/akshare 覆盖：个股新闻+全球快讯、中国宏观(CPI/PPI/PMI/M2/GDP/LPR)、ETF基本面
config["data_vendors"] = {
    "core_stock_apis": "akshare",
    "technical_indicators": "akshare",
    "fundamental_data": "eastmoney",
    "news_data": "eastmoney",
    "macro_data": "akshare_macro",
    "prediction_markets": "polymarket",
}

# ---- 结果目录 ----
results_dir = os.path.join(PROJECT_DIR, "results", "daily")
os.makedirs(results_dir, exist_ok=True)

# ---- 重试配置 ----
MAX_RETRIES = 5
INITIAL_DELAY = 10  # 秒


def run_prediction_with_retry(ticker: str, analysis_date: str) -> tuple:
    """带指数退避重试的预测执行"""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            ta = TradingAgentsGraph(debug=False, config=config)
            result, decision = ta.propagate(ticker, analysis_date)
            return result, decision
        except Exception as e:
            last_error = e
            error_lower = str(e).lower()

            # 判断是否为网络错误
            is_network = any(kw in error_lower for kw in [
                "timeout", "connect", "refused", "reset", "eof", "ssl",
                "network", "proxy", "remote", "broken pipe", "503", "502",
                "unreachable", "tunnel", "connection", "timed out",
            ])

            if not is_network or attempt >= MAX_RETRIES:
                break

            delay = min(INITIAL_DELAY * (2 ** attempt), 300)
            logger.warning(
                "[%s] 第 %d/%d 次重试，%ds 后重试: %s",
                ticker, attempt + 1, MAX_RETRIES, delay, str(e)[:100]
            )
            time.sleep(delay)

    raise last_error


# ---- 主流程 ----
print(f"\n{'='*60}")
print(f"  运行时间: {today.strftime('%Y-%m-%d %H:%M')}")
print(f"  分析标的: {', '.join(tickers)}")
print(f"  分析日期: {analysis_date}")
print(f"  LLM: {config['llm_provider']} | 模型: {config['deep_think_llm']}/{config['quick_think_llm']}")
print(f"  最大重试: {MAX_RETRIES} 次")
print(f"{'='*60}\n")

failed = []
for i, ticker in enumerate(tickers, 1):
    print(f"\n[{i}/{len(tickers)}] 开始分析 {ticker} ...")
    try:
        result, decision = run_prediction_with_retry(ticker, analysis_date)

        # 保存结果
        report_file, summary_file = save_daily_result(
            results_dir, ticker, run_id, today, analysis_date, result, decision)

        print(f"\n{'='*60}")
        print(f"  ✅ [{i}/{len(tickers)}] {ticker} 预测完成: {decision}")
        print(f"  📄 报告: {report_file}")
        print(f"  📊 摘要: {summary_file}")
        print(f"{'='*60}")

    except Exception as e:
        failed.append(ticker)
        error_file = os.path.join(results_dir, f"error_{ticker}_{run_id}.log")
        with open(error_file, "w", encoding="utf-8") as f:
            f.write(f"Error at {today.isoformat()} after {MAX_RETRIES} retries:\n{type(e).__name__}: {e}\n")
        print(f"\n❌ [{i}/{len(tickers)}] {ticker} 运行失败（已重试 {MAX_RETRIES} 次）: {type(e).__name__}: {e}")
        print(f"错误日志: {error_file}")
        # 单个标的失败不阻断后续标的，继续跑下一个
        continue

if failed:
    print(f"\n⚠️  完成，{len(failed)}/{len(tickers)} 只标的失败: {', '.join(failed)}")
    sys.exit(1)
else:
    print(f"\n🎉 全部 {len(tickers)} 只标的预测完成。")
