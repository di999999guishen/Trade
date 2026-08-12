"""TradingAgents 每日盘前预测 — 自动化脚本（网络容错版）
自动取最新交易日数据，分析个股走势方向，网络中断自动重试
"""
import os, sys, json, time, logging
from datetime import datetime, timedelta

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 🔧 强制代理绕过（必须在所有 import 之前）
from retry_utils import setup_proxy_bypass, retry_on_network_error
setup_proxy_bypass()

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

# 日志配置
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
analysis_date = (today + timedelta(days=1)).strftime("%Y-%m-%d")
ticker = os.getenv("DAILY_TICKER", "3032.HK")

# ---- 配置 ----
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = os.getenv("TRADINGAGENTS_LLM_PROVIDER", "deepseek")
config["deep_think_llm"] = os.getenv("TRADINGAGENTS_DEEP_THINK_LLM", "deepseek-v4-flash")
config["quick_think_llm"] = os.getenv("TRADINGAGENTS_QUICK_THINK_LLM", "deepseek-v4-flash")
config["output_language"] = "Chinese"
config["max_debate_rounds"] = 1
config["max_risk_discuss_rounds"] = 1

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
print(f"  分析标的: {ticker}")
print(f"  分析日期: {analysis_date}")
print(f"  LLM: {config['llm_provider']} | 模型: {config['deep_think_llm']}/{config['quick_think_llm']}")
print(f"  最大重试: {MAX_RETRIES} 次")
print(f"{'='*60}\n")

try:
    result, decision = run_prediction_with_retry(ticker, analysis_date)

    # 保存结果
    ts = today.strftime("%Y%m%d_%H%M")
    report_file = os.path.join(results_dir, f"{ticker}_{ts}.md")
    summary_file = os.path.join(results_dir, f"{ticker}_{ts}.json")

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(f"# {ticker} 每日预测\n\n")
        f.write(f"- 运行时间: {today.strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"- 分析日期: {analysis_date}\n")
        f.write(f"- 最终决策: **{decision}**\n\n")
        f.write("---\n\n")
        f.write(str(result)[:5000])

    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump({
            "ticker": ticker,
            "run_time": today.isoformat(),
            "analysis_date": analysis_date,
            "decision": decision
        }, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"  ✅ 预测完成: {decision}")
    print(f"  📄 报告: {report_file}")
    print(f"  📊 摘要: {summary_file}")
    print(f"{'='*60}")

except Exception as e:
    error_file = os.path.join(results_dir, f"error_{today.strftime('%Y%m%d_%H%M')}.log")
    with open(error_file, "w", encoding="utf-8") as f:
        f.write(f"Error at {today.isoformat()} after {MAX_RETRIES} retries:\n{type(e).__name__}: {e}\n")
    print(f"\n❌ 运行失败（已重试 {MAX_RETRIES} 次）: {type(e).__name__}: {e}")
    print(f"错误日志: {error_file}")
    sys.exit(1)
