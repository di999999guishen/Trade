# ETF 研究预测整体流程文档（TradingAgents）

> 最后更新：2026-10-07。本文档描述项目当前**实际执行**的研究预测全流程，含自动化编排、各步骤职责、数据源、模型配置、质量门、交付物与红线。
> 适用路径：`E:\Trade\TradingAgents`（唯一有效路径，C 盘副本已删）。

---

## 0. 一句话定位

本流程是 **A股 / 港股 / 美股 QDII ETF** 的「全市场资金流粗筛 → 严格滚动回测 → 逻辑回归概率 → 质量门 → **LLM 多智能体深度分析（AI agent，强制）** → 研究报告」自动化研究管线。

**铁律**：只用国内/不限流数据源（东方财富 / 新浪 / akshare / 腾讯），**禁用** yfinance / Reddit / StockTwits / FRED。

---

## 1. 每日自动化编排（三条定时任务）

| 时间 | 自动化 ID | 命令 | 职责 |
|---|---|---|---|
| **16:00** | `b459af1e-2d1b-4185-93f0-7cef48bd8e3c` | `run_research.py backfill` | 历史日线回填与结算（ENUM→REFRESH→SETTLE，约 12–14 分钟）。清空待回填清单，为当晚 cycle 备好数据。 |
| **18:00** | `automation-1784594402282` | `daily_predict.py` | 510050 / 515220 + SPY / QQQ / GLD 每日盘前预测（含 Polymarket 快照）。 |
| **19:00** | `7100f2bb-5876-4ad5-8fdf-aaadaa88a0fd` | `run_research.py cycle` → **`run_research.py run-agents --top 5`** | 全市场定量筛选 + 概率 + 质量门 + 冻结结算 → **AI agent 多智能体辩论（强制）** → 研究报告。 |

- 三条自动化**各有独立记忆目录**，执行前先 `automation_update list` 确认当次是哪条。
- 待抓集合 = `pending_history_assets()` + `_flow_screen_backlog()` 并集；backfill **必须做结算**，只刷新不结算则下轮重抓。
- **`pending_before` 上百属正常**（全 `waiting_for_horizon`），可直接进 cycle；**禁止带真实积压跑 cycle**。

---

## 2. 主流程步骤（19:00 自动化）

### 步骤 0 — 前置回填复查（关键，决定窗口内能否跑完）
- 先跑 `run_research.py backfill` 复查：若 `pending_before` 很小（个位数）或为 0 ⇒ 前置就绪，继续；若仍有上百只 ⇒ **先把 backfill 跑完再进 cycle**，绝不带积压跑 cycle（否则拖到数小时、当晚出不了报告）。
- 16:00 盘后自动化已独立完成回填时，本步通常直接就绪。

### 步骤 1 — 判交易日（必须先做）
```
curl -s "https://hq.sinajs.cn/list=sh000001" -H "Referer: https://finance.sina.com.cn"
```
看回显时间戳。休市日（如 2026 国庆 10/1–10/7）**不跑 cycle / run-agents**，只跑 backfill + 出 `etf_holiday_brief_<YYYYMMDD>.md` 休市简报（含 A50 期指跳空、QDII 溢价、港股表现）。10/8 开市恢复。

### 步骤 2 — 定量周期 `cycle --top 5`
全市场 ETF 快照 → 资金流拆解粗筛（主力/超大单净额+占比）→ 取前 5 → 5/20 日严格滚动回测 → 逻辑回归概率 → 质量门 → 冻结结算 → 研究报告。
- 若日志出现 `falling back to Sina`（东财快照失败回退新浪），属预期路径，继续执行，但必须在汇报声明降级（见 §4）。

