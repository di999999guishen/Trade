"""
个股大事件新闻流聚合器 - 数据抓取模块（网络容错版）
支持 A股 / 港股 / 美股，网络中断自动重试
"""
import os
import re
import sys
import time
from datetime import datetime
from difflib import SequenceMatcher
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


def is_title_relevant(title: str, code: str, name: str) -> bool:
    """判断一条新闻是否真的在讲这只标的，而非大盘综述 / 同板块别家公司的稿件。

    为何需要：东方财富 stock_news_em 是按关键词/板块返回的，会混入
    「A股收评」「主力资金净流出」「实控人遭留置（其实是富祥股份）」这类
    与目标股票无关的稿件。不校验就会把它们算到目标股票（如贵州茅台 8.6% 权重）
    头上，直接污染「成分股加权情绪分」。

    规则：标题含股票代码，或含股票全称 → 视为相关（宽松版，只做粗筛不做语义判断）。
    """
    t = (title or "").strip()
    if not t:
        return False
    if code and str(code) in t:
        return True
    if name and name in t:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 二级市场「行情 / 资金」复盘类稿件的特征词
#
# 为什么必须单独拦（2026-10-16 实测）：这类稿标题里常直接带成分股名，
# 能穿过 is_title_relevant，但主语其实是「概念板块 / 行业资金」而不是这家公司：
#   「中芯国际概念下跌3.31%，主力资金净流出69股」 → 被误记为 中芯国际 的利空（-2）
#   「工商银行股价创历史新高，银行板块集体走强」  → 被误记为 工商银行 的利好
# 后半条尤其危险：它描述的是「当日已发生的涨跌」，喂给预测模型等于诱导动量外推
# （把今天涨直接推到明天涨），必须剔除。
#
# 刻意「不」包含的裸词：涨停 / 跌停 / 上涨 / 下跌 / 创新高 / 领涨 / 居前
#   —— 它们会出现在真正的行业驱动稿里（「焦煤期货上涨」「秦皇岛煤价创新高」），
#      误伤这些比漏过几条资金流稿代价更大。
# ─────────────────────────────────────────────────────────────────────────────
MARKET_ACTION_TOKENS: tuple[str, ...] = (
    # 资金 / 筹码
    "主力资金", "净流入", "净流出", "资金流入", "资金流出", "资金流向",
    "抢筹", "龙虎榜", "盘后交易", "换手率", "两融余额", "融资余额", "融资客",
    "北向资金", "机构席位", "股东户数", "筹码集中", "筹码趋向", "筹码新动向",
    # 榜单 / 市值排名
    "资金流入榜", "资金流出榜", "涨幅榜", "跌幅榜",
    "市值第一", "市值跌破", "市值排名", "重回A股", "股王", "高价股",
    # 行情复盘
    "收评", "收涨", "收跌", "两市成交", "板块集体走强", "板块集体下跌",
    "股价创历史新高", "股价创历史新低", "股价创新高", "股价创新低",
    "股价腰斩", "涨停潮", "跌停潮",
    # 概念 / 板块涨跌
    "概念涨", "概念跌", "概念走强", "概念走弱", "概念活跃",
)


def is_market_action_title(title: str) -> bool:
    """标题是否为「行情/资金复盘」类稿件（非公司经营消息）。"""
    t = title or ""
    return any(kw in t for kw in MARKET_ACTION_TOKENS)


# ─────────────────────────────────────────────────────────────────────────────
# 同事件去重
#
# 为什么需要（2026-10-16 实测）：同一条公司事件会被多家媒体改写重发。
# 海光信息「前三季度净利预增 38%~52%」一次事件，在单只股票的 10 条新闻里出现 6 条
# 不同措辞的标题；兆易创新回购 2 条、中微公司增资 2 条、长江电力发电量 2 条。
# 危害不只是「报告里刷屏」——同事件被重复计入会让某只股票的情绪均值失真：
# 1 条利空 + 3 条同事件利好的措辞副本 → 均值被摊薄成中性，坏消息被稀释掉。
#
# 阈值来源（实测，非拍脑袋）：对真实标题做「应合并 / 应保留」两组采样，
#   应合并组（8 对同事件改写稿）相似度最低 0.275、均值 0.646；
#   应保留组（6 对同公司不同事件）相似度最高 0.217。
#   故取 0.25 作为分界线，落在可分区间 (0.217, 0.275] 内。
# 已知局限：字面高度相似但实为两件事的稿件（如同一公司两个规模不同的新建项目）
#   可能被误并成一条。相比「重复稀释坏消息」的危害，这个代价可接受。
# ─────────────────────────────────────────────────────────────────────────────
_FULL2HALF = str.maketrans("０１２３４５６７８９％～—－　", "0123456789%~-- ")
_TITLE_NOISE = re.compile(r"[\s，。、：；？！%（）()\[\]【】\-—·,.\"'“”‘’]+")

DUP_SIM_THRESHOLD = 0.25


def normalize_title(title: str) -> str:
    """标题归一化：全角转半角、波浪线/横线统一、去标点空白、转小写。

    目的：让「同比增38.16%-51.93%」与「同比增长38.16%~51.93%」这类
    只差标点的改写稿归一到同一串，供同事件判定使用。
    """
    s = (title or "").translate(_FULL2HALF)
    for ch in ("～", "—", "－"):
        s = s.replace(ch, "-")
    s = s.replace("~", "-")
    return _TITLE_NOISE.sub("", s).lower()


def is_same_event(title_a: str, title_b: str) -> bool:
    """两条标题是否在讲同一件事（同一档改写稿）。"""
    a, b = normalize_title(title_a), normalize_title(title_b)
    if not a or not b:
        return False
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= DUP_SIM_THRESHOLD


def merge_sentiment(a: str | None, b: str | None) -> str:
    """合并同一事件被分类成不同情绪时的取值：取更严重的一档。

    bad > good > neutral。实测同一事件的多个措辞副本会被关键词分类器判成
    不同情绪（海光信息业绩预增：一条 good、一条 neutral），取更靠前的档位，
    避免「靠哪条稿先到」决定情绪。
    """
    order = {"bad": 0, "good": 1, "neutral": 2}
    if a not in order:
        return b or "neutral"
    if b not in order:
        return a
    return a if order[a] <= order[b] else b


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
