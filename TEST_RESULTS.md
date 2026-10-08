# TradingAgents 合并后测试结果（2026-09-15）

## 当前验收结论

已将 `C:\Users\Administrator\TradingAgents` 的代码修改合入 `E:\Trade\TradingAgents`，并保留 E 盘的时点校验、冻结记录、校准修复、六因子筛选和 agents 证据传递。

- 离线验证：**851 passed，71 subtests passed，1 skipped，1 deselected**，用时72.34秒。
- 跳过项为未安装可选 `langchain_aws` 的 Bedrock 测试；排除项为真实 DeepSeek API 联网测试。首轮联网测试被环境网络限制阻断，不能声称在线验证已通过。
- 粗筛：**同指数／同赛道每组最多1只，无总数量上限**；`screen-etfs --limit` 可显式限制。
- 量化：默认申请粗筛前 **20** 只；LLM预算仍为 **5**，cycle内仍配置跳过。
- 六因子：主力流30%、流动性25%、单日动量10%、大小单背离10%、资金流／市值10%、同类相对强度15%；缺失因子从分母排除。
- 五档资金流已接入采集、旧库迁移、冻结报告和 agents 输入；交易员保持五档评级。
- 尚未用新行情重跑完整 LLM 流程，也未验证新规则的收益或预测准确率。

```powershell
.venv/Scripts/python.exe -m pytest prediction_research/tests tests -m "not integration" -q --tb=short --disable-warnings
```

详细记录：[合并说明](prediction_research/docs/C_DRIVE_MERGE_20260915.md)。日志：`reports/c_drive_merge_20260915/pytest_offline.txt`。原 C 盘文档完整副本：`reports/c_drive_merge_20260915/TEST_RESULTS.source.md`。

---

# 以下为合并前 C 盘历史测试归纳

下列10项测试、三因子公式、旧评级和旧回测指标仅描述当时版本，不能代替上面的合并后配置或验收结果。

> 最后更新：2026-09-15
> 用途：给想关联 / 接手这套流程的人一个**结论先行**的现状快照，避免重复探索源码与运行记录。

---

## 一、一句话结论

- **单元测试全绿**：`prediction_research` 模块 10/10 通过（`unittest`，纯标准库，无网络依赖）。
- **完整流程可跑通**：`cycle --top 5`（定量，~2 分钟）+ `run-agents --top 5`（LLM 多智能体，~37 分钟）均成功产出报告。
- **但当前一切输出都是「研究信号」，不是「交易信号」**：模型 Brier 尚未跑赢历史上涨率基线，质量门连续未通过，全部标 `research_only_failed_validation`。

---

## 二、信号从哪来（三层职责，勿混淆）

| 层级 | 模块 | 产出 | 能否给买卖信号 |
|---|---|---|---|
| ① 粗筛 | `screening.py` | 候选池（同赛道去重，每组 1 只，不设上限） | ❌ 纯打分排序，无方向判断 |
| ② 定量预测 | `cycle.py` 回测 + 逻辑回归 | 5/20 日**涨概率** + 质量门状态 | ⚠️ 给概率，但质量门未过 = 仅研究 |
| ③ AI 校验 | `run-agents` 多智能体 | Buy/Hold/Sell **方向** + 支撑/阻力/止损 | ⚠️ 方向决策，但仍标研究信号 |

**粗筛既不能确认买入也不能确认卖出**，必须走完 ②③ 两级；而即便走完，当前质量门未达标时仍只能当研究参考。

粗筛公式（唯一打分依据）：

```text
screen_score = 0.50 × 主力净占比 + 0.35 × 对数成交额流动性 + 0.15 × 当日涨跌动量
```

---

## 三、单元测试（最新，10/10 通过）

运行命令（在项目根目录，用项目 venv）：

```powershell
.venv/Scripts/python.exe -m unittest prediction_research.tests.test_core -v
```

结果：`Ran 10 tests ... OK`

| 用例 | 覆盖点 |
|---|---|
| `test_label_starts_at_next_open` | 标签起点对齐次日开盘（无未来函数） |
| `test_walk_forward_has_point_in_time_boundary` | 滚动回测时点边界 |
| `test_prediction_is_idempotent` | 预测幂等去重 |
| `test_rss_parsing_and_relevance_mapping` | RSS 解析 + 敞口映射 |
| `test_broad_source_without_keyword_has_no_exposure` | 无关键词源不误判 |
| `test_rss_date_is_normalized_to_utc` | 时间归一化 |
| `test_etf_taxonomy_uses_separate_asset_and_market_axes` | ETF 资产/市场双轴分类 |
| `test_screen_group_dedupes_same_theme_across_fund_companies` | 同主题跨基金公司去重 |
| `test_screen_group_merges_same_index_and_same_track` | 同指数/同赛道归并 |
| `test_agent_decision_direction_is_not_a_probability` | LLM 决策方向→涨跌映射 |

