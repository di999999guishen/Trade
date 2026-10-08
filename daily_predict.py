"""每日预测：配置的持仓 + 固定 SPY/QQQ/黄金(GLD)，附 Polymarket 证据。"""

import argparse
import json
import logging
import os
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from daily_artifacts import new_run_id, save_daily_result

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_HOLDINGS = ("510050.SS", "515220.SS")
MANDATORY_DAILY_TICKERS = ("SPY", "QQQ", "GLD")
MAX_RETRIES = 5
INITIAL_DELAY = 10
logger = logging.getLogger(__name__)


def build_daily_tickers(configured: str | None = None) -> list[str]:
    """DAILY_TICKER changes holdings, never removes the three daily instruments."""
    holdings = DEFAULT_HOLDINGS if configured is None else configured.split(",")
    return list(dict.fromkeys(
        ticker.strip().upper()
        for ticker in (*holdings, *MANDATORY_DAILY_TICKERS)
        if ticker.strip()
    ))


def build_config() -> dict:
    from tradingagents.default_config import DEFAULT_CONFIG

    config = deepcopy(DEFAULT_CONFIG)
    config.update({
        "llm_provider": os.getenv("TRADINGAGENTS_LLM_PROVIDER", "deepseek"),
        "deep_think_llm": os.getenv("TRADINGAGENTS_DEEP_THINK_LLM", "deepseek-v4-flash"),
        "quick_think_llm": os.getenv("TRADINGAGENTS_QUICK_THINK_LLM", "deepseek-v4-flash"),
        "output_language": "Chinese",
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
    })
    # The akshare adapter already serves US OHLCV/indicators through Sina.
    # Yahoo answers 429 for SPY/QQQ/GLD, which the snapshot path would report as
    # "no rows"; the domestic source is opted in through data_vendors below.
    #
    # us_ohlcv_vendor used to be the switch that picked that route. Nothing
    # reads the key any more: the CN/US OHLCV branch that honoured it lived in
    # stockstats_utils and was replaced by the vendor router, where akshare
    # (load_ohlcv_akshare) serves those tickers directly. The line is kept
    # commented, not deleted, so the reason US OHLCV is Sina stays on the
    # record and the dead key is not mistaken for a working switch.
    # config["us_ohlcv_vendor"] = "sina"
    config["data_vendors"] = {
        "core_stock_apis": "akshare",
        "technical_indicators": "akshare",
        "fundamental_data": "eastmoney",
        "news_data": "eastmoney",
        "macro_data": "akshare_macro",
        "prediction_markets": "polymarket",
    }
    return config


def build_ticker_config(base_config: dict, ticker: str, snapshot: dict | None) -> dict:
    """Isolate per-instrument evidence and US ETF sources from holdings config."""
    config = deepcopy(base_config)
    # The shared vendor registry merges dictionaries. Explicitly reset news
    # routing for holdings that follow a US ETF in a custom DAILY_TICKER order.
    config.setdefault("tool_vendors", {}).setdefault(
        "get_news", config.get("data_vendors", {}).get("news_data", "eastmoney"))
    if ticker in MANDATORY_DAILY_TICKERS:
        # Eastmoney fundamentals only cover mainland ETFs. Keep Sina prices
        # and domestic global news while enabling the existing US ETF vendor.
        config.setdefault("data_vendors", {})["fundamental_data"] = "yfinance"
        config.setdefault("tool_vendors", {})["get_news"] = "yfinance,eastmoney"
    if snapshot is not None:
        from tradingagents.dataflows.daily_prediction_markets import (
            render_daily_prediction_market_context,
        )

        config["screen_evidence_context"] = (
            config.get("screen_evidence_context", "")
            + "\n\n" + render_daily_prediction_market_context(snapshot)
        )
    return config


def run_prediction_with_retry(ticker: str, analysis_date: str, config: dict,
                              *, graph_factory=None) -> tuple:
    """Reuse one frozen evidence snapshot throughout retries of this ticker."""
    if graph_factory is None:
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph_factory = TradingAgentsGraph
    for attempt in range(MAX_RETRIES + 1):
        try:
            ta = graph_factory(debug=False, config=deepcopy(config))
            return ta.propagate(ticker, analysis_date)
        except Exception as exc:
            error_lower = str(exc).lower()
            is_network = any(word in error_lower for word in (
                "timeout", "connect", "refused", "reset", "eof", "ssl",
                "network", "proxy", "remote", "broken pipe", "503", "502",
                "unreachable", "tunnel", "timed out",
            ))
            if not is_network or attempt >= MAX_RETRIES:
                raise
            delay = min(INITIAL_DELAY * (2 ** attempt), 300)
            logger.warning("[%s] 第 %d/%d 次重试，%ds 后重试: %s",
                           ticker, attempt + 1, MAX_RETRIES, delay, str(exc)[:100])
            time.sleep(delay)


