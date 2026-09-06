# ETF 日常运行与观测手册

更新：2026-09-06。适用范围仅为 ETF 研究流程；A 股个股和 TradingAgents-Astock 暂不执行。本手册用于每天收盘后的运行、检查、故障判断和结果复盘。六项修订的设计与验收细节见 [ETF_CYCLE_REMEDIATION_PLAN.md](ETF_CYCLE_REMEDIATION_PLAN.md)。

## 1. 本轮修改解决了什么

1. 分开记录行情日、资金流观察时间、来源接收时间、实际决策时间和冻结时间，所有预测证据使用同一个决策截止点。
2. 预测一经写入即保持不可变；重复运行返回原概率、原证据和原时间。同日同规则的 ETF 粗筛也复用首次冻结批次。
3. 报告严格绑定本轮 cycle，不再读取数据库全局最近 6 条或其他运行的最新产物补位。
4. 主力资金流在主报告中展示金额、占比、时间、排行、粗筛贡献、价流方向和 5/20 个观察日窗口。目前资金流参与粗筛，不直接修改上涨概率。
5. 已退出当前候选池、但尚未结算的预测和粗筛记录仍进入行情更新与结算检查。
6. 历史资金流筛选研究使用当时可得截面，进行资金流、等权合格池和动量候选的成对比较，并记录成本、缺失行情和样本外概率结果。
7. cycle 分开表达执行结果、数据就绪和模型验证。抓取或筛选失败时不会拿旧候选继续生成本轮预测。

核心实现涉及 `cycle.py`、`screening.py`、`pipeline.py`、`store.py`、`workflow.py`，并新增 `flow_context.py`、`flow_backtest.py`、`settlement.py` 和 `cycle_reporting.py`。

## 2. 每日标准操作

在仓库根目录执行：

```powershell
Set-Location 'E:\Trade\TradingAgents'
.\.venv\Scripts\python.exe -m prediction_research.cli cycle --top 3
.\.venv\Scripts\python.exe -m prediction_research.cli status
```

正常日常运行不要使用 `--skip-fetch`。该参数只用于断网调试、代码验收和冻结结果复现：

```powershell
.\.venv\Scripts\python.exe -m prediction_research.cli cycle --top 3 --skip-fetch
```

完整 cycle 自动执行快照、新闻、筛选、待跟踪行情、证据整合、历史资金流研究、5/20 日回测与预测、结算和报告，不需要逐阶段手工触发。

## 3. 每天先看哪些状态

`status` 输出中优先检查以下字段：

| 字段 | 正常或可接受值 | 含义 |
|---|---|---|
| `latest_cycle.outcome` | `complete` 或 `complete_with_data_waits` | 后者表示工程已执行，但历史、标签或证据仍在等待 |
| `latest_cycle.data_mode` | 日常应为 `network_refresh` | `cached_validation` 表示本轮没有联网刷新 |
| `latest_cycle.data_readiness` | `ready` | 当前候选行情满足本轮预测要求 |
| 阶段 3 `status` | `complete` | 本轮筛选成功且有明确冻结报告 |
| `historical_observation_days` | 随交易日逐步增加 | 资金流独立观察日数，同日复跑不会增加 |
| `historical_screen_validation_ready` | 初期通常为 `false` | 只有观察日、价格、标签和样本外预测均满足才会变为 true |
| `historical_validation_status` | 初期为 `waiting_for_history_or_prices` | 不等于程序失败 |
| `latest_cycle.validation.5/20.passed` | 当前通常为 `false` | 原量价概率模型未通过质量门，仍属研究输出 |

三个状态不可混为一谈：

- `outcome` 回答流程有没有正确执行；
- `data_readiness` 回答本轮候选数据能不能预测；
- `validation` 回答模型有没有达到发布质量门。

## 4. 每天检查主力资金流

打开 `latest_cycle.artifacts.report` 指向的 Markdown。报告应包含以下部分：

1. “本轮候选资金流”：候选 ETF 的净流入金额、净流入占比和粗筛分。
2. “本轮来源截面的金额排行”：全源前五净流入和净流出，和筛选候选明确分开。
3. “本轮引用的冻结预测”：本轮全部预测 ID、概率、周期和冻结时间，不限制为 6 条。
4. “每只 ETF 预测内的资金流依据”：观察时间、首次可得时间、资金价格方向、粗筛贡献和 5/20 日累计。
5. “本轮模型验证”和“历史资金流筛选研究”：模型质量门、观察日、成本和缺失行情。

超大单与大单背离因子的字段、公式和证据交接边界见 [ETF_ORDER_DIVERGENCE.md](ETF_ORDER_DIVERGENCE.md)。旧缓存没有分项时报告 `no_data`；正常联网获取新格式快照后才会出现真实背离信号。