> 根目录 `tests/`（62 个上游 pytest 用例）依赖 pytest + yfinance/FRED/API-key，与本项目「纯国内数据源」路线冲突，**本机不跑**。

---

## 四、完整流程运行结果

### 4.1 定量周期 `cycle --top 5`

- 全市场 ETF 快照 **1611 条** → 过滤后 **999 条**过门槛 → 粗筛同赛道去重（修复后 170 个赛道）→ 取前 5 深度预测。
- 运行 ~2 分钟，`tradingagents` 步骤在 cycle 内按配置跳过（属正常）。

### 4.2 LLM 深度分析 `run-agents --top 5`

5 个候选各跑 16 步多智能体辩论，产出方向决策。**注意**：最近一次 run-agents（2026-09-14）是在「粗筛去重修复」**之前**跑的，其中 159992 创新药ETF银华 与 159748 创新药ETF富国 属同一赛道重复入选。修复后 top5 构成会变（见 4.3），**完整流程尚未在修复后重跑**。

修复前 top5 决策（供参考）：

| 代码 | 名称 | 20日涨概率 | LLM 决策 | 止损 |
|---|---|---|---|---|
| 159748 | 创新药ETF富国 | 46.49% | Hold | 0.775 |
| 159825 | 农业ETF富国 | 48.45% | Hold | 0.71 |
| 159837 | 生物科技ETF易方达 | 47.63% | Hold | 0.477 |
| 159992 | 创新药ETF银华 | 47.35% | Underweight | 0.795 |
| 563080 | 中证A50ETF易方达 | 49.13% | Hold | 1.34 |

### 4.3 粗筛去重修复后的 top5 预览（重放验证，未跑 LLM）

| 排名 | 代码 | 名称 | 粗筛分 |
|---|---|---|---|
| 1 | 159748 | 创新药ETF富国 | 0.6978 |
| 2 | 159825 | 农业ETF富国 | 0.6661 |
| 3 | 563080 | 中证A50ETF易方达 | 0.6518 |
| 4 | — | 消费50ETF富国 | 0.6444 |
| 5 | — | 半导体ETF博时 | 0.6362 |

> 变化原因：生物科技ETF易方达（0.6655）与创新药同属 `pharma` 赛道被去重，只留分数最高的创新药ETF富国。

---

## 五、质量门现状（关键约束）

| 周期 | Brier | 基线（历史上涨率） | 是否通过 |
|---|---|---|---|
| 5 日 | 0.2534 | 0.2509 | ❌ 未通过 |
| 20 日 | 0.2675 | 0.2526 | ❌ 未通过 |

- 状态标签：`research_only_failed_validation`（仅研究，非交易信号）。
- 20 日概率全面 <50%（46.5%~49.1%），整体偏空，连续多轮无 Buy。
- 升级为可交易信号的前提：Brier 严格样本外跑赢历史上涨率基线。

---

## 六、最近修复与提交（2026-09-15）

| 提交 | 内容 |
|---|---|
| `ba155c1` | `daily_predict.py` 标的 159063.SZ(粮食) → 515220.SS(煤炭) |
| `c98e362` | 修复 `screen_group` 兜底把基金公司名混入分组键，导致 `max_per_group=1` 失效（创新药 6 只重复入选） |
| `53d9e86` | 粗筛去重升级为「同指数/同赛道只留 1 只」，并去掉 20 只上限 |

---

## 七、数据源与模型档位（避免踩坑）

- **数据源**：东方财富（push2his / ETF 快照 / 资金流五档）+ akshare + 新浪（K 线降级）。**禁用** yfinance / Reddit / StockTwits / FRED。
- **代理**：本机需走系统代理（127.0.0.1:17891），`run_research.py` 内部已做国内域名旁路。`push2his.eastmoney.com`（K 线）本机不可达 → 日线自动降级新浪。
- **模型档位**：`deep_think = deepseek-v4-pro`（研究经理 + 组合经理两个核心环节），`quick_think = deepseek-v4-flash`（其余环节）。
- **资金流五档**：主力/超大单/大单/中单/小单（净额+占比），其中大单信号与超大单背离是人工解读重点；五档仅作审计字段，未纳入粗筛公式。

---

## 八、快速验证命令（接手必读）

```powershell
# 单元测试（无网络，秒级）
.venv/Scripts/python.exe -m unittest prediction_research.tests.test_core -v

# 完整定量周期（联网，~2 分钟）
.venv/Scripts/python.exe run_research.py cycle --top 5

# LLM 深度分析（联网 + DeepSeek API，~37 分钟）
.venv/Scripts/python.exe run_research.py run-agents --top 5

# 离线复跑（用已冻结快照，不联网，用于代码验收）
.venv/Scripts/python.exe -m prediction_research.cli cycle --top 5 --skip-fetch
```

详细命令与参数见 `prediction_research/USAGE.md`。
