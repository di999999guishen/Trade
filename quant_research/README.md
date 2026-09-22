# 美股 ETF 量化研究子项目

2026-09-19第二轮：[SEC财务口径与期间修复](S4_FINANCIAL_V2_20260919.md)。Apple同期间现金流TTM已覆盖121/121截面，总债务核验7个截面；新增连续四季度重建及跨路径/修订冲突拒绝。99项测试通过。原BBBY/Sears剩余缺失与S4—S8后续依赖在新报告中逐项列明。

2026-09-19最新：[继续执行结果](CONTINUATION_20260919.md)、[文件输入规范](FILE_INPUTS.md)、[执行计划](EXECUTION_PLAN_20260919.md)。S8十年固定组合已运行，5/10/25bp年化约5.07%/1.39%/-8.91%；SEC三发行人公开财务重建已落盘但含陈旧期间和缺失债务；Cboe真实样本不覆盖策略入场窗口。下列2026-09-18记录保留为历史版本，不代表本轮最新进度。

最新交付：[S4–S8组合与执行补充](S4_S8_COMPLETION_PROGRESS.md)。S8固定三因子已完成2023年起组合回测，5bp年化6.27%、10bp为2.33%、25bp为-8.65%，未超过同期S1/S3；S4/S5新增配置，S6/S7新增借券/分红/期权校验与指派计算。89项测试通过，S8独立进程重放一致；未来留出仅登记，尚未完成。

最新回测补充：[S2 无预测逆波动对照](S2_INVERSE_VOLATILITY.md)。同一十年区间、5bp交易成本下，逆波动年化7.79%／回撤22.97%，原S2为5.69%／16.52%；收益更高但回撤更大。9个场景账本通过，原6个S2场景与已发布结果哈希完全相同，82项测试通过。

本轮补充：[S4／S5／S8 数据与实现](STRATEGY_INPUTS_PROGRESS.md)。SEC与东方财富美股财务实测可用；财务版本/排名、财报事件校验、受限因子表达式和持久试验预算已实现；81项测试通过。尚未产出S4/S5/S8完整历史策略收益。

最新交付：[S6 残差反转／S7 期权盈亏复现](REMAINING_STRATEGIES_RESULTS.md)。S6 完成十年、三个变体、39 个假设场景，计入 5bp 交易成本和假设 2% 借券费后年化约 -4.91%／-4.85%／-4.76%；S7 完成三种到期盈亏结构，没有历史链回测。正式做空／期权仍未就绪。

最新交付：[十年扩展回测](TEN_YEAR_RESULTS.md)。保留原 18 资产池，S1/S2 从 2016-09-19 起、S3 从 2023-01-03 起测试；独立的新浪＋腾讯快照保留逐行补录证据与成交量限制。此前报告为历史版本。

最新扩展结果：[S3／Ridge／规则消融比较](EXTENDED_COMPARISON.md)。已完成 54 个组合场景及冻结模型离线重放；2026 年模型表现未超过 S1、SPY、QQQ。[其余策略缺口](STRATEGY_GAPS.md)单独列明。所有结果仍是探索研究。

2026-09-18：首轮真实美股 ETF 探索性回测已完成，采用新浪供应商的 18 只美国上市资产，预热后区间为 2020-01-02 至 2026-09-17。**这是抽样核验数据上的历史研究，尚未通过完整数据/样本外/LEAN 投资验收。** 年化、回撤、成本压力及局限见 [US_ETF_BACKTEST.md](US_ETF_BACKTEST.md)，逐项状态见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)。

这是独立 src-layout Python 项目。现有 TradingAgents、国内 ETF prediction_research、根 `.venv` 和日预测任务均不依赖它。只在本目录运行安装命令。

## 安装和第一步验收

```powershell
Set-Location E:\Trade\TradingAgents\quant_research
uv sync --locked --python 3.12
& .venv/Scripts/quant-research.exe doctor --offline
& .venv/Scripts/python.exe -m pytest -q --disable-warnings
& .venv/Scripts/ruff.exe check src tests
```

依赖的唯一锁文件为 `uv.lock`，完整安装 Qlib 0.9.7 及其依赖，没有使用 `--no-deps`。已实际验证 Windows / Python 3.12.14；没有宣称完成 WSL 集成。`doctor` 检查导入与配置，不把导入成功解释为真实数据、LEAN 或交易已就绪。

配置从原设计模板转为 `runtime-1.0`。所有层级拒绝未知字段；不支持的模式/开关拒绝；费用、权重、资产代码等有额外约束。多数设计参数锁定，只有 `config.py:TUNABLE` 中列出的字段允许修改，防止接受实际上未执行的配置。更改参数就是新研究合同。

## 一条离线演示流程

```powershell
$fixture = (& .venv/Scripts/quant-research.exe fixture | ConvertFrom-Json)
$run = (& .venv/Scripts/quant-research.exe backtest --snapshot $fixture.snapshot --config configs/fixture.json | ConvertFrom-Json)
& .venv/Scripts/quant-research.exe replay --manifest (Join-Path $run.directory 'manifest.json') --offline
& .venv/Scripts/quant-research.exe factors --snapshot $fixture.snapshot --config configs/fixture.json --horizon 5
```

`fixture` 生成明确标为 `synthetic_fixture_not_market_data` 的 SPY/QQQ 两资产序列。另有三天手算 CSV 验证 1,000 美元买 5 单位、成本 1 美元后收盘净值 1,049 的恒等式。合成两资产数据不足以作横截面 IC 或 5+1+1 年模型验证，相关报告返回 `insufficient_cross_section` / `insufficient_data`。

