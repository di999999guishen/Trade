# ETF 证据整合操作说明

## 运行

在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m prediction_research.cli integrate-etfs
.\.venv\Scripts\python.exe -m prediction_research.cli evidence-status
.\.venv\Scripts\python.exe -m prediction_research.cli evidence-profiles
.\.venv\Scripts\python.exe -m prediction_research.cli evidence-chains
```

`integrate-etfs` 复用本地已入库快照，从 E1 自动运行到 E6；每阶段保存 JSON 与 Markdown。任一阶段失败会保留记录，阻止依赖该失败阶段的任务；独立状态检查继续。整合失败退出码为 2，数据/历史等待的退出码为 0，真实状态必须读取 `outcome` 和阶段状态。

已有 `cycle` 默认启用证据整合，顺序为获取 ETF/新闻→粗筛→候选历史→ETF 证据阶段→候选回测和预测→结算→统一报告。每次 cycle 自动重新检查可用数据；不额外创建操作系统定时任务。

```powershell
# 用现有快照验收完整流程，不请求网络/大模型
.\.venv\Scripts\python.exe -m prediction_research.cli cycle --skip-fetch --top 3
# 正常更新并继续各阶段
.\.venv\Scripts\python.exe -m prediction_research.cli cycle --top 3
```

新预测只引用截至特征日 `data_cutoff` 当时已可得的证据 ID。旧的冻结预测不回填证据；标准证据不参与原量价概率计算。原 TradingAgents 按既有配置继续跳过。

## 外部标准交换格式

这是本项目定义的文件接口，**不是** FinGenius / smart-money-profiler 原生 JSON 格式，也不是已经启动了这些第三方系统。第三方原始报告必须先转换为以下格式并提供真实出处。FinGenius 的个股原始结果不在本轮接入范围；不能把个股代码替换为 ETF 代码来通过校验。TradingAgents-Astock 全部延期。

文件顶层为 `{"schema_version": 1, "records": [...]}`。每条记录的字段如下：

| 字段 | 约束 |
|---|---|
| source | `fingenius` 或 `smart-money-profiler` |
| source_key | 来源内稳定记录 ID，修订使用新的 ID |
| symbol | 当前 universes 中的 ETF / 存量基金代码字符串；拒绝其他标的 |
| kind | `flow`、`news`、`actor_profile`、`anomaly`、`analysis` |
| epistemic | `fact`、`inference`、`hypothesis`，不得把模型推断标为直接披露事实 |
| claim | 可审阅的结论文本 |
| source_ref | 原始数据方法/文章/报告地址 |
| snapshot_sha256 | 原始来源快照的 64 位小写 SHA-256 |
| observed_at_utc | 带时区的观察时间 |
| available_at_utc | 带时区的来源可得时间；导入器强制不早于本地首次接收时间 |
| direction | `up`、`down`、`neutral`、`unknown`；方向不明使用 unknown |
| score | null 或 [-1,1] 内有限数，仅研究评分，不能冒充校准概率 |
| independence_key | 可选，同一事实/转载/多角色重复引用使用相同值，避免重复计数 |

通过 `import-evidence 文件路径` 导入，或将标准 JSON 放入 `prediction_research/datasets/evidence_inbox/` 由每次整合自动读取。完整文件按 SHA-256 归档，同一记录不可覆盖。身份相同而内容变化会报冲突，需使用修订 ID。批次中任何记录格式错误则整批不入库；其他文件独立处理，阶段报告保留失败文件名。

接收时间防止将今天生成的大模型历史解释回填到历史特征。不明身份仍标为不可观测；ETF 成交单大小估计不能替代申购赎回、基金份额或账户披露。当前没有 Pandadata 客户端或真实聪明钱主体数据，不能声称已经完成人物/机构身份识别。

## 增益实验

```powershell
.\.venv\Scripts\python.exe -m prediction_research.cli backtest-evidence --universe commodity --horizon 5
.\.venv\Scripts\python.exe -m prediction_research.cli backtest-evidence --universe commodity --horizon 20
```

对 `etf_flow`、`news_chain`、`tradingagents`、`fingenius`、`smart-money-profiler` 分别安排单模块、全部模块及去掉一个模块的对照。以现有量价 v2 为基线，固定样本、标签、时间切分和训练配置，记录 Brier、准确率、历史上涨率 Brier 和动量准确率。每组追加评分均值、评分可用指示与去重证据数量，未观测数据保留缺失指示。

实验同时保存全量量价基线与该组证据覆盖样本上的成对结果。只有具备该组训练证据的时间窗口进入成对比较；组合组使用共同覆盖。不同组可能覆盖不同日期，跨组不能直接比较各自 Brier，需要检查成对基线。回测限定当前配置 ETF 池，不代表历史全市场轮动或扣费收益。

默认需 60 个证据观察日、至少 20 个训练证据日期及 200 条可比较样本外记录。尚不满足时输出 `waiting_for_evidence_or_history`，不捏造增益。全组输入/价格快照/证据/配置哈希相同则复用上次实验；哈希变化重新运行。即使研究 Brier 改善，也不自动发布正式模型，仍需独立前瞻验证。

报告记录工程执行状态与数据就绪状态。`complete_with_data_waits` 表示自动流程运行完成，但部分外部数据或历史还不具备条件。
