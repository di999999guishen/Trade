# 跨市场环境稳健性检验报告

**日期**：2026-10-06
**问题**：把模型放到非单边上涨的窗口里，它到底有没有价值？四条建议固化后结论是否改变？
**数据源**：`datasets/market_full`（新浪/腾讯全历史，21 只全拉取成功，与原 `datasets/market` 并存、未覆盖）
**摩擦档**：D_full = 10bps 往返手续费 + 10bps 单边滑点 + 涨跌停不可成交

---

## 一、结论先行

**模型确实有用途，但只在"买了不动很惨"的窗口里。** 三种环境给出三个不同答案，不存在一个 universal 的最优用法。

| 窗口 | 环境 | 买入持有 | 最优用法 | 策略收益 | **几何超额** |
|---|---|---:|---|---:|---:|
| cached 2010-11~2012-11 | 长熊 | **−23.53%** | 择时 thr0.55 | −6.56% | **+22.19%** |
| cached 2023-05~2025-06 | 横盘 | +2.22% | 选股 top3 | +22.85% | **+20.18%** |
| commodity 2013-05~2015-06 | 商品熊 | −12.93% | 择时 thr0.55 | −10.27% | +3.07% |
| commodity 2016-11~2018-12 | 商品横盘 | −0.86% | 择时 thr0.50 | −4.51% | −3.69% |
| commodity 2022-07~2026-09 | 商品牛 | **+76.97%** | 择时 thr0.50 | +64.55% | **−7.02%** |

买入持有越差，模型超额越高；**牛市里一律跑输。** 昨天「改择时也跑输买入持有 86%」的结论，是因为那个池在那段窗口是黄金单边牛——环境偏袒，不是模型无用。

---

## 二、四条建议已全部固化

### 1. 强制配买入持有基准，用超额收益决策

`evaluation.py` 新增 `excess_metrics(strategy, benchmark)`：

- **漏传基准直接抛 `ValueError`**——不是给出没有参照的漂亮数字
- 超额用几何口径 `(1+s)/(1+b)−1`，不做百分比简单相减
- 同时输出 `cagr_excess` / `sharpe_excess` / `drawdown_delta` / `beats_benchmark`

`constrained_backtest.py` 的输出已改为全部以 `geometric_excess` 排序，绝对收益降为附属列。

### 2. 用 IC 而非 Brier 判断能不能选股

新增 `ICIR_GATE = 0.30` 与 `signal_verdict(selection)`，返回 `usages` 字段显式列出允许/禁止的用法：

```python
{"stock_picking": "禁用：ICIR 未过闸", "market_timing": "允许：概率均值可用于判断整体方向"}
```

回测脚本开头即打印闸门结果，**先把用法锁死再跑策略**。

### 3. 滑点优先于佣金

新增 `DEFAULT_SLIPPAGE_BPS = 10.0`，回测**默认计入**。实测又一次验证优先级正确（长熊窗口 择时 thr0.55）：

| 档位 | 超额 | 损耗 |
|---|---:|---:|
| A 理想 | 25.04% | — |
| B 仅手续费 | 24.08% | −0.96pp |
| C +滑点 | 22.19% | **−1.89pp** |

滑点杀伤是手续费的约两倍。要关必须显式 `--no-slippage`，且输出标注"不可用于决策"。

### 4. 定位改为市场方向择时

`constrained_backtest.py` 现在**默认同时跑择时与选股两种模式**，输出 `recommended_usage` 字段按闸门结果自动判定。

---

## 三、过程中发现的三个新问题

### 3.1 旧 cache 撑不起下行窗口（根因）

第一次跑 cached 熊市窗口得到 `predictions=0 folds=0`，策略全程空仓——**在下跌市里空仓必然"跑赢"基准**，输出的是一张完全误导的漂亮表格。

根因：原 `datasets/market` 里 cached 池 10 只有 8 只数据从 2022-09 才开始，2021 年只有 2 只；窗口内 feature_date 数 251 < `model.min_train_dates = 252`，**差一天**导致 `walk_forward` 的 `range(min_dates, len(dates), window)` 直接为空。

已加 **FATAL 守卫**：无预测时中止并打印原因与处理建议，杜绝这类静默假象。

解法：`etf_data_source.refresh_pool()` 把全池拉到新目录 `datasets/market_full`，21/21 成功，深度从约 1023 行扩到最多 5253 行（510050 至 2005-02）。**原 cache 未改动**，通过 `--data-dir` 切换。

### 3.2 选型闸门曾经形同虚设（隐蔽 bug）

`selection_metrics._summarize` 输出键名是 `icir`，而 `signal_verdict` 读的是 `icir_naive`——**字段名漂移导致闸门恒定返回 None，无论信号多好都判"禁用"**。

