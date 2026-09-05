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


def screen_group(name: str, subtype: str) -> str:
    groups = {
        "livestock": ("养殖", "畜牧"), "coal": ("煤炭",), "gaming": ("游戏",),
        "home_appliance": ("家电",), "hong_kong_tech": ("港股科技", "恒生科技"),
        "shipbuilding": ("船舶",), "information_technology": ("信息技术",),
        "food_beverage": ("食品饮料",), "consumer": ("消费",), "energy": ("能源",),
        "agriculture": ("农业",), "securities": ("证券ETF",), "china_internet": ("中概互联网",),
        "aerospace": ("航空航天",),
        "broad_a50": ("A50",), "broad_csi500": ("中证500",),
        "dividend_low_vol": ("红利低波",), "value": ("价值ETF",),
    }
    for group, keywords in groups.items():
        if any(keyword.upper() in name.upper() for keyword in keywords):
            return group
    return f"{subtype}:{name[:12]}"
