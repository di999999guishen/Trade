# 开源工具选型与环境实施方案

版本：2026-09-17。状态：**开发方案，尚未安装、接入或完成回测**。本文中的阶段、参数和预算是拟议设计；官方链接用于核实产品能力与许可，不构成收益证据。实施时必须对选定版本再次核实。

## 1. 选型结论

按当前需求，首期做**美股 ETF、日线信号、每日评估调仓、只做多、先用免费数据**。每日评估不代表每天强制买卖：目标权重不变或变化小于阈值时可以不交易。后续再扩展股票截面、多空和期权。

推荐按 P0–P2 三阶段主线建设，P3 单独扩展，起步只保留一个研究回测引擎：

| 阶段 | 主线工具 | 要解决的问题 | 进入下一阶段的依据 |
|---|---|---|---|
| P0：免费 ETF 验证 | Python + pandas/NumPy + yfinance 适配器 + Parquet/DuckDB + **Qlib 规则策略与日线回测** | 数据可追溯、ETF 趋势/轮动规则、费用后收益、SPY/QQQ 基准比较 | 人工账本与回测一致；结果可复现；多个时间段结果完整；不要求必须跑赢 |
| P1：因子与模型 | 在 P0 上增加 Qlib 因子工作流 + Alphalens-reloaded；需要时训练 LightGBM、使用 Optuna；统一 MLflow 记录 | 检验新因子是否提供样本外增量，而非增加参数数量 | 因子和模型在冻结样本外检验通过；相对简单规则确有增益 |
| P2：复核与模拟 | 独立环境的 **LEAN engine**；自建数据适配器；以后接纸面账户 | 对 ETF 候选信号做独立事件驱动复核，补充真实股数、撮合、公司行动和现金账本验证 | 两引擎差异有解释；数据、成交和持仓逐项核对；纸面记录满足项目验收规则 |
| P3：扩展研究 | 股票 PIT 数据、做空/期权适配、可选 RD-Agent | 分别研究股票策略、借券融资、期权生命周期与自动因子研发 | 每项扩展独立验证数据、交易机制和预算，不沿用 ETF MVP 的通过标签 |

Qlib 在 P0 只消费规则生成的分数/目标权重，不要求训练机器学习模型。少量 ETF 的横截面样本有限，不能把“接入 LightGBM”本身当作进步。Qlib 包的依赖可能包含 LightGBM 和 MLflow，因此“首期不训练模型”不等于强行删除包依赖。

**vectorbt 社区版仅作可选加速器**，不列入必须安装的开源主线。它当前采用 Apache 2.0 加 Commons Clause，属于源码可用的 fair-code；这与不含该限制的 Apache 2.0 不同。严格要求宽松开源时，直接跳过。NautilusTrader 和 RD-Agent 也不在 P0 中安装。

## 2. 工具对照表

“P0 必需”指本方案中的职责是否必须；允许因冻结版本兼容问题采用等价实现，但不能静默更换回测口径。

