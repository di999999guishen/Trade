# 本次验收记录

## 2026-09-18 S4–S8组合／执行扩展

- 89项测试通过，28.71秒，135条依赖提示；ruff对src/tests/scripts通过。
- S4行业配置可行性、S5事件退出和行业额度、S6过期借券及召回/分红负债结算、S7报价/预算/同步/指派变化、S8同S3趋势门检查通过。
- S8仅复用已登记三个表达式，因子预算仍3/30；2023-01-03至2026-09-17，三个成本场景独立账本核对通过。
- 独立进程PYTHONHASHSEED=131重算特征、子进程因子和组合，结果哈希一致：`e9454c8d1f5306dd99961044d28db3c5175175085a497b5ebba3b45587d20349`。
- 进程输入分离不是操作系统权限沙箱；2026-09-21至2028-09-20未来留出仅登记，尚未采集或评估。
- S4–S7新增事件算例全部标记人为构造，不是历史收益；详情见 [S4_S8_COMPLETION_PROGRESS.md](S4_S8_COMPLETION_PROGRESS.md)。

## 2026-09-18 S2 无预测逆波动对照

- 82项测试通过，23.52秒，135条依赖／日历提示；ruff 对 src/tests/scripts 通过。
- 同十年、三策略三档成本共9个场景，全部独立账本核对通过；原S2/等权消融共6场景完整结果哈希与此前十年基线一致。
- 新增逆波动控制证明不依赖趋势、动量或预测得分，并保留S2风险及执行约束。没有调整既有参数。
- PYTHONHASHSEED=97独立进程重算全部9个场景，结果哈希精确一致：`d3f093719517f9b74afdf9fd3e85a8fe72bd33f1fbaf1f87b61fcbb361de55d5`。计算源码、依赖、数据和参数相同；运行脚本的静态检查与版本记录改动单独记入重放元数据。
- 结果及产物见 [S2_INVERSE_VOLATILITY.md](S2_INVERSE_VOLATILITY.md)。其较高历史年化同时伴随较大回撤，仍无最终留出／raw／LEAN投资晋级证据。

## 2026-09-18 S4／S5／S8 输入与信号模块

- 81项测试通过，24.48秒，135条依赖／日历提示；ruff 对 src/tests/scripts 通过。
- S4财务版本按可得时点选择，未来修订不覆盖过去；缺失财务剔除、金融行业排除及少样本标准化回退已测试。SEC采集记录不自动赋予历史可得时间。
- S5校验预期发布前60分钟可得、至少六个历史季度、每股口径一致、晚到数据延后、历史百分位排除未来与不可比事件。
- S8拒绝任意导入／属性访问／目标列／负滞后／未登记窗口／过深表达式；前缀计算不变、横截面标准化及失败/重复试验计数持久性通过。
- 三个预登记候选在冻结美股开发数据上计算、相关性检查及等权排名；PYTHONHASHSEED=97独立进程重算因子哈希一致，研究预算维持3/30。未评估S8收益，未使用最终留出。
- Windows首次归档因未关闭SQLite备份连接失败；已显式关闭，并从保留的未发布目录校验/重算后恢复，不新增试验或重置预算。
- 详情和产物见 [STRATEGY_INPUTS_PROGRESS.md](STRATEGY_INPUTS_PROGRESS.md)。S4/S5/S8仍不具备完整历史策略回测或投资晋级证据。

## 2026-09-18 S6／S7 新增实现

- 65 项测试通过，25.73 秒，114 条既有依赖提示；ruff 对 src/tests/scripts 通过。
- 新增 S6 信号前缀不变性、最近五日不进入回归拟合、下调仓位风险约束、卖空现金与负债、跨周末借券计提、独立审计篡改拒绝、费用拖累、持续入选也五日强制退出测试；S7 覆盖上下行手算、价差保护封顶、显式非100乘数、裸卖与错配交割拒绝。
- S6 2016-09-19 至 2026-09-17，三个固定变体、39 个成本／借券费假设场景，全部独立账本核对通过。S7 为人为设定输入的30个到期场景，没有历史年化结果。
- 独立进程 PYTHONHASHSEED=97 重算信号、目标及全部场景，精确结果哈希一致：`6e1c4128474bfeacbe65e530b4e60a4894a6ac6d9e9b59f3b541e70692732d48`。不是仅读取已经保存的净值。
- 结果、复现产物与局限见 [REMAINING_STRATEGIES_RESULTS.md](REMAINING_STRATEGIES_RESULTS.md)。S6 的假设可借、复权总回报代理及简化抵押不等于真实可执行做空；S7 到期算例不等于完整报价、滚动、指派回测；正式就绪门未改变。

## 2026-09-18 十年扩展

