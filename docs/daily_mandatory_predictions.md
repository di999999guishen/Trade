# 每日持仓、SPY、QQQ 与黄金预测

每日入口为 `daily_predict.py`。沿用已有任务调用该入口，每次依次尝试全部标的；单只预测失败会写错误日志并继续下一只，最后以非零退出码汇报失败。这里没有创建新的定时任务。

## 每日标的

默认顺序：

1. `510050.SS`：原持仓。
2. `515220.SS`：原持仓。
3. `SPY`：标普 500 ETF。
4. `QQQ`：纳斯达克 100 ETF。
5. `GLD`：黄金 ETF，作为黄金预测的交易标的代理；其每股价格不是黄金每盎司现货或期货报价。

`DAILY_TICKER` 继续配置持仓清单，逗号分隔、忽略大小写并去重。SPY、QQQ、GLD 始终追加，即使环境变量只包含其他股票，或者设为空字符串。

例如，`DAILY_TICKER=NVDA,SPY` 的实际顺序为 NVDA、SPY、QQQ、GLD。

## 数据如何进入预测

每个固定 ETF 在模型运行前调用 Polymarket Gamma 的公开搜索接口，一次运行每只获取一份快照，同一只的模型网络重试复用这份快照。

| 预测标的 | 搜索词 | 可能的关联市场 |
| --- | --- | --- |
| SPY | SPY、S&P 500 | SPY ETF、标普 500 指数事件 |
| QQQ | QQQ、Nasdaq | QQQ ETF、纳指相关事件；以合约规则区分具体指数 |
| GLD | gold、GLD | 黄金商品、黄金期货、GLD ETF 事件 |

过滤已关闭、已到期、无明确带时区结算时间、结果价格无效或不相关的市场；跨搜索去重，优先选取未来 7 天内结算的事件，再按累计成交额排序，默认最多保留 6 个市场。检索覆盖有限，不保证存在对应合约。

快照保存每个结果名称及隐含概率、截止时间、成交额、流动性、价差、API 更新时间、市场链接与结算规则摘要。概率对应具体事件，不直接作为 ETF 明日涨跌概率、涨跌幅或目标价。指数点位、黄金报价和 ETF 每股价格分别标注。

快照通过现有 `screen_evidence_context` 进入图的 `instrument_context`，随分析传给分析师、研究讨论、交易和风险决策节点；快照变化也会改变 checkpoint 签名。模型无需自行选择调用 Polymarket 工具才能看到这份证据。

行情与技术指标继续使用项目现有的新浪美股适配器；这三个 ETF 的基本面使用现有 yfinance 适配器，个股新闻按 yfinance、eastmoney 顺序尝试。原境内持仓的数据源配置继续使用 eastmoney 等原有设置。

## 输出与异常

目录仍为 `results/daily/`，使用运行时间与 UUID 区分重复运行：

- `<ticker>_<run_id>_polymarket.json`：模型运行前写入的独立证据快照，后续模型失败也保留。
- `<ticker>_<run_id>.json`：完整预测结果，包含 `result.prediction_market_snapshot`。
- `<ticker>_<run_id>.md`：最终决策、可读的 Polymarket 证据以及完整结构化结果。
- `error_<ticker>_<run_id>.log`：单只预测失败记录。

Polymarket 状态为 `available`（有证据）、`no_matches`（搜索成功但没有通过筛选的合约）、`partial`（部分搜索失败）、`unavailable`（获取失败）。失败或无匹配时仍继续该标的的行情、新闻及模型分析，报告明确记录状态，不补造概率。Polymarket 不需要新增 API 密钥。

## 运行和检查

在项目目录中：

```powershell
# 仅检查标的，不联网、不运行模型、不写预测结果
.\.venv\Scripts\python.exe daily_predict.py --dry-run

# 使用原有模型配置运行每日预测
.\.venv\Scripts\python.exe daily_predict.py
```

完整任务从默认 2 个标的增加到 5 个，模型调用量与运行时间会相应增加。这里的预测市场快照是本次运行获取的实时数据，不能直接用于历史回测。

接口依据：[Polymarket Gamma 搜索接口](https://docs.polymarket.com/api-reference/search/search-markets-events-and-profiles)。
