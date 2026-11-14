# ETF 主流程与粗筛检查 · 2026-11-05

本次以当前未提交工作区为基础增加交接与验证功能。生产粗筛权重、模型档位和定时任务定义未改。

## 使用

在 `E:\Trade\TradingAgents` 运行，`--date` 填实际已收盘的境内交易日：

```powershell
.venv/Scripts/python.exe run_pipeline.py --date 2026-11-05 --dry-run
.venv/Scripts/python.exe run_pipeline.py --date 2026-11-05
.venv/Scripts/python.exe run_pipeline.py --date 2026-11-05 --no-holdings
```

`--dry-run` 只显示流程。正式运行会抓数据并调用项目现有 LLM，产生调用费用。主流程为：

```text
news_aggregator/run_flow.py → 冻结新闻 JSON
  → run_research.py backfill
  → run_research.py cycle
      → 快照日期核验 → 粗筛冻结 → 历史/证据 → 回测/预测
      → 同批候选 TradingAgents → 结算 → 研究报告
  → daily_predict.py（持仓与固定代理；显式指定分析日和新闻文件）
  → 从本轮候选报告/持仓 JSON 生成挂单文本与综合简报
```

产物位于 `prediction_research/runs/pipelines/<日期_编号>/`，包含 `manifest.json`、各阶段日志、`news.json`、`daily/`、`order_plans/`、`briefing.json` 和 `briefing.md`。它不会向券商发单。订单价格缺失时保留缺失，不生成价格。

续跑使用原目录与相同参数：

```powershell
.venv/Scripts/python.exe run_pipeline.py --date 2026-11-05 --resume prediction_research/runs/pipelines/2026-11-05_实际编号
```

阶段成功后记录输入/产物哈希；已成功阶段不会再次执行。输入参数或产物被改动时拒绝续跑。`cycle` 重试沿用原有逐标的断点能力，并在绑定模式下检查规则、行情快照和新闻证据是否一致。进程互斥锁随进程退出自动释放。强制终止后 manifest 可能仍显示 running；续跑会重试未完成阶段。

独立 `run-agents` 也拒绝超过配置 `max_quote_age_days`（当前 4 个自然日）的候选。主流程额外要求行情日期精确等于 `--date`。未内置交易所节假日日历，因此显式日期是必填项。

`morning_briefing.py` 原脚本包含额外分析；主流程生成自己的同批综合简报。QDII 专项旧 runner 仍独立，因为其溢价估算及境内外时点没有统一到本轮数据合同。旧定时任务仍需改为调用统一入口，不能同时保留重复的整链触发。`.workbuddy/memory/automations/*/memory.md` 是运行记忆，不是调度定义；本次没有把修改记忆文件当成更新调度。

## 本次确认的断点

1. 最新 `cycle_20261031_002839_077107.json` 尚为 running；2026-11-05 凌晨的 agent batch 使用 2026-10-30 的候选，9 项均为复用。这不是 11 月 5 日新预测。
2. `cycle` 已有完整研究主干，但原 adapter 用全局 latest screen；现由 cycle 把本轮冻结路径传给 adapter。
3. `daily_predict` 原本取系统当天作为 analysis_date，跨午夜补跑依赖临时 monkeypatch；现有正式 `--analysis-date` 参数。
4. 原 cycle 的历史 agent 结算数可以使当前空批次看起来有 LLM 覆盖。现只认本轮产出/本轮合法复用，空批次或部分失败明确标为 partial。
5. 原挂单工具只读 daily 目录。新主入口也读取本轮 TradingAgents 的 `3_trading/trader.md`，不拿别的日期的 latest 文件补位。
6. 独立新闻报告原本过期只加提示仍注入；现报告超过 18 小时、时间未知或未来时间不再作为有效上下文。
7. 财联社流原本只保留持仓映射结果及少量全局新闻。现保留去重后的事件流，并给动态候选追加明确标注的名称/主题证据。主题字符串匹配不是经过核实的成分股权重映射；缺覆盖不会写成消息中性。
8. 同一成分股属于多只 ETF 时，原代码匹配第一只后 break；现保留所有匹配，并继续使用既有共享事件标识。

## 粗筛诊断与结果

复现入口：

```powershell
.venv/Scripts/python.exe run_research.py screen-review --top 10 --output outputs/screen_review_20261105.json
```

数据库以只读方式打开，不改状态库、不拟合权重、不调用 LLM。四套固定规则在相同的历史可用候选池与相同分组上对照；特征只取截至信号日的 K 线，下一次可交易开盘必须晚于快照可得时间。假设往返总摩擦 30bp。

本次原始结果保存在 `C:\Users\Administrator\etf_review_20261105\screen_review.json`。21 个重建观察日中，5 日持有期有 14 个四套方案都完整的配对批次：

