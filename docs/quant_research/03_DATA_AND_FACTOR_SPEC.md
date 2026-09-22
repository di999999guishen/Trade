# 数据、因子与时间规范

状态：开发规格；没有下载本文件所列全量数据，没有产生策略绩效。

## 1. 免费起步的数据路线

| 数据 | P0来源与用途 | 不足与升级条件 |
|---|---|---|
| ETF日线OHLCV | yfinance，原始响应本地存档，用于探索 | 不保证无历史修订、完整退市覆盖或服务连续性；候选有效后再跨源核对 |
| 分红、拆股 | 同源actions，保留供应商字段 | 除息日不等于支付日；真实现金可用时间需要补充 |
| 基准 | SPY、QQQ实际ETF含分红收益 | 不拿不含分红指数对比含分红策略 |
| 交易日历 | exchange_calendars的XNYS及冻结版本 | 半日交易、夏令时、异常休市以日历与行情共同核验 |
| ETF成立/类别 | 基金发行商事实资料+行情首日核验 | 历史类别/终止信息可能缺失，今天分类不能冒充历史快照 |
| 宏观数据 | P0不需要；后续FRED/ALFRED候选 | 观察期不等于发布时刻；需vintage，不能直接用修订后值回填 |
| 股票财务 | P0不需要；后续SEC披露解析 | SEC免费披露不等于即用的清洗后PIT财务数据库 |
| 历史期权与借券 | P0不提供 | 缺历史链、双边报价、可借量/费用时，不做可信收益回测 |

