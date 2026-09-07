# ETF 预测研究核心

完整的中文命令、日常运行顺序、输出位置及状态解释见 [USAGE.md](USAGE.md)。

这是 TradingAgents 仓库内的独立 ETF 研究模块。它负责把盘后可获得的数据转换成可复现的特征，产生未来 5/20 个交易日预测，并把每次预测冻结到 SQLite，待到期后自动复盘。

第一版只依赖 Python 标准库，离线验收行情已经迁入模块自己的 `datasets/market/`。它包含：

- 严格的盘后预测口径：交易日 `t` 收盘后形成特征，收益标签为 `t+1` 开盘到 `t+h` 收盘；
- 时间顺序的滚动样本外验证，训练集标签必须在测试窗口开始前已经到期；
- 逻辑回归概率基线、仅在独立校准段样本充足时启用的概率校准；
- Brier、Log Loss、准确率、覆盖率和简单动量基线；
- 不可覆盖的预测记录、模型版本、证据引用和到期结算；
- 大宗商品 ETF 与资源股票 ETF 的分组元数据；
- 11 条商品期货/海外代理序列，按收盘可得性处理境内外时差；
- EIA、美联储、国家发改委新闻快照、事件去重、UTC 时间和商品敞口映射；
- 总体与逐 ETF 的样本外指标，以及未达标时禁止发布正式概率的质量门。

## 快速运行

```powershell
$py = 'C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe'
& $py -m prediction_research.cli doctor
& $py -m prediction_research.cli status
& $py -m prediction_research.cli fetch --universe commodity
& $py -m prediction_research.cli fetch-external
& $py -m prediction_research.cli fetch-news
& $py -m prediction_research.cli fetch-etfs
& $py -m prediction_research.cli ingest-etfs
& $py -m prediction_research.cli etf-summary
& $py -m prediction_research.cli screen-etfs --limit 20
& $py -m prediction_research.cli fetch-screened --top 5
& $py -m prediction_research.cli agents-plan --top 5
& $py -m prediction_research.cli run-agents --top 5
& $py -m prediction_research.cli settle-agents
& $py -m prediction_research.cli settle-screen
& $py -m prediction_research.cli cycle --top 5
& $py -m prediction_research.cli events --limit 10
& $py -m prediction_research.cli backtest --universe commodity --horizon 5 --feature-set external
& $py -m prediction_research.cli predict --universe commodity --horizon 5 --feature-set external
& $py -m prediction_research.cli settle
```

从 TradingAgents 仓库根目录执行。输出默认进入 `prediction_research/runs/`，数据库位于 `prediction_research/state/research.db`。所有路径和实验参数来自本模块的 `config/research.json`。

`commodity` 是目标研究池；`cached` 只用于当前离线验收。新拉取的数据只写入 `prediction_research/datasets/market`。大宗商品池的数据尚未落地时，`doctor` 会明确报告缺失，不会用其他 ETF 冒充商品 ETF。

`events` 默认只展示已映射到目标商品敞口的事件。所有原始条目仍保留在数据库和带 SHA-256 的快照中，便于审计。事件层当前只作为证据，不会直接改变预测概率；只有通过独立增益回测后才会进入模型特征。

`ingest-etfs` 会将最新资金流截面幂等写入 SQLite；`screen-etfs` 会冻结当日入选结果，`settle-screen` 在 5/20 个交易日标签可得后结算。只有累计至少 60 个独立观察日，才把完整资金流轮动策略标记为可回测。

`backtest` 和 `predict` 另支持 `--model-scope exposure` 实验开关。现有回测已证明完全分敞口训练弱于默认的 `pooled`，所以它只保留作反例和后续分层模型的对照，不建议用于生成结果。

## 当前边界

当前包含量价对照模型与商品外部特征模型。总体 Brier 指标仍未超过历史上涨率基线，因此预测会标记为 `research_only_failed_validation`，不能作为正式交易信号。报告中的概率只有在校准段样本达到门槛时才标记为已校准；历史回测不能替代上线后的前瞻记录。
