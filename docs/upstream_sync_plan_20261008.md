# 上游同步方案（v0.4.x → v0.6.0）

> 生成时间：2026-10-08 06:55
> 触发：用户要求「把目前最新提交拉取和我当前的代码文件合并」
> 状态：**仅取证与方案，未执行合并**（详见 §7 结论）
> 取证手段：`git merge-tree --write-tree --name-only`（内存合并，全程零触碰工作区）
> 备份点：见 §8.1

---

## 1. 结论先行

| 项 | 结论 |
|---|---|
| 能不能「拉一下就好了」 | **不能。** 这不是快进或小冲突，是**跨 6 个版本的引擎重写移植** |
| 规模 | 上游领先 **225** 个提交，本地领先 **255** 个；共同祖先 `a33fd4c`（2026-07-18） |
| 真实冲突 | **45 个**：content 32 + add/add 7 + modify/delete 6 |
| 上游版本 | `upstream/main` HEAD = `1394a3f`（2026-10-03），即 **v0.6.0** |
| `origin` 有无新提交 | **无**（本地领先 origin/main 2 个提交）。所谓「最新提交」只能指上游 |
| 风险面 | 冲突 **100% 集中在 `tradingagents/` 引擎包**；自有编排层 **零冲突**（见 §5） |
| 九点前能否安全完成 | **不能。** 硬赶会危及今晚 18:00/19:00/20:00 三条链 |

---

## 2. 分叉事实

```
                    a33fd4c (2026-07-18, 共同祖先)
                        |
        +---------------+---------------+
        |                               |
   [本地 main e12fbc7]            [upstream/main 1394a3f]
   255 个自有提交                  225 个上游提交
   (首条 2026-08-12)               (v0.4.1 → v0.4.2 → v0.5.0
                                     → v0.5.1 → v0.5.2 → v0.6.0)
```

| 维度 | 本地 | 上游 |
|---|---|---|
| `tradingagents/` 引擎改动 | 31 文件，+1871 / −189 | **95 文件，+5569 / −3393**（近乎重写） |
| `tests/` 文件数 | 64 | 86 |
| 自有编排层（`prediction_research/` 等） | **177 文件，+22303 行** | **0 文件** |

`origin` = `git@github.com:di999999guishen/Trade.git`（公开），无新提交。
`upstream` = `https://github.com/TauricResearch/TradingAgents.git`。

---

## 3. 依赖断层（这是最容易被低估的一条）

上游 v0.6.0 把依赖整体抬了一个大版本，**其中两个是破坏性升级**：

| 依赖 | 本地 | 上游 v0.6.0 | 风险 |
|---|---|---|---|
| `requires-python` | `>=3.10` | **`>=3.11`** | 低 |
| `langchain-core` | `>=0.3.81` | **`>=1.6.5`** | **高**（0.x → 1.x 破坏性） |
| `langgraph` | `>=0.4.8` | **`>=1.2.12`** | **高**（checkpoint API 重写） |
| `langgraph-checkpoint-sqlite` | `>=2.0.0` | **`>=3.1.1`** | 高 |
| `pandas` | `>=2.3.0` | **`>=3.0.6`** | **高**（3.0 默认 Copy-on-Write，索引/赋值语义变化） |
| `yfinance` | `>=1.4.1` | `>=1.7.0` | 中（本地已停用 yfinance，见 MEMORY §4） |
| `langchain-anthropic` | `>=0.3.15` | `>=1.7.4` | 高 |
| `langchain-openai` | `>=0.3.23` | `>=1.6.6` | 高 |
| `langchain-google-genai` | `>=4.0.0` | `>=4.4.0` | 低 |

> **含义**：全量合并 = 同时升 langchain 0.3→1.6 + langgraph 0.4→1.2 + pandas 2→3。
> 本地 `tradingagents/` 与 `prediction_research/` 都大量直接使用 pandas，
> pandas 3.0 的 CoW 变更可能让**编排层的 22303 行自有代码**出现静默行为漂移（不报错、但数值变化）。
> **这是本方案里唯一一个「不报错但会让结果不可信」的风险点**，必须单独立项验证。

---

## 4. 45 个冲突逐个定性

