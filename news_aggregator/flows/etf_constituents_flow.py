"""
ETF 成分股权重新闻流 (ETF Constituent Flow)
==========================================
解决痛点：TradingAgents 现在对「ETF 本身的新闻流」叙述很少，尤其缺少成分股层面的消息。

做法：
  1. 对每个 active 的 ETF，用 akshare `index_stock_cons_weight_csindex` 取跟踪指数的成分股 + 权重。
  2. 取权重最高的前 top_n 只成分股，复用现有 `fetcher.fetch_stock_news` 抓个股新闻。
  3. 每条新闻标注「所属成分股权重」，按 sentiment 约定给坏消息 2 倍权重，
     汇总得到该 ETF 的「加权消息面分」，直接可用于每日分析 515220 等。

依赖：news_aggregator/config.py / fetcher.py / sentiment.py（同目录上层）。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_BASE = str(Path(__file__).resolve().parent.parent)   # news_aggregator/
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from flow_config import (                                  # noqa: E402
    TRACKED_ETFS, BAD_NEWS_WEIGHT, NEWS_WINDOW_DAYS, SECTOR_KEYWORD_OVERRIDES,
)
from fetcher import (                                    # noqa: E402
    fetch_stock_news, is_major_event, is_title_relevant,
    is_market_action_title, is_same_event, merge_sentiment,
)
from sentiment import classify_sentiment               # noqa: E402


def _parse_news_time(t: str) -> datetime | None:
    """解析新闻时间字符串，失败返回 None。兼容 '2026-10-14 16:58' / '2026-10-14'。"""
    if not t:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(t).strip()[:19], fmt)
        except Exception:
            continue
    return None


def _get_constituents(index_code: str) -> list[dict] | None:
    """取指数成分股 + 权重。失败返回 None。"""
    if not index_code:
        return None
    try:
        import akshare as ak
        df = ak.index_stock_cons_weight_csindex(symbol=index_code)
        out = []
        for _, r in df.iterrows():
            code = str(r.get("成分券代码", "")).strip()
            name = str(r.get("成分券名称", "")).strip()
            w = r.get("权重")
            try:
                w = float(w)
            except Exception:
                w = None
            if code and name:
                out.append({"code": code, "name": name, "weight": w})
        return out if out else None
    except Exception as e:
        print(f"  [WARN] 取指数 {index_code} 成分股权重失败: {str(e)[:80]}")
        return None


# 指数名里需要剥掉的通用词（剥完剩下的才是「板块关键词」）
_INDEX_NOISE_TOKENS = (
    "中证", "上证", "深证", "国证", "全指", "申万", "科创", "科创板",
    "创业板", "指数", "公用事业", "产业",
)
# 剥完若只剩这类词，说明该指数没有明确的行业含义（宽基），不做板块匹配
_INDEX_GENERIC_CORES = {
    "升级", "成长", "价值", "龙头", "红利", "低波", "质量", "50", "100", "300",
    "500", "1000", "180", "A50", "A股",
}


def _derive_sector_keywords(index_name: str) -> list[str]:
    """从指数名派生板块关键词，如「中证煤炭」→「煤炭」，「中证全指电力公用事业」→「电力」。"""
    s = (index_name or "").strip()
    for tok in _INDEX_NOISE_TOKENS:
        s = s.replace(tok, "")
    s = s.strip()
    if len(s) >= 2 and not s.isdigit() and s.upper() not in _INDEX_GENERIC_CORES:
        return [s]
    return []


def _sector_keywords(etf: dict) -> list[str]:
    """该 ETF 的板块关键词：优先用 flow_config 手工覆盖，否则从指数名派生。"""
    ov = SECTOR_KEYWORD_OVERRIDES.get(etf.get("ticker", ""))
    if ov is not None:
        return list(ov)
    return _derive_sector_keywords(etf.get("index_name", ""))


def _is_sector_news(title: str, sector_kws: list[str]) -> bool:
    """标题是否属于该 ETF 的板块/行业新闻（不点名单只成分股，但整体相关）。"""
    if not sector_kws:
        return False
    t = title or ""
    return any(kw in t for kw in sector_kws)


def _analyze_etf(etf: dict) -> dict[str, Any]:
    name = etf["name"]
    index_code = etf.get("index_code", "")
    market = etf.get("market", "A股")
    top_n = int(etf.get("top_n", 10))

    cons = _get_constituents(index_code)
    if cons is None:
        return {"ticker": etf["ticker"], "name": name, "ok": False,
                "error": "成分股/权重获取失败（指数代码可能不被中证接口覆盖）",
                "constituents": [], "news": [], "weighted_score": None}

    cons.sort(key=lambda c: (c["weight"] or 0), reverse=True)
    top = cons[:top_n]
    rest = cons[top_n:]

    now = datetime.now()
    cutoff = now.timestamp() - NEWS_WINDOW_DAYS * 86400
    sector_kws = _sector_keywords(etf)

    direct_news: list[dict] = []          # 个股直接新闻（可归因到具体成分股），去重后生成
    sector_map: dict[str, dict] = {}      # 板块新闻（按标题去重，不归因到单只股票）
    kept_by_stock: dict[str, list[dict]] = {}   # 每只成分股已保留的新闻（供同事件去重比对）
    dropped_stale = 0
    dropped_irrelevant = 0
    dropped_market = 0
    dup_merged = 0

    for c in top:
        cnews = fetch_stock_news(c["code"], c["name"], market)
        w = c.get("weight") or 0.0
        for n in cnews:
            # 1) 时间窗过滤：超过 NEWS_WINDOW_DAYS 天的旧闻丢弃
            nt = _parse_news_time(n.get("time", ""))
            if nt is not None and nt.timestamp() < cutoff:
                dropped_stale += 1
                continue

            title = (n.get("title") or "").strip()
            if not title:
                continue

            # 2) 行情/资金复盘类稿件：标题带了成分股名，但讲的是概念板块或当日涨跌，
            #    不是这家公司的经营消息，且会把「今天涨」诱导成「明天涨」→ 剔除并单独计数。
            if is_market_action_title(title):
                dropped_market += 1
                continue

            # 3) 分层相关性判定
            if is_title_relevant(title, c["code"], c["name"]):
                # ① 直接相关：标题真的讲了这只成分股 → 可归因、按权重计分
                sent = classify_sentiment(title)
                bucket = kept_by_stock.setdefault(c["code"], [])
                dup = next((x for x in bucket if is_same_event(x["title"], title)), None)
                if dup is not None:
                    # 同事件改写稿：不新增条目，只升级情绪（bad>good>neutral），
                    # 信息更全的标题（更长）覆盖短标题。
                    dup_merged += 1
                    dup["sentiment"] = merge_sentiment(dup.get("sentiment"), sent)
                    if len(title) > len(dup["title"]):
                        dup["title"] = title
                        dup["url"] = n.get("url", "")
                        dup["time"] = n.get("time", "")
                    continue
                n["scope"] = "个股"
                n["constituent_code"] = c["code"]
                n["constituent_name"] = c["name"]
                n["constituent_weight"] = w
                n["sentiment"] = sent
                bucket.append(n)
            elif _is_sector_news(title, sector_kws):
                # ② 板块相关：只作 ETF 级参考，**不**归因到某只成分股（避免张冠李戴）
                n["constituent_code"] = None      # 板块稿可能先被 A 股的抓取命中，
                n["constituent_name"] = None      # 会把 A 股的名字留在字段里造成误读
                n["constituent_weight"] = None
                sector_map.setdefault(title, n)
            else:
                # ③ 大盘综述 / 同板块别家公司稿件 → 丢弃
                dropped_irrelevant += 1

    # ── 去重完毕后统一打分 ──
    # 必须在去重之后算：合并同事件会升级情绪，若在合并前计分，分数会停留在旧情绪上。
    per_stock_scores: dict[str, list[float]] = {}
    for c in top:
        w = c.get("weight") or 0.0
        for n in kept_by_stock.get(c["code"], []):
            sent = n.get("sentiment", "neutral")
            s = 1 if sent == "good" else (-BAD_NEWS_WEIGHT if sent == "bad" else 0)
            n["is_major"] = is_major_event(n.get("title", ""))
            n["weighted_sentiment"] = round(w * s, 4)
            per_stock_scores.setdefault(c["code"], []).append(s)
            direct_news.append(n)

    # 个股直接新闻：按权重×情绪排序，权重高的坏消息排前
    direct_news.sort(key=lambda n: n.get("weighted_sentiment", 0))

    # 板块新闻：按标题去重后逐条判情绪，得到板块情绪均分（∈[-2,+1]）
    sector_news: list[dict] = []
    _sec_scores: list[float] = []
    for title, n in sector_map.items():
        sent = classify_sentiment(title)
        s = 1 if sent == "good" else (-BAD_NEWS_WEIGHT if sent == "bad" else 0)
        n["scope"] = "板块"
        n["sentiment"] = sent
        n["is_major"] = is_major_event(title)
        _sec_scores.append(s)
        sector_news.append(n)
    sector_news.sort(key=lambda n: n.get("sentiment", ""))
    sector_score = round(sum(_sec_scores) / len(_sec_scores), 4) if _sec_scores else None

    # 加权情绪分：先算每只成分股的平均情绪（∈[-2,+1]），再按指数权重加权平均。
    # 这样结果落在 [-2,+1] 可解读区间，不会被新闻条数放大。
    wsum = 0.0
    score = 0.0
    per_stock_report = []
    for c in top:
        scores = per_stock_scores.get(c["code"]) or []
        if not scores:
            continue
        avg = sum(scores) / len(scores)
        w = c.get("weight") or 0.0
        wsum += w
        score += w * avg
        per_stock_report.append({
            "code": c["code"], "name": c["name"], "weight": w,
            "news_count": len(scores), "avg_sentiment": round(avg, 3),
        })
    weighted_score = round(score / wsum, 4) if wsum else None
    per_stock_report.sort(key=lambda x: x["avg_sentiment"])

    # 置信度：直接相关的成分股太少时，加权分不具代表性。
    # 防止「仅 1 只股票 2 条新闻 => 偏空」这类垃圾结论被当成有效信号。
    covered = len(per_stock_scores)
    if covered >= 5 and wsum >= 30:
        confidence = "高"
    elif covered >= 3 and wsum >= 15:
        confidence = "中"
    else:
        confidence = "低"

    if weighted_score is None:
        bias = "无数据"
    elif confidence == "低":
        bias = "样本不足"          # 不给出方向性结论，避免误用
    elif weighted_score < 0:
        bias = "偏空"
    elif weighted_score > 0:
        bias = "偏多"
    else:
        bias = "中性"

    if sector_score is None:
        sector_bias = "无数据"
    elif sector_score < 0:
        sector_bias = "偏空"
    elif sector_score > 0:
        sector_bias = "偏多"
    else:
        sector_bias = "中性"

    return {
        "ticker": etf["ticker"],
        "name": name,
        "index_name": etf.get("index_name", ""),
        "index_code": index_code,
        "ok": True,
        "constituent_count": len(cons),
        "top_constituents": [{"code": c["code"], "name": c["name"], "weight": c["weight"]} for c in top],
        "rest_count": len(rest),
        "news": direct_news,
        "news_count": len(direct_news),
        "sector_news": sector_news,
        "sector_news_count": len(sector_news),
        "dropped_stale": dropped_stale,
        "dropped_irrelevant": dropped_irrelevant,
        "dropped_market_action": dropped_market,
        "dup_merged": dup_merged,
        "covered_constituents": covered,
        "weighted_score": weighted_score,
        "confidence": confidence,
        "sector_score": sector_score,
        "sector_bias": sector_bias,
        "per_stock_sentiment": per_stock_report,
        "bias": bias,
    }


def fetch_etf_constituent_flow() -> dict[str, Any]:
    print("\n[ETF成分股流] 逐个 ETF 取成分股权重新闻...")
    results = []
    for etf in TRACKED_ETFS:
        if not etf.get("active", True):
            print(f"  ⏭️  {etf['name']} 未启用（active=False）")
            continue
        print(f"  ▶ {etf['name']} ({etf['ticker']}) ...")
        r = _analyze_etf(etf)
        if r["ok"]:
            print(f"    成分股 {r['constituent_count']} 只｜"
                  f"个股相关新闻 {r['news_count']} 条（覆盖 {r['covered_constituents']} 只）"
                  f"｜板块新闻 {r['sector_news_count']} 条")
            print(f"    剔除 旧闻{r['dropped_stale']}/无关{r['dropped_irrelevant']}"
                  f"/行情复盘{r['dropped_market_action']}｜同事件合并 {r['dup_merged']} 条")
            print(f"    个股加权分 {r['weighted_score']}（{r['bias']}，置信度{r['confidence']}）"
                  f"｜板块分 {r['sector_score']}（{r['sector_bias']}）")
        else:
            print(f"    ⚠️ {r['error']}")
        results.append(r)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "etfs": results,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_etf_constituent_flow(), ensure_ascii=False, indent=2, default=str))