重点判断规则：

- `fund_flow.status=available`：该资金流在实际决策前已可得且未过期；
- `unavailable_at_decision`：来源在决策后才收到，不得用于这次筛选依据；
- `stale_at_decision`：截面过旧；
- `missing_flow_values`：金额或占比缺失，不按 0 处理；
- `used_for_probability=false`：当前资金流只影响候选筛选，不应解释成概率模型输入；
- 5/20 日窗口显示“数据不足”是正常等待，不允许补零或用不足窗口冒充完整累计。

当前资金流是基于成交单大小的估算，不代表 ETF 份额申赎、机构账户或已识别的“聪明钱”主体。

## 5. 预测和复盘检查

每日确认：

- 5 日、20 日均有各自回测和预测产物；
- 报告引用的回测路径和预测路径都属于同一 cycle；
- 重复运行时预测的 `inserted=false`，且 `frozen_at_utc`、`decision_at_utc` 和概率保持原值；
- 新预测从实际决策时刻之后的首个交易日开盘开始计算收益；
- `waiting_for_horizon` 表示尚未到期，`waiting_for_bars` 表示标的行情缺失；
- 退出当前候选池的未结算标的仍应出现在跟踪或等待记录里。

质量门目前要求严格滚动样本外数据量达到配置下限，并且 Brier Score 优于历史上涨概率基线。`research_only_failed_validation` 只能作为研究记录，不能视为交易信号。

## 6. 历史资金流研究

cycle 会自动运行，也可单独执行：

```powershell
.\.venv\Scripts\python.exe -m prediction_research.cli backtest-flow
```

观察输出中的：

- `timely_screen_days`：在相应决策时间前可获得的历史截面数；
- `late_snapshots_excluded`：因迟到被排除的截面数；
- `missing_histories`：缺少行情的完整标的清单；
- `minimum_paired_days_per_horizon`：每个周期所需的最低成对日期数；
- `metrics`：只有严格条件全部满足后才生成；
- `model_waits`：历史筛选后没有匹配样本外预测的记录。

收益口径为扣除统一往返成本后的独立等权 cohort。不同日期的 5/20 日持有期可能重叠，因此不是连续净值，也不报告年化收益或 Sharpe。

## 7. 异常处理

| 现象 | 应采取的动作 |
|---|---|
| `partial_failure` | 打开 cycle JSON 的 `steps`，找到 `failed` 步骤和输入引用；不要用旧报告判断本轮成功 |
| 本轮没有 `artifacts.screen` | 说明筛选未成功；本轮不应存在新候选预测 |
| `data_readiness=waiting_for_history` | 检查候选及历史未结算标的行情更新结果，再重跑正常 cycle |
| `complete_with_data_waits` | 查看报告中的具体等待；若只是观察日、到期或第三方证据不足，无需把它当故障 |
| CLI 返回码 2 | 本轮存在 `partial_failure`，适合由任务调度器触发告警 |
| 资金流窗口长期不增长 | 检查是否每天都在使用正常联网 cycle，以及最新快照的观察日是否变化 |

即使报告生成失败，cycle JSON 仍会落盘；结算等独立步骤也会继续执行。修复后可以重跑，同一冻结身份不会改写原预测。

## 8. 输出位置与保留口径

| 输出 | 位置 |
|---|---|
| cycle 运行记录 | `prediction_research/runs/cycle_*.json` |
| 综合报告 | `prediction_research/runs/research_report_*.md` |
| ETF 粗筛 | `prediction_research/runs/screen_etf_flow_*.json` |
| 历史资金流研究 | `prediction_research/runs/flow_strategy_*.json` |
| 5/20 日回测与预测 | `prediction_research/runs/backtest_*`、`prediction_research/runs/prediction_*` |
| 状态数据库 | `prediction_research/state/research.db` |

日常观测以 `status.latest_cycle.artifacts` 为唯一入口，避免手工选择目录中时间最新但不属于同一次 cycle 的文件。

## 9. 当前已知等待项

- 真实资金流目前只有 1 个独立观察日，5/20 日累计窗口尚未形成；
- 全量合格池等权对照当前缺 1101 个 ETF 历史序列，普通 cycle 不会自动发起上千次下载；
- 5/20 日量价模型尚未超过历史概率 Brier 基线；
- FinGenius 和 smart-money-profiler 只有标准证据入口，尚无真实第三方输入；
- ETF 份额、净值、申赎及可识别账户主体数据尚未接入；
- A 股个股及 TradingAgents-Astock 按当前范围延期。

这些等待项不得被报告为已验证增益或正式预测能力。