已修：两处输出双键 + `signal_verdict` 做 fallback 兼容。修复后闸门才真正开始工作（首例输出 ICIR=0.2729）。

### 3.3 回测超额 ≠ 选股能力（最重要的张力）

| 窗口 | IC 均值 | ICIR | 能否选股 | 选股 top3 超额 |
|---|---:|---:|---|---:|
| cached 长熊 | **−0.6836** | — | 禁用 | **+9.66%** |
| cached 横盘 | +0.0948 | 0.2729 | 禁用 | **+20.18%** |
| commodity 牛 | +0.0714 | 0.1308 | 禁用 | −37.44% |

**选股策略在横盘窗口做出 +20.18% 超额，但 IC 只有 0.0948、ICIR 0.2729 未过闸；长熊窗口 IC 甚至是 −0.68（排序反向），回测却 +9.66%。**

这两件事同时成立只有一个解释：**策略赚的不是"排序"的钱，是"仓位开关"的钱。** 若据此以为模型有选股 alpha，是把运气当能力——这正是第 2 条建议存在的理由。

---

## 四、必须知道的证据强度限制

1. **长熊窗口只有 3/10 标的参与**（159915/512170/510050），其余 7 只在 2010-2012 时尚未上市。该窗口的 +22.19% 证据强度弱于其他窗口。
2. **ICIR 是 naive 口径**。h20 的标签跨 20 个交易日、彼此重叠，naive 标准误被低估，真实显著性更低。0.2729 距离 0.3 门槛看着近，按重叠调整后大概率更远。
3. **单窗口结论不可外推**。横盘窗口只有 249 个横截面日、13 个 walk-forward fold，选股 +20% 可能来自 top3 的集中度而非排序能力。
4. 本次全程未触碰任何 LLM Agent 链路。TradingAgents 的主模型是否有 alpha，**问题仍然没有答案**（沿用昨天的结论）。

---

## 五、现在的实际判决

- **可以用**：在市场明显弱势/高波动时按池子平均概率做**仓位开关**（长熊 +22.19%、商品熊 +3.07% 两个窗口一致为最优）
- **不可以用**：把它当**选股器**——五个窗口的 ICIR 全不过 0.3，其中两个窗口的回测超额与 IC 方向相反
- **牛市请什么都不做**：商品牛窗口所有用法超额均为负（−7.02% ~ −37.44%）

一句话：**这个模型的价值是"该不该在场"，不是"买哪一只"。**

---

## 六、代码改动清单

| 文件 | 改动 |
|---|---|
| `prediction_research/evaluation.py` | 新增 `ICIR_GATE` / `IC_MEAN_GATE` / `DEFAULT_SLIPPAGE_BPS` / `excess_metrics` / `signal_verdict` / `decision_report`；修 `_summarize` 键名漂移。`selection_metrics` 与 `metrics` 未改动，现有调用不受影响 |
| `quant_stack_probe/constrained_backtest.py` | 加 `--window` 预设 / `--start` `--end` / `--data-dir` / `--no-slippage`；无信号 FATAL 守卫；默认双模式（选股+择时）；输出以几何超额排序、附 `recommended_usage` |
| `quant_stack_probe/window_scan.py` | 新增。扫描等权净值定位真实下行/震荡窗口，支持 `--data-dir` |
| `quant_stack_probe/summarize_windows.py` | 新增。多窗口汇总成市场环境矩阵 |
| `quant_stack_probe/ic_analysis.py` | 加 `--start/--end/--data-dir`，接入 `signal_verdict` |
| `prediction_research/datasets/market_full/` | 新增。21 只全历史缓存，与原 cache 并存 |

## 七、复现命令

```bash
# 1. 定位窗口
python window_scan.py --data-dir datasets/market_full --out window_scan_full.json

# 2. 跑窗口回测（自带买入持有基准 + IC 闸门 + 默认滑点）
python constrained_backtest.py --universe cached --horizon 20 --top-k 3 \
       --window cached_down_500 --data-dir datasets/market_full --out cbt.json

# 3. 汇总矩阵
python summarize_windows.py cbt_*.json
```

## 八、下一步建议

1. **把 naive ICIR 换成 Newey-West 或按 horizon 去重叠**（用 `horizon` 长度做 block），否则 0.2729 这种边缘值无法真正判断。
2. **长熊窗口补标的**：找一个更多标的共存的弱势窗口（2018 全年 A股单边下跌，但需确认 ETF 上市覆盖）。
3. **把择时仓位做成连续而非 0/1**：目前 `avg_prob > threshold` 二元开关，阈值 0.5 与 0.55 结果差幅很大（长熊窗口 18.80% vs 22.19%），说明阈值敏感度高，需要按概率缩放仓位。
4. **LLM 预测落库**（沿用昨日建议）：本轮所有结论都只对量价模型成立。
