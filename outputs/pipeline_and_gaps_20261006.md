# 加入回测后的完整流程与待完善清单

> 生成时间：2026-10-06
> 依据：`cycle.py` 实际执行链、`research.json` 当前配置、`state/research.db` 实况、`runs/` 最近四次 cycle

---

## 一、先说三个纠正

写总结前先纠正我前几轮的三个说法，避免拿着错结论做事：

| 我之前的说法 | 实际情况 |
|---|---|
| "TradingAgents 主模型从未被验证" | **机制早就齐全**：`run_tradingagents → agent_analyses → settle_agent_analyses → agent_outcomes` 在 `cycle.py` 里是必经步骤。问题是主链路已被配置关闭 |
| "`experiment_universe` 默认是 commodity" | **`experiment_universe` 实际是 `None`**。每日 cycle 的回测与预测跑的是 `screened_current`（3 只、动态产出自当日粗筛），不是我这几天验证的 commodity/cached |
| "回测只是事后验证" | 回测在流程里有**前置否决权**：`pipeline._validation_gate` 决定是否放行当日预测 |

第三条纠正后反而暴露了最严重的问题——**否决权用的是错的判据**，见 P0-3。

---

## 二、当前完整流程（`cycle.py` 实际执行链）

```
1  fetch_etf_snapshot      全市场 ETF 快照 → ingest 入 SQLite
2  fetch_news              新闻源快照与打分
3  screen_etfs             资金流 + 流动性粗筛 → top_n → screened_current
4  pending_history_assets  找出缺历史的标的
5  fetch_tracked_histories 补齐缺失历史
6  candidate_history_status 数据就绪检查（不足则 exclusion）
7  historical_flow_screen  历史资金流分层回测
8  [per horizon 5 / 20]
   8a training_readiness   样本量校验：≥80 行 且 ≥252 交易日 且 标签含两类
   8b backtest             walk-forward，产出冻结 JSON
   8c predict              ← 携带上一次 backtest 的上下文
       └─ _validation_gate  ← 【回测的唯一卡点在这】
          通过 → 正常预测
          失败 → research_only_failed_validation（仍产出，但不给交易建议）
9  tradingagents           LLM 多智体 ← 【当前被 skip】
10 settle_predictions / settle_agent_analyses / settle_screen_selections
11 build_report → archive_cycle（含哈希校验的不可变归档）
```

回测的位置是 **`backtest → gate → predict`**，即「先验后放」而非事后复盘。这个结构是对的。

设计的严谨处值得一提：`predict` 会校验携带的 backtest JSON 的 `universe/horizon/feature_set/model_scope/model_version` 五项是否完全匹配，不匹配直接抛 `ValueError`。这是防「用 A 条件下的回测给 B 条件下的预测背书」。

---

## 三、我这三天加的东西：在流程之外

我加的是**独立的外围验证层**，没有接入 cycle：

| 产出 | 位置 | 作用 |
|---|---|---|
| `etf_data_source.py` | 项目内 | 数据源三级降级 eastmoney→sina→tencent |
| `selection_metrics()` | `evaluation.py` 追加 | IC / RankIC / ICIR / 分位组 / spread |
| `signal_verdict()` `excess_metrics()` `decision_report()` | `evaluation.py` 追加 | 用法闸门、强制基准、几何超额 |
| `constrained_backtest.py` | 工作区 | 连续净值回测，默认含滑点、强制买入持有基准、双模式 |
| `ic_analysis.py` `window_scan.py` `summarize_windows.py` | 工作区 | IC 分析、窗口扫描、跨环境矩阵 |

**这些目前不参与每日决策。** 这是 P1-1 的由来。

---

## 四、待完善清单（按严重度）

### P0-1 主链路处于关闭态，且系统不告警

```json
"skip_tradingagents": true,
"skip_reason": "user_requested_skip_authentication_blocker"
```

09-28 / 09-29 / 09-30 三次 cycle 里 `tradingagents` 均为 `skipped_by_configuration`，
而三次的 `outcome` 仍是 **`complete_with_data_waits`**。

含义是：**你现在每天跑的这套系统，产出的预测 100% 来自量价 logistic，TradingAgents 主模型的实际贡献为零。** 而 `skipped_by_configuration` 被当作正常状态，不会计入失败，所以没人会发现它停了。

`agent_analyses` 只有 8 行（最后一条 2026-09-15），`agent_outcomes` 只有 5 行。

### P0-2 即使恢复主链路，有效样本依然会是 0

仅有的 5 条成功分析，**决策字段全部是 `Hold`**：

