"""
个股新闻情绪分类 —— 好消息 / 坏消息 标注与加权。

设计原则（用户 2026-10-15 指令）：
- 消息面一直是弱项，坏消息对决策的影响被系统性低估。
- 因此坏消息权重 > 好消息：净情绪分 = 好消息数 − 2 × 坏消息数。
- 净分 < 0 即判定「消息面偏空」，触发条件单收紧（见 post_run_holdings.py）。

仅做关键词命中分类，不做语义推断；命中即标注，未命中为 neutral。
"""

# 利好关键词（命中 → good）
POSITIVE_KEYWORDS = [
    # 中文
    "上涨", "大涨", "涨停", "创新高", "历史新高", "利好", "超预期", "大超预期的",
    "增长", "高增", "盈利", "扭亏", "回购", "增持", "中标", "签约", "获批",
    "获准", "突破", "上调", "上调评级", "看好", "拐点", "复苏", "政策利好",
    "分红", "派息", "扩产", "订单", "战略合作", "合作", "超预期", "利润大增",
    "营收增长", "净利增长", "困境反转", "困境反转", "困境反", "需求旺盛",
    "量价齐升", "机构增持", "北向增持", "外资增持",
    # 英文
    "surge", "rally", "beat", "upgrade", "buyback", "profit", "growth",
    "approval", "breakthrough", "partnership", "dividend", "record high",
    "outperform", "raised", "beats", "bullish", "expansion", "win", "contract win",
]

# 利空关键词（命中 → bad，权重翻倍）
NEGATIVE_KEYWORDS = [
    # 中文
    "下跌", "大跌", "暴跌", "跌停", "利空", "不及预期", "低于预期", "下滑",
    "亏损", "减亏", "减持", "退市", "立案", "调查", "处罚", "罚款", "警示",
    "风险", "下调", "下调评级", "降级", "裁员", "停产", "违约", "暴雷",
    "诉讼", "官司", "制裁", "封杀", "看空", "泡沫", "收紧", "加息", "通胀",
    "衰退", "滞涨", "质疑", "造假", "退市风险", "ST", "业绩变脸", "商誉减值",
    "计提", "踩雷", "爆雷", "监管", "问询", "停牌", "利空出尽", "需求疲软",
    "订单取消", "合同终止", "下调指引", "亏损扩大", "现金流紧张",
    # 治理 / 人事风险（2026-10-16 新增）
    # 背景：实测「中国神华：总会计师、董秘宋静刚辞职」「财务总监被留置」这类
    #       治理负面信号原先被判为 neutral，导致权重最高的成分股风险被系统性漏掉。
    "辞职", "辞任", "离任", "免职", "留置", "被查", "被调查", "落马",
    "逮捕", "刑拘", "内幕交易", "违规", "问责", "降职", "失信",
    "被执行", "被冻结", "爆仓", "强平", "约谈", "整改", "责令",
    # 英文
    "plunge", "miss", "downgrade", "lawsuit", "fine", "investigation",
    "layoff", "default", "sanction", "recession", "inflation", "rate hike",
    "warning", "halt", "delist", "fraud", "short", "bearish", "cut",
    "weak", "slowdown", "recall", "probe", "subpoena", "bankruptcy", "loss",
]

# 坏消息相对好消息的权重倍数（用户要求「加大权重」）
BAD_NEWS_WEIGHT = 2


def classify_sentiment(title: str) -> str:
    """返回 'good' / 'bad' / 'neutral'。同时命中好坏时，坏消息优先（更保守）。"""
    if not title:
        return "neutral"
    t = title.lower()
    is_good = any(kw.lower() in t for kw in POSITIVE_KEYWORDS)
    is_bad = any(kw.lower() in t for kw in NEGATIVE_KEYWORDS)
    if is_bad:
        return "bad"          # 坏消息优先判定，避免被利好措辞掩盖风险
    if is_good:
        return "good"
    return "neutral"


def tag_news(news_list: list[dict]) -> list[dict]:
    """就地给每条新闻加 sentiment 字段，返回同一列表。"""
    for item in news_list:
        item["sentiment"] = classify_sentiment(item.get("title", ""))
    return news_list


def summarize_sentiment(news_list: list[dict]) -> dict:
    """统计好/坏/中性数量与加权净分。"""
    good = [n for n in news_list if n.get("sentiment") == "good"]
    bad = [n for n in news_list if n.get("sentiment") == "bad"]
    neutral = [n for n in news_list if n.get("sentiment") == "neutral"]
    net_score = len(good) - BAD_NEWS_WEIGHT * len(bad)
    return {
        "good": good,
        "bad": bad,
        "neutral": neutral,
        "good_count": len(good),
        "bad_count": len(bad),
        "neutral_count": len(neutral),
        "net_score": net_score,
        "bias": "偏空" if net_score < 0 else ("偏多" if net_score > 0 else "中性"),
    }
