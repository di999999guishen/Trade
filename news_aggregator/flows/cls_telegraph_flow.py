"""
财联社电报快讯流（CLS Telegraph Flow）
======================================
为什么加这条流（2026-10-20 实测对比，不是猜测）：
  现有 etf_constituents_flow 走 akshare `stock_news_em`（东财资讯检索），
  当日产物 news_flow_2026-10-20.json 里：
    煤炭ETF 5 条新闻，跨度 2026-10-09 ~ 10-20
    上证50ETF 12 条新闻，跨度 2026-10-12 ~ 10-20
    sentiment_score 全为 None（加权分没算出来）
  本质是「历史资讯关键词检索」，不是「实时快讯流」——今晚跑出来最旧的一条是 11 天前。

  财联社电报是 7x24 分钟级快讯，且自带三个结构化标签：
    level      重要度（A/B/C，实测夜间 B 约占 20%）
    subjects   主题标签（"锂电池"/"A股公告速递"/"能源类期货"…）→ 可直接做板块匹配
    stock_list 关联个股（含 StockID + 实时涨跌幅）→ 可直接做成份股匹配
  这比「标题字符串里找股票名」精确得多，也天然避开「行情复盘稿污染情绪分」的老问题。

接口（无鉴权，2026-10-20 实测 errno=0 可用）：
  GET https://www.cls.cn/v1/roll/get_roll_list
  sign = md5(sha1(urlencode(sorted(params))))
  返回 data.roll_data，按 ctime 倒序。

⚠️ 分页坑（2026-10-20 实测，踩过）：
  只传 rn / last_time 时接口会**忽略 last_time**，永远返回第一页 50 条 ——
  看起来「翻了 N 页共 N×50 条」，实际是同一页重复 N 次。
  必须同时带 `refresh_type=1` 才能生效（重叠 50 → 0，首条 ctime 正确前移）。
  rn 上限也是 50（rn=100 直接返回空数组）。

实测密度：50 条/页，约 10 条/分钟全市场快讯（含海外）。
故不做全量抓取，按「回溯小时数 + 结构化标签过滤」取当日相关部分。

设计约束：
  - 不修改任何现有模块，情绪分类只读复用 sentiment.classify_sentiment
  - 输出结构与 etf_constituents_flow 对齐（etfs[].news[]），便于后续接 report_flow
  - 失败不抛异常，返回 ok=False + error，交上层汇报
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
import statistics
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

_BASE = str(Path(__file__).resolve().parent.parent)   # news_aggregator/
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from sentiment import classify_sentiment, BAD_NEWS_WEIGHT  # noqa: E402
from fetcher import is_market_action_title, is_same_event  # noqa: E402

# ── 接口常量 ──
CLS_URL = "https://www.cls.cn/v1/roll/get_roll_list"
CLS_BASE_PARAMS = {"app": "CailianpressWeb", "os": "web", "sv": "7.7.5"}
CLS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://www.cls.cn/telegraph",
}

# 复盘/资金流类稿件会被财联社大量转发，同样不能进情绪分（与 fetcher 同口径）
# 额外补充财联社特有的「盘面快照」句式
CLS_EXTRA_MARKET_TOKENS = (
    "盘中快照", "收盘快照", "板块异动", "个股异动", "异动快报",
    "两市融资余额", "ETF行情", "今日涨停", "今日跌停",
)

# ── level 权重：重要度直接进入加权分 ──
LEVEL_WEIGHT = {"A": 3, "B": 2, "C": 1}
# 注：不设 level 预过滤。初版按 ("A","B") 先砍 C 级，结果把 8 小时内仅有的
# 两条正相关快讯（豪威集团 4.5 亿回购贷款 / 山西煤炭稳产稳供）一起丢掉了。
# 现改为「先匹配 ETF、再按关联强度决定 level 去留」：
#   强关联（成分股/指数点名/核心主题）不看 level；
#   弱关联只留 B 级以上，C 级弱关联视为纯噪音丢弃。

# ── 跨板块联动折扣 & 市场情绪阈值 ──
SHARED_DISCOUNT = 0.5            # 同一事件命中 ≥2 只 ETF 时，对单只 ETF 加权贡献打五折，
                                  # 避免单条宏观主题把多只 ETF 的置信度同时拉满
MARKET_DECISIVE_PCT = 1.5        # 关联个股涨跌中位数达到该幅度（%）才视为有效情绪信号；
                                  # 区间内的小幅波动 / 涨跌互现判 neutral（市场没给明确方向）

# ── 板块主题映射：财联社 subjects 的 subject_name 关键词 → ETF ──
#
# 分「核心 / 弱相关」两档，理由（2026-10-20 实测踩坑）：
#   初版把「医药」「美股」「能源类期货」这类宽词直接挂上 ETF，结果
#   「礼来股价创历史新高」(subject=医药) 同时命中 589720 / 159748 / 512170 三只，
#   「美油布油跌超2%」(subject=能源类期货) 被算成煤炭ETF 的利好 —— 全是假信号。
#
# 规则：
#   CORE  进情绪分。要求 subject 是该 ETF 的**直接行业**，且事件主体在境内。
#   WEAK  只作参考列示、不进分。宽基沾边、海外映射、大宗同向传导都归这档。
#
# 宽基（510050/513650/513390）刻意**不配 CORE 主题词**：
#   宽基一旦允许主题匹配，「美股」「A股公告」会把全市场快讯都吸过来，
#   失去区分度。宽基只靠 ① stock_list 精确命中成分股 ② 指数名直接点名。
CORE_SUBJECT_MAP: dict[str, list[str]] = {
    "515220.SS": ["煤炭", "焦煤", "动力煤", "煤化工", "煤炭开采", "煤炭行业"],
    "512400.SS": ["有色金属", "工业金属", "铜", "铝", "稀土", "贵金属", "黄金",
                  "白银", "小金属", "锂矿", "钴", "镍", "锗"],
    "159611.SS": ["电力", "电力公用事业", "绿电", "火电", "水电", "核电", "电网"],
    "510160.SS": ["智能制造", "高端制造", "专精特新", "工业母机", "产业升级"],
    "589720.SS": ["创新药", "生物医药", "CXO", "细胞治疗", "基因", "基因测序"],
    "159748.SS": ["创新药", "生物医药", "CXO", "疫苗"],
    "516090.SS": ["新能源", "光伏", "风电", "储能", "锂电池", "锂电", "电池",
                  "氢能", "钠离子电池"],
    "512170.SS": ["医疗器械", "医疗服务", "医药商业", "疫苗", "血制品"],
    "510050.SS": [],    # 宽基：只认成分股 + 指数点名
    "513650.SS": [],
    "513390.SS": [],
}

WEAK_SUBJECT_MAP: dict[str, list[str]] = {
    "515220.SS": ["原油市场动态", "能源类期货", "海外大宗商品", "期货市场情报",
                  "天然气", "中东冲突"],
    "512400.SS": ["海外大宗商品", "期货市场情报"],
    "159611.SS": ["能源行业新闻"],
    "516090.SS": ["碳酸锂", "锂"],
    "589720.SS": ["医药", "医药商业", "减肥药"],
    "159748.SS": ["医药", "医药商业", "减肥药"],
    "512170.SS": ["医药", "医药商业", "减肥药", "生物科技"],
    "513650.SS": ["美股动态", "美股盘前", "美国经济", "美联储", "环球市场情报"],
    "513390.SS": ["半导体芯片", "存储芯片", "人工智能", "算力", "英伟达",
                  "美股科技", "美股动态", "芯片"],
    "510050.SS": ["A股公告速递", "A股风险提示公告", "盘面直播", "ETF最新动态",
                  "大盘蓝筹", "银行", "保险", "券商", "白酒", "中字头", "国企改革"],
}

# 指数点名：快讯正文/标题直接出现 ETF 跟踪的指数或代码时，视为该 ETF 的强信号
INDEX_ALIAS: dict[str, list[str]] = {
    "510050.SS": ["上证50", "510050"],
    "513650.SS": ["标普500", "513650", "S&P 500"],
    "513390.SS": ["纳斯达克", "纳指", "513390"],
    "515220.SS": ["中证煤炭"],
    "512400.SS": ["中证申万有色金属"],
    "159611.SS": ["中证全指电力"],
    "589720.SS": ["科创板创新药"],
    "159748.SS": ["中证创新药产业"],
    "516090.SS": ["中证新能源"],
    "512170.SS": ["中证医疗"],
    "510160.SS": ["上证产业升级"],
    # 2026-11-12 新持仓/预测池：仅补指数名 + 代码精确别名，不配 CORE/WEAK 主题词
    # （宽基与跨境映射一旦放开主题匹配会把全市场快讯吸过来，见上方规则说明）。
    "588940.SS": ["科创50", "科创板50", "588940"],
    "510100.SS": ["上证50", "510100"],
    "159740.SZ": ["恒生科技", "恒生科技指数", "159740"],
    "159792.SZ": ["港股通互联网", "159792"],
}

# 「事件主体在海外」的判定词。命中则一律降为弱关联、不进情绪分：
# A 股 ETF 的次日方向由境内资金定价，海外同业的单日涨跌只构成背景，不是驱动。
OVERSEAS_TOKENS = ("美股", "环球市场", "海外", "美国经济", "美联储", "欧洲", "日本",
                   "英国", "德国", "法国", "原油市场", "国际油价", "地缘", "中东")


def _sign(params: dict) -> str:
    q = urllib.parse.urlencode(sorted(params.items()))
    return hashlib.md5(hashlib.sha1(q.encode()).hexdigest().encode()).hexdigest()


def fetch_raw(hours_back: float = 8.0, max_pages: int = 160,
              rn: int = 50, verbose: bool = False) -> list[dict]:
    """回溯抓取财联社快讯原始流（按 ctime 倒序，翻页直到越过时间窗）。

    hours_back: 回溯小时数。默认 8 小时 ≈ 覆盖「当日收盘 15:00 → 深夜 23:00」。
    max_pages : 翻页上限，防御性熔断（实测 1 页 50 条 ≈ 5 分钟，8 小时约 96 页）。
    """
    cutoff = time.time() - hours_back * 3600
    out: list[dict] = []
    seen_ids: set = set()
    last_time = ""

    for page in range(max_pages):
        params = dict(CLS_BASE_PARAMS)
        params["rn"] = str(rn)
        params["last_time"] = str(last_time)
        params["refresh_type"] = "1"      # 必须带，否则 last_time 被忽略、永远回第一页
        params["sign"] = _sign(params)
        try:
            r = requests.get(CLS_URL, params=params, headers=CLS_HEADERS, timeout=20)
            if r.status_code != 200:
                if verbose:
                    print(f"  [CLS] 第 {page+1} 页 HTTP {r.status_code}，停止翻页")
                break
            payload = r.json()
            if payload.get("errno") != 0:
                if verbose:
                    print(f"  [CLS] 第 {page+1} 页 errno={payload.get('errno')}，停止翻页")
                break
            batch = (payload.get("data") or {}).get("roll_data") or []
        except Exception as e:
            if verbose:
                print(f"  [CLS] 第 {page+1} 页异常 {type(e).__name__}: {str(e)[:80]}")
            break

        if not batch:
            break

        for item in batch:
            cid = item.get("id")
            if cid in seen_ids:
                continue          # 翻页边界会重复返回，实测「南威软件」跨页重复出现
            seen_ids.add(cid)
            out.append(item)

        last_ctime = batch[-1].get("ctime") or 0
        if last_ctime and last_ctime < cutoff:
            break
        last_time = last_ctime

    return out


def _fmt_time(ctime) -> str:
    try:
        return datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _title_of(item: dict) -> str:
    """快讯标题：有 title 用 title，纯电报只有 brief/content，取首句。"""
    t = (item.get("title") or "").strip()
    if t:
        return t
    brief = (item.get("brief") or item.get("content") or "").strip()
    # 「财联社10月20日电，」这类前缀去掉，只留正文首句
    brief = brief.replace("财联社", "", 1)
    for sep in ("。", "，", "；"):
        if sep in brief[:80]:
            return brief.split(sep)[0][:80]
    return brief[:80]


def _is_noise(title: str) -> bool:
    if not title:
        return True
    if is_market_action_title(title):
        return True
    return any(tok in title for tok in CLS_EXTRA_MARKET_TOKENS)


def _valid_stock_changes(item: dict) -> list[tuple[str, float]]:
    """从 stock_list 提取有效实时涨跌幅（RiseRange）。

    财联社每条关联个股自带 RiseRange（如 南威软件 10.02）。这是市场用真金白银
    投出的方向，比关键词猜「下跌/增加」可靠得多。
    - RiseRange 为 0 → 无信号（不纳入）；
    - rise_range_has_null 字段含义不稳（实测返回 0.1002 但 RiseRange 有效），
      故不依赖它，只要求 RiseRange 是数值且非 0。
    """
    out = []
    for s in (item.get("stock_list") or []):
        r = s.get("RiseRange")
        try:
            r = float(r)
        except (TypeError, ValueError):
            continue
        if r == 0.0:
            continue
        out.append((s.get("name") or "", r))
    return out


def _resolve_sentiment(item: dict) -> tuple[str, str, list[tuple[str, float]]]:
    """情绪判定：优先用关联个股的实时涨跌（市场已投票），否则回退关键词。

    返回 (sentiment, source, changes)。
      - 关联个股涨跌中位数 |mid| ≥ MARKET_DECISIVE_PCT → good / bad
      - 在区间内 或 涨跌互现（中位近 0）→ neutral（市场未给明确方向）
      - 无有效个股数据 → classify_sentiment 关键词兜底（覆盖纯宏观/政策稿）
    """
    changes = _valid_stock_changes(item)
    if changes:
        vals = sorted(v for _, v in changes)
        mid = statistics.median(vals)
        if mid >= MARKET_DECISIVE_PCT:
            return "good", "market", changes
        if mid <= -MARKET_DECISIVE_PCT:
            return "bad", "market", changes
        return "neutral", "market", changes
    title = item.get("_title") or _title_of(item)
    return classify_sentiment(title), "keyword", changes


def _match_etfs(item: dict, constituents: dict[str, set[str]] | None) -> list[dict]:
    """把一条快讯映射到 ETF。返回 [{ticker, via, strength, detail}]。

    命中依据优先级：成分股 > 指数点名 > 核心主题 > 弱主题。
    strength 为 "strong" 的才进情绪分，"weak" 仅作参考列示。
    """
    hits: list[dict] = []
    title = _title_of(item)
    subject_names = [str(s.get("subject_name") or "") for s in (item.get("subjects") or [])]
    # 海外主体判定：主题标签或标题带海外词 → 全部降级
    overseas = any(tok in " ".join(subject_names + [title]) for tok in OVERSEAS_TOKENS)

    # ① 最强证据：stock_list 直接关联个股 → 成分股（个股本身是境内的，不受 overseas 影响）
    for s in (item.get("stock_list") or []):
        raw_id = (s.get("StockID") or "").lower()          # sh603636 / sz300750
        code = raw_id[2:] if len(raw_id) > 2 else raw_id
        if constituents:
            for ticker, codes in constituents.items():
                if code in codes:
                    hits.append({"ticker": ticker, "via": "成分股", "strength": "strong",
                                 "detail": f"{s.get('name')}({code})"})

    # ② 指数点名：标题或正文直接出现该 ETF 的指数名/代码
    for ticker, aliases in INDEX_ALIAS.items():
        if any(a in title for a in aliases):
            hits.append({"ticker": ticker, "via": "指数点名", "strength": "strong",
                         "detail": next(a for a in aliases if a in title)})

    # ③ 核心主题：subject 是该 ETF 的直接行业
    for ticker, kws in CORE_SUBJECT_MAP.items():
        for sn in subject_names:
            if any(kw == sn or kw in sn for kw in kws):
                hits.append({"ticker": ticker,
                             "via": "主题",
                             "strength": "weak" if overseas else "strong",
                             "detail": sn})
                break

    # ④ 弱相关主题：宽基沾边 / 大宗传导 / 海外映射，永不进分
    for ticker, kws in WEAK_SUBJECT_MAP.items():
        for sn in subject_names:
            if any(kw == sn or kw in sn for kw in kws):
                hits.append({"ticker": ticker, "via": "弱主题", "strength": "weak",
                             "detail": sn})
                break

    # 同一 ticker 多路命中只留最强、最靠前那路
    prio = {"成分股": 0, "指数点名": 1, "主题": 2, "弱主题": 3}
    uniq: dict[str, dict] = {}
    for h in hits:
        cur = uniq.get(h["ticker"])
        if cur is None:
            uniq[h["ticker"]] = h
        elif h["strength"] == "strong" and cur["strength"] == "weak":
            uniq[h["ticker"]] = h
        elif h["strength"] == cur["strength"] and prio[h["via"]] < prio[cur["via"]]:
            uniq[h["ticker"]] = h
    return list(uniq.values())


def fetch_cls_flow(hours_back: float = 8.0,
                   constituents: dict[str, set[str]] | None = None,
                   etf_names: dict[str, str] | None = None,
                   verbose: bool = True) -> dict:
    """抓取 + 过滤 + 归类，返回按 ETF 聚合的快讯流。

    constituents: {ticker: {成分股代码集合}}，可选。有则启用成分股精确匹配。
    etf_names   : {ticker: 显示名}，可选。
    """
    t0 = time.time()
    raw = fetch_raw(hours_back=hours_back, verbose=verbose)
    elapsed = time.time() - t0

    # 只做噪音过滤，不限 level —— level 去留放到「匹配之后」判定，理由见下。
    # 实测（2026-10-20）：8 小时 306 条 C 级里只有 2 条有价值
    #   「豪威集团：取得不超过4.5亿元回购专项贷款承诺函」(上证50 成分股)
    #   「山西多管齐下推进复工复产 煤炭稳产稳供成效显现」(煤炭ETF)
    # 若先按 level 砍掉 C 级，这 2 条会被丢；若全留 C 级，又会被 300 条噪音灌满。
    # 正解：C 级只在「精确命中我的 ETF」时保留 —— 财联社的 level 是全市场口径，
    # 不是「对我这只 ETF 重不重要」。
    kept, noise = [], 0
    all_events = []
    for item in raw:
        title = _title_of(item)
        if _is_noise(title):
            noise += 1
            continue
        item["_title"] = title
        kept.append(item)

    # 同事件去重（复用 fetcher 的相似度阈值，财联社同一条也常被改写重发）
    deduped: list[dict] = []
    for item in kept:
        if any(is_same_event(item["_title"], d["_title"]) for d in deduped):
            continue
        deduped.append(item)

    buckets: dict[str, dict] = {}
    unmatched = []
    linked: list[dict] = []          # 跨板块联动事件（共享事件只归集一次，避免重复计权）
    for item in deduped:
        title = item["_title"]
        sentiment, src, changes = _resolve_sentiment(item)
        level = item.get("level") or "C"
        w = LEVEL_WEIGHT.get(level, 1)
        base = {
            "title": title,
            "time": _fmt_time(item.get("ctime")),
            "level": level,
            "level_weight": w,
            "sentiment": sentiment,
            "sentiment_src": src,
            "stock_changes": [f"{n}{v:+.2f}%" for n, v in changes],
            "subject": next((str(s.get("subject_name")) for s in (item.get("subjects") or [])
                             if s.get("subject_name")), ""),
            "stocks": [s.get("name") for s in (item.get("stock_list") or []) if s.get("name")],
            "url": f"https://www.cls.cn/detail/{item.get('id')}",
            "id": item.get("id"),
            "shared": False,
        }
        # Retain the deduplicated stream for this run's dynamically screened ETFs.
        # Holding-only buckets cannot cover a changing whole-market candidate set.
        all_events.append(dict(base))
        hits = _match_etfs(item, constituents)
        if not hits:
            unmatched.append(base)
            continue
        # 先算「存活命中」（弱关联 + C 级被丢弃的不计入联动计数）
        surviving = [h for h in hits
                    if not (h["strength"] == "weak" and level == "C")]
        # 联动/折扣只看「强关联跨 ETF」——弱关联不进分，谈不上置信度膨胀
        strong_tickers = {h["ticker"] for h in surviving if h["strength"] == "strong"}
        shared = len(strong_tickers) >= 2
        base["shared"] = shared
        if shared:
            linked.append({
                "title": title, "time": base["time"], "level": level,
                "sentiment": sentiment, "sentiment_src": src,
                "tickers": sorted(strong_tickers),
                "url": base["url"],
            })
        for h in surviving:
            b = buckets.setdefault(h["ticker"], {"strong": [], "weak": []})
            key = "strong" if h["strength"] == "strong" else "weak"
            b[key].append({**base, "via": h["via"], "match_detail": h["detail"]})

    etfs = []
    for ticker, b in buckets.items():
        strong = sorted(b["strong"], key=lambda x: x["time"], reverse=True)
        weak = sorted(b["weak"], key=lambda x: x["time"], reverse=True)
        # 情绪分只由强关联驱动：加权净分 = Σ(有效权重 × 情绪符号)，坏消息 2 倍。
        # 共享事件（同一快讯命中 ≥2 只 ETF）对单只 ETF 的贡献打 SHARED_DISCOUNT 折，
        # 避免「碳酸锂下跌」这类宏观主题把多只 ETF 的置信度同时拉满。
        net = 0.0
        for n in strong:
            w_eff = n["level_weight"]
            if n.get("shared"):
                w_eff = round(w_eff * SHARED_DISCOUNT, 2)
            if n["sentiment"] == "good":
                net += w_eff
            elif n["sentiment"] == "bad":
                net -= w_eff * BAD_NEWS_WEIGHT
        good = sum(1 for n in strong if n["sentiment"] == "good")
        bad = sum(1 for n in strong if n["sentiment"] == "bad")
        etfs.append({
            "ticker": ticker,
            "name": (etf_names or {}).get(ticker, ticker),
            "ok": True,
            "strong_count": len(strong),
            "weak_count": len(weak),
            "good_count": good,
            "bad_count": bad,
            "weighted_score": net,
            "bias": "偏空" if net < 0 else ("偏多" if net > 0 else "中性"),
            # 强关联不足 2 条时不给方向结论，避免 1 条快讯定调
            "confidence": "足量" if len(strong) >= 2 else ("单条" if strong else "无信号"),
            "strong_news": strong[:12],
            "weak_news": weak[:8],
        })
    etfs.sort(key=lambda x: (-x["strong_count"], -x["weak_count"]))

    # 全局桶：没归属到任何 ETF 的，但被财联社标为 B 级 → 当日环境，单独列
    global_news = sorted(
        [n for n in unmatched if n["level"] in ("A", "B")],
        key=lambda x: x["time"], reverse=True,
    )

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "财联社电报 (cls.cn/v1/roll/get_roll_list)",
        "hours_back": hours_back,
        "raw_count": len(raw),
        "noise_filtered": noise,
        "after_noise_filter": len(kept),
        "after_dedup": len(deduped),
        "matched": len(deduped) - len(unmatched),
        "unmatched": len(unmatched),
        "elapsed_sec": round(elapsed, 1),
        "etfs": etfs,
        "events": all_events,
        "global_news": global_news[:20],
        "linked_events": linked[:20],
    }


def load_holdings_context() -> tuple[dict[str, set[str]], dict[str, str]]:
    """从 holdings_config.json 读取 ETF 名单，并为配了 index_code 的标的加载成分股。

    返回 (constituents, etf_names)，供 fetch_cls_flow 使用。
    只有配了 index_code 的标的能做成分股精确匹配，其余退化为主题/别名匹配。

    ⚠️ 2026-11-12 起 holdings_config.json 换成 588940/510100/159740/159792 四只，
    四只的 index_code 均未核实而留空 ⇒ 成分股精确匹配全部停用，新闻信号只能靠
    INDEX_ALIAS 的指数名/代码点名，命中量与信号强度会明显低于旧池（旧池
    515220=399998 / 510050=000016 可做成分股命中）。补 index_code 前须按 f10
    成分股核实，禁止按基金名称猜。
    """
    import json
    cons: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    try:
        from flows.etf_constituents_flow import _get_constituents
    except ImportError:
        from etf_constituents_flow import _get_constituents  # 直接跑本文件时的路径
    cfg_path = Path(__file__).resolve().parents[2] / "holdings_config.json"
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    for h in cfg.get("holdings", []):
        names[h["ticker"]] = h["name"]
        if h.get("index_code"):
            c = _get_constituents(h["index_code"])
            if c:
                cons[h["ticker"]] = {x["code"] for x in c}
    return cons, names


if __name__ == "__main__":
    # 从 holdings_config 自动取已验证的 index_code，启用成分股精确匹配
    _cons: dict[str, set[str]] = {}
    _names: dict[str, str] = {}
    try:
        _cons, _names = load_holdings_context()
        print(f"成分股匹配启用: {[(k, len(v)) for k, v in _cons.items()]}")
    except Exception as e:
        print(f"[WARN] 成分股加载失败，退化为纯主题匹配: {str(e)[:80]}")

    res = fetch_cls_flow(hours_back=8.0, constituents=_cons, etf_names=_names)
    print(f"\n原始 {res['raw_count']} 条 → 噪音过滤 {res['after_noise_filter']}"
          f" （噪音 {res['noise_filtered']}）→ 去重 {res['after_dedup']}"
          f" → 匹配 ETF {res['matched']} / 未匹配 {res['unmatched']}")
    print(f"耗时 {res['elapsed_sec']}s\n")
    for e in res["etfs"]:
        if e["strong_count"] == 0 and e["weak_count"] == 0:
            continue
        print(f"■ {e['name']} ({e['ticker']}) 强{e['strong_count']}/弱{e['weak_count']} "
              f"好{e['good_count']}/坏{e['bad_count']} 加权分 {e['weighted_score']} "
              f"→ {e['bias']} [{e['confidence']}]")
        for n in e["strong_news"][:6]:
            tag = "*联动" if n.get("shared") else ""
            print(f"    强[{n['level']}/{n['sentiment_src']}{tag}] {n['time']} "
                  f"({n['via']}:{n['match_detail']}) {n['title'][:44]}")
        for n in e["weak_news"][:3]:
            print(f"    弱[{n['level']}] {n['time']} ({n['via']}:{n['match_detail']}) {n['title'][:50]}")
        print()
    print("── 全局宏观/海外（未归属ETF，B级以上）──")
    for n in res["global_news"][:12]:
        print(f"    [{n['level']}] {n['time']} {n['title'][:58]}  subj={n['subject']}")