| 标的 | 方向 | 实际 h5 收益 | 判定值 |
|---|---|---:|---|
| 159516 | Hold | +6.89% | `None` |
| 589850 | Hold | +7.31% | `None` |
| 588170 | Hold | +8.27% | `None` |
| 588770 | Hold | +6.90% | `None` |
| 516050 | Hold | +6.13% | `None` |

`tradingagents_adapter.py:185` 规定 `neutral`/`unclassified` 一律记 `correct = None`。
这是**正确的严谨设计**（不臆断中性标签的方向），但副作用直接：

> 多智体辩论天然倾向输出保守的 Hold，而评估体系对 Hold 不计分。两者结合 = 有效样本无限期卡在 0。

那 5 只标的当周全部上涨 6-8%，系统连「踏空了」都没记录。

这是**设计层面的死结**，不是跑量能解决的——跑 1000 条 analysis，可能 900 条是 None。

### P0-3 生产闸门用的判据，正是我证明不充分的那个

`pipeline.py:188`：

```python
if gate.get("require_brier_below_historical_rate", True)
   and metric["brier"] >= metric["historical_rate_brier"]:
    return {"passed": False, "reason": "Brier score did not beat historical-rate baseline"}
```

而这几天的结论是：**Brier 有 skill ≠ 有选股能力**。

- commodity h20：Brier skill **+0.00528**（block bootstrap CI 全在正区间，Bonferroni 六重校正通过）→ 闸门**放行**
- 同一组合的 ICIR = **0.051**，IC = 0.0267，行业及格线 0.3 → **没有选股能力**

**闸门在放行一个「校准合格但大概率不会选股」的模型。** 这是现有产物里最值得着急的一处。

建议：把已写好的 `signal_verdict(ICIR_GATE=0.30)` 接进 `_validation_status`，让 `passed` 的含义从「能校准」升级为「能排序」，并把 `usages` 写入每条 prediction。

### P1-1 回测池与生产池错位

- cycle 每日实际跑的是 `screened_current`（3 只，由当日粗筛动态产出）
- 我验证的是 `commodity`（8 只）/ `cached`（10 只）
- `experiment_universe` 为 `None`，两者没有显式绑定

在这两天结论落到 `screened_current` 之前，它们不能直接用于当日决策。

### P1-2 新的验证层没有接入 cycle

`constrained_backtest` / `ic_analysis` / `window_scan` 都是手动脚本。建议至少在 `cycle` 里加一个可选 stage，否则它会随时间失效——正如 TradingAgents 被静默 skip 一样。

### P1-3 settle 长期 waiting_for_data

09-28~09-30 三次 cycle 里，`settle_predictions` 与 `settle_screen_selections` 全部 `waiting_for_data`。

147 条预测中 90 条未结算。按第一天的估算，要让选股结论达到 80% power 需约 392 条，还差 335 条——**以当前的结算速率，等待时间以年计**。

### P2 其他

| 项 | 说明 |
|---|---|
| naive ICIR 未去重叠 | h20 标签跨 20 日重叠，真实显著性低于显示值。横盘窗口那个 0.2729 是边缘值，调整后大概率更远 |
| 缓存口径分裂 | `datasets/market`（旧，1023 行）与 `datasets/market_full`（新，max 5253 行）并存，`--data-dir` 手动切换，容易混用 |
| `flow_backtest` 自评限制未解决 | 作者自注：不保证涨跌停成交、未模拟市场冲击、非连续净值 |
| 数据大量闲置 | `etf_flow_snapshots` **18232 行**、`research_evidence` **792 行**、`events` **86 行**，而 `features.py` 只用了 10 个技术特征。这是目前最大的未开发资产 |

---

## 五、建议的执行顺序

1. **修 P0-3**（半天）：把 `signal_verdict` 接进 `pipeline._validation_status`。工作量最小，但影响的是「每天放行的预测有没有资格」这件事。
2. **改 P0-2**（半天）：让 `direction_correct` 之外再记一个可计分口径——相对强弱或置信度连续值，否则有效样本永远为 0。
3. **解决认证后恢复 P0-1**，并给 `skip_tradingagents` 加**显式告警**，不能让它显示为 complete。
4. **P1-1**：在 `screened_current` 上复跑一遍 IC，确认结论迁移。
5. **P1-2**：回测模块入 cycle。

第 1、2 项合计一天内可完成，且不需要解决认证问题。

---

## 六、一句总结

流程骨架是**对的**（先验后放、五项上下文校验、不可变归档、认证失败熔断），这几天补的四条口径也都生效了。