| 固定方案 | 平均每批 5 日扣费收益 |
|---|---:|
| 当前粗筛规则 | -1.79% |
| 单纯流动性排序 | -4.31% |
| 20 日动量 | -1.31% |
| 中期趋势与波动约束 | -0.64% |

趋势组合为预先固定的假设：20 日动量 30%、60 日动量 25%、60 日均线趋势 20%、低波动 15%、流动性 10%，在同资产类型/市场范围内做百分位。未根据结果搜索最优参数，**也未替换生产评分**。

限制：这是历史探索性、重叠批次诊断，不是连续组合净值，不报告年化或 Sharpe。只比较有足够历史的共同子池，不代表全市场原生产表现；不是独立未触碰留出。20 日持有期完整配对为 0，无法比较。历史价格修订、分红复权、缺失与存活偏差仍须核验。>22% 的历史单日断层只作为未解决数据问题排除，不猜测复权。14 个批次不能视为 14 个独立样本。因此 `promotion_allowed=false`。

已有 `doribt_momentum_20d_backtest_20261023.json` 记录：9 只固定国内 ETF，2022-09-01 至 2026-10-12，动量轮动年化 11.37%，上证 50 基准 1.30%。它支持继续研究中期动量，不足以证明全市场有效。原实现对公司行动使用价格断层推断，且缺同池等权/风险匹配和真正未触碰留出，不能把该收益直接搬进生产粗筛。

## 更合理的下一版筛选

建议按以下顺序形成可消融的研究方案，而不是一次增加许多指标：

1. 可交易性：历史长度、报价时效、近 20 日成交额、价差、规模、停牌、已核验复权、QDII 溢价与净值时点。缺数据为 unknown，不等于风险低。
2. 按跟踪指数/行业敞口形成候选组，先选敞口，再选 ETF 载体。现在的名字分组会把沪深 300 的不同简称当成不同组，也可能把不同周期行业合并过粗。应引入真实指数 ID、可得时点成分和收益相关性。
3. 研究市场状态：趋势、波动、市场宽度；比较允许现金/不选的规则与固定凑满 top10。不要用当天红盘多少推导固定次日上涨概率。
4. 分开验证趋势延续、趋势内回撤、事件驱动三个候选队列。资金流作为确认或风险信息，和量价相关的多个变换不能算多份独立证据。
5. 分别评估 5/20 日净超额收益、下行损失、Rank IC、分位组单调性、换手和连续净值。概率校准与横截面排序是两个任务，不能用 Brier 合格替代选股能力。
6. 对 news、量价、LLM 分别消融：量价基线 → 加新闻 → 加 LLM。LLM 用于核查事件和解释冲突，规则从 Hold/Underweight 自动升级为 Overweight 的效果应独立验证。

当前代码已有 `selection_validation`，所以 10 月初报告中的“完全没有排序闸门”不再是当前事实。本次未重复增加同类闸门。

## 新闻流应如何继续升级

- 保存 `published_at / first_seen_at / revised_at / source / event_id / available_at`；事件更新时间不能回填成首次可得时间。
- 事件结构包括主体、事件类型、确定性、相对预期的变化、影响方向、持续期和 ETF 暴露权重。大宗价格上涨对上游生产商与下游使用者可能方向相反。
- 跨媒体去重；同一事件命中多只 ETF 不是多条独立利好。
- 财联社现有 `sentiment_src=market` 来自关联个股实时涨跌。新交接显式区分它与文本证据，不把它作为独立新闻 alpha。
- 5 日和 20 日分别设置事件衰减、验证新增收益；当前没有完整新闻历史，不能声称完成新闻因子回测。
- 13F 是季度持仓披露，常规申报时限为季度结束后 45 天，适合作为慢变量背景；不能当成今天机构买卖信号。[SEC 说明](https://www.sec.gov/rules-regulations/staff-guidance/frequently-asked-questions-about-form-13f)

## 可借鉴的软件

| 框架 | 与当前系统的对应价值 |
|---|---|
| [Microsoft Qlib](https://github.com/microsoft/qlib) | 数据、因子、训练、实验记录、组合评估的完整研究链；适合借鉴粗筛验证和数据合同 |
| [VeighNa / vn.py](https://github.com/vnpy/vnpy) | 本土行情/交易适配、策略、回测、组合；[官方 Alpha 教程](https://www.vnpy.com/forum/topic/34625-veighnaduo-yin-zi-ru-men-xi-lie-1-mo-kuai-zong-lan-yu-zhun-bei) 展示从数据到因子与组合回测的链路 |
| [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | 多智能体研究编排、确定性分析算子和报告追溯，和当前 LLM 研究层较接近 |
| [Microsoft RD-Agent](https://github.com/microsoft/RD-Agent) | 因子/模型研究自动化；必须配独立留出与实验登记，避免自动搜索加重过拟合 |

建议先复用它们的数据时点、实验记录和验证设计。无需为本次改动迁移框架或安装新的量化依赖。