判据：`git diff --numstat <base>..<side> -- <file>`，格式 `新增/删除`。
「LOCAL (same)」= 本地未触碰该路径（可直接取上游）。

### 4.1 可直接取上游（本地未改或仅是路径重命名配对）—— 5 个

| 文件 | LOCAL | UPSTREAM | 处理 |
|---|---|---|---|
| `tradingagents/agents/context.py` | (same) | 218/0 | 取上游 |
| `tradingagents/dataflows/router.py` | (same) | 297/0 | 取上游 |
| `tradingagents/dataflows/vendors/fred.py` | (same) | 286/0 | 取上游（本地 `dataflows/fred.py` 的改名配对） |
| `tradingagents/dataflows/vendors/stocktwits.py` | (same) | 162/0 | 取上游（同上） |
| `tradingagents/dataflows/date_window.py` | 30/0 | 170/0 | 取上游（add/add，上游实现更完整） |

### 4.2 🔴 高危：上游删除 / 本地仍在用 —— 6 个

**「照单全收上游版 = 本地 pipeline 当场 ImportError」。这不是合并，是需要业务决策的移植。**

| 本地被删模块 | 本地改动 | 上游替代位置 | 本地引用点（全部） |
|---|---|---|---|
| `agents/utils/memory.py` | 45/10 | `tradingagents/memory/`（新包：`__init__.py`/`log.py`/`reflection.py`/`settlement.py`） | `graph/trading_graph.py:33`；`tests/test_memory_log.py:10,848,853`；`tests/test_memory_pointintime.py:13`；**且 `agents/utils/memory.py:6` 自身 import rating** |
| `agents/utils/rating.py` | 48/13 | `tradingagents/agents/rating.py` | `graph/signal_processing.py:17`；`agents/utils/memory.py:6`；`tests/test_signal_processing.py:13` |
| `dataflows/reddit.py` | 72/9 | `dataflows/vendors/reddit.py` | `agents/analysts/sentiment_analyst.py:43` |
| `dataflows/stockstats_utils.py` | 61/6 | `dataflows/vendors/yahoo/ohlcv.py`（+ `market.py`/`snapshot.py`） | `dataflows/market_data_validator.py:18`；`tests/test_date_boundaries.py:10`；`tests/test_ohlcv_cache_freshness.py:17`；`tests/test_yfinance_stale_ohlcv_guard.py:20` |
| `dataflows/yfinance_news.py` | 4/28 | `dataflows/vendors/yahoo/news.py` | `tests/test_news_lookahead.py:13`；`tests/test_symbol_normalization_paths.py:11` |
| `graph/signal_processing.py` | 14/7 | 无对等文件；评分解析收进 `agents/rating.py` | `scripts/smoke_structured_output.py:29`；`tests/test_signal_processing.py:20` |

⚠️ 注意 `dataflows/stockstats_utils.py` 本地只改了 61/6 行，但其中含**自有保护逻辑**
（`_assert_ohlcv_not_stale` 陈旧数据护栏、`load_ohlcv` 日期列解析）。
上游的 `vendors/yahoo/ohlcv.py` 是**另一套实现**，不能假设它带了同样的护栏 —— **必须逐条比对后再决定是「移植上游」还是「保留本地护栏 + 适配调用方」。**

### 4.3 🟠 本地重度改造，必须保 `--ours` 后手工 rebase —— 2 个

| 文件 | LOCAL | UPSTREAM | 说明 |
|---|---|---|---|
| `main.py` | **93/13** | 1/4 | ⭐ **上游 `main.py` 是 16 行 demo 脚本**（`ta.propagate("NVDA", ...)`），无 argparse。我们的 `main.py` 是**整份本地自建的 CLI**（`ticker` / `--date` / `--reports-dir` / `--quiet` / `--screen-evidence`），是 `prediction_research/tradingagents_adapter.py:190` 的**唯一入口契约**。→ **必须保 ours**，只吸收上游那 1 行改动 |
| `agents/schemas.py` | **205/14** | 61/23 | 本地 205 行含 MEMORY §2 的**价格块 schema**（`current_price`/`support_levels`/`resistance_levels`/`entry_zone`/`stop_loss`/`invalidation_price`/`reduce_trigger`/`scale_out_ladder`）。→ **保 ours + 合并上游新增字段** |

