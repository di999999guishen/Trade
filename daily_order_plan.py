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

# 从自由文本里抓价格块
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


def latest_json(ticker: str, date: str | None) -> Path:
    if date:
        hits = sorted(RESULTS_DIR.glob(f"{ticker}_{date}_*.json"))
    else:
        hits = sorted(RESULTS_DIR.glob(f"{ticker}_*.json"), key=lambda p: p.stat().st_mtime)
    hits = [p for p in hits if not p.name.startswith("error_")]
    if not hits:
        raise FileNotFoundError(f"results/daily 下没有 {ticker} 的预测 JSON")
    return hits[-1]


def extract(plan_text: str) -> dict:
    out: dict = {"raw_found": bool(plan_text)}
    for key, pat in PATTERNS.items():
        m = re.search(pat, plan_text)
        out[key] = float(m.group(1)) if m else None

    m = SUPPORT_RE.search(plan_text)
    out["support"] = [float(x) for x in re.findall(r"[0-9]+\.[0-9]+", m.group(1))] if m else []
    m = RESIST_RE.search(plan_text)
    out["resist"] = [float(x) for x in re.findall(r"[0-9]+\.[0-9]+", m.group(1))] if m else []

    out["ladder"] = []
    m = SCALE_RE.search(plan_text)
    if m:
        for rung, price, pct in ROW_RE.findall(m.group(1)):
            out["ladder"].append(
                {"rung": int(rung), "price": float(price), "close_pct": int(pct)}
            )

    total = sum(x["close_pct"] for x in out["ladder"])
    out["ladder_sum"] = total
    out["ladder_ok"] = (total == 100) if out["ladder"] else None
    return out


def render(ticker: str, path: Path, d: dict, note: str | None) -> str:
    L: list[str] = []
    A = L.append
    A(f"# 执行清单 · {ticker}")
    A(f"> 价位来源：`{path}`")
    A(f"> 生成命令：`./.venv/Scripts/python.exe daily_order_plan.py {ticker}`")
    A("")
    A("## 一、模型结论")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A(f"| Rating | {note or '（见 JSON final_trade_decision）'} |")
    A(f"| 现价 | {d['current_price'] or '无数据'} |")
    A(f"| Entry Price | {d['entry_price'] or '无数据'} |")
    A(f"| 止损 | {d['stop_loss'] or '无数据'} |")
    A(f"| 证伪价 | {d['invalidation_price'] or '无数据'} |")
    A("")
    A("## 二、支撑 / 阻力（近→远）")
    A("")
    A(f"- 支撑：{' / '.join(str(x) for x in d['support']) or '无数据'}")
    A(f"- 阻力：{' / '.join(str(x) for x in d['resist']) or '无数据'}")
    A("")
    A("## 三、建仓区（由支撑带推导，禁止超出上沿）")
    A("")
    if len(d["support"]) >= 2:
        lo, hi = d["support"][1], d["support"][0]
        A(f"- 首档（带内上沿附近）：**{hi}**，数量 1/4 单位")
        A(f"- 二档（带内下沿附近）：**{lo}**，数量 1/3 单位")
        A(f"- ⚠️ 高于 {hi} 的价格 = 追高，**不挂单**")
    else:
        A("- ⚠️ 支撑数据不足，**不挂建仓单**（协议红线：无价位来源不得下单）")
    A("")
    A("## 四、止损条件单")
    A("")
    if d["stop_loss"]:
        A(f"- 触发价：**{d['stop_loss']}**（取结构位，避开整数噪声带）")
        A("- 类型：止损条件单，触发即市价离场")
    else:
        A("- ⚠️ 模型未给止损位 → **否决建仓**（协议红线：无止损位的建仓不挂单）")
    A("")
    A("## 五、止盈阶梯")
    A("")
    if d["ladder"]:
        A("| 档 | 触发价 | 减仓比例 | 条件单类型 |")
        A("|---|---|---|---|")
        for x in d["ladder"]:
            A(f"| {x['rung']} | {x['price']} | {x['close_pct']}% | 限价条件单 |")
        flag = "✅ 合计 100%" if d["ladder_ok"] else "❌ 合计不等于 100%，须修正"
        A("")
        A(f"合计 {d['ladder_sum']}%　{flag}")
    else:
        A("- ⚠️ 模型未给阶梯 → 无数据")
    A("")
    A("## 六、下单前闸门自检")
    A("")
    A("- [ ] 宏观外生冲击已核查（" + " / ".join(MACRO_TRIGGERS) + "）")
    A("      → 命中任一项：**次日开盘前 30 分钟不建仓不加仓**")
    A("- [ ] 建仓价在支撑带内（未超上沿）")
    A("- [ ] 已挂止损条件单，价位取自模型 `stop_loss`")
    A("- [ ] 各档数量符合金字塔（越买越多，非等量）")
    A("- [ ] 股数 / 成本 / 总资产已补全（否则结论降级为「不可核算」）")
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
    args = ap.parse_args()

    path = latest_json(args.ticker, args.date)
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = payload.get("result", payload)
    plan_text = result.get("trader_investment_plan") or ""
    final_text = result.get("final_trade_decision") or ""
    if not plan_text and not final_text:
        raise SystemExit("该预测 JSON 没有 trader_investment_plan / final_trade_decision")

    d = extract(plan_text or final_text)
    note = None
    m = re.search(r"\*\*Rating\*\*:\s*\**([A-Za-z/ ]+)", final_text)
    if m:
        note = m.group(1).strip()

    md = render(args.ticker, path, d, note)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"written: {args.out}")
    else:
        print(md)


if __name__ == "__main__":
    main()