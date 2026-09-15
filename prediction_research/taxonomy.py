from __future__ import annotations

import re


ETF_TAXONOMY = {
    "equity": "主要投资股票；二级标签再区分宽基、行业、主题、策略/因子和主动管理",
    "bond": "跟踪或主要投资债券；包括国债、政金债、地方债、信用债、城投债、可转债和综合债券",
    "commodity": "直接或通过期货追踪商品价格；区分黄金现货与商品期货",
    "money": "仅投资货币市场工具并在交易所交易申赎",
    "multi_asset": "合同约定同时配置两类或以上资产，不能归入单一资产类别",
    "unknown": "公开快照无法可靠判断，保留待基金合同或交易所产品资料复核",
}


def classify_etf(name: str) -> dict[str, str]:
    upper = name.upper().replace("ＥＴＦ", "ETF")
    if any(key in upper for key in ("货币", "保证金")) or ("现金" in upper and "现金流" not in upper):
        return {"asset_class": "money", "subtype": "money_market"}
    if "黄金股" in upper:
        return {"asset_class": "equity", "subtype": "sector"}
    if any(key in upper for key in ("黄金", "金ETF")):
        return {"asset_class": "commodity", "subtype": "gold_spot"}
    if any(key in upper for key in ("豆粕", "有色期货", "能源化工", "商品期货")):
        return {"asset_class": "commodity", "subtype": "commodity_futures"}
    if any(key in upper for key in ("债", "国开", "政金", "可转债", "信用")):
        subtype = "convertible_bond" if "转债" in upper else "government_bond" if "国债" in upper else "policy_bank_bond" if any(k in upper for k in ("国开", "政金")) else "credit_bond" if "信用" in upper else "bond_other"
        return {"asset_class": "bond", "subtype": subtype}
    if any(key in upper for key in ("多资产", "股债", "全天候")):
        return {"asset_class": "multi_asset", "subtype": "multi_asset"}
    if "主动" in upper and "ETF" in upper:
        return {"asset_class": "equity", "subtype": "active_equity"}
    broad = ("沪深", "中证A", "上证", "深证", "创业板", "科创", "北证", "A500", "A50", "A100", "A股", "红利")
    sector = ("银行", "证券", "金融", "医药", "医疗", "消费", "军工", "地产", "煤炭", "钢铁", "有色", "化工", "农业", "传媒", "通信", "汽车", "电力", "能源")
    strategy = ("增强", "价值", "成长", "低波", "质量", "自由现金流", "ESG")
    if "ETF" in upper:
        is_broad = any(k in upper for k in broad) or re.search(r"中证(?:A)?\d{2,4}", upper) is not None
        subtype = "strategy_factor" if any(k in upper for k in strategy) else "broad_market" if is_broad else "sector" if any(k in upper for k in sector) else "thematic"
        return {"asset_class": "equity", "subtype": subtype}
    return {"asset_class": "unknown", "subtype": "unclassified"}


def market_scope(name: str) -> str:
    upper = name.upper()
    if any(key in upper for key in ("纳指", "标普", "恒生", "港股", "日经", "德国", "法国", "沙特", "东南亚", "中概", "海外", "全球")):
        return "cross_border"
    return "domestic_or_unverified"


_FUND_COMPANY_SUFFIXES = (
    "华泰柏瑞", "申万菱信", "汇丰晋信", "浦银安盛", "前海开源", "西部利得",
    "国投瑞银", "民生加银", "国泰君安", "国寿安保", "方正富邦", "中信保诚",
    "富国", "银华", "东财", "广发", "摩根", "易方达", "国泰", "华夏", "南方",
    "嘉实", "博时", "华安", "华宝", "招商", "平安", "泰康", "汇添富", "天弘",
    "景顺", "鹏华", "大成", "融通", "中欧", "工银", "建信", "兴全", "交银",
    "海富通", "华商", "永赢", "中银", "国联安", "东方", "诺安", "万家",
    "长盛", "中邮", "银河", "新华", "金鹰", "宝盈", "德邦", "东吴",
    "华富", "中金", "国投", "红土创新", "浙商", "财通", "兴业",
)


