"""2026-11-13 用户指令：10 只备选 + 4 只自选跑完都要给出买卖指标。

锁住四条：① PM 最终层两种版式都能解析；② 决策方向识别正确；
③ 缺字段一律 None / 「无数据」，绝不补造；④ 减配 / 卖出 / 观望不生成建仓指令。
"""
from pathlib import Path

from daily_order_plan import direction_of, extract, render

PM_STAR = """**Rating**: Underweight

**Executive Summary**: 减配，不新建仓。

**Price Target**: 0.467

**Current Price**: 0.483

**Support Levels** (nearest first): 0.475 / 0.467 / 0.461 / 0.456

**Resistance Levels** (nearest first): 0.4855 / 0.4904 / 0.499

**Entry / Add Zone**: 仅在上修条件满足后才可买回，本次评级不允许加仓：日收盘站上 0.492 且放量。

**Stop Loss**: 0.455

**Reduce Trigger**: 日收盘跌破 0.467 立即把剩余敞口压到标准配置的 15%-20%。

**Scale-out Ladder** (close % is of the original position):

| Rung | Trigger Price | Close % | Condition |
| --- | --- | --- | --- |
| 1 | 0.483 | 45 | 现价限价减超额部分，不追打 |
| 2 | 0.49 | 15 | 反弹至阻力区第一笔 |
| 3 | 0.499 | 10 | 收盘无法站上则执行 |

FINAL TRANSACTION PROPOSAL: **UNDERWEIGHT**
"""

PM_EXEC = """**Rating**: Underweight

**Executable Price Block**:
- current_price: 0.678
- support_levels: 0.6605 / 0.6484 / 0.633 / 0.601
- resistance_levels: 0.690 / 0.708 / 0.740 / 0.7536
- entry_zone: 0.708-0.740，仅在回补确认四条件同时满足后才考虑；否则不新增、不加仓。
- stop_loss: 0.601（以日线收盘价计；盘中跌破不触发）
- reduce_trigger: 日线收盘 <0.650 时强制把总敞口压到 1/3。盘中破位不算。
- scale_out_ladder:

| Rung | Trigger Price | Close % | Condition |
| --- | --- | --- | --- |
| 1 | 0.678 | 20 | 仅当仓位高于 1/3 时减超额部分 |
| 2 | 0.700 | 20 | 阻力区第一笔减仓 |
| 3 | 0.708 | 20 | 收盘无法站上时执行 |
| 4 | 0.633 | 25 | 仅在收盘 <0.633 时执行 |

**Investment Thesis**: 文本。

FINAL TRANSACTION PROPOSAL: **UNDERWEIGHT**
"""

TRADER_DRAFT = """**Action**: Underweight

**Entry Price**: 0.704

**Stop Loss**: 0.712

**Current Price**: 0.678

**Invalidation Price**: 0.712

**Support Levels** (nearest first): 0.6605 / 0.6484 / 0.633 / 0.601

**Resistance Levels** (nearest first): 0.69 / 0.708 / 0.74 / 0.7536

**Scale-out Ladder** (close % is of the original position):

| Rung | Trigger Price | Close % | Condition |
| --- | --- | --- | --- |
| 1 | 0.702 | 33 | 阻力区第一笔减仓 |
| 2 | 0.708 | 33 | 第二次触及 |
| 3 | 0.633 | 17 | 收盘破位 |
| 4 | 0.601 | 17 | 收盘清仓 |
"""


def test_direction_of_maps_ratings():
    assert direction_of("Sell") == "reduce"
    assert direction_of("Underweight") == "reduce"
    assert direction_of("Buy") == "buy"
    assert direction_of("Overweight") == "buy"
    assert direction_of("Hold") == "hold"
    assert direction_of(None) == "unknown"


def test_pm_star_labels_indicators():
    d = extract(PM_STAR)
    assert d["price_block_format"] == "pm_star_labels"
    assert d["direction"] == "reduce"
    assert d["current_price"] == 0.483
    assert d["stop_loss"] == 0.455
    assert d["support"] == [0.475, 0.467, 0.461, 0.456]
    assert d["resist"] == [0.4855, 0.4904, 0.499]
    assert d["price_target"] == 0.467
    assert "上修条件" in d["entry_zone"]
    assert "日收盘跌破" in d["reduce_trigger"]
    assert [x["price"] for x in d["ladder"]] == [0.483, 0.49, 0.499]
    assert d["ladder"][0]["condition"] == "现价限价减超额部分，不追打"
    assert d["ladder_sum"] == 70
    assert d["ladder_ok"] is True  # ≤100 合规，不是必须 =100


def test_pm_executable_block_indicators():
    d = extract(PM_EXEC)
    assert d["price_block_format"] == "pm_executable_block"
    assert d["direction"] == "reduce"
    assert d["current_price"] == 0.678
    assert d["stop_loss"] == 0.601
    assert d["support"][0] == 0.6605
    assert "回补确认四条件" in d["entry_zone"]
    assert "盘中破位不算" in d["reduce_trigger"]
    assert len(d["ladder"]) == 4
    assert d["ladder"][3]["close_pct"] == 25
    assert d["ladder_sum"] == 85
    assert d["ladder_ok"] is True


def test_trader_draft_stays_compatible_and_invents_nothing():
    d = extract(TRADER_DRAFT)
    assert d["price_block_format"] == "trader_draft"
    assert d["entry_price"] == 0.704
    assert d["stop_loss"] == 0.712
    assert d["invalidation_price"] == 0.712
    assert d["entry_zone"] is None        # PM 层字段不伪造
    assert d["reduce_trigger"] is None
    assert d["direction"] == "reduce"


def test_missing_fields_are_never_invented():
    d = extract("**Entry Price**: 1.0\n\n**Stop Loss**: 0.9")
    assert d["current_price"] is None
    assert d["entry_zone"] is None
    assert d["reduce_trigger"] is None
    assert d["support"] == []
    assert d["ladder"] == []
    assert d["direction"] == "unknown"
    assert "无数据" in render("510050.SS", Path("x.json"), d, "Hold")


def test_reduce_never_emits_build_order():
    d = extract(PM_STAR)
    md = render("159740.SZ", Path("x.json"), d, "Underweight",
                label="159740.SZ 恒生科技ETF", price_source="PM 最终层")
    assert "不生成建仓单" in md
    assert "## 三、建仓区" not in md
    assert "1/4 单位" not in md
    assert "越买越多" not in md
    assert "买卖指标" in md
    assert "159740.SZ 恒生科技ETF" in md
    assert "PM 最终层" in md


def test_hold_never_emits_build_order():
    d = extract("**Rating**: Hold\n\n**Current Price**: 1.406\n\n**Stop Loss**: 1.355")
    md = render("159539.SZ", Path("x.json"), d, "Hold")
    assert "不建仓、不加仓" in md
    assert "## 三、建仓区" not in md


def test_buy_keeps_build_zone_from_real_support():
    d = extract("**Rating**: Buy\n\n**Current Price**: 1.0\n\n"
                "**Support Levels** (nearest first): 0.95 / 0.90 / 0.85\n\n"
                "**Stop Loss**: 0.88")
    md = render("510050.SS", Path("x.json"), d, "Buy")
    assert "### 建仓区" in md
    assert "**0.95**" in md
    assert "**0.9**" in md