- 56 项测试通过，26.05 秒，114 条依赖弃用提示；ruff 对 src/tests/scripts 通过。新增跨供应商逐行来源、不修改其他有效行、拒绝未登记修补／未补缺口、拒绝复权响应／错标的／非法报价的测试。
- 新浪＋腾讯独立快照保留原18资产，补录/替换572行，XLRE 7个上市初期缺失日未填充且全部在253点预热门前。核对实际S1持仓，XLRE与XLC均未在符合入池条件前持有。
- 十年规则窗口2513个净值点；2023年起模型窗口930个净值点；LightGBM/Ridge各15折，合计30次拟合，不超过登记预算。54个场景现金/成交/NAV审计通过。
- 全量冻结模型重放：不同进程 PYTHONHASHSEED=97，特征重算、模型预测核对、全部组合重算与精确结果哈希一致。状态 `reproduced_offline_from_frozen_models`，未重新训练。结果哈希 `ac1863205b9c25d3ff938cf27a06185d9a18beca74c215292253f19e5ccf7162`。
- 补充真实因子、训练期相关性、风险、滚动3/5年窗口描述；图已目视核对，源码与锁文件归档。结果入口：[TEN_YEAR_RESULTS.md](TEN_YEAR_RESULTS.md)。
- 失败的数据准备记录保留：首次SPY重叠校验窗口过窄，改为核对完整可用重叠；随后起始日恰为SPY除息日，增加一根前收盘，收益起点仍固定2016-09-19。未计算失败版本的策略收益。
- 仍为探索：部分供应商成交量有差异、XLF 2016分派采用估值代理、无实际分派股数／支付日账本和独立全历史认证／未触碰留出／LEAN复核。二十年QQQ历史尚未恢复。
- 以下保留此前较短窗口的验收历史，旧实验需对应源码归档复现。

## 2026-09-18 S3 与扩展比较

- 53 项测试通过，24.80 秒，114 条依赖弃用提示；ruff 对 src/tests/scripts 通过。新增季度切分、假日起点与排他终点、成熟隔离、测试标签缺失不影响 LightGBM/Ridge 拟合、预测排名与强制趋势退出检查。
- 固定 16 特征、6 次拟合、54 个组合场景全部完成；每个场景现金/成交/NAV 独立核算通过。原有 18 个场景指标与前次有效结果完全一致。
- 不同进程以 PYTHONHASHSEED=97 进行离线重放，重新计算特征、核对冻结模型预测并重算所有组合，精确结果哈希一致：`a214e0b579c684a463a5b9a3c54dc1ba52cf6f65743ca8cd803b4fcbb15caa22`。这次重放不重新训练，状态为 `reproduced_offline_from_frozen_models`。
- 新增真实 16 因子诊断、训练折相关性、风险/主动收益时间分块描述；SPY 自身对照的 beta 约为 1、主动收益区间为零。数值比较允许浮点精度，不要求 beta 位级等于 1。
- 净值图已目视检查，源码及锁文件另存归档，详见 [EXTENDED_COMPARISON.md](EXTENDED_COMPARISON.md)。初次嵌套路径过长的失败实验保留，修正目录长度后重跑。
- S3 只有 2026 年部分期间，无未触碰最终留出、完整三年模型测试、独立全历史数据认证或 LEAN raw 复核，仍未通过投资晋级。以下为历史验收记录。

## 2026-09-18 美股真实探索回测

- 49 项测试通过，24.47 秒，71 条既有库 warning；ruff 对 src/tests/scripts 通过。
- 新增分红/拆股手算、同日/未来价格扰动、未来因子重基不改过去收益、异常事件/脚本后缀拒绝，以及不同 `PYTHONHASHSEED` 多资产结果一致性测试。
- 18 只美国上市资产，原策略/成本参数保持，预热后 2020-01-02 至 2026-09-17，18 个场景账本逐日复算通过。图已目视检查。
- 最新结果入口：[US_ETF_BACKTEST.md](US_ETF_BACKTEST.md)。投资验收仍 NO-GO：数据只做抽样核验，无独立全历史认证/未触碰 OOS/LEAN。
- 最终全量研究 hash seed 1 与独立进程重放 hash seed 97 一致，状态 `reproduced_offline`，结果哈希 `07b9083dcb42d7f5bb8c037fa78b8e491b34494f36cc96aef49bfcf2e751597c`。
- 首轮跨进程精确哈希未通过，已修正无序集合浮点求和并发布新结果，旧产物保留；细节见结果报告。
- 以下旧记录为历史，不代表最新代码或最新源数据状态。

## 2026-09-18 11:58 国内数据入口验收

- 新增新浪候选采集、基础价格校验、独立冻结目录，以及离线逐日缺失/异常审计。
- 全量测试 **44 passed，20.09 秒，71 warnings**；`ruff check src tests scripts` 通过。
- 18 只原始响应落盘，14 只基本校验通过；4 只异常被拒绝，真实收益回测未启动。路径及证据见 [国内数据与策略现状](DOMESTIC_DATA_AND_STRATEGIES.md)。
- 本次新增源码和测试已改变源码哈希；以下旧研究 manifest 需对应版本才能重放，未把旧结果冒充新源码下的运行。

## 2026-09-18 11:30 北京时间追加验收

