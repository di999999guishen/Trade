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

from flow_config import TRACKED_ETFS, BAD_NEWS_WEIGHT, NEWS_WINDOW_DAYS  # noqa: E402
from fetcher import fetch_stock_news, is_major_event   # noqa: E402
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
    news_items = []
    seen_titles: set[tuple[str, str]] = set()
    dropped_stale = 0
    # 每个成分股的情绪分列表，最后按指数权重加权平均
    per_stock_scores: dict[str, list[float]] = {}

    for c in top:
        cnews = fetch_stock_news(c["code"], c["name"], market)
        w = c.get("weight") or 0.0
        for n in cnews:
            # 1) 时间窗过滤：超过 NEWS_WINDOW_DAYS 天的旧闻丢弃
            nt = _parse_news_time(n.get("time", ""))
            if nt is not None and nt.timestamp() < cutoff:
                dropped_stale += 1
                continue
            # 2) 去重：同一股票的同一标题只算一次（板块新闻会被多只股票命中）
            key = (c["code"], (n.get("title") or "").strip())
            if key in seen_titles:
                continue
            seen_titles.add(key)

            sent = classify_sentiment(n.get("title", ""))
            s = 1 if sent == "good" else (-BAD_NEWS_WEIGHT if sent == "bad" else 0)
            n["constituent_code"] = c["code"]
            n["constituent_name"] = c["name"]
            n["constituent_weight"] = w
            n["sentiment"] = sent
            n["is_major"] = is_major_event(n.get("title", ""))
            n["weighted_sentiment"] = round(w * s, 4)
            per_stock_scores.setdefault(c["code"], []).append(s)
            news_items.append(n)

    # 按权重×情绪排序，权重高的坏消息排前
    news_items.sort(key=lambda n: n.get("weighted_sentiment", 0))

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

    return {
        "ticker": etf["ticker"],
        "name": name,
        "index_name": etf.get("index_name", ""),
        "index_code": index_code,
        "ok": True,
        "constituent_count": len(cons),
        "top_constituents": [{"code": c["code"], "name": c["name"], "weight": c["weight"]} for c in top],
        "rest_count": len(rest),
        "news": news_items,
        "news_count": len(news_items),
        "dropped_stale": dropped_stale,
        "covered_constituents": len(per_stock_scores),
        "weighted_score": weighted_score,
        "per_stock_sentiment": per_stock_report,
        "bias": ("偏空" if (weighted_score or 0) < 0 else
                 ("偏多" if (weighted_score or 0) > 0 else "中性")),
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
            print(f"    成分股 {r['constituent_count']} 只，"
                  f"近{NEWS_WINDOW_DAYS}日新闻 {r['news_count']} 条"
                  f"（过滤旧闻 {r['dropped_stale']} 条，覆盖 {r['covered_constituents']} 只），"
                  f"加权情绪分 {r['weighted_score']}（{r['bias']}）")
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
