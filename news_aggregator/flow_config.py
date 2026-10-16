"""
新闻流 (News Flow) 统一配置
================================
本文件集中管理「独立新闻流」所需的全部参数，便于不动代码只改配置：

  1. MACRO_RELEASES  —— 2026 美国宏观发布日历（CPI/PPI/非农/PCE/FOMC）
                       解决痛点：美国CPI昨天发布了我却不知道。
  2. TRACKED_ETFS    —— 需要每日做「成分股权重新闻」的 ETF 及其跟踪指数代码
  3. INSTITUTIONS    —— 需要追踪 13F 持仓变动的大型机构 (桥水/伯克希尔/高盛)
  4. 各类阈值         —— 大事件/大变动判定

所有日期为美东时间发布日；流里会再换算成「距今天数 / 是否刚发布」。
"""

from __future__ import annotations

from datetime import date

# ─────────────────────────────────────────────────────────────────────────────
# 1. 美国宏观发布日历（2026，美东时间 8:30 公布，FOMC 为决议日 14:00）
#    来源：BLS 官方 release schedule + Federal Reserve FOMC calendar（2026-10 核查）
#    格式：indicator 名称 / data_month 数据所属月 / release_date 发布日
# ─────────────────────────────────────────────────────────────────────────────
MACRO_RELEASES: list[dict] = [
    # CPI（消费者物价指数）
    {"indicator": "CPI", "data_month": "2026-08", "release_date": "2026-09-11"},
    {"indicator": "CPI", "data_month": "2026-09", "release_date": "2026-10-14"},
    {"indicator": "CPI", "data_month": "2026-10", "release_date": "2026-11-10"},
    {"indicator": "CPI", "data_month": "2026-11", "release_date": "2026-12-10"},
    # PPI（生产者物价指数）
    {"indicator": "PPI", "data_month": "2026-08", "release_date": "2026-09-10"},
    {"indicator": "PPI", "data_month": "2026-09", "release_date": "2026-10-15"},
    {"indicator": "PPI", "data_month": "2026-10", "release_date": "2026-11-13"},
    {"indicator": "PPI", "data_month": "2026-11", "release_date": "2026-12-15"},
    # 非农（Employment Situation / Nonfarm Payrolls）
    {"indicator": "NFP", "data_month": "2026-09", "release_date": "2026-10-02"},
    {"indicator": "NFP", "data_month": "2026-10", "release_date": "2026-11-06"},
    {"indicator": "NFP", "data_month": "2026-11", "release_date": "2026-12-04"},
    # PCE（个人消费支出物价指数，美联储首选通胀指标）
    {"indicator": "PCE", "data_month": "2026-09", "release_date": "2026-10-29"},
    {"indicator": "PCE", "data_month": "2026-10", "release_date": "2026-11-25"},
    # FOMC 利率决议（2pm ET 当天）
    {"indicator": "FOMC", "data_month": "2026-09", "release_date": "2026-09-16"},
    {"indicator": "FOMC", "data_month": "2026-10", "release_date": "2026-10-28"},
    {"indicator": "FOMC", "data_month": "2026-12", "release_date": "2026-12-09"},
]

# 宏观指标 → akshare 取数函数名 + 对应日历键（仅用国内可访问源，符合「禁 FRED/yfinance」约定）
# 取值：函数返回 DataFrame，最后一行为未来发布占位行（现值 NaN），须取「最后一个非空值行」。
#   - cpi_yoy 列: ['时间','发布日期','现值','前值']
#   - 其余列: ['商品','日期','今值','预测值','前值']
# 注：akshare 的 cpi_monthly/ppi/non_farm 等「月率」序列存在约一年数据滞后，
#     故这些值仅作参考，发布日一律以 MACRO_RELEASES 日历为准（已实测 cpi_yoy 当前可用）。
MACRO_AKSHARE_FUNCS: dict[str, dict] = {
    "CPI同比":  {"func": "macro_usa_cpi_yoy",          "cal_key": "CPI"},
    "CPI环比":  {"func": "macro_usa_cpi_monthly",       "cal_key": "CPI"},
    "核心CPI":  {"func": "macro_usa_core_cpi_monthly",  "cal_key": "CPI"},
    "PPI":      {"func": "macro_usa_ppi",              "cal_key": "PPI"},
    "非农就业": {"func": "macro_usa_non_farm",          "cal_key": "NFP"},
    "失业率":   {"func": "macro_usa_unemployment_rate", "cal_key": "NFP"},
}

