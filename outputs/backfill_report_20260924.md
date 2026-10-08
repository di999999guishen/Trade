# ETF 历史回填与结算报告（2026-09-24 盘后）

**性质**：19:00「ETF研究预测完整流程」的**前置数据准备**，不含任何预测。

| 项 | 值 |
|---|---|
| 命令 | `.venv/Scripts/python.exe run_research.py backfill` |
| 结果 JSON | `prediction_research/runs/backfill_20260924_181735_615083.json` |
| 耗时 | 18:17:35 → 18:27:28（本地）= **9 分 54 秒** |
| 结果 | `outcome = complete`（exit 0） |
| 速率 | 383 只 / 593 秒 ≈ **39 只/分** |

## 一、回填（抓日线）

| 指标 | 数值 |
|---|---|
| 回填前待结算（pending_before） | **347** |
| 回填前 flow-screen 缺行情 | **51** |
| 去重后目标总数（targets_total） | **383**（347 + 51，重叠 15） |
| 请求数 | 383 |
| **抓取成功** | **383** |
| **抓取失败** | **0**（`failures = {}`） |
| 数据源链 | `eastmoney_push2his` → `sina_kline` |
| **东财熔断触发** | **是**：`primary_provider_paused = true`，`primary_provider_skipped = 373` |

执行前 curl 探测 `push2his.eastmoney.com/api/qt/stock/kline/get` 无响应，**东财 `push2his /api/qt/*` 今日仍被 WAF 按源 IP 阻断**。前 10 只（连续 3 次失败后）走新浪，其余 373 只由新浪直接承担。按既定约定这是**预期情况而非故障**，新浪回退是既定可用路径，未重试到打满时间。

缓存落地校验：`prediction_research/datasets/market/` 中 **383 个** CSV 于本次运行窗口内被更新，与 `refreshed=383` 一致。

## 二、结算（三类）

| 类别 | 处理对象 | **本次结算** | 未结算（等待） |
|---|---|---|---|
| `predictions` | 54 条未结算预测 | **0** | 54 条，全部 `waiting_for_horizon` |
| `agent_analyses` | 5 条 `status=ok` 分析 | **0** | 函数不产出 waiting 明细 |
| `screen_selections` | 840 条未结算粗筛 | **125** | 715 条：683 `waiting_for_horizon` + 32 `waiting_for_bars` |

**粗筛首次出现实质结算**：125 条到期结清（2026-09-23 为 0 条），说明 09-15/09-17 批次的 h5 窗口已陆续到期，结算链路本身工作正常。

## 三、回填后剩余

| 指标 | 值 | 变化 |
|---|---|---|
| `pending_after` | **347** | 与 before 相同 |
| `flow_screen_missing_after` | **51** | 与 before 相同 |

## 四、剩余原因（已逐项取证，均为正常等待，非失败）

配置的 `horizons = [5, 20]` 个**交易日**。

**1) `waiting_for_horizon`（683 粗筛 + 54 预测）**
到期日尚未到来（20 交易日批次的到期日在 10 月）。结算函数 `decision_outcome()` 需「决策日之后满 horizon 根 bar」才能得出结果，未满足即返回 `None`，记为等待。属正常成熟等待。

**2) `waiting_for_bars`（32 条粗筛 / 15 个标的）**
**不是抓取失败**，而是标的**历史长度不足**。`load_cache()` 有硬门槛：少于 61 根有效 bar 直接抛 `ValueError`（60 根回看 + 1 根当前）。本次涉及标的：

`158022 / 158041 / 159049 / 159066 / 159078 / 510140 / 512740 / 515940 / 516040 / 561610 / 561690 / 562170 / 588530 / 589460 / 589510`

与 2026-09-23 记录的 15 个标的**完全一致**（数量 32 条也一致）⇒ 是同一批短历史标的，非新增故障。这类标的会随交易日累积自然毕业。

**3) `flow_screen_missing_after = 51` 的构成**
数据集目录中有效 bar < 61 根的缓存文件**恰为 51 个**（`prediction_research/datasets/` 下 1404 个 CSV 中，其中 `market/` 1083 个），与 `flow_screen_missing_after` **数量精确相等** ⇒ 该残差 100% 由新上市短历史标的构成，不含任何网络/解析失败。

## 五、合规确认

- 数据源仅使用国内/不限流源：东方财富 + 新浪。**未使用** yfinance / Reddit / StockTwits / FRED。
- **未改动**任何筛选规则、阈值或配置中的 `min_amount` / `min_market_cap`。
- 未因东财阻断而重试到打满时间；熔断按设计生效（`trip_after=3` / `reprobe_every=50`）。

## 六、结论

本次回填**完全成功**：383 只目标全部刷新、0 失败，缓存已补热；粗筛结算 125 条落地。剩余 347 + 51 已逐条取证为「horizon 未到期」与「新上市标的样本不足 61 根」两类**正常等待**，不存在抓取或结算故障。19:00 的 `cycle` 将面对热缓存，该步骤不再需要重抓这批标的。

**待观察**：`pending_after` 连续三日（09-22 / 09-23 / 09-24）维持在 347，主因是最近一批预测的 horizon（h5 到期 ≈ 09-29、h20 到期 ≈ 10 月）尚未到期，结构上自洽；粗筛侧已开始产出结算（125 → 后续会继续增加）。