def _strip_fund_company(name: str) -> str:
    """剥离 A 股 ETF 名称中的基金公司名，返回纯主题词。

    场内 ETF 名称基本是「主题/指数 + ETF + 公司名」结构，用「ETF」作锚点截断即可；
    少数不含「ETF」锚点的名称（LOF/普通基金）回退为剥离已知公司名后缀。
    """
    upper = name.upper().replace("ＥＴＦ", "ETF")
    idx = upper.find("ETF")
    if idx > 0:
        return name[:idx].strip()
    core = name.strip()
    for suffix in _FUND_COMPANY_SUFFIXES:
        if core.endswith(suffix):
            core = core[: -len(suffix)]
            break
    return core.strip()


# 赛道归一化：把相近主题词归并到同一赛道，供粗筛「每组最多 1 只」去重。
# 顺序敏感：更具体的赛道（如 new_energy）必须排在宽泛赛道（如 cyclical 含「能源」）之前，
# 否则「新能源」会被「能源」子串误归到周期。
_TRACK_KEYWORDS = {
    "pharma": ("创新药", "生物科技", "生物医药", "医药", "医疗", "医疗器械", "疫苗", "中药", "CXO", "港股通医疗", "医美"),
    "semiconductor": ("半导体", "芯片", "集成电路", "晶圆", "存储"),
    "new_energy": ("新能源", "光伏", "电池", "锂电", "储能", "风电", "碳中和", "清洁能源"),
    "consumer": ("消费", "食品饮料", "食品", "白酒", "酒", "乳品", "家电", "饮料", "零售"),
    "tech": ("信息技术", "信创", "软件", "计算机", "人工智能", "机器人", "数字经济", "云计算", "大数据"),
    "finance": ("银行", "证券", "券商", "保险", "金融"),
    "military": ("军工", "国防", "航空航天", "航天"),
    "auto": ("汽车", "新能源车", "智能汽车", "整车", "智能驾驶"),
    "agriculture": ("农业", "养殖", "畜牧", "粮食", "种业"),
    "media": ("传媒", "游戏", "影视", "动漫"),
    "telecom": ("通信", "5G", "光模块", "光通信"),
    "real_estate": ("地产", "房地产", "基建", "建筑", "建材"),
    "cyclical": ("煤炭", "钢铁", "有色", "稀土", "化工", "能源", "油气", "石油", "航运", "船舶"),
    "dividend": ("红利", "低波", "股息"),
    "gold": ("黄金", "金ETF"),
    "hong_kong_tech": ("港股科技", "恒生科技", "港股通科技"),
    "china_internet": ("中概互联网", "互联网", "中概"),
}

_BROAD_INDEXES = (
    "中证2000", "中证1000", "中证800", "中证500", "中证300",
    "沪深300", "上证50", "上证180", "上证指数", "中证A50", "中证A100", "中证A500",
    "科创50", "科创100", "科创创业", "创业板", "创业板50", "北证50",
    "深证100", "深证成指",
)

_STRATEGY_MARKERS = ("增强", "价值", "成长", "低波", "质量", "等权", "优选", "精选", "自由现金流")


def _normalize_index(theme: str) -> str | None:
    """把「宽基指数 + 策略后缀」归一为指数名；非宽基指数返回 None。

    例如「中证2000增强」→ index:中证2000、「沪深300」→ index:沪深300。
    纯策略因子（价值/成长等，剥离后无宽基特征）返回 None，交给赛道归并/兜底处理。
    """
    core = theme
    changed = True
    while changed:
        changed = False
        for marker in _STRATEGY_MARKERS:
            if core.endswith(marker):
                core = core[: -len(marker)].strip()
                changed = True
                break
    for idx in _BROAD_INDEXES:
        if core.upper() == idx.upper():
            return f"index:{idx}"
    if re.search(r"(中证|沪深|上证|深证|科创|创业|北证)", core):
        return f"index:{core[:12]}"
    return None


def screen_group(name: str, subtype: str) -> str:
    theme = _strip_fund_company(name)
    # 宽基/策略因子类：按跟踪指数归并（同指数只留 1 只）
    if subtype in ("broad_market", "strategy_factor"):
        index = _normalize_index(theme)
        if index:
            return index
    # 行业/主题类：归并到同一赛道（同赛道只留 1 只）
    for track, keywords in _TRACK_KEYWORDS.items():
        if any(keyword.upper() in theme.upper() for keyword in keywords):
            return track
    if theme:
        return f"{subtype}:{theme[:12]}"
    return f"{subtype}:{name[:12]}"