### 4.4 上游重度改造，取上游后重贴本地小补丁 —— 6 个

| 文件 | LOCAL | UPSTREAM | 处理 |
|---|---|---|---|
| `cli/main.py` | 118/97 | **102/1259** | 取上游（TUI 重写），本地 118 行改动需重贴 |
| `tradingagents/graph/trading_graph.py` | 185/57 | **316/369** | 双方都大改 → 逐块人工合并，**最耗时的一个** |
| `tradingagents/default_config.py` | 14/8 | **139/94** | 取上游 + 重贴本地 14 行配置 |
| `tradingagents/llm_clients/model_catalog.py` | 9/9 | 79/32 | 逐块合并（本地新模型条目须保留） |
| `tradingagents/agents/analysts/sentiment_analyst.py` | 27/2 | 51/62 | 取上游 + 重贴（且 reddit import 路径要改） |
| `tradingagents/agents/managers/portfolio_manager.py` | 21/1 | 32/21 | 取上游 + 重贴 |

### 4.5 测试文件 —— 15 个（含 6 个 add/add）

`tests/test_capabilities.py`、`test_checkpoint_lifecycle.py`、`test_dataflows_config.py`、
`test_debate_opening.py`、`test_env_overrides.py`、`test_fred.py`、`test_llm_max_tokens.py`、
`test_memory_log.py`、`test_memory_pointintime.py`、`test_news_lookahead.py`、`test_ohlcv_latest_bar.py`、
`test_reddit_fallback.py`、`test_signal_processing.py`、`test_social_lookahead.py`、`test_symbol_normalization_paths.py`

**统一策略：取上游。** 理由：这些测试测的是上游引擎模块，上游 v0.6.0 的测试是配套新结构的，保留本地旧版会大面积失败。本地自有测试在 `prediction_research/tests/`（6 个）和根 `tests/` 的独立文件里，**不在这批冲突中**，天然安全。

### 4.6 杂项 —— 11 个

`.env.example`(2/0 vs 39/29)、`README.md`(15/3 vs 140/25)、`pyproject.toml`(5/3 vs 22/23)、
`agents/analysts/news_analyst.py`(1/1 vs 15/21)、`agents/managers/research_manager.py`(10/1 vs 11/7)、
`agents/researchers/{bear,bull}_researcher.py`(各 4/1 vs 10/6)、
`agents/risk_mgmt/{aggressive,conservative,neutral}_debator.py`(各 7/2 vs 16/7)、
`agents/trader/trader.py`(39/7 vs 46/15)

**策略**：`.env.example` / `README.md` / `pyproject.toml` 以**上游为基底**，把本地条目（自有环境变量、自有依赖）重新追加。其余 7 个 agent 文件是**小补丁（个位数行）**，取上游 + 重贴。

---

## 5. 好消息：自有编排层零冲突

| 路径 | 本地文件数 | 上游文件数 | 冲突 |
|---|---|---|---|
| `prediction_research/` | 66 | 0 | **0** |
| `quant_research/` | 104 | 0 | **0** |
| `news_aggregator/` | 5 | 0 | **0** |
| `run_research.py` / `daily_predict.py` | 各 1 | 0 | **0** |

上游**根本没有这些路径** → 逐路径合并时天然无冲突。
**22303 行自有代码不受本次合并影响**（除 §3 的 pandas 3.0 行为漂移风险）。

唯一接线点是 `prediction_research/tradingagents_adapter.py:190`，
启动子进程 `python main.py <ticker> --date <trade_date> --reports-dir <root> --quiet`。
**只要 `main.py` 的 CLI 契约不变，编排层与引擎层就是松耦合的。**

---

## 6. 不可逆点清单