# 距今天数 ≤ 此值 视为「刚发布」，必须在新闻流里高亮
FRESH_WINDOW_DAYS = 6

# ─────────────────────────────────────────────────────────────────────────────
# 2. 需要每日做「成分股权重新闻」的 ETF
#    index_code = 该 ETF 跟踪的中证指数代码（用于 index_stock_cons_weight_csindex）
#    top_n      = 取权重最高的前 N 只成分股抓新闻（其余并入「其余成分」统计）
#    market     = A股 / 港股 / 美股（决定新闻抓取方式）
# ─────────────────────────────────────────────────────────────────────────────
TRACKED_ETFS: list[dict] = [
    {
        "ticker": "515220.SS",
        "name": "煤炭ETF国泰",
        "index_code": "399998",        # 中证煤炭
        "index_name": "中证煤炭",
        "market": "A股",
        "top_n": 12,
        "active": True,
    },
    {
        "ticker": "510050.SS",
        "name": "上证50ETF华夏",
        "index_code": "000016",        # 上证50
        "index_name": "上证50",
        "market": "A股",
        "top_n": 15,
        "active": True,
    },
    # 513650 是「标普500ETF南方」、513390 是「纳指100ETF博时」——两者都不是中证指数，
    # index_stock_cons_weight_csindex 取不到成分股权重，默认不跑成分股新闻（避免抓空报错）。
    # 如需成分股权重，需另接 S&P 500 / Nasdaq 100 官方成分表，再把 active 改 True。
    {
        "ticker": "513650.SS",
        "name": "标普500ETF南方",
        "index_code": "",
        "index_name": "标普500",
        "market": "美股",
        "top_n": 10,
        "active": False,
    },
    {
        "ticker": "513390.SS",
        "name": "纳指100ETF博时",
        "index_code": "",
        "index_name": "纳斯达克100",
        "market": "美股",
        "top_n": 10,
        "active": False,
    },
]

# ─────────────────────────────────────────────────────────────────────────────
# 3. 追踪 13F 持仓变动的大型机构
#    cik = SEC EDGAR 零填充 10 位 CIK
# ─────────────────────────────────────────────────────────────────────────────
INSTITUTIONS: list[dict] = [
    {
        "key": "bridgewater",
        "name": "桥水基金 (Bridgewater)",
        "cik": "0001350694",
        "active": True,
    },
    {
        "key": "berkshire",
        "name": "伯克希尔 (Berkshire Hathaway)",
        "cik": "0001067983",
        "active": True,
    },
    {
        "key": "goldman",
        "name": "高盛 (Goldman Sachs)",
        "cik": "0000886982",
        "active": True,
    },
]

# 13F 大变动判定阈值
BIG_CHANGE_VALUE_USD = 300_000_000   # 单标的持仓变动绝对值 ≥ 3 亿美元 视为大变动
BIG_CHANGE_PCT_PORTFOLIO = 1.0       # 或 占组合比重变动 ≥ 1%

