# ETF 信息获取与预测研究系统使用手册

## 1. 项目用途

本模块用于在 TradingAgents 仓库中构建可审计的 ETF 研究流程：

```text
全市场 ETF 行情与资金流
→ ETF 分类
→ 资金流粗筛
→ 候选历史行情
→ 5/20 日滚动回测
→ 概率预测与质量门
→ 预测冻结
→ 到期结算与研究报告
```

项目目录为：

```text
E:\Trade\TradingAgents\prediction_research
```

模块使用自己的配置、数据、数据库和输出目录，不依赖外层 Trade 项目的配置与行情目录。

> 当前系统属于研究工具。未通过质量门的概率会标记为 `research_only_failed_validation`，不能视为正式交易信号。

## 2. 运行环境

从 TradingAgents 仓库根目录执行命令：

```powershell
Set-Location 'E:\Trade\TradingAgents'
$py = 'C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe'
```

如使用其他 Python，可将 `$py` 改为对应解释器路径。第一版核心流程仅依赖 Python 标准库；TradingAgents 使用其独立虚拟环境。

## 3. 推荐运行方式

### 3.1 执行完整日常周期

```powershell
& $py -m prediction_research.cli cycle --top 3
```

该命令依次执行：

1. 更新全市场 ETF 行情和资金流快照；
2. 更新新闻事件；
3. 运行资金流粗筛并冻结前 20 名；
4. 获取排名前 3 的候选 ETF 日线；
5. 分别运行 5 日和 20 日回测；
6. 冻结最新概率预测；
7. 执行 TradingAgents（当前按配置跳过）；
8. 检查预测、智能体结论和粗筛记录是否到期；
9. 生成综合研究报告和周期运行记录。

`--top` 控制进入深度预测流程的候选数量，不改变粗筛报告默认保存的前 20 名。

### 3.2 使用已有快照离线复跑

```powershell
& $py -m prediction_research.cli cycle --top 3 --skip-fetch
```

该方式不会联网更新 ETF、新闻和候选历史数据，适合代码验收、调试和复现已有结果。

## 4. 检查当前状态

```powershell
& $py -m prediction_research.cli status
```

重点字段：

- `current_stage`：当前流程阶段；
- `active_path`：当前执行路径；
- `historical_observation_days`：已积累的资金流独立交易日数；
- `historical_screen_validation_ready`：是否达到完整资金流轮动回测条件；
- `open_forward_selections`：等待 5/20 日结算的粗筛记录；
- `current_screen_open_predictions`：当前候选池的未结算预测；
- `all_open_predictions`：数据库全部未结算预测；
- `successful_cycle_runs`：成功完成的研究周期数。

当前默认路径为 `quantitative_without_tradingagents`。TradingAgents 被跳过不会阻止定量流程继续运行。

## 5. 分步骤命令

### 5.1 环境与数据覆盖检查

```powershell
& $py -m prediction_research.cli doctor
```

用于检查配置、缓存、目标 ETF 和外部数据是否具备运行条件，不修改源数据。

### 5.2 获取 ETF 全市场快照

```powershell
& $py -m prediction_research.cli fetch-etfs
```

抓取 ETF 行情板块并自动写入 SQLite。保存的主要字段包括价格、涨跌幅、成交额、市值、主力净流入和主力净流入占比。

如果已经存在最新快照，只想重新幂等入库：

```powershell
& $py -m prediction_research.cli ingest-etfs
```

同一个快照不会重复写入；同一交易日的多次快照只计为一个历史观察日。

### 5.3 查看 ETF 分类统计

```powershell
& $py -m prediction_research.cli etf-summary
```

输出资产类别、子类型、境内/跨境范围及未识别示例。

当前快照覆盖的是所配置的东方财富 ETF 行情板块，不应直接宣称为沪深交易所全部合同级产品清单。

### 5.4 运行资金流粗筛

```powershell
& $py -m prediction_research.cli screen-etfs --limit 20
```

当前粗筛公式：

```text
粗筛分 =
0.50 × 截断后的主力净流入占比
+ 0.35 × 对数成交额流动性
+ 0.15 × 截断后的当日涨跌动量
```

系统同时应用资产类别、最低成交额、最低市值、有效价格和同主题数量限制。

资金流字段是按成交单大小估算的交易资金流，不等于 ETF 申购赎回、份额变化或机构账户披露。

### 5.5 获取粗筛候选历史行情

```powershell
& $py -m prediction_research.cli fetch-screened --top 3
```

系统优先使用东方财富，失败时降级到新浪行情，并记录实际数据源和失败原因。

### 5.6 获取大宗商品目标池行情

```powershell
& $py -m prediction_research.cli fetch --universe commodity
```

`commodity` 是大宗商品 ETF 与资源类 ETF 研究池。`cached` 仅用于离线验收，不代表商品池。

### 5.7 获取商品外部序列

```powershell
& $py -m prediction_research.cli fetch-external
```

用于更新期货或海外代理序列。模型会根据各市场收盘时间处理境内外信息可得性，减少前视偏差。

### 5.8 获取新闻与事件证据

```powershell
& $py -m prediction_research.cli fetch-news
& $py -m prediction_research.cli events --limit 10
```

当前来源包括 EIA、美联储和国家发改委。系统保存原始快照、SHA-256、发布时间、来源、事件评分和敞口映射。