### 步骤 3 — 分析/决策步骤（⭐ hy3 编排 + AI agent 强制）
对粗筛 top5 候选，先跑 **`run_research.py run-agents --top 5`**（TradingAgents 多智能体辩论，AI agent，**强制不可跳过**），再由 **hy3（助手）整合**框架定量结论 + AI agent 输出，产出最终 Buy / Overweight / Hold / Underweight / Sell 决策 + 关键支撑/阻力/止损价格块。
- **"只用 hy3 不许用其他模型"的正确解读**：指**不要硬编码 `deepseek-v4-pro` 或乱换其他非标准/付费外部模型**。run-agents 用的就是项目标准档 `deepseek-v4-flash`（来自 `.env`），它**不是"其他模型"，允许且必须跑**。
- **`research.json` 的 `workflow.skip_tradingagents` = false`**（2026-10-07 用户明确要求"这个流程必须跑"后维持；曾误设为 true 已回退）。
- ⭐ hy3 负责**编排与整合分析**（含本文档所述的全部汇报口径），但**不得用"只用 hy3"这句口径去压制 run-agents**——它俩是"框架跑辩论 + 助手整合"的互补关系，不是二选一。
- 量化数据（筛选/回测/概率/资金流）仍由框架 `cycle` 产出，hy3 在其上做判断。

### 步骤 4 — 汇总汇报
top5 候选的：定量涨概率（5日/20日）、质量门状态、资金流拆解（主力/超大单，及大单/超大单背离）、多智能体最终决策、关键支撑/阻力/止损。必须显式声明数据降级（§4）。

---

## 3. run-agents（AI agent）专项说明

| 项 | 说明 |
|---|---|
| 是什么 | **TradingAgents 多智能体辩论**，对粗筛 top5 做方向决策与价格块（见 §2 价格块定义） |
| 命令 | `run_research.py run-agents --top 5` |
| 模型 | `deepseek-v4-flash`（`.env` 开关，项目标准档）；**禁止**硬编码 `deepseek-v4-pro` |
| 是否强制 | **是**。2026-10-07 用户明确"这个流程必须跑"，`skip_tradingagents` 保持 false |
| 跳过后果 | cycle outcome 加 `_without_llm` 后缀，视为**流程缺环需补跑**（不是 partial_failure） |
| 休市日 | 不跑（与 cycle 同）；框架仅在正常交易日执行 |

**价格块必备字段**（Trader/PM 输出必须含）：`current_price`、`support_levels`、`resistance_levels`、`entry_zone`、`stop_loss`、`invalidation_price`、`reduce_trigger`、`scale_out_ladder`（2–4 档，每档 `trigger_price`+`close_pct`，合计 ≤100）。点位必须来自真实价格结构，无证据写 `null`，严禁编造。

---

## 4. 数据源与降级声明

**允许源（国内/不限流）**：东方财富（逐接口分别放通，不是整站开关）、新浪（既定回退源）、akshare、腾讯。
**禁用源**：yfinance / Reddit / StockTwits / FRED。

**降级必须声明**（当快照走新浪回退时）：
- 新浪只有「主力聚合 + 超大单」两层，无大/中/小单 ⇒ `order_divergence` 全为 `missing_data`。
- 新浪 K 线不含成交额 ⇒ `amount_status=unavailable`。
- 东财 `push2his`（含国内 secid）当前常被 IP 级 WAF 拦，间歇放通，**看到"恢复"绝不能写"已解封"**。
- 汇率日线不可用 ⇒ 跨假期累计汇率无法精确还原（±0.3pp 误差）。
- 低成交额候选（约 0.1–0.2 亿）得分易被 `flow_to_cap` 放大，实盘须按流动性折价，不得与成交额数亿候选等同推荐。
- 港股 ETF K 线历史（东财 push2his / 腾讯 HkKlineController / 新浪港股日线）三条源常同时不可用 ⇒ 港股分析退化为「实时报价 + 指数 52 周区间 + 宏观」区间定位，无精确 5/20 日回溯/ATR。

---

## 5. 质量门与已知问题（⛔ 持续未过，最高优先级待修）

- **连续多轮两周期全未过**（Brier 劣于基线；20d 曾 acc 0.4674）⇒ **概率数字不构成信号强度，汇报不得当信号强度讲**。
- **5 日模型零区分度**：五只概率全等、`discrimination_status=baseline_only`。与「全库 `probability_status` 无一通过校验」并案排查**校准环节**。
- **选股闸门**：`selection_usable` 判据是 `signal_verdict` 的 **ICIR ≥ 0.30**。生产 h20 实测 ICIR **−0.1634** → **选股用途一律禁用**，模型只能当择时/仓位开关。
- **结论漂移**：同一份数据不同日跑结论会漂移（同为 9/24 收盘，9/25 Hold、9/26 Underweight）。
- **停用成交额类特征**必须动手——新浪源本就无成交额，该特征是残缺输入。

---

## 6. 关键持仓价位（9/30 收盘基准，10/8 前不变）

| 标的 | 9/30 收盘 | 判断 | 观察位 | 硬止损 |
|---|---:|---|---|---|
| 510050 | 2.938 | **Underweight** | A50 隐含开盘 2.898–2.909 | **2.90**（开盘 <2.90 直接减、不等反抽） |
| 515220 | 1.256 | **Hold** | 1.230（优先） | **1.196** |

减法序（载体成本优先，10/8 开盘后执行）：159941 减 60–100% > 159660 减 50–100% > 513650 减 30–50% > 159643 清仓 > 159745 减 50% > 512690 减 30% > **159866 持有观察**（已折价）。

**QDII 溢价（10/07 口径）**：159941 **+10.45%** / 159660 **+8.00%** / 513650 **+5.51%** / 159866 **−2.08%（折价）**。溢价是买入即刻垫付的确定成本；高溢价时正确动作是**降低载体敞口**，而非押注涨跌。

---

## 7. 交付物路径

| 产物 | 路径 |
|---|---|
| 研究报告 | `prediction_research/runs/research_report_*.md` |
| cycle 结果 JSON | `prediction_research/runs/cycle_<date>_<ts>_<id>.json` |
| 回填报告 | `outputs/backfill_report_<YYYYMMDD>.md`（需手工补写，09-24 起持续不生成） |
| 休市简报 | `prediction_research/runs/etf_holiday_brief_<YYYYMMDD>.md` |
| run-agents 分析 | `prediction_research/runs/`（按 cycle 归档，经 settle 结算） |
| 日线缓存 | `prediction_research/datasets/market/cache_<symbol>.csv` |

---

## 8. 红线速查

1. **AI agent（run-agents）是主流程强制步骤**：`run-agents --top 5`（TradingAgents 辩论，标准档 `deepseek-v4-flash`）必须执行，`skip_tradingagents` 永远不得为 true（除非用户当次临时要求）。"只用 hy3 不许用其他模型"= 不硬编码 `deepseek-v4-pro`/乱换外部付费模型，run-agents 的标准档模型允许且必须跑。
2. **休市日不跑 cycle / run-agents**，只跑 backfill + 出简报。
3. **禁用外网限流源**（yfinance 等）；东财 push2 阻断是 IP 级 WAF，间歇放通不能写"已解封"。
4. **禁止硬编码 `deepseek-v4-pro`**；模型只用 `.env` 开关的 `deepseek-v4-flash`。
5. **价格点位必须来自真实价格结构**，无证据写 `null`，严禁编造。
6. **降级必须显式声明**（新浪回退的 order_divergence / amount_status、港股 K 线不可得、汇率误差）。
7. **概率数字不构成信号强度**（质量门持续未过）；低成交额候选须流动性折价。
8. 个人财务文件禁止入库；出口 IP 脱敏成 `113.25.x.x`。