| # | 动作 | 是否可逆 | 说明 |
|---|---|---|---|
| 1 | 建分支 / 打补丁 | ✅ 可逆 | 已做，见 §8.1 |
| 2 | 在隔离分支上 `git merge upstream/main` | ✅ 可逆 | `git merge --abort` 或删分支 |
| 3 | 解决冲突（写文件） | ✅ 可逆 | 分支隔离，未动 main |
| 4 | **升依赖（langchain 1.x / langgraph 1.x / pandas 3.0）** | ⚠️ **半可逆** | 升完会污染当前 venv；回退需重装 |
| 5 | **把合并结果落到 `main`** | ⚠️ 半可逆 | 影响今晚流水线 |
| 6 | `git push` 到 `origin`（公开仓库） | ❌ 实质不可逆 | 推送前必须过 MEMORY §7 自检 4 条 |

**绝对红线：第 4、5 步不要在今天（10-08）做。**

---

## 7. 三条路径与推荐

### 路径 A：隔离分支完整合并（推荐，但需要整块时间）
在 `upstream-sync-v0.6.0` 上完整合并 v0.6.0，逐冲突解决 + 分批测试，全绿后合回 `main`。
- 优点：一次到位，拿到上游全部修复与新能力（backtest/portfolio/reporting/memory 包）。
- 代价：45 冲突 + 依赖大升级 + pandas 3.0 行为验证，**预计需要一整段不被打断的工作时间**。
- 风险：依赖升级一旦出问题，回退成本高。

### 路径 B：只 cherry-pick 真正需要的上游修复
不动包结构、不升依赖，只把上游 v0.6.0 里的**具体 bug fix** 挑过来。
- 优点：风险可控，当天可完成。
- 代价：拿不到 v0.6.0 的结构收益；上游后续改动会持续累积。
- 前置：需要先**列清单**——上游这 225 个提交里，哪些修的是我们真实遇到的问题。**这份清单目前还没有，需要单独做一轮筛选。**

### 路径 C：只固化本地，择期合并
把当前 18 个未提交改动 + 未跟踪源码提交固化，备份补齐，合并另行安排。
- 优点：零风险，且**这是路径 A/B 的共同前置步骤**。
- 代价：上游继续落后。

> **推荐：先做路径 C（今天，安全），再走路径 A（择期，用 Claude Code 执行）。**
> 若用户想「今天就看到东西」，走路径 B 但需要先花时间筛提交清单。

---

## 8. 路径 A 执行步骤（供后续派给 Claude Code）

### 8.1 已完成的前置（本次已落盘）

| 项 | 位置 | 状态 |
|---|---|---|
| 备份分支 | `backup/pre-upstream-merge-20261008` | ✅ 已建 |
| 隔离分支 | `upstream-sync-v0.6.0` | ✅ 已建（指向当前 main HEAD，**尚未合并**） |
| 未提交改动补丁 | `E:\Trade\_backup\uncommitted_before_upstream_merge_20261008.patch`（85990 B） | ✅ 已存 |
| 工作区状态快照 | `E:\Trade\_backup\status_before_upstream_merge_20261008.txt` | ✅ 已存 |
| ⚠️ **缺口** | 上述补丁**不含未跟踪文件**（`outputs/*.md`、`prediction_research/validation/`、`docs/daily_mandatory_predictions.md` 等） | 见 §8.2 |

### 8.2 后续步骤