新闻目前只作为证据，不直接改变预测概率。

## 6. 回测与预测

### 6.1 运行严格滚动回测

基础量价模型：

```powershell
& $py -m prediction_research.cli backtest --universe commodity --horizon 5 --feature-set base
& $py -m prediction_research.cli backtest --universe commodity --horizon 20 --feature-set base
```

商品外部特征模型：

```powershell
& $py -m prediction_research.cli backtest --universe commodity --horizon 5 --feature-set external
& $py -m prediction_research.cli backtest --universe commodity --horizon 20 --feature-set external
```

可选参数：

- `--horizon 5` 或 `--horizon 20`；
- `--feature-set base`：量价特征；
- `--feature-set external`：量价加外部商品序列；
- `--feature-set auto`：根据资产池自动选择；
- `--model-scope pooled`：默认池化模型；
- `--model-scope exposure`：按敞口独立训练的实验对照。

回测严格按时间顺序执行。交易日 `t` 收盘后形成特征，标签从 `t+1` 开盘开始，到 `t+h` 收盘结束；训练样本的标签必须在测试窗口开始前已经到期。

### 6.2 冻结最新预测

```powershell
& $py -m prediction_research.cli predict --universe commodity --horizon 5 --feature-set external
& $py -m prediction_research.cli predict --universe commodity --horizon 20 --feature-set external
```

系统会记录：

- ETF 代码和特征日；
- 5/20 日周期；
- 上涨概率；
- 模型版本；
- 数据快照；
- 证据引用；
- 质量门状态；
- 预期到期时间。

相同标的、特征日、周期和模型版本重复运行时不会重复插入。

### 6.3 结算到期预测

```powershell
& $py -m prediction_research.cli settle
```

当未来日线足够覆盖预测周期时，系统写入实际收益、实际涨跌和结算日期。未到期记录保持开放，不会提前填充结果。

### 6.4 结算资金流粗筛结果

```powershell
& $py -m prediction_research.cli settle-screen
```

粗筛前 20 名会分别建立 5 日和 20 日前瞻记录。同一特征日、筛选规则、ETF 和周期只保留最新一条，避免同日重复运行造成样本重复。

至少积累 60 个独立资金流观察日后，才将完整资金流轮动策略标记为具备历史验证条件。

## 7. TradingAgents

查看将要提交的候选，不调用大模型：

```powershell
& $py -m prediction_research.cli agents-plan --top 3
```

实际执行：

```powershell
& $py -m prediction_research.cli run-agents --top 3
```

结算成功产生的方向判断：

```powershell
& $py -m prediction_research.cli settle-agents
```

当前 TradingAgents 因用户决定跳过密钥问题而处于禁用状态。失败、跳过或缺少结论的任务不会被转换成概率或伪造为预测结果。

## 8. 生成综合报告

```powershell
& $py -m prediction_research.cli report
```

报告包含：

- 当前流程阶段；
- ETF 快照和资金流口径；
- 粗筛前 20 名；
- 5/20 日回测指标；
- 当前冻结预测；
- 与候选直接映射的事件证据；
- 等待结算和下一检查点。

## 9. 输出目录

| 内容 | 路径 |
|---|---|
| 独立配置 | `prediction_research/config/research.json` |
| ETF 快照 | `prediction_research/datasets/etf_market/` |
| 候选日线 | `prediction_research/datasets/market/` |
| 商品外部序列 | `prediction_research/datasets/external/` |
| 新闻原始快照 | `prediction_research/datasets/news/raw/` |
| SQLite 状态库 | `prediction_research/state/research.db` |
| 粗筛、回测和预测结果 | `prediction_research/runs/` |
| 综合研究报告 | `prediction_research/runs/research_report_*.md` |
| 完整周期记录 | `prediction_research/runs/cycle_*.json` |

## 10. 预测状态解释

| 状态 | 含义 |
|---|---|
| `research_only_failed_validation` | 模型没有超过基线，只能用于研究 |
| `legacy_development_pre_gate` | 质量门建立前的旧开发记录 |
| 已校准/可发布状态 | 只有达到校准要求并通过质量门时才允许出现 |

主要质量指标为 Brier Score、Log Loss、准确率和覆盖率。模型至少需要在严格样本外验证中优于历史上涨概率基线，才有资格升级。

## 11. 推荐的日常操作

每个交易日收盘后执行：

```powershell
Set-Location 'E:\Trade\TradingAgents'
$py = 'C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe'
& $py -m prediction_research.cli cycle --top 3
& $py -m prediction_research.cli status
```

如果网络不可用，可以先复用冻结数据验证流程：

```powershell
& $py -m prediction_research.cli cycle --top 3 --skip-fetch
```

同一交易日重复运行不会增加独立资金流观察日，也不会重复冻结同版本预测。要验证资金流轮动效果，需要持续积累不同交易日的数据。

## 12. 当前边界

- 当前 ETF 快照不是沪深交易所合同级完整产品清单；债券和多资产 ETF 尚待补齐；
- 主力资金流不是 ETF 申购赎回或机构持仓数据；
- 新闻事件尚未通过独立增益回测，因此不修改概率；
- 当前模型尚未通过总体质量门；
- TradingAgents 当前跳过；
- 系统只生成研究结论，不自动下单，也不构成投资建议。