但它现在跑的是一条**没有主模型参与、送给二叉决策、且用错判据放行**的流水线。

优先把 P0-3 修掉——那是改动最小、但每一天都在影响产出质量的一处。

---

## 七、实施记录（2026-10-06）

第 1~3 项已按建议顺序落地，均不需要先解决 LLM 认证问题。

### P0-3 选股闸门接入 `pipeline._validation_status`（已完成）

| 改动 | 位置 |
|---|---|
| `selection_metrics`：横截面 IC / RankIC / ICIR / Q1–Q5 分位收益与胜率 / `spread_top_minus_bottom` / `monotonic_quantiles` | `prediction_research/evaluation.py` |
| `signal_verdict(selection)`：`ICIR_GATE=0.30`，输出 `selection_usable` / `usages` / `reason` | 同上 |
| `excess_metrics(strategy, benchmark)`：漏传 benchmark 直接 `ValueError`，输出几何超额 | 同上 |
| `DEFAULT_SLIPPAGE_BPS=10.0`；修 `_summarize` 写 `icir` 而 `signal_verdict` 读 `icir_naive` 的字段名漂移（原导致闸门恒 None/恒禁用） | 同上 |
| `selection_validation(cfg, predictions)`：从 backtest JSON 的 `predictions` 明细直接算闸门，不必改 `run_backtest` | `prediction_research/pipeline.py` |
| `_latest_backtest` 带出 predictions，`_validation_status` 返回前清洗掉 report 里的 predictions | 同上 |

`passed` 布尔语义未动（有既有断言依赖），只新增 `selection_usable` / `allowed_usages`。端到端实测：生产池 `screened_current` h20 的 selection ICIR = **−0.1634**（负），连原 Brier 闸门也未过 → **双重不合格**，即模型不具备跨标的区分能力，只可能用作择时。

### P0-2 Hold / 未识别评级不再污染方向命中率（已完成）

- `agent_outcomes` 新增 `hold_correct_vs_buy` / `peer_mean_return` / `excess_vs_peer` 三列（`store.py`；旧库走 `ALTER TABLE` 迁移）。
- `settle_agent_analyses`：方向为 `neutral` / `unclassified` 时 `direction_correct` 保持 NULL，另按**机会成本口径**记 `hold_correct_vs_buy`（上涨=踏空 0，下跌=避损 1）；同决策日的候选等权均值作为 `peer_mean_return`，算出 `excess_vs_peer`。三者都不混进 `direction_correct`。
- `run_tradingagents` 写的分析与 `decision_semantics` 一并带出；`INSERT INTO agent_outcomes` 改为显式列名。
- 测试回归修复：`test_rating_mapping` 原先用 `SELECT *` 按位置比较历史行，schema 加列后假失败 → 改为显式列名断言，并补齐 `hold_correct_vs_buy` 的断言。

### P0-1 `skip_tradingagents` 不再显示成 complete（已完成）

- `cycle.run_cycle` 新增 `report["llm_coverage"]`（`analyses_settled` / `skip_flag` / `skip_reason` / `usable`）与 `report["warnings"]`。
- **判定**：`settle` 会补结历史遗留分析，所以「结算条数 > 0」不等于本轮真跑了 LLM；只要开关跳过或 tradingagents step 未跑成，`usable=False`。
- **Outcome**：任何 `waiting_for_data` 之外的成功 outcome，在 `usable=False` 时一律加 `_without_llm` 后缀（`complete_without_llm` / `complete_with_data_waits_without_llm`），并写入 `llm_step_not_run` 告警。
- `workflow.py`：认为 `complete*` 均为跑通（避免旧 cycle 被误判为失败），并把 `llm_coverage` / `warnings` 透到 `latest_cycle`。
- `cycle_reporting.py`：报告正文新增「本轮告警」表格 + 「TradingAgents 覆盖」一行——缺 LLM 这件事读报告的人也能看到，不需要翻 JSON。
- 测试：新增 2 个用例（开关关闭且有结算→不加后缀；开跳过但补结到历史数据→仍告警），并更新 2 个既有用例的期望值。

### 测试状态

`prediction_research/tests`：**208 passed**（本轮结束时全绿）。

> 补记：上一版记的「185 passed + 3 个 `test_screen_handoff` 失败（`No module named 'langchain_core'`）」已不成立。langchain 依赖现已可用，那 3 个用例通过，测试总数从 185 增至 208（Newey-West 新增 5 例）。

---

## 八、P1 / P2 实施记录（2026-10-06 续）