```
# 0. 补齐未跟踪文件的备份（tar 归档，排除 logs/ 与探针产物）
cd /e/Trade/TradingAgents
tar czf /e/Trade/_backup/untracked_20261008.tgz \
    outputs docs/daily_mandatory_predictions.md prediction_research/validation \
    prediction_research/etf_data_source.py prediction_research/tests/test_selection_metrics.py \
    tests/test_daily_predict.py tests/test_daily_prediction_markets.py \
    tradingagents/dataflows/daily_prediction_markets.py

# 1. 固化本地工作（见 §9 的入库/不入库清单）
git add <...>  &&  git commit -m "chore: freeze local work before upstream v0.6.0 sync"

# 2. 在隔离分支上合并（不动 main）
git switch upstream-sync-v0.6.0
git merge upstream/main            # 预期 45 个冲突

# 3. 按 §4 的分类逐批解决
#    批次 1（最易，5 个）：4.1 直接取上游
#    批次 2（15 个）：4.5 测试文件全取上游
#    批次 3（11 个）：4.6 杂项，取上游 + 重贴本地条目
#    批次 4（6 个）：4.4 上游重度改造，取上游 + 重贴
#    批次 5（2 个）：4.3 main.py / schemas.py 保 ours 再吸收上游
#    批次 6（6 个）：4.2 高危 modify/delete —— 逐条做 import 路径迁移
#                    v0.6.0 新路径已核实存在：
#                      agents/rating.py                      ✅
#                      memory/{__init__,log,reflection,settlement}.py ✅
#                      dataflows/vendors/reddit.py           ✅
#                      dataflows/vendors/yahoo/{ohlcv,news,...}.py    ✅
#                      graph/{analyst_execution,checkpointer,conditional_logic,propagation,setup}.py ✅

# 4. 依赖升级（隔离环境做，不要污染生产 venv）
#    注意 pandas 3.0 的 Copy-on-Write 语义变化

# 5. 分批测试（本机 pytest 一次跑整个 tests/ 会 SIGTERM —— 必须按文件分批）
#    5a. 先跑上游新测试，确认引擎层本身健康
#    5b. 再跑本地自有测试 prediction_research/tests/（6 个文件）
#    5c. 最后跑端到端：run_research.py cycle --top 1 冒烟

# 6. 全绿后才合并回 main（并避开 18:00-20:00 流水线窗口）
git switch main && git merge --no-ff upstream-sync-v0.6.0
```

### 8.3 回滚点

| 回滚到 | 命令 |
|---|---|
| 本次合并前的 main | `git switch main && git reset --hard backup/pre-upstream-merge-20261008` |
| 丢掉隔离分支 | `git branch -D upstream-sync-v0.6.0` |
| 恢复未提交改动 | `git apply E:/Trade/_backup/uncommitted_before_upstream_merge_20261008.patch` |

---

## 9. 路径 C 的入库 / 不入库清单

### 应入库
**已跟踪、已修改（18 个）**：
```
daily_artifacts.py
daily_predict.py
prediction_research/adapters/eastmoney.py
prediction_research/config/research.json
prediction_research/cycle.py
prediction_research/cycle_reporting.py
prediction_research/docs/etf_research_pipeline.md
prediction_research/evaluation.py
prediction_research/pipeline.py
prediction_research/store.py
prediction_research/tests/test_cycle_remediation.py
prediction_research/tests/test_rating_mapping.py
prediction_research/tests/test_remediation_integration.py
prediction_research/tradingagents_adapter.py
prediction_research/workflow.py
tradingagents/dataflows/akshare_data.py
tradingagents/dataflows/polymarket.py
tradingagents/dataflows/stockstats_utils.py
```

**未跟踪、属源码/文档**：
```
docs/daily_mandatory_predictions.md
outputs/*.md                                  (15 份回填/评估报告，手工撰写、不可再生)
prediction_research/etf_data_source.py
prediction_research/validation/               (6 个 .py，含 constrained_backtest.py)
prediction_research/tests/test_selection_metrics.py
tests/test_daily_predict.py
tests/test_daily_prediction_markets.py
tradingagents/dataflows/daily_prediction_markets.py
```

### 不入库
```
logs/                                          (1.2M 运行日志 + _probe*.txt 临时文件)
prediction_research/runs_mf_probe_market/      (1.8M 探针 JSON)
prediction_research/runs_mf_probe_market_full/ (2.8M 探针 JSON)
```

### 入库前安全自检（MEMORY §7）
- [x] 扫 IPv4 与「成本/持仓/浮亏/份额」→ 命中项全为代码语义（交易成本、持仓天数、滑点），**无真实数据**
- [x] 扫密钥 `sk-*` → 无
- [x] 无个人财务文件
- 注：`outputs/eastmoney_connectivity_report_20261005.md:11` 含 `http://127.0.0.1:11625` —— 环回地址，非出口 IP，可入库

---

## 10. 对今晚流水线的保护

**本次操作全程未触碰 `main` 工作区**，`main` HEAD 仍是 `e12fbc7`。
今晚 18:00 / 19:00 / 20:00 三条链按原计划执行，不受影响。

⚠️ **合并一律避开 18:00–20:00 窗口，且避开 20:00 之后 run-agents 的 70–90 分钟运行期。**