# 机构持仓变动 → 与用户 ETF 的相关性映射
# 命中后，该条变动会标注「关联你的 ETF: <ticker>」，并在每日预测时注入分析上下文。
#
# ⚠️ ETF 语义已按东方财富实测名称核对（2026-10-16）：
#   515220 煤炭ETF国泰 / 512400 有色金属ETF南方 / 159611 电力ETF广发
#   510160 产业升级ETF南方 / 589720 科创创新药ETF国泰 / 159748 创新药ETF富国
#   516090 新能源ETF易方达 / 512170 医疗ETF华宝
#   513650 标普500ETF南方（★不是纳指100★） / 513390 纳指100ETF博时 / 510050 上证50ETF华夏
# 注意：513650 与 513390 常被搞混，半导体/AI 命中的是 513390，宽基蓝筹才命中 513650。
RELEVANCE_MAP: list[dict] = [
    {
        "keywords": ["COAL", "PEABODY", "ARCH RESOURCES", "CONSOL ENERGY", "WARRIOR MET",
                     "ALPHA METALLURGICAL", "煤炭", "焦煤", "动力煤"],
        "etf": "515220.SS", "note": "煤炭",
    },
    # 油气/综合能源：伯克希尔砍西方石油、雪佛龙这类动作对煤炭ETF是同向行业信号
    {
        "keywords": ["EXXON", "CHEVRON", "OCCIDENTAL", "CONOCOPHILLIPS", "SHELL",
                     "BP PLC", "BRITISH PETROLEUM", "EOG RESOURCES", "SLB",
                     "SCHLUMBERGER", "PIONEER NATURAL", "DEVON ENERGY", "HESS",
                     "MARATHON PETROLEUM", "VALERO", "PHILLIPS 66",
                     "石油", "油气", "原油", "能源"],
        "etf": "515220.SS", "note": "煤炭/能源",
    },
    {
        "keywords": ["GOLD", "NEWMONT", "BARRICK", "KINROSS", "AGNICO", "WHEATON",
                     "FRANCO-NEVADA", "SIBANYE", "GOLD FIELDS", "ANGLOGOLD", "黄金", "贵金属"],
        "etf": "512400.SS", "note": "黄金/贵金属",
    },
    {
        "keywords": ["COPPER", "FREEPORT", "SOUTHERN COPPER", "ALCOA", "ALUMINUM",
                     "CLEVELAND-CLIFFS", "TECK", "有色金属", "铜业", "铝业"],
        "etf": "512400.SS", "note": "有色金属",
    },
    {
        "keywords": ["ELECTRIC", "UTILITY", "UTILITIES", "NEXTERA", "DUKE ENERGY",
                     "SOUTHERN CO", "DOMINION ENERGY", "AMERICAN ELECTRIC",
                     "CONSTELLATION ENERGY", "VISTRA", "NRG", "电力", "公用事业"],
        "etf": "159611.SS", "note": "电力",
    },
    {
        "keywords": ["TESLA", "SOLAR", "FIRST SOLAR", "ENPHASE", "SOLAREDGE",
                     "LITHIUM", "ALBEMARLE", "PLUG POWER", "BLOOM ENERGY",
                     "新能源", "光伏", "锂电", "储能"],
        "etf": "516090.SS", "note": "新能源",
    },
    {
        "keywords": ["NVIDIA", "ADVANCED MICRO", "MICRON", "TAIWAN SEMICONDUCTOR",
                     "BROADCOM", "APPLIED MAT", "LAM RESEARCH", "KLAC", "ASML",
                     "INTEL", "QUALCOMM", "TEXAS INSTR", "ANALOG DEVICES",
                     "MARVELL", "ARM HOLDINGS", "SANDISK", "SK HYNIX",
                     "半导体", "英伟达", "芯片"],
        "etf": "513390.SS", "note": "纳指100/半导体",
    },
    {
        "keywords": ["ALPHABET", "GOOGLE", "META PLATFORMS", "AMAZON", "MICROSOFT",
                     "APPLE", "NETFLIX", "SALESFORCE", "ORACLE", "ADOBE", "AMD",
                     "PALANTIR", "SNOWFLAKE", "CROWDSTRIKE",
                     "互联网", "软件", "云计算", "人工智能"],
        "etf": "513390.SS", "note": "纳指100/科技",
    },
    {
        "keywords": ["JOHNSON & JOHNSON", "PFIZER", "MERCK", "ABBVIE", "ELI LILLY",
                     "AMGEN", "GILEAD", "VERTEX", "REGENERON", "BRISTOL",
                     "MEDTRONIC", "ABBOTT", "THERMO FISHER", "DANAHER",
                     "医疗", "医药", "制药", "生物科技", "BIOTECH", "THERAPEUTICS"],
        "etf": "512170.SS", "note": "医疗",
    },
    {
        "keywords": ["ONCOLOGY", "IMMUNOLOGY", "GENETICS", "GENOMICS", "CRISPR",
                     "SEAGEN", "ARGENX", "INSMED", "SUMMIT THERAPEUTICS",
                     "创新药", "基因", "细胞治疗"],
        "etf": "589720.SS", "note": "科创创新药",
    },
    # 宽基/蓝筹兜底：大型金融、消费、工业龙头 → 标普500 + 上证50 + 产业升级
    {
        "keywords": ["BANK OF AMER", "JPMORGAN", "WELLS FARGO", "CITIGROUP",
                     "GOLDMAN SACHS", "MORGAN STANLEY", "BERKSHIRE",
                     "VISA", "MASTERCARD", "PROCTER", "COCA COLA", "PEPSICO",
                     "WALMART", "HOME DEPOT", "MCDONALD", "NIKE", "BOEING",
                     "CATERPILLAR", "HONEYWELL", "UNITEDHEALTH", "EXXON",
                     "CHEVRON", "宽基", "蓝筹", "标普"],
        "etf": "513650.SS", "note": "标普500",
    },
]