- 下载限流保护及校验门修正后：`python -m pytest -q --disable-warnings` 为 **39 passed，19.36 秒，71 warnings**；`ruff check src tests` 通过。
- 新增测试覆盖：首只限流、部分成功后限流、普通超时继续重试、仅单基准合格拒绝、仅预热无执行日拒绝。
- 当前源码下重新运行 18 组固定合成场景并离线重放，状态 `reproduced_offline`，哈希一致。
- 当前报告：[report.md](outputs/research_20260918T033000749026Z_9d2cb85cc1384aab9cb55ae0b41369da/report.md)。
- 当前结果 SHA-256：`100ee3a795bb9176ed74705a3eada9ddfc51521fad1ff5bee5ecff06e55c61ad`。
- 重放命令：`.venv/Scripts/quant-research.exe replay --manifest outputs/research_20260918T033000749026Z_9d2cb85cc1384aab9cb55ae0b41369da/manifest.json --offline`。
- 联网 SPY 小样本仍限流；Stooq 返回浏览器验证 HTML，未取得行情。未运行真实策略验收。
- 以下为 10:54 的历史验收记录和产物。其源码哈希对应旧版本；当前源码请使用上述新 manifest 重放，旧产物未覆盖。

2026-09-18，Windows，独立解释器 `E:\Trade\TradingAgents\quant_research\.venv\Scripts\python.exe`，Python 3.12.14。没有运行旧系统日预测或联网 LLM。

## 实际结果

- `uv sync --project quant_research --python 3.12`：完整依赖安装并锁定，Qlib 0.9.7、yfinance 1.7.0、exchange-calendars 4.13.2、pandas 2.3.3、LightGBM 4.7.0。
- `python -m quant_research.cli doctor --offline`：所有核心依赖导入通过；真实数据和 LEAN 明确未就绪。
- `python -m pytest -q --disable-warnings`：**35 passed，18.31 秒**。71 条 warning 为已安装库的弃用提示（主要 NumPy timedelta 与 LightGBM eval_set）；未把 warning 隐瞒成无任何提示。
- `ruff check src tests`：通过。
- 根目录 `git diff --check`：通过（git 提示既有 daily_predict.py 的换行转换，不属于本次修改）。新增子项目为未跟踪目录，语法/风格由上述子项目检查覆盖。
- 合成研究：6 个策略 × 3 个成本，18 组，全部 Qlib 成交现金逐日独立复算通过。
- 最新冻结结果离线重放：`reproduced_offline`，结果哈希完全相同。
- `factors --horizon 5`：16 因子产出；两资产截面不足，短历史不满足 5/1/1 年切分，正确返回不足状态。
- `overview.png` 已打开检查：标签完整，无裁切；标题明确标记合成样例。

## 可打开的产物

- [最新研究报告](outputs/research_20260918T025419651359Z_6b0b93e82f0d407aaa72da13b0cd55f0/report.md)
- [HTML 与图](outputs/research_20260918T025419651359Z_6b0b93e82f0d407aaa72da13b0cd55f0/report.html)
- [运行 manifest](outputs/research_20260918T025419651359Z_6b0b93e82f0d407aaa72da13b0cd55f0/manifest.json)
- [因子 manifest](outputs/factors_20260918T025000817611Z_138a7808aace4f128662c1351cad43d5/manifest.json)
- [真实数据失败 manifest](data/raw/snapshot_20260918T023051395439Z_6051121e67e84dbe9144c727271f66a5/manifest.json)

快照：`snapshot_20260918T023652321665Z_7789f079ec0347aeb85d99a3d32dba4c`，仅合成 SPY/QQQ fixture。

结果 SHA-256：

```text
0080dee16245a1e9f1e9b6c928f8dff4eb3965c4e5c39193f1e761e03c876ed6
```

精确重放命令，在子项目目录执行：

```powershell
& .venv/Scripts/quant-research.exe replay --manifest outputs/research_20260918T025419651359Z_6b0b93e82f0d407aaa72da13b0cd55f0/manifest.json --offline
```

`data/outputs/state/.venv` 不进入 git；本机可直接打开这些产物。复制项目到另一台机器后，请运行 README 的 fixture 流程生成独立样例，不假定这些本地产物随源码存在。

## 已验证的不变量

严格配置拒错；交易日/DST/半日；253 点预热；快照篡改拒绝；失败部分不可冒充成功；未来行情扰动不改变过去因子/权重；次日开盘且只移位一次；分数单位/费用/现金手算；缺开盘不按 close 偷成交；缺持仓估值直接失败；费用压力；含现金与不含现金换手分别记录；风险强制退出优先；零波动指标为 null；标签成熟与日期分组；模型测试标签不进入训练；证据错标的/迟到/歧义拒绝；延迟前向信号不倒填；单实例锁、研究恢复和幂等；完整离线重放与产物篡改拒绝。

## 没有验证过的能力

真实 ETF 策略独立超额收益、PIT 全市场存活偏差、真实拆股/支付日和整数股账本、S3 完整多年样本外有效性、LEAN 独立复核、前向账户运营、借券、历史期权、自动因子隔离与任何实盘成交。S3 季度组合工程探索已完成。完整列表见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)。
