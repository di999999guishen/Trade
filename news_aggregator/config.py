"""
个股大事件新闻流聚合器 - 配置
覆盖 A股 / 港股 / 美股
"""

# 自选股列表 - 可自由增删
STOCKS = {
    # A股（6位代码）— 精简至核心持仓
    "A股": [
        {"code": "600519", "name": "贵州茅台"},
        {"code": "000858", "name": "五粮液"},
        {"code": "300750", "name": "宁德时代"},
        {"code": "601318", "name": "中国平安"},
    ],
    # 港股（5位代码）— 精简至核心持仓
    "港股": [
        {"code": "00700", "name": "腾讯控股"},
        {"code": "09988", "name": "阿里巴巴"},
        {"code": "01810", "name": "小米集团"},
    ],
    # 美股（股票代码）— 精简至核心持仓
    "美股": [
        {"code": "AAPL", "name": "苹果"},
        {"code": "NVDA", "name": "英伟达"},
        {"code": "MSFT", "name": "微软"},
    ],
}

# 大事件关键词 - 匹配到这些关键词的新闻会被标注为"大事件"
MAJOR_EVENT_KEYWORDS = [
    # 财报/业绩
    "财报", "业绩", "营收", "利润", "净利润", "季报", "年报", "中报",
    "earnings", "revenue", "profit", "quarterly", "annual report",
    # 分红
    "分红", "派息", "股息", "dividend",
    # 并购/重组
    "收购", "并购", "重组", "合并", "入股", "参股",
    "acquisition", "merger", "buyout", "takeover",
    # 重大合同/合作
    "重大合同", "战略合作", "签约", "中标",
    "partnership", "contract", "deal",
    # 监管/处罚
    "立案", "调查", "处罚", "罚款", "监管", "警示",
    "investigation", "fine", "regulatory", "SEC", "lawsuit",
    # 股价异常
    "涨停", "跌停", "停牌", "复牌", "退市",
    "surge", "plunge", "halted", "delisted",
    # 产品/技术重大突破
    "发布", "上市", "获批", "获准", "突破", "里程碑",
    "launch", "approval", "breakthrough", "FDA",
    # 管理层变动
    "辞职", "变更", "任免", "董事长", "CEO",
    "resign", "appoint", "executive",
    # 回购/增减持
    "回购", "增持", "减持", "buyback", "repurchase",
    # 其他重大
    "重大", "重要", "公告", "声明",
]

# 输出目录
OUTPUT_DIR = "reports"

# 每只股票最多获取新闻条数
MAX_NEWS_PER_STOCK = 10