def run_daily_predictions(tickers, analysis_date, run_time, base_config, results_dir,
                          *, graph_factory=None, snapshot_loader=None) -> list[str]:
    """Run every holding and mandatory instrument, even after an earlier failure."""
    from tradingagents.dataflows.daily_prediction_markets import (
        get_daily_prediction_market_snapshot,
    )
    from tradingagents.dataflows.symbols import safe_ticker_component

    snapshot_loader = snapshot_loader or get_daily_prediction_market_snapshot
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    run_id = new_run_id(run_time)
    tickers = build_daily_tickers(",".join(tickers))
    failed = []
    for index, ticker in enumerate(tickers, 1):
        print(f"\n[{index}/{len(tickers)}] 开始分析 {ticker} ...")
        snapshot = None
        try:
            safe_ticker = safe_ticker_component(ticker)
        except ValueError as exc:
            failed.append(ticker)
            logger.error("Invalid daily ticker: %s", exc)
            continue
        try:
            if ticker in MANDATORY_DAILY_TICKERS:
                try:
                    snapshot = snapshot_loader(ticker)
                except Exception as exc:
                    logger.warning("[%s] Polymarket unavailable: %s", ticker, type(exc).__name__)
                    snapshot = {
                        "schema_version": 1, "ticker": ticker, "status": "unavailable",
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "queries": [], "markets": [], "error": type(exc).__name__,
                    }
                snapshot = {**snapshot, "analysis_date": analysis_date}
                # Keep evidence even if the subsequent LLM analysis fails.
                snapshot_file = results_dir / f"{safe_ticker}_{run_id}_polymarket.json"
                with snapshot_file.open("x", encoding="utf-8") as handle:
                    json.dump(snapshot, handle, ensure_ascii=False, indent=2, allow_nan=False)
                print(f"  Polymarket: {snapshot['status']}，{len(snapshot['markets'])} 个相关市场")

            config = build_ticker_config(base_config, ticker, snapshot)
            result, decision = run_prediction_with_retry(
                ticker, analysis_date, config, graph_factory=graph_factory)
            if snapshot is not None:
                result = {**result, "prediction_market_snapshot": snapshot}
            report, summary = save_daily_result(
                results_dir, safe_ticker, run_id, run_time, analysis_date, result, decision)
            print(f"  ✅ {ticker} 预测完成: {decision}\n  📄 报告: {report}\n  📊 摘要: {summary}")
        except Exception as exc:
            failed.append(ticker)
            error_file = results_dir / f"error_{safe_ticker}_{run_id}.log"
            error_file.write_text(
                f"Error at {datetime.now().isoformat()}:\n{type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            print(f"  ❌ {ticker} 运行失败: {type(exc).__name__}: {exc}\n  错误日志: {error_file}")
    return failed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印每日标的，不联网、不调用模型、不写预测结果")
    args = parser.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv(PROJECT_DIR / ".env")
    tickers = build_daily_tickers(os.getenv("DAILY_TICKER"))
    if args.dry_run:
        print(json.dumps({"tickers": tickers, "mandatory": MANDATORY_DAILY_TICKERS,
                          "gold_proxy": "GLD", "polymarket": MANDATORY_DAILY_TICKERS},
                         ensure_ascii=False, indent=2))
        return 0

    from retry_utils import setup_proxy_bypass

    setup_proxy_bypass()
    (PROJECT_DIR / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(PROJECT_DIR / "logs" / "daily_predict.log", encoding="utf-8")],
    )
    now = datetime.now()
    analysis_date = now.strftime("%Y-%m-%d")
    config = build_config()
    print(f"\n运行时间: {now:%Y-%m-%d %H:%M}\n分析日期: {analysis_date}")
    print(f"分析标的: {', '.join(tickers)}\n固定每日: SPY, QQQ, GLD（黄金ETF代理）")
    failed = run_daily_predictions(
        tickers, analysis_date, now, config, PROJECT_DIR / "results" / "daily")
    if failed:
        print(f"\n⚠️ 完成，{len(failed)}/{len(tickers)} 只标的失败: {', '.join(failed)}")
        return 1
    print(f"\n🎉 全部 {len(tickers)} 只标的预测完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