yfinance定位个人研究/教育用途，数据使用权需遵守提供方条款。[官方说明](https://ranaroussi.github.io/yfinance/)。SEC提供公司披露历史与XBRL接口，但历史可得口径需要用披露版本和时间自行构建。[SEC官方API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)。

升级采购先提交“缺口表”：字段、标的、频率、时段、需要的历史版本、用途授权、样本检查结果、报价。Norgate等数据商可以作为日线历史覆盖候选，具体套餐的退市/历史成分权限单独核实，不假定包含财务共识或期权数据。[Norgate数据FAQ](https://norgatedata.com/data-package-faq.php)。

## 2. 首批资产池

静态探索名单共18只，目的在于覆盖不同经济敞口，而非按已知历史收益选冠军：

| 组别 | ticker | 说明 |
|---|---|---|
| 美国股票宽基/风格代理 | SPY, QQQ, IWM, MDY | SPY主基准，QQQ副基准，同时允许策略持有 |
| 美国股票行业 | XLB, XLE, XLF, XLI, XLK, XLP, XLU, XLV, XLY, XLRE, XLC | 各产品成立日不同，不能统一回填历史 |
| 美债 | IEF, TLT | 利率风险资产，不等同无风险现金 |
| 黄金 | GLD | 美国上市，但不是美国股票敞口 |

这不是“历史上完整可投资ETF全市场”。名单在2026年确定，含选择偏差与存活偏差风险；历史结果应标 `fixed_universe_exploratory`。正式升级需要ETF生存/终止记录、当时资产类别和预先定义的入池规则，或者持续积累此后固定池的前向证据。

入池条件：

1. 标的当时已上市且未终止，当前资产映射有效。
2. 按全部启用因子的依赖自动计算预热；首版至少253个有效收盘价格点，才能计算252个交易日的收益；缺行情不假设存在。
3. 当时已知最近20个完整交易日美元成交额均值达到配置阈值，示例为1000万美元，仅是研究初始值。
4. 最新交易日价格/量通过质量门；若被排除且已有持仓，区分“停止买入”和“无法成交”，不能凭空按最后价卖出。
5. 排除杠杆、反向、ETN等不同机制资产；引入时另建风险规则。

初始请求历史区间为2010-01-01起至运行时最新**已完成且已获取**的美股交易日。不承诺每只ETF都拥有该长度，也不把中国本地日期当美股已收盘日期。

## 3. 时间的四个含义

每个数据事实保存：

| 字段 | 意义 |
|---|---|
| `event_at` | 交易/经济事件发生时间 |
| `published_at` | 原始来源公布时间，如可获得 |
| `available_at` | 在研究假设下，完成发布与处理后可用于决策的时间 |
| `ingested_at` | 本系统实际下载保存的时间，不伪造为历史时刻 |

再保存 `availability_mode = observed / reconstructed / assumed`。今天下载2015年行情时，`ingested_at`是今天；历史可得时间只能按来源证据重建，并标为reconstructed或assumed。不能强行要求历史行ingested_at在2015年之前，也不能称其为当时已冻结数据。

历史研究采用 `available_at <= decision_at`，且快照版本被固定。前向模拟还要求实际接收到数据并完成信号生成后才能下模拟订单。数据到达晚时跳过该次决策或延迟执行，不改写 `generated_at`。

默认每天美国东部18:00决策（时区America/New_York，不写死UTC偏移），最早下一XNYS交易日开盘。若使用当天尚未结束的日线，则拒绝该输入。执行日开盘成交容量只能基于截至前一日已知成交量估计，不能提前看到执行日全天成交量。

## 4. 存储表

| 表 | 主键/版本 | 关键字段 |
|---|---|---|
| `assets` | asset_id + valid_from | ticker, exchange, currency, type, inception, termination, category, source |
| `bars` | snapshot_id + asset_id + session | open,high,low,close,volume,price_basis,available_at,ingested_at,quality_flags |
| `actions` | asset_id + event_id + revision | type,split_ratio,dividend_per_share,ex_date,pay_date,announcement_at,known_at |
| `universe_membership` | universe_version + session + asset_id | eligible, exclusion_reasons, warmup_count, lagged_adv_usd |
| `factors` | factor_version + asset_id + decision_at | raw_value, transformed_value, input_snapshot, max_input_available_at |
| `labels` | label_version + asset_id + signal_session | entry_at,exit_at,return_value,label_available_at,price_mode |
| `predictions` | run_id + decision_at + asset_id | score,model_hash,fit_start,fit_end,max_label_available_at |
| `targets` | run_id + decision_at + asset_id | target_weight, score, reasons, earliest_execution_at |
| `orders/fills` | engine_run_id + id | quantity,side,price,commission,slippage,created_at,filled_at,status |
| `positions/nav` | engine_run_id + valuation_at | cash,receivables,holdings_value,liabilities,nav,stale_price_flags |
| `experiments` | experiment_id + revision | hypothesis,trial_budget,split_manifest,config_hash,status,all_results |

大表存Parquet按数据类型/年分区；DuckDB用于元数据、查询和报告索引。不要一只资产一天一个小文件。数据修订生成新snapshot，不覆盖被实验引用的旧版本。

## 5. 价格与公司行动口径

### 5.1 下载不能靠默认参数

yfinance接口有自动复权、actions及结束日期排他等行为；下载器应显式指定参数并保存返回schema。[参数文档](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)。初始设计设 `auto_adjust=False, actions=True`，保留供应商原值、Adj Close与行动记录。

**`auto_adjust=False` 不证明OHLC就是历史真实未拆股价格。** 供应商可能已对拆股处理。必须识别价格basis，选真实拆股案例核验，必要时重建raw价格；若未能确定则不能用于真实股数账本。成交额、低价过滤和股票数量不允许从不明复权序列直接计算。

### 5.2 两种模式必须分开

| 模式 | 使用范围 | 算法约束 |
|---|---|---|
| `adjusted_total_return_proxy` | P0 Qlib探索 | 同一调整因子处理OHLC，收益含分红近似；不另加现金股利、不再调整持仓股数；允许分数单位；只收比例成本；输出注明是归一化资产单位 |
| `raw_with_corporate_actions` | LEAN与可信账本 | 验证后的raw成交/估值；拆股改变份额；除息确认应收、支付日变可用现金；真实股数/费用/结算模型 |

Qlib日频研究的总回报代理不自动等于真实成交模拟。默认不把其内部归一化份额导出为券商订单；**只导出目标权重**。其隐含的分红再投资、缺失支付日和成交近似在报告中列出。若要改Qlib处理raw行动，必须另写事件适配和对账测试后才可更改模式名称。

P0显式设置 `trade_unit=None`（分数研究单位），`open_cost=close_cost=bps/10000`、`min_cost=0`、`impact_cost=0`、`volume_threshold=None`。5/10/25bps是按每笔买卖名义金额计提的**总摩擦研究假设**，不额外叠加一份同义滑点或最低佣金。容量由外层截至决策日已知的ADV约束，不能通过引擎使用执行日全天成交量。实际版本参数在适配器实现时核对。[Qlib Exchange源码](https://github.com/microsoft/qlib/blob/main/qlib/backtest/exchange.py)。

换手也区分两个字段：报告标准风险资产单边换手 `0.5*sum(abs(delta_weight_risky_assets))`；组合渐进调仓软上限使用含现金的 `0.5*sum(abs(delta_weight_all_including_cash))`，初值20%。例如现金全部买成ETF，两者分别50%和100%。费用始终直接按实际买卖金额计算，不能拿含现金换手乘2估计费用。风险强制退出可超过软上限，必须记录豁免理由。

代理回测允许按下一调整后open理想化构建目标权重；这不代表开盘前可以预知开盘价并计算精确整数股。P2 raw需使用当时已知价格预算订单数量，明确资金缓冲、未成交和持仓偏离，重新判断收益。

跨引擎验算先在无公司行动的可控fixture核对，再对真实数据逐项解释股利现金时点、股数取整与费用差异；不能用扩大容差把系统性差异掩盖。候选策略只有在raw账本下仍有效才升级。

研究特征优先使用因果总回报比率；绝对价格/美元成交额/流动性使用相应时点真实或已核验的价格量。禁止用今天ETF持仓为十年前计算穿透盈利质量，也不把今天的基金规模套用到过去。

## 6. 因子第一版

令 `TR_t` 为统一口径的收盘总回报序列，`r_t=TR_t/TR_(t-1)-1`，`C_t`为可比收盘价格，所有窗口截至决策日已完成行情。

| 因子 | 定义 | 初始假设 / 角色 |
|---|---|---|
| mom_20/60/120 | `TR_t / TR_(t-k) - 1` | 趋势与相对强度 |
| mom_252_skip21 | `TR_(t-21) / TR_(t-252) - 1` | 长期动量，跳过近月；预热至少253价格点 |
| trend_200 | `TR_t / mean(TR[t-199:t]) - 1` | 趋势过滤 |
| vol_20/60 | `std(r, ddof=1)*sqrt(252)` | 风险与风险预算，不自动假设越低收益越高 |
| downside_vol_60 | `sqrt(mean(min(r,0)^2))*sqrt(252)` | 下行风险，明确不是只在负收益样本上std |
| drawdown_60 | `TR_t / max(TR[t-59:t]) - 1` | 趋势受损或反转假设需分别登记 |
| reversal_5 | `-(TR_t/TR_(t-5)-1)` | 独立反转假设，不能与动量混淆方向 |
| beta_126 | `cov(r_i,r_SPY)/var(r_SPY)` | 暴露解释、分母异常置缺失 |
| residual_mom_60 | 过去窗口对SPY或预定因子回归后的残差累计 | 回归仅用当时历史，稳定性优先 |
| range_20 | `mean((high-low)/close)` | 特征或流动性风险代理，复权口径一致 |
| volume_ratio_20 | 当日量 / 过去20日均量 | 拆股量口径一致；不称机构资金流 |
| dollar_adv_20 | 过去20完整日美元成交额均值 | 入池、成本与容量限制，不直接当alpha |

默认先登记不超过20个因子；多窗口变体也算独立尝试。少量ETF不适合从几千因子中筛“最好”的几个再沿用相同样本宣称有效。

Qlib Alpha158 是后续候选库，ETF日线不保证有真实VWAP等字段。缺字段就删因子并重命名缩减集，禁止 `VWAP=close` 冒充真实数据。资产数小的模型先用10–20个经济含义明确的特征，再比较扩充是否有样本外增益。

## 7. FactorSpec契约与挖掘流程

每个因子记录：ID、版本、表达式、经济假设、输入字段、最长窗口、可得延迟、预处理、缺失处理、预期方向、资产范围、负责人、父实验、代码hash。

1. 先登记假设与方向，例如趋势持续或短期回归；不能看到测试集IC后翻转方向。
2. 实现纯函数与数值fixture，检查常数序列、缺口、拆股、日期边界。
3. 做“未来扰动”验证：改变t之后数据，t及之前因子和信号应不变。
4. 只在训练窗口决定去极值、标准化、缺失填充和因子选择规则。截面变换使用同一日可得资产，不能使用未来才入池资产。
5. 验证覆盖率、RankIC、分组收益、换手、与已有因子相关性、时段稳定性。
6. 将因子加入简单等权/固定权重组合做消融；增量只在既定验证集判断。
7. 冻结新组合后进入未使用测试期；测试结果反馈后，该测试期即不再是未触碰留出集。

ETF截面只有18只且强相关，日度IC波动很大。不要把“资产×日期”视作完全独立样本；使用按交易日分组与时间块的稳定性分析。

## 8. 标签与训练时序

规则策略不需要监督标签。模型第一版研究5日和20日收益，但每天可输出信号；预测期限不自动等于固定持有期限。

定义决策日为交易序列t，进入时间是 `open[t+1]`，h交易日标签的退出为 `close[t+h]`（h=1即次日开盘至次日收盘）。收益考虑统一公司行动口径；标签在退出价格实际可得后成熟。

训练集必须满足 `label_available_at < validation_or_test_decision_start`；有区间重叠时按 `[entry_at,exit_at]` 做purge，不能只机械删固定五行。横截面同一天全部资产进入同一时间折。默认使用5年训练、1年验证、1年测试的滚动方案，数据不足要明确缩短方案/不训练，而非随机切分凑样本。

Qlib默认示例标签不能不看就沿用；本项目的预测目标和实际入场价格必须一致。若用超额收益标签，SPY也使用相同进入/退出区间；报告另外展示原始总收益与风险暴露。

## 9. 数据质量硬门

- 唯一键无重复；OHLC满足范围关系；价格为正，量非负；币种单位明确。
- 交易日缺口与非交易日区分；上市前/终止后不补价格；缺失开盘不成交。
- 不将异常收益一律剪掉，先检查拆股、分红、币种、修订与价格basis。
- 资产元数据、actions与价格版本相关联；无法解释的行动事件不得进入可信账本。
- 同一实验中的策略与基准使用相同起止区间、估值时间、成本/分红约定。
- 快照记录输入条数、日期覆盖、排除原因、下载参数、包版本及SHA-256。
- 所有更改数据的“repair”行为生成单独修复日志和新快照；原始文件永久保留。

## 10. 免费到付费的决策门

先回答策略是否优于相同风险的简单基线、是否在较高成本下仍有研究价值、结果是否集中于少数年份、数据缺口能否改变结论。只有缺口明确且候选值得继续时再采购：日线/退市与公司行动 → PIT财务/事件 → 借券 → 历史期权链。购买顺序跟实际策略需求走，不一次订阅所有数据。