### P1-1 结论已迁移到生产池 `screened_current`（已完成）

原报告的 ICIR = −0.1634 是在旧 `datasets/market`（1023 行标的口径）下算的，而 cycle 每日实际跑的是 `screened_current`。已做两步：

**① 隔离探针**（临时脚本，未进项目）：同一 `screened_current` 池分别喂 `market` 与 `market_full`，确认换数据源不改变结论方向。

**② 正式迁移**：`config/research.json` 改为

```json
"market_data_dir": "datasets/market_full",
"legacy_cache_dir": "datasets/market",
```

> 踩坑：编辑时误留了原有的 `"legacy_cache_dir": null` 占位行，JSON 取**最后一个**同名键 → 兜底失效，49 条预测仍 `waiting_for_bars`。已删掉 null 行。**教训：改 JSON 配置时凡新增键，必须先确认没有同名占位。**

`screened_current` 当前 3 只（516670 / 159593 / 515220）在 `market_full` 下的实测：

| horizon | IC 均值 | naive ICIR | Newey-West t | 选股闸门 |
|---|---:|---:|---:|---|
| h5 | +0.0423 | 0.0599 | 0.73 | `usable=False` |
| h20 | −0.1013 | −0.1440 | −0.96 | `usable=False` |

数值与旧口径不同（符号与量级都变了），但**「禁用选股、仅可择时」的结论纹丝不动**。6 个 backtest（screened_current / commodity / cached × h5/h20）已全部迁进标准 `runs/`，时间戳 `20261006_1844xx`。

### P1-2 验证脚本已收进项目包，防腐烂（已完成）

`constrained_backtest.py` / `ic_analysis.py` / `window_scan.py` / `skill_significance.py` 从工作区搬进 `prediction_research/validation/`：

- 4 个脚本的硬编码 `ROOT = Path(r"E:\Trade\TradingAgents")` 全改为 `REPO = Path(__file__).resolve().parents[2]`（相对路径，换机器可用）
- `skill_significance.py` 去掉硬编码输出路径，改 `main(argv=None)` + `--out`
- 新增 `__init__.py`（SUBCOMMANDS 映射）+ `__main__.py`（dispatcher）
- 统一入口：`python -m prediction_research.validation <window-scan|ic-analysis|constrained-backtest|skill-significance> [args]`
- 已用 `window-scan --universes commodity` 端到端跑通（market_full 2012→2026，3438 根 bar）

**仍属可选增强**：目前是「搬进项目 + 有统一入口」，尚未作为 cycle 的可选 report step 自动触发。

### P1-3 结算链路诊断完成，并修掉一个迁移副作用（已完成）

147 条预测：57 已结算 / 90 未结算（20 条 h5 + 70 条 h20，feature_date 2026-09-04→09-30）。诊断结论：

- **90 条全部是 `waiting_for_horizon`，不是 bug** —— 数据止于 09-30，叠国庆休市（10-01~10-08），horizon 尚未到期。
- **但发现并修掉一个 P1-1 切换的副作用**：90 条中有 **49 条 `waiting_for_bars`** —— 因为它们引用 29 只仅存于旧 `market` 目录的历史标的（原 5 只 universe 被砍后缓存孤立）。这正是上面 `legacy_cache_dir` 兜底要解决的问题。

修完实跑 `settle_predictions`：`eligible_open=90, settled=0, waiting=90×waiting_for_horizon`，`waiting_for_bars` 归零。

> 另注：`settle_screen_selections`（在 `screening.py:371`，不在 `pipeline.py`）的大量 `waiting_for_bars` 是**迁移前就存在的独立问题** —— 宽基候选标的从未拉过行情。它不阻塞预测结算，属另一条待办。

### P2 Newey-West 去重叠已实现并接入闸门（已完成）

**先纠正一个概念错误**：函数最初叫 `icir_newey_west`，返回的是 **t 统计量**（显著性），而 `icir = mean/std` 是**信息比率**（一致性）。两者量纲与含义都不同，**不可比大小**。已重命名为 `ic_tstat_newey_west`：

```
t = mean(IC) / SE_mean
SE_mean = sqrt( (gamma0 + 2*Σ_{l=1..L} gamma_l*(1 - l/(L+1))) / n )     # Bartlett 核，L 默认 min(n-2, 20)
```

h20 标签相邻重叠 19 天 → IC 序列强自相关 → 朴素 `SE = std/√n` **低估标准误、高估 t 值**。实测去重叠效果：