| 工具 | 本项目角色 | 许可/费用边界与主要限制 | P0 必需 | 官方依据 |
|---|---|---|---|---|
| Qlib / `pyqlib` | 因子表达式、规则信号接口、日频组合研究、后续模型训练与实验工作流 | MIT；需要自行准备可信美股数据并编写 ETF 策略适配；中国市场样例参数不可照搬；不以其日线撮合替代券商真实成交 | **是，唯一首期研究回测引擎** | [主仓库](https://github.com/microsoft/qlib)、[许可](https://github.com/microsoft/qlib/blob/main/LICENSE)、[数据层](https://qlib.readthedocs.io/en/stable/component/data.html) |
| LightGBM | 低成本树模型基线；对因子作非线性组合或排序 | MIT；先比较线性/规则基线，控制容量；ETF 小样本容易过拟合；官方仓库已迁移到 `lightgbm-org` | 否；可能作为 Qlib 依赖被安装，P1 才考虑训练 | [官方仓库与迁移说明](https://github.com/lightgbm-org/LightGBM) |
| DuckDB + Parquet / PyArrow | 分区行情、因子、目标权重和结果文件；SQL 质量检查及分析 | DuckDB MIT，Apache Arrow Apache 2.0；文件格式不授予上游数据使用权；DuckDB 不作为券商订单状态数据库 | **是，标准存储层** | [DuckDB](https://github.com/duckdb/duckdb)、[Apache Arrow](https://github.com/apache/arrow) |
| yfinance | 免费探索数据下载适配器，保存原始响应/参数/抓取时间 | 工具开源不代表 Yahoo 数据可自由商业再分发；项目声明面向研究教育并提醒个人用途条款；无可靠服务承诺，不能假定退市 ETF 历史齐全 | **是，首个可替换数据适配器** | [项目与数据使用说明](https://github.com/ranaroussi/yfinance) |
| exchange_calendars | 美股交易日、开收盘与半日市日历 | 具体交易所、历史修订与冻结版本须验证；不能用普通工作日序列替代真实日历 | 是，此职责必需 | [官方项目](https://github.com/gerrymanoim/exchange_calendars) |
| Alphalens-reloaded | 因子 IC、分层收益、换手和分组分析 | Apache 2.0；分析工具并非交易执行回测器；十余个高度相关 ETF 的五分位统计可能不稳定，须报告样本数 | 否，P1 | [维护分支](https://github.com/stefan-jansen/alphalens-reloaded) |
| MLflow | 配置、指标、因子/模型文件、数据快照标识、失败试验记录 | Apache 2.0；本地可运行；要遵守 Qlib 所选版本的 MLflow 约束，不能独立升级到最新版 | 是，优先复用 Qlib recorder；无需独立服务器 | [许可](https://github.com/mlflow/mlflow/blob/master/LICENSE.txt)、[Tracking](https://mlflow.org/docs/latest/ml/tracking) |
| Optuna | 在训练/验证区间内做有预算的超参数搜索 | MIT；优化器无法防止研究者反复看测试集造成的过拟合；所有 trial 都计入研究次数 | 否，P1 的模型/多参数策略需要时才启用 | [官方项目](https://github.com/optuna/optuna) |
| LEAN engine | 候选组合的独立复核；以后扩展证券交易、纸面账户和期权 | 引擎 Apache 2.0；数据、券商连接、服务及插件权限分别核实；直接源码部署有维护成本 | 否，P2；形成可信交易候选前必须完成独立复核 | [引擎](https://github.com/QuantConnect/Lean)、[许可](https://github.com/QuantConnect/Lean/blob/master/LICENSE) |
| LEAN CLI | 可选的项目、容器、云端和本地运行管理工具 | CLI 源码 Apache 2.0；**官方使用流程要求加入付费层级组织**；CLI 免费可安装不等于相关产品功能免费可用 | 否，不作为零付费路线前提 | [CLI 许可](https://github.com/QuantConnect/lean-cli/blob/master/LICENSE)、[Getting Started](https://www.quantconnect.com/docs/v2/lean-cli/key-concepts/getting-started) |
| vectorbt 社区版 | 对已冻结规则做快速数组级筛选、参数敏感性探索 | Apache 2.0 + Commons Clause；源码可用、内部研究可免费使用；销售以该软件为主要价值的产品/服务有额外限制；与 PRO 功能/API 不同 | 否，可完全省略 | [社区说明](https://github.com/polakowo/vectorbt)、[许可原文](https://github.com/polakowo/vectorbt/blob/master/LICENSE.md) |
| VectorBT PRO | 额外商业功能、支持及加速能力 | 独立商业产品；不把 PRO 示例函数写进社区版实现；费用以上线时官方页面为准 | 否，暂无采购理由 | [PRO 官网](https://vectorbt.pro/) |
| RD-Agent | 自动提出和实现因子候选，在受控预算下做研究辅助 | MIT；官方当前说明只支持 Linux，许多场景需要 Docker；模型推理/嵌入和运算可能产生费用；不能自动接触最终测试集 | 否，P3 独立试点 | [官方项目](https://github.com/microsoft/RD-Agent)、[许可](https://github.com/microsoft/RD-Agent/blob/main/LICENSE) |
| NautilusTrader | 低延迟事件驱动、复杂多市场执行的备选路线 | LGPL-3.0；研究和执行适配成本较高；Windows/Python 支持以具体发行版为准，不能混用 1.x 安装包和 2.x 文档 | 否；将来出现明确执行需求时，与 LEAN 比较后择一 | [官方项目](https://github.com/nautechsystems/nautilus_trader)、[安装说明](https://nautilustrader.io/docs/latest/getting_started/installation/) |

## 3. Qlib 规则 ETF 回测如何落地

### 3.1 可行性与职责边界

Qlib 的 `BaseSignalStrategy` 接受 `pandas.Series`、`DataFrame` 等信号输入，因此可以用动量、波动率、均线等确定性规则生成分数，无需先训练模型。目标权重轮动需要实现薄适配层；不要假定 `TopkDropoutStrategy` 的默认换仓、持有期及资金分配恰好等于本项目设计。依据：[官方信号策略源码](https://github.com/microsoft/qlib/blob/main/qlib/contrib/strategy/signal_strategy.py)。

```text
原始 ETF 行情/公司行动快照
    → 标准 Parquet 数据 + DuckDB 质量检查
    → Qlib provider 数据导出/日历/有效证券区间
    → 只用 t 日已完成且已可得行情，在美东 18:00 计算规则分数
    → 组合构建生成下一 XNYS 交易日生效的目标权重
    → Qlib 调整后开盘价代理回测 + 可核对的归一单位/现金账本
    → 费用后绩效与 SPY/QQQ 总收益基准
```

Parquet 是可移植的数据事实层；Qlib provider 是可重建的派生格式。以后接 LEAN、替换数据源或加入可选 vectorbt 时，读取同一冻结数据和信号，避免每个框架自行联网下载一套不同数据。

### 3.2 必须显式设置的美股参数

| 配置职责 | 本方案要求 | 实施验收 |
|---|---|---|
| 市场区域 | `region=REG_US`；不使用默认中国市场配置 | 记录初始化配置；验证无 A 股 100 股交易单位与固定涨跌幅限制 |
| 最小单位 | P0 `adjusted_total_return_proxy` 使用归一化资产的分数单位，显式设 `trade_unit=None`；P2 raw 账本按整数股 | P0 验证分数单位和余额；P2 验证一股、余额不足与整数取整；两种数量不能互换 |
| 成交时间 | `t` 日完整行情可得后，在 `America/New_York` 18:00 形成决策；下一 XNYS 交易日开盘近似成交 | 检查引擎内部信号前移，防重复 shift；前向生成延迟到开盘以后时不倒填订单 |
| 价格字段 | P0 的 `deal_price` 显式指定**调整后 open**，OHLC 使用相同总收益代理调整；P2 用验证后的 raw open | 数据字段映射验收；开盘缺失不得用事后收盘静默填补 |
| 仓位 | 显式给定目标权重、现金余额、单 ETF 上限及组合总权重上限 | 不使用未审阅的默认风险仓位；首期权重非负，总和不超过 1 |
| 费用/滑点 | P0 单边 5/10/25 bps 是按成交额收取的总摩擦情景；`open_cost=close_cost=bps/10000`，`min_cost=0`、`impact_cost=0` | 不在总摩擦之外重复加佣金/价差/滑点；同时输出毛收益、净收益和成本占比 |
| 成交容量 | P0 显式设 `volume_threshold=None`，外层依据决策前已知的美元 ADV 约束目标交易额 | 不使用执行日全天成交量决定开盘可成交数量或冲击成本；容量假设注明为日线近似 |
| 总收益 | 区分原始交易价格、拆分、分红、复权研究序列 | 明确采用哪种账本；禁止复权总收益和现金分红重复计入 |
| 基准 | SPY、QQQ 用与策略一致日期和分红口径 | 不把上证/沪深基准混入美股报告 |
| 可交易资格 | ETF 上市后、指标预热完成且有有效行情才可入池 | 不把后来发行 ETF 补回过去；退市、代码变更、缺失须标记 |

Qlib 官方区域配置当前显示：US 的 `trade_unit=1`、`limit_threshold=None`，但 `deal_price` 仍默认为 `close`。P0 必须覆盖交易单位和成交字段；Exchange 支持显式 `trade_unit=None` 关闭单位取整。手续费、最小费用、冲击成本与成交量限制是不同参数，不能只改买卖费率就认为其他默认行为已关闭。“未设置固定涨跌幅限制”也不等于模拟了美股暂停交易与所有市场规则。依据：[区域配置源码](https://github.com/microsoft/qlib/blob/main/qlib/config.py)、[Exchange 参数及撮合实现](https://github.com/microsoft/qlib/blob/main/qlib/backtest/exchange.py)。

P0 固定采用 `adjusted_total_return_proxy`：收益已含分红近似，不另加现金分红，也不再次按拆股调整归一单位。其隐含再投资、理想开盘再平衡和比例总摩擦仅用于研究；只向 LEAN 导出目标权重，不导出代理单位作为真实股数。

P2 使用 `raw_with_corporate_actions`，独立处理真实股数、公司行动和可用现金，并按实际研究假设拆分 commission、spread、slippage 等费用。若在 P2 使用这些拆分项，就不再叠加 P0 的 all-in 总摩擦；可额外运行同一总摩擦场景用于诊断，但每次回测只采用一种完整成本口径。跨引擎差异逐项归因，不能把 P0 研究结果称为可实盘收益。

### 3.3 什么暂时不做

P0 不训练深度模型，不开分布式集群，不建 Redis/MongoDB 服务，不把所有 Alpha158/Alpha360 字段默认视为有效 ETF 因子。Qlib 内部已经依赖的包按选定发行版正常安装；“不用某服务”与“破坏依赖安装”是两回事。

Qlib 提供美股数据/示例的事实只能证明接口适配能力。中国市场回测、官方模型排行榜和样例数据结果均不能用于宣称 ETF 策略跑赢 SPY/QQQ。免费样例数据还要核实覆盖日期、更新频率、退市记录和分红质量。[Qlib 数据文档](https://qlib.readthedocs.io/en/stable/component/data.html)

## 4. LEAN engine 与 CLI 必须分开理解

| 路线 | 可以据此承诺的内容 | 不能据此承诺的内容 |
|---|---|---|
| 引擎源码自建、自己的数据、本地运行 | 引擎许可证本身不收许可费；官方仓库给出直接构建和运行 Launcher 的路线 | 不是零维护、零算力成本；不包含任意数据源、券商服务、实时行情权益 |
| 官方 LEAN CLI 工作流 | 容器和开发流程较省事，支持官方集成流程 | 官方当前要求付费组织，不能宣称所有 CLI 本地功能免费 |
| QuantConnect 云服务或数据购买 | 可在各服务权限内使用对应能力 | 引擎开源不等于数据可免费批量下载或可自由再分发 |

本项目的 P2 默认研究路线是**独立 Linux 容器中直接运行锁定的 LEAN engine，使用本项目导出的数据和信号**。具体做法在实施阶段确定：锁定引擎 commit、阅读对应构建文档、选择符合授权的镜像或自行构建、配置 Launcher、关闭自动数据购买路径、使用本地数据源运行最小样例。容器方式是本项目部署设计，尚未在当前机器验证；如果不采用容器，也可以在独立 WSL2 环境按官方源码路线构建。[LEAN 本地源码说明](https://github.com/QuantConnect/Lean#linux-debian-ubuntu)

两引擎复核先输入**同一目标权重文件**，不在各自框架重写一遍信号公式。先排除日历、分红、现金、取整、价格与费用差异，再考虑重实现策略。纸面券商接入另外核实适配器能力及账户权限；本方案不预设购买服务。[CLI 访问要求](https://www.quantconnect.com/docs/v2/lean-cli/key-concepts/getting-started)、[QuantConnect 当前产品页面](https://www.quantconnect.com/pricing)

## 5. Windows / WSL2 环境路线

### 推荐：Windows 保留现有 Agent，WSL2 承载新增量化环境

| 环境 | 用途 | 隔离要求 |
|---|---|---|
| 当前 Windows TradingAgents 环境 | 继续生成现有 AI 研究输出 | 不升级现有 `.venv`、不覆盖原依赖文件；与量化层用结构化文件交互 |
| 新 WSL2 Ubuntu 研究环境 | P0/P1 的 Qlib、数据处理、规则回测、因子和模型 | 独立工作目录、解释器和锁文件；Python 3.11 可作为首轮兼容候选，实际以冻结版本 smoke test 结果为准 |
| 独立 LEAN 容器/源码目录 | P2 复核及以后模拟交易 | LEAN 的 .NET、Python 和依赖按该 commit 单独管理；不能强塞进 Qlib 虚拟环境 |
| 独立 RD-Agent 容器/环境 | P3 受控自动因子试验 | 按其 Linux、Docker 和模型端点要求配置；与最终测试数据物理隔离 |

Windows 原生是备选，不是禁止路线：Qlib 包元数据列出 Windows，但具体 wheel、Cython/NumPy、求解器和多进程行为仍应做安装与功能检查。RD-Agent 官方目前仅声明支持 Linux，因此不应承诺 Windows 原生一键安装。NautilusTrader 的发行版、Python 版本和操作系统矩阵与 Qlib 不同，未来选用时另行冻结。[Qlib 包元数据](https://github.com/microsoft/qlib/blob/main/pyproject.toml)、[RD-Agent 安装要求](https://github.com/microsoft/RD-Agent#-quick-start)、[NautilusTrader 安装文档](https://nautilustrader.io/docs/latest/getting_started/installation/)

数据量较大时，将频繁读写的数据和虚拟环境放在 WSL2 Linux 文件系统；Windows 路径只用于编辑/导出。不要同时从两个进程写同一个 DuckDB 或 MLflow SQLite 文件。首期不要求 GPU，现有 CPU 机器即可进行 ETF 日线原型；后续是否添置硬件由实测耗时和内存峰值决定。

## 6. 依赖冻结与复现方法

不在方案阶段编造“已验证版本组合”。实施时先选定稳定发行 tag，再生成真实锁文件。开发分支页面用于调查，不能直接作为永远可复现的安装来源。

1. 新建独立量化包/环境，记录操作系统、Python 补丁版本、处理器架构和环境管理器版本；禁止修改现有 Agent 依赖来迁就新框架。
2. 根据选定 Qlib tag 的 `pyproject.toml` 解析依赖；先只启用 P0 所需能力，不加深度学习、RL 和分布式 extras。
3. 使用一个管理器生成精确锁文件，例如 `uv.lock` 或带哈希的 `requirements.lock`；记录来源 index、直接依赖、传递依赖和 VCS commit。未来安装命令按锁文件执行，不用无上限的 `pip install -U`。
4. 保存 `environment_manifest.json`：Python/包版本、Git commit、许可清单、数据版本、配置哈希、随机种子、时区、线程数。LEAN 另外保存镜像 digest、引擎 commit 和运行配置。
5. 对 P0 做有意义的最小验收：包导入；日历/数据导出；两至三只 ETF 的确定性规则回测；归一化分数单位、跨公司行动的代理收益与比例费用账本；信号时间和重复运行一致性。真实一股取整、拆股份额及分红现金可用性在 P2 raw 账本验收。
6. 验证成功才冻结版本。升级时保存旧锁文件和数据快照，对同一固定案例核对收益、成交和持仓差异；不能无说明覆盖历史报告。

特别注意 MLflow 兼容性：本次核实的 Qlib `main` 包元数据有 `mlflow<3.13` 约束，说明不能把 Qlib 和“最新 MLflow”分别安装后假定相容。该数字是本次查阅状态，并非本项目已锁定的依赖；实际以选定 tag 的依赖和验证结果为准。[Qlib 依赖声明](https://github.com/microsoft/qlib/blob/main/pyproject.toml)

MLflow 本地元数据可显式使用 SQLite、工件放本地目录；也可按冻结 Qlib 版本所需配置文件后端。不要依赖不同 MLflow 版本变化中的默认路径。不需要为单人 P0 搭建远程服务。[MLflow 本地架构](https://mlflow.org/docs/latest/self-hosting/architecture/overview/)

## 7. 数据、费用和升级触发条件

### 7.1 免费数据首先用于验证工程与初筛

首期通过 yfinance 下载 ETF 日线和可得公司行动字段，同时保存下载参数、日期区间、是否自动复权、时区、源字段、抓取时间及内容哈希。免费源的缺失、限流和历史修订应作为质量结果记录，不能靠反复下载直到“收益更好”。具体数据使用范围遵守源条款。[yfinance 数据说明](https://github.com/ranaroussi/yfinance)

固定一组今天仍在交易的 ETF 回溯历史，只能回答“这组存活 ETF 的规则过去表现如何”，不能证明对当时整个 ETF 市场有效。后来发行的 ETF 不得回填上市前价格；缺少退市 ETF、历史分类和当时可选范围时，报告保留“存活偏差/选择偏差未消除”标签。

付费升级优先级为：可信公司行动与历史证券状态 → 更完整的历史池/退市数据 → 股票研究所需 point-in-time 财务数据 → 执行所需更细粒度行情 → 期权历史链。不是先买昂贵模型或云服务器。

本方案**没有核实到足以直接支撑可信长期期权回测的免费完整历史链方案**。当前期权链快照不能替代过去每日当时可交易合约、买卖价差、到期、行权和公司行动历史；未解决这些问题前不输出“期权策略跑赢”的结论。

### 7.2 内部预算范围，不是供应商报价

以下是便于排期的可调整月度预算上限区间，以美元表示，不含已有硬件、电费、人员时间、交易本金、税费或交易佣金。并非任何供应商的承诺价格；是否采购由验证结果和实际合同决定。

| 阶段 | 建议内部月度预算 | 可以覆盖的目标 | 超出预算时的动作 |
|---|---|---|---|
| P0 免费探索 | **新增软件许可/数据订阅 0 美元** | 本地开源研究工具 + 允许个人研究使用的数据源；不新增 LLM 调用 | 保持 ETF 日线范围，减少联网频率和研究组合；不自动购买 |
| P1 提升数据与常规研究 | 预留 50–300 美元 | 为候选付费数据/备份/适量算力留空间；实际覆盖范围取决于报价 | 优先保证数据质量，减少附加工具和搜索预算 |
| P3 自动因子试点 | 另设 20–100 美元试验额度 | 小规模 LLM 调用；按 trial、token 和时间多重限额 | 达到额度停止新增试验；保留所有失败记录 |
| P3 广泛历史证券/期权研究 | 预留 200–1,000+ 美元 | 仅作为更完整历史数据和服务的采购准备区间 | 取得具体覆盖/授权/报价后重估；可能远高于该区间，不承诺足够 |

即使付费数据质量更好，也不能证明策略将跑赢指数；采购理由是减少数据偏差、完善检验和执行模拟。

## 8. 替代路线与决策边界

| 需求变化 | 建议决策 | 保留的可迁移资产 |
|---|---|---|
| 坚持全部工具采用宽松开源许可 | 使用 Qlib + LEAN engine；不依赖 vectorbt 社区版/PRO | Parquet、数据 manifest、目标权重、账本和报告 |
| 单人 ETF 参数筛选确有耗时瓶颈且仅内部研究 | 可在隔离环境评估 vectorbt 社区版，审阅 Commons Clause；先用固定案例对齐 Qlib | 同一信号和数据；不改变默认主引擎 |
| ETF 样本不足以训练稳定模型 | 保持规则策略和少量经济含义清晰的因子；LightGBM 可以不启用 | 因子版本、失败研究和样本外结果 |
| 主要工作变为订单簿、多市场复杂执行 | 对 LEAN 与 NautilusTrader 做小规模能力对比，择一作为执行主线 | 标准化订单、事件、持仓与风险接口 |
| 希望降低 LEAN 自建维护时间 | 在明确费用和权限后评估官方 CLI/托管服务 | 不迁移研究逻辑到不可导出的专有数据结构 |
| 免费数据暴露公司行动/退市缺口 | 替换数据适配器，再重跑冻结研究 | 原始旧快照仍保留；不得把新旧收益当作同一版本 |

最终选型的判断标准是：能否重现、能否解释成交与现金、能否严格隔离训练和测试、能否追溯数据授权与版本。框架名称、社区热度和模型复杂度都不能替代这些验收条件。