# ETF 成分股新闻：单只成分股新闻的权重乘数 = 该成分股指数权重(%)，
# 坏消息按 sentiment 约定权重 2 倍放大（与 sentiment.py 一致）。
BAD_NEWS_WEIGHT = 2

# 成分股新闻只保留最近 N 天内的（东财接口会返回几个月前的旧闻，
# 不过滤会让尾部旧闻污染加权情绪分）。
#
# 2026-10-16 从 7 天放宽到 14 天，依据是对 515220 前 12 大成分股（东财每股只返回
# 10 条，实测调大条数上限拿不到更多）的采样：
#     7 天窗 → 仅 2 条真新闻（覆盖 1 只）→ 每日输出恒为「样本不足」，等于没信息
#    14 天窗 → 7 条（覆盖 2 只）
#    30 天窗 → 11 条（覆盖 5 只）
# 30 天对「每日盘前信号」太陈旧（8 月的中报会被当成当前消息），故取 14 天折中：
# 仍属「近期」，又能把覆盖度从 1 只拉到可用的量级。
# 注意：窗口只影响「哪些新闻进入加权分与上下文」，不影响置信度门槛；
# 覆盖不足时依然输出「样本不足」，不会因为放宽窗口就凭空给出方向性结论。
NEWS_WINDOW_DAYS = 14

# ─────────────────────────────────────────────────────────────────────────────
# 板块/行业新闻关键词（2026-10-16 新增，解决「ETF 新闻流太薄」）
#   背景：东财 stock_news_em 给单只股票返回的稿件里，个股直接新闻极少，
#         大量是板块级稿件（「煤炭行业今日涨1.40%」「焦煤期货走强」），
#         这类稿标题不含任何一只成分股名，严格闸门会全部丢弃 → 结果只剩 1-2 条。
#   处理：作为「板块消息面」单独成一条线，只做 ETF 级参考，**不**归因到某只成分股。
#   取值：优先用这里的覆盖项；没覆盖的 ETF 会从 index_name 自动派生
#         （中证煤炭→煤炭；中证全指电力公用事业→电力；上证50→无）。
# ─────────────────────────────────────────────────────────────────────────────
SECTOR_KEYWORD_OVERRIDES: dict[str, list[str]] = {
    "515220.SS": ["煤炭", "焦煤", "动力煤", "煤价", "煤化工", "煤电"],
    "510050.SS": [],   # 上证50 是宽基，无单一行业概念，不做板块匹配
}