| horizon | naive t（=ICIR×√n） | NW t | 折减 |
|---|---:|---:|---:|
| h5 | 0.0599×√497 ≈ 1.34 | 0.73 | 1.8× |
| h20 | 0.1440×√302 ≈ 2.51 | 0.96 | 2.6× |

接入点：
- `selection_metrics` 输出 `icir_newey_west`（字段名保留以兼容既有签名，值为 t 统计量）
- `signal_verdict` 新增 `overlap_inflates_significance`：**仅当**朴素 ICIR 过闸 **且** NW 值存在 **且** < 门限时为真。缺 NW 字段时退回朴素口径，不臆断。
- `reason` 字符串会明说「仅朴素口径过闸、NW 调整后未过闸，显著性由重叠标签高估」

单测从 13 例增到 19 例。关键一条 `test_tstat_newey_west_is_more_conservative_under_positive_autocorrelation` 修掉了旧版的**双重错误**：旧构造 `ics=[0.30]; 0.9*x+0.03` 会收敛到常数 0.30（因 0.9×0.30+0.03=0.30），方差≈0 导致除法爆炸到 1e17；且断言方向也错（拿 ICIR 比 t）。新版用带随机扰动的平稳 AR(1)，并分别验证「无自相关时 NW-t ≈ 朴素 t」与「强正自相关时 NW-t 明显更保守」。

### P2 资金流数据：原判断有误，实为**已在使用**（已核查，无需改动）

原 P2 写「`etf_flow_snapshots` 18232 行未被 `features.py` 使用，是最大的未开发资产」。核查后**这个判断不成立**，两点原因：

**① 它已经接进生产链路了，不在工作区闲置：**

| 接入点 | 用途 |
|---|---|
| `screening.py`（51 处 flow 引用） | 资金流 + 流动性粗筛，产出 `screened_current` |
| `flow_context.py` | 决策时点的资金流上下文（严格做 point-in-time 防未来函数） |
| `flow_backtest.py` | 历史资金流分层回测 |
| `cycle.py:166` / `cli.py:98` | 每日 cycle 第 7 步 + `backtest-flow` 命令 |

**② 「18232 行」这个数字有误导性 —— 它不是时间序列。** 18232 = 1693 只标的 × 11 个快照时刻；按 `quote_epoch` 还原后**只有 8 个交易日**（2026-09-04 / 09-15 / 09-17 / 09-22 / 09-24 / 09-28 / 09-29 / 09-30）。

这正是它**不该**进 `features.py` 当 walk-forward 特征的原因：模型要求 `min_train_dates = 252` 个交易日训练，资金流只有 8 天，缺口 244 天。硬塞进去只会得到一条几乎全零的特征列，反而污染已有 10 个有效技术特征。

实跑 `backtest-flow` 确认这是**正确行为而非 bug**：`observed_days=8, timely_screen_days=4, minimum_paired_days_per_horizon=60` → `status=waiting_for_history_or_prices`（6 个迟到快照已被自动排除）。**结论：资金流要成为选股特征，前提是先积累到 ≥60 个有效配对交易日，现在只能当粗筛与决策上下文用。**

### P2 其余两项现状

- **双口径并存**：已由 `market_data_dir` + `legacy_cache_dir` 机制化（主源 + 兜底源），不再是「手动切 `--data-dir` 容易混用」的风险。
- **`flow_backtest` 自评限制**（不保证涨跌停成交、未模拟市场冲击、非连续净值）：属回测方法论上限，非工程缺陷，需在决策时人工打折。

---

## 九、当前状态

| 项 | 状态 |
|---|---|
| P0-1 主链路告警 | ✅ 已完成 |
| P0-2 Hold 不污染方向命中率 | ✅ 已完成 |
| P0-3 选股闸门接入 | ✅ 已完成 |
| P1-1 结论迁移到生产池 | ✅ 已完成（结论不变） |
| P1-2 验证脚本入项目 | ✅ 已完成（可选 report step 未接） |
| P1-3 结算链路诊断 | ✅ 已完成（等 horizon 到期，非 bug） |
| P2 Newey-West 去重叠 | ✅ 已完成 |
| P2 资金流 | ⚠️ 原判断有误，实为已在使用，无需改动 |
| P2 双口径 | ✅ 已机制化 |
| P2 flow_backtest 自评限制 | ⏳ 方法论上限，需人工打折 |
| `settle_screen_selections` 候选无行情 | ⏳ 迁移前就存在的独立问题 |

**核心结论未变，且被这轮独立复现两次**：模型只学到市场整体基准率（校准），不具备横截面排序能力。当前只可用于**择时**，不可用于**选股**。