`backtest` 同时运行 SPY/QQQ 持有、月末等权、SPY 趋势、S1、S2，在 5/10/25 bps 下输出 18 组结果。Qlib Exchange/Position 真实执行成交和现金更新；替换的只有离线报价加载入口。美股配置、分数研究单位、下一 XNYS 开盘执行、零最低佣金、按买卖名义额收取费用均显式设置。不使用 Qlib 默认信号移位，也不额外计入分红或拆股份额。

Qlib 用于开盘撮合的内部 `$close` 设置为同日 **open**，仅避免其停牌检查提前读取尚未产生的 close；真正日终估值独立读取 close，缺失持仓收盘价直接失败。ADV/容量只来自信号日已经完成的历史数据，Qlib 不读取执行日全天成交量来限定订单。

结果在 `outputs/<run_id>/`：

- `manifest.json`：配置、数据与源代码哈希、依赖、版本、时间、生成模式和结果哈希。
- `experiment_contract.json`：运行前固定的策略、成本和试验预算。
- `targets.parquet`：完整权重、零权重行、决策/生成/最早执行时间、版本与约束原因。
- `fills.parquet`、`holdings.parquet`、`nav.parquet`：归一化研究单位及现金账本。
- `results.json`、`metrics.json`、`data_quality_report.json`：全部场景和独立逐日现金复算。
- `report.md`、`report.html`、`overview.png`：研究限制、收益、回撤、敞口和成本。

目录原子发布，不覆盖历史；失败实验也保留报告。DuckDB 只是索引，冻结 manifest 是事实来源。离线重放核对文件、源快照、配置、源码和锁文件，再重算结果哈希；源码升级后应使用原版本重放，不能假装新实现就是旧实验。

## 真实数据入口和当前阻塞

2026-09-18 后续更新：国内新浪采集入口 `data fetch-domestic` 已接通；18 只 ETF 的原始响应已冻结，14 只通过基础解析，4 只存在价格异常，仍需缺失日、复权及产品核验。详见 [国内数据与策略现状](DOMESTIC_DATA_AND_STRATEGIES.md)。此入口单独存入 `data/domestic`，不生成可冒充已核验 Yahoo 总回报快照的 `Adj Close`。

```powershell
& .venv/Scripts/quant-research.exe data fetch --start 2026-08-17 --end 2026-09-17
& .venv/Scripts/quant-research.exe data validate --snapshot <snapshot_directory>
```

`--end` 为排他结束日。默认按美东 18:00 与 XNYS 日历选已完成的决策日；不使用中国日期或简单工作日。下载保留 OHLCV、Adj Close、分红拆股、供应商 metadata 与时间，部分失败不允许当作完整池回测。

`data fetch --parent <complete_snapshot> --end <later_exclusive_date>` 读取旧快照末尾 5 行对应窗口，记录供应商修订、消失的旧日期和新增日期，发布新快照；不改写旧文件。同一完整请求直接返回原快照，不联网。增量合并仅限同一供应商、起始日和资产池；更早历史的供应商修订需要新全量快照。

下载遇到首次 Yahoo 限流即停止当前批次，未请求标的记为 `SkippedAfterRateLimit`（尝试次数 0），已成功标的保留，失败快照不可回测。普通暂时性错误仍有限重试。`data validate` 只有在 SPY/QQQ 存在共同合格信号日且有后续交易日时才以 0 退出；结果中的 `backtest_ready` 仅表示回测入口条件满足。

本次沙箱内网络不可达；获得联网执行权限后，18 只 ETF 重试均返回 `YFRateLimitError`。失败快照已保留，未更换供应商拼凑结果。即使恢复下载，`auto_adjust=False` 也不证明历史 raw 价格、成交量口径或成立日期已核验；未核验时不给出合格 ADV/入池资格。还需要独立的产品/价格口径核验与新快照发布流程。

## 后续模块边界

- `factors.py` 登记 16 个因果因子；`diagnostics.py` 输出日度 RankIC、覆盖率、分组标签收益和 5/20 日 block bootstrap 描述；不自动筛因子或翻转方向。
- `validation.py` 生成 t+1 open → t+h close 标签，以及日期分组、purge/embargo 的 5/1/1 年滚动切分。
- `model.train_fold` 支持固定 LightGBM 与 Ridge；`compare --snapshot ... --config ...` 完成 5 年训练＋1 年验证的季度重训、S3 同约束组合及两种规则消融。`replay-comparison --manifest ... --offline` 核对特征和冻结模型预测，再精确重放组合。当前只有 2026 年部分测试期，没有完整三年 OOS 或未触碰留出。
- `evidence.py` 只解析带时间和版本的结构化证据，拒绝迟到、错标的及歧义评级；Overweight 不转换为上涨概率。尚未接现有 daily JSON 全量适配或真实影子实验。
- `daily --snapshot ...` 是最新完成日的**历史研究刷新**，支持单实例锁、阶段恢复与幂等；拒绝陈旧快照，不是前向交易。锁文件在异常进程终止后保留；须先确认进程已退出再人工清理，不能自动抢占未知运行者。
- `forward_execution_at` 已测试晚于开盘生成时顺延，不能倒填开盘；前向持仓/订单状态机仍未接入。
- `status` 列出模型晋级、LEAN、前向模拟、借券、期权和自动因子的缺失证据；不会启动这些能力。

原始设计位于 [docs/quant_research](../docs/quant_research/README.md)。Qlib API 根据实际安装的 0.9.7 源码检查；官方参考：[Qlib release](https://github.com/microsoft/qlib/releases/tag/v0.9.7)、[yfinance API](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.history.html)。Qlib/yfinance 分别为 MIT/Apache-2.0 软件许可，行情使用权不因软件许可而自动授予。
