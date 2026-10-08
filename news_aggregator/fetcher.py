"""
个股大事件新闻流聚合器 - 数据抓取模块（网络容错版）
支持 A股 / 港股 / 美股，网络中断自动重试
"""
import os
import sys
import time
from datetime import datetime
from typing import Any

import pandas as pd

# 🔧 代理绕过（必须在网络请求前设置）
_BYPASS_DOMAINS = "api.deepseek.com,push2.eastmoney.com,eastmoney.com,yahoo.com"
for _key in ("NO_PROXY", "no_proxy"):
    _existing = os.environ.get(_key, "")
    if _existing and _existing != "localhost,127.0.0.1,::1":
        missing = [d for d in _BYPASS_DOMAINS.split(",") if d not in _existing]
        if missing:
            os.environ[_key] = f"{_existing},{','.join(missing)}"
    else:
        os.environ[_key] = f"localhost,127.0.0.1,::1,{_BYPASS_DOMAINS}"

from config import STOCKS, MAJOR_EVENT_KEYWORDS, MAX_NEWS_PER_STOCK

# ── 重试配置 ──
FETCH_RETRIES = 3
FETCH_RETRY_DELAY = 3.0  # 基础延迟（秒）


def _is_network_error(exc: Exception) -> bool:
    """判断异常是否为网络相关"""
    err_str = f"{type(exc).__name__} {exc}".lower()
    for kw in ["timeout", "connect", "refused", "reset", "eof", "ssl",
               "network", "proxy", "remote", "broken", "503", "502",
               "timed out", "unreachable", "tunnel"]:
        if kw in err_str:
            return True
    return False


def _get_symbol(code: str, market: str) -> str:
    """转换股票代码为 akshare stock_news_em 所需格式"""
    if market == "A股":
        return code          # 如 "600519"
    elif market == "港股":
        return f"hk{code}"   # 如 "hk00700"
    else:  # 美股
        return code          # 如 "AAPL"


def fetch_stock_news(code: str, name: str, market: str) -> list[dict[str, Any]]:
    """获取个股新闻 — 带网络重试"""
    last_error = None
    for attempt in range(FETCH_RETRIES + 1):
        try:
            import akshare as ak
            symbol = _get_symbol(code, market)
            df = ak.stock_news_em(symbol=symbol)

            if df is None or df.empty:
                return []

            df = df.head(MAX_NEWS_PER_STOCK)
            return _parse(df, code, name, market)

        except Exception as e:
            last_error = e
            if not _is_network_error(e) or attempt >= FETCH_RETRIES:
                break
            delay = FETCH_RETRY_DELAY * (2 ** attempt)
            time.sleep(delay)

    if last_error:
        err_msg = str(last_error)[:60]
        print(f"  [WARN] {market} {name}({code}) 获取失败（已重试{FETCH_RETRIES}次）: {err_msg}")
    return []


def _parse(df: pd.DataFrame, code: str, name: str, market: str) -> list[dict[str, Any]]:
    """解析东方财富新闻 DataFrame（列: 关键词/新闻标题/新闻内容/发布时间/文章来源/新闻链接）"""
    news = []
    for _, row in df.iterrows():
        try:
            title = str(row.get("新闻标题", ""))
            if not title or title in ("nan", ""):
                continue

            time_str = str(row.get("发布时间", ""))
            pub_time = _parse_time(time_str)

            news.append({
                "title": title.strip(),
                "content": str(row.get("新闻内容", ""))[:200],
                "source": str(row.get("文章来源", "")).strip(),
                "time": pub_time,
                "url": str(row.get("新闻链接", "")).strip(),
                "stock_code": code,
                "stock_name": name,
                "market": market,
            })
        except Exception:
            continue
    return news


def _parse_time(time_str: str) -> str:
    """解析时间字符串"""
    if not time_str or time_str == "nan":
        return datetime.now().strftime("%Y-%m-%d %H:%M")
    ts = str(time_str).strip()
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts[:16] if len(ts) >= 16 else ts


def is_major_event(title: str) -> bool:
    """根据关键词判断是否为重大事件"""
    t = title.lower()
    return any(kw.lower() in t for kw in MAJOR_EVENT_KEYWORDS)


def fetch_all_news() -> dict[str, list[dict[str, Any]]]:
    """抓取所有市场所有自选股新闻（单品失败不中断整体）"""
    all_news: dict[str, list[dict[str, Any]]] = {"A股": [], "港股": [], "美股": []}

    total = sum(len(v) for v in STOCKS.values())
    done = 0
    failed = 0

    for market, stocks in STOCKS.items():
        print(f"\n{'─'*50}")
        print(f"  [{market}] 开始获取...")
        for stock in stocks:
            done += 1
            code, name = stock["code"], stock["name"]
            print(f"  [{done:2d}/{total}] {market} {name}({code})...", end=" ")
            news = fetch_stock_news(code, name, market)
            if news:
                print(f"{len(news)} 条")
                all_news[market].extend(news)
            else:
                failed += 1
                print("0 条（失败）")
            time.sleep(0.3)

    if failed > 0:
        print(f"\n  ⚠️ {failed}/{total} 只股票新闻获取失败，已跳过")
    return all_news


def classify_news(all_news: dict) -> tuple[list, list]:
    """分类：大事件 vs 普通新闻，按时间倒序"""
    major, normal = [], []
    for news_list in all_news.values():
        for item in news_list:
            item["is_major"] = is_major_event(item.get("title", ""))
            (major if item["is_major"] else normal).append(item)

    major.sort(key=lambda x: x.get("time", ""), reverse=True)
    normal.sort(key=lambda x: x.get("time", ""), reverse=True)
    return major, normal
