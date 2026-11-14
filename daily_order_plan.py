"""从 TradingAgents 每日预测 JSON 提取执行清单（交易执行协议 第1-2 步）。

用法：
    ./.venv/Scripts/python.exe daily_order_plan.py 515220.SS
    ./.venv/Scripts/python.exe daily_order_plan.py 515220.SS --date 20261014

输出：可直接照抄挂条件单的清单（Markdown），并对红灯项给出否决提示。
价位来源唯一：results/daily/<ticker>_<run_id>.json，缺失字段一律写「无数据」，
不做任何推测或补全。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

RESULTS_DIR = Path("results/daily")

# 协议红线（docs/trading_execution_protocol.md §六）
MACRO_TRIGGERS = (
    "美 CPI/PCE 超预期",
    "鹰派表态",
    "地缘升级",
    "单夜纳指跌 >2%",
)

# 从自由文本里抓价格块（Trader 草案版式）
PATTERNS = {
    "stop_loss": r"\*\*Stop Loss\*\*:\s*([0-9.]+)",
    "entry_price": r"\*\*Entry Price\*\*:\s*([0-9.]+)",
    "current_price": r"\*\*Current Price\*\*:\s*([0-9.]+)",
    "invalidation_price": r"\*\*Invalidation Price\*\*:\s*([0-9.]+)",
}
SUPPORT_RE = re.compile(r"\*\*Support Levels\*\*[^\n]*:\s*([0-9./\s]+)")
RESIST_RE = re.compile(r"\*\*Resistance Levels\*\*[^\n]*:\s*([0-9./\s]+)")
SCALE_RE = re.compile(r"\*\*Scale-out Ladder\*\*.*?\n\n((?:\|.*\n)+)")
ROW_RE = re.compile(r"\|\s*(\d+)\s*\|\s*([0-9.]+)\s*\|\s*(\d+)\s*\|")
PM_STOP_RE = re.compile(r"硬止损\s*([0-9.]+)")

# ── PM 最终决策层版式 ────────────────────────────────────────────────────────
# 2026-11-13：Trader 草案（**Entry Price**:）与 PM 最终层是**不同层**，后者的
# 必备字段为 current_price / support_levels / resistance_levels / entry_zone /
# stop_loss / invalidation_price / reduce_trigger / scale_out_ladder。PM 层有两种版式：
#   A 型：**Current Price**: x / **Entry / Add Zone**: 文本 / **Reduce Trigger**: 文本
#   B 型：**Executable Price Block**: 后接 `- key: value` 列表
# 决策标签（Hold/Underweight/Sell）来自 PM 层，故挂单价位也须优先取 PM 层，
# 否则会出现「评级 Sell、清单却叫建仓」的层级错配。缺字段一律 None/[]，不推测。
_HEADING_RE = re.compile(
    r"\n\*\*(?:Rating|Executive Summary|Investment Thesis|Price Target|Time Horizon|"
    r"Current Price|Support Levels|Resistance Levels|Entry / Add Zone|Entry Price|"
    r"Entry Zone|Stop Loss|Reduce Trigger|Scale-out Ladder|Invalidation Price|"
    r"Position Sizing|Action|Reasoning|Executable Price Block)\*\*", re.I)
_BULLET_RE = re.compile(r"^\s*-\s*([a-z_]+)\s*:\s*(.*)$", re.M | re.I)
_LADDER_ROW_RE = re.compile(
    r"^\|\s*(\d+)\s*\|\s*([0-9.]+)\s*\|\s*(\d+)\s*%?\s*\|\s*([^|]*)\|", re.M)

_REDUCE_WORDS = ("SELL", "UNDERWEIGHT", "REDUCE", "清仓", "减仓", "减配", "卖出", "减持")
_BUY_WORDS = ("BUY", "OVERWEIGHT", "ACCUMULATE", "买入", "增持", "加仓", "建仓")
_HOLD_WORDS = ("HOLD", "观望", "持有", "中性")


def direction_of(action):
    """把评级归到买入 / 减配卖出 / 观望 / 未知，供挂单文案分方向渲染。"""
    s = (action or "").strip().upper()
    if not s:
        return "unknown"
    if any(w in s for w in _REDUCE_WORDS):
        return "reduce"
    if any(w in s for w in _BUY_WORDS):
        return "buy"
    if any(w in s for w in _HOLD_WORDS):
        return "hold"
    return "unknown"


def _num(text):
    m = re.search(r"[0-9]+(?:\.[0-9]+)?", text or "")
    return float(m.group(0)) if m else None


def _levels(text):
    return [float(x) for x in re.findall(r"[0-9]+\.[0-9]+", text or "")]


def _section(text, label):
    """A 型：从 **Label**: 起，到下一个已知加粗标题之间（含表格）。"""
    m = re.search(r"\*\*" + re.escape(label) + r"\*\*[^\n]*?:\s*", text, re.I)
    if not m:
        return None
    start = m.end()
    nxt = _HEADING_RE.search(text, start)
    return text[start:nxt.start() if nxt else len(text)].strip()


def _bullet(text, key, bullets):
    """B 型：从 - key: 起取该行；表格类另由 _ladder_section 处理。"""
    return bullets.get(key)


def _ladder_section(text, bullets):
    section = _section(text, "Scale-out Ladder")
    if section:
        return section
    m = re.search(r"^\s*-\s*scale_out_ladder\s*:\s*$", text, re.M | re.I)
    if not m:
        return ""
    start = m.end()
    nxt = _HEADING_RE.search(text, start)
    return text[start:nxt.start() if nxt else len(text)].strip()


def latest_json(ticker: str, date: str | None, results_dir: str | None = None) -> Path:
    root = Path(results_dir) if results_dir else RESULTS_DIR
    if date:
        hits = sorted(root.glob(f"{ticker}_{date}_*.json"))
    else:
        hits = sorted(root.glob(f"{ticker}_*.json"), key=lambda p: p.stat().st_mtime)
    hits = [p for p in hits if not p.name.startswith("error_")]
    if not hits:
        raise FileNotFoundError(f"{root} 下没有 {ticker} 的预测 JSON")
    return hits[-1]


def extract(plan_text: str) -> dict:
    """解析买卖指标；Trader 草案与 PM 最终层两种版式都认，缺字段不推测、不补价。

    旧键（current_price / entry_price / stop_loss / invalidation_price / support /
    resist / ladder / ladder_sum / ladder_ok / raw_found）保持兼容；新增 PM 层字段
    entry_zone / reduce_trigger / stop_loss_text / price_target / time_horizon /
    action / direction / price_block_format。
    """
    text = plan_text or ""
    out: dict = {"raw_found": bool(plan_text)}

    # 1) Trader 草案版式（旧键，向后兼容）
    for key, pat in PATTERNS.items():
        m = re.search(pat, text)
        out[key] = float(m.group(1)) if m else None
    m = SUPPORT_RE.search(text)
    out["support"] = _levels(m.group(1)) if m else []
    m = RESIST_RE.search(text)
    out["resist"] = _levels(m.group(1)) if m else []
    out["ladder"] = []
    m = SCALE_RE.search(text)
    if m:
        for rung, price, pct in ROW_RE.findall(m.group(1)):
            out["ladder"].append({"rung": int(rung), "price": float(price),
                                  "close_pct": int(pct), "condition": ""})

    # 2) PM 最终层：B 型 - key: 列表 / A 型 **Label**: 段落
    bullets = {k.lower(): v.strip() for k, v in _BULLET_RE.findall(text)}
    exec_block = "**Executable Price Block**" in text

    def pm(key, label):
        return bullets.get(key) or _section(text, label)

    cur = pm("current_price", "Current Price")
    sup = pm("support_levels", "Support Levels")
    res = pm("resistance_levels", "Resistance Levels")
    ent = pm("entry_zone", "Entry / Add Zone") or _section(text, "Entry Zone")
    stp = pm("stop_loss", "Stop Loss")
    inv = pm("invalidation_price", "Invalidation Price")
    red = pm("reduce_trigger", "Reduce Trigger")
    ptarget = _section(text, "Price Target")
    horizon = _section(text, "Time Horizon")

    if out["current_price"] is None and cur:
        out["current_price"] = _num(cur)
    if out["stop_loss"] is None and stp:
        out["stop_loss"] = _num(stp)
    if out["invalidation_price"] is None and inv:
        out["invalidation_price"] = _num(inv)
    if not out["support"] and sup:
        out["support"] = _levels(sup)
    if not out["resist"] and res:
        out["resist"] = _levels(res)

    ladder_section = _ladder_section(text, bullets)
    if ladder_section:
        parsed = [{"rung": int(r), "price": float(p), "close_pct": int(c),
                   "condition": cond.strip()}
                  for r, p, c, cond in _LADDER_ROW_RE.findall(ladder_section)]
        if parsed:
            out["ladder"] = parsed  # 带触发条件，优先于 Trader 草案的无条件表格

    out["entry_zone"] = ent
    out["reduce_trigger"] = red
    out["stop_loss_text"] = stp
    out["price_target"] = _num(ptarget)
    out["time_horizon"] = horizon

    m = (re.search(r"\*\*(?:Rating|Action)\*\*\s*[:\*]*\s*([A-Za-z][A-Za-z/ ]{1,30})", text)
         or re.search(r"FINAL TRANSACTION PROPOSAL:\s*\**\s*([A-Za-z][A-Za-z/ ]{1,30})", text))
    out["action"] = m.group(1).strip() if m else None
    out["direction"] = direction_of(out["action"])
    out["price_block_format"] = (
        "pm_executable_block" if exec_block else
        "pm_star_labels" if (ent or red) else
        "trader_draft" if text else "none")

    total = sum(x["close_pct"] for x in out["ladder"])
    out["ladder_sum"] = total
    # 输出规范：2-4 档、合计 ≤100%（不足部分由 reduce_trigger 补足），不是必须正好 100%。
    out["ladder_ok"] = (2 <= len(out["ladder"]) <= 4 and 0 < total <= 100) if out["ladder"] else None
    return out


def _shown(value):
    """缺字段一律「无数据」，绝不补造价位。"""
    if value is None or value == "" or value == []:
        return "无数据"
    if isinstance(value, (list, tuple)):
        return " / ".join(str(x) for x in value) if value else "无数据"
    return str(value)


def _cell(value, limit=None):
    """表格单元格：去掉竖线与换行，避免破坏 Markdown；可选截断。"""
    s = " ".join(_shown(value).replace("|", "／").split())
    if limit and len(s) > limit:
        s = s[:limit] + "…"
    return s


def render(ticker: str, path: Path, d: dict, note: str | None,
           label: str | None = None, price_source: str = "未标注") -> str:
    """按评级方向渲染买卖指标；reduce / hold / unknown 一律不生成建仓指令。

    2026-11-13 修：旧版无条件输出「建仓区 / 止损条件单 / 越买越多金字塔」，
    Sell 与 Underweight 的标的会被误导成建仓单（且空头止损高于现价时被写成
    「触发即市价离场」）。现在按 direction 分支，并显式标注价位来源层级。
    """
    direction = d.get("direction") or direction_of(note)
    rating = note or d.get("action") or "（见预测 JSON）"
    direction_label = {"buy": "买入 / 加仓", "reduce": "减配 / 卖出",
                       "hold": "观望 / 持有", "unknown": "未识别（不下单）"}.get(
                           direction, "未识别（不下单）")
    L: list[str] = []
    A = L.append
    A(f"# 执行清单 · {label or ticker}")
    A(f"> 价位来源：`{path}`（{price_source}；版式 {d.get('price_block_format') or '未识别'}）")
    A(f"> 生成命令：`./.venv/Scripts/python.exe daily_order_plan.py {ticker}`")
    A("> ⚠️ 全部价位直接取自模型文本，缺项写「无数据」，不推测、不补价。")
    A("> ⚠️ 质量门 ICIR < 0.30 未过 ⇒ 选股禁用；以下仅为纪律价位 / 条件单参考，**不构成买入建议**。")
    A("")
    A("## 一、模型结论")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A(f"| Rating | {_cell(rating)} |")
    A(f"| 执行方向 | {direction_label} |")
    A(f"| 现价 | {_shown(d.get('current_price'))} |")
    A(f"| 目标价 | {_shown(d.get('price_target'))} |")
    A(f"| 时间窗 | {_cell(d.get('time_horizon'))} |")
    A("")
    A("## 二、买卖指标")
    A("")
    A("| 指标 | 值 | 说明 |")
    A("|---|---|---|")
    A(f"| 买入 / 加仓触发 `entry_zone` | {_cell(d.get('entry_zone'))} | 含前置条件，条件未满足不挂单 |")
    A(f"| 入场价 `entry_price`（草案单点） | {_shown(d.get('entry_price'))} | 仅买入方向适用 |")
    A(f"| 止损 `stop_loss` | {_shown(d.get('stop_loss'))} | {_cell(d.get('stop_loss_text'))} |")
    A(f"| 证伪价 `invalidation_price` | {_shown(d.get('invalidation_price'))} | 触发即认定判断失效 |")
    A(f"| 减仓触发 `reduce_trigger` | {_cell(d.get('reduce_trigger'))} | 盘中 / 收盘口径照原文 |")
    A(f"| 支撑（近→远） | {_shown(d.get('support'))} | 价格结构位 |")
    A(f"| 阻力（近→远） | {_shown(d.get('resist'))} | 价格结构位 |")
    A("")
    A("## 三、执行方向")
    A("")
    if direction == "reduce":
        A("- 本轮评级为**减配 / 卖出**：**不生成建仓单**。无持仓者不新建；已持有者按第五节分批减仓阶梯与 `reduce_trigger` 处理。")
        if d.get("stop_loss") and d.get("current_price") and d["stop_loss"] > d["current_price"]:
            A(f"- ⚠️ `stop_loss={d['stop_loss']}` **高于现价 {d['current_price']}**：属反向证伪 / 空头止损位，**不是多头建仓止损**，不可按「跌破离场」挂单。")
    elif direction == "buy":
        A("### 建仓区（由支撑带推导，禁止超出上沿）")
        A("")
        if len(d.get("support") or []) >= 2:
            lo, hi = d["support"][1], d["support"][0]
            A(f"- 首档（带内上沿附近）：**{hi}**，数量 1/4 单位")
            A(f"- 二档（带内下沿附近）：**{lo}**，数量 1/3 单位")
            A(f"- ⚠️ 高于 {hi} 的价格 = 追高，**不挂单**")
        else:
            A("- ⚠️ 支撑数据不足，**不挂建仓单**（协议红线：无价位来源不得下单）")
    elif direction == "hold":
        A("- 本轮评级为**观望 / 持有**：不建仓、不加仓；支撑 / 阻力仅作观察位。")
    else:
        A("- 方向未识别 ⇒ **不生成任何下单指令**，仅保留指标供人工核对。")
    A("")
    A("## 四、止损 / 证伪条件单")
    A("")
    if d.get("stop_loss"):
        if direction == "reduce":
            if d.get("current_price") and d["stop_loss"] > d["current_price"]:
                nature = "反向证伪位（高于现价，触发代表减配判断失效）"
            else:
                nature = "下行破位离场位（低于现价，触发即按减仓纪律离场）"
            A(f"- 触发价：**{d['stop_loss']}**；性质：{nature}，**不是多头建仓止损**。")
        else:
            A(f"- 触发价：**{d['stop_loss']}**（取结构位，避开整数噪声带）")
            A("- 类型：止损条件单，触发即市价离场")
    else:
        A("- ⚠️ 模型未给止损位 → **否决建仓**（协议红线：无止损位不挂建仓单）")
    A("")
    A("## 五、分批减仓 / 止盈阶梯")
    A("")
    if d.get("ladder"):
        A("| 档 | 触发价 | 减仓比例 | 触发条件（原文） | 条件单类型 |")
        A("|---|---|---|---|---|")
        for x in d["ladder"]:
            A(f"| {x['rung']} | {x['price']} | {x['close_pct']}% | "
              f"{_cell(x.get('condition'))} | 条件单（按原文条件） |")
        flag = ("✅ 档数与合计合规（2-4 档、≤100%；不足部分由 reduce_trigger 补足）"
                if d.get("ladder_ok") else "❌ 档数或合计超出规范，须人工核对")
        A("")
        A(f"合计 {d.get('ladder_sum', 0)}%　{flag}")
    else:
        A("- ⚠️ 模型未给阶梯 → 无数据")
    A("")
    A("## 六、下单前闸门自检")
    A("")
    A("- [ ] 宏观外生冲击已核查（" + " / ".join(MACRO_TRIGGERS) + "）")
    A("      → 命中任一项：**次日开盘前 30 分钟不建仓不加仓**")
    if direction == "buy":
        A("- [ ] 建仓价在支撑带内（未超上沿）")
        A("- [ ] 已挂止损条件单，价位取自模型 `stop_loss`")
        A("- [ ] 各档数量符合金字塔（越买越多，非等量）")
    else:
        A("- [ ] 本轮为减配 / 观望：确认**未挂任何建仓单**")
        A("- [ ] 减仓触发口径（盘中 / 收盘）已按原文核对")
    A("- [ ] 股数 / 成本 / 总资产已补全（否则结论降级为「不可核算」）")
    A("- [ ] 跨境 ETF 建仓前已填同日溢价（>5% 否决、≥3% 提示）")
    A("")
    A("---")
    A("")
    A("挂单后请把实际成交（价 / 量 / 时间）发给 Agent，由 Agent 写入")
    A("`trading_journal/YYYY-MM-DD.md` 并复盘偏差。")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--date", help="YYYYMMDD，缺省取最新")
    ap.add_argument("--out", help="写入文件路径")
    ap.add_argument("--results-dir", help="预测 JSON 目录；新池产物在 <run>/daily/ 时用此参数指定")
    args = ap.parse_args()

    path = latest_json(args.ticker, args.date, args.results_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = payload.get("result", payload)
    plan_text = result.get("trader_investment_plan") or ""
    final_text = result.get("final_trade_decision") or ""
    if not plan_text and not final_text:
        raise SystemExit("该预测 JSON 没有 trader_investment_plan / final_trade_decision")

    # 2026-11-13：评级取自 PM 最终层，价位也优先取同一层，避免层级错配。
    primary = final_text or plan_text
    d = extract(primary)
    note = None
    m = re.search(r"\*\*Rating\*\*:\s*\**([A-Za-z/ ]+)", final_text)
    if m:
        note = m.group(1).strip()

    md = render(args.ticker, path, d, note,
                price_source="PM 最终层" if final_text else "Trader 草案")
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"written: {args.out}")
    else:
        print(md)


if __name__ == "__main__":
    main()