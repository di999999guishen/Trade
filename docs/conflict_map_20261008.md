# 上游合并冲突地图（以我方版本为主）

> 生成时间：2026-10-08 07:0x
> 基线：`a33fd4c`（2026-07-18 共同祖先）｜我方 HEAD `787bdfc`｜上游 `upstream/main` = v0.6.0 `1394a3f`
> 实测冲突：**46 个**（`UU` 33 + `AA` 7 + `UD` 6），比初版方案记的 45 多 1 个
> 取证方式：在隔离 worktree `E:/Trade/_ta_upstream_sync` 真实执行 `git merge upstream/main`，取 `git status` 冲突码 + 两侧 `--numstat` 改动量；取完即 `--abort`，worktree 保持干净

---

## 1. 结论先行

| 项 | 结论 |
|---|---|
| 冲突总数 | **46** |
| 分布 | **100% 在 `tradingagents/` 引擎包与 `tests/`**；自有编排层（`prediction_research/`、`quant_research/`、`news_aggregator/`、`run_research.py`、`daily_predict.py`）**零冲突** |
| 我方自建数据源是否受影响 | **不受影响**。`dataflows/akshare_data.py`(436行)、`eastmoney_data.py`(453行)、`daily_prediction_markets.py`(323行)、`interface.py` 上游**没有这些路径** → 天然保留 |
| 最危险的一类 | **6 个「上游删除 / 本地仍在用」**（`UD`）：照单收上游 = 本地 pipeline 当场 ImportError |
| 「以我为主」能否直接达成 | **能**。`git merge -X ours` 一步得到「冲突块全取我方 + 非冲突部分自动吸收」的基线 |
| 但纯 `-X ours` 的代价 | 上游对引擎的重写（`trading_graph.py` 316/369、`cli/main.py` 102/1259、`default_config.py` 139/94）**全部放弃**；且 6 个被删模块会「新旧并存」 |

---

## 2. 46 个冲突总表（按处置分档）

判据：`LOCAL` / `UPSTREAM` = 相对基线 `a33fd4c` 的 `新增行/删除行`。
`本地无此文件` = 该路径在我方 HEAD 不存在（上游新增或搬家后的新路径）。

### A 档｜上游独占，本地无此文件 —— 5 个 → **可直接收下，零损失**

| 文件 | LOCAL | UPSTREAM | 说明 |
|---|---|---|---|
| `tradingagents/agents/context.py` | 本地无此文件 | 218/0 | 上游新增模块 |
| `tradingagents/dataflows/router.py` | 本地无此文件 | 297/0 | 上游新增模块 |
| `tradingagents/dataflows/vendors/fred.py` | 本地无此文件 | 286/0 | 上游把 `dataflows/fred.py` **搬到** `vendors/fred.py` |
| `tradingagents/dataflows/vendors/polymarket.py` | 本地无此文件 | 149/0 | 同上（`dataflows/polymarket.py`） |
| `tradingagents/dataflows/vendors/stocktwits.py` | 本地无此文件 | 162/0 | 同上（`dataflows/stocktwits.py`） |

⚠️ 后三个的**冲突根源**：我方**改了旧路径**（`fred.py` 40/5、`polymarket.py` 75/5、`stocktwits.py` 44/1），上游把文件搬走了 → git 判定为冲突。
→ 处理时必须决定：旧路径里我方那几十行改动**要不要搬到新文件**。

### B 档｜测试文件 —— 15 个 → **取上游**（低风险）

`test_capabilities.py`、`test_checkpoint_lifecycle.py`(AA)、`test_dataflows_config.py`、
`test_debate_opening.py`(AA)、`test_env_overrides.py`、`test_fred.py`、`test_llm_max_tokens.py`(AA)、
`test_memory_log.py`、`test_memory_pointintime.py`(AA)、`test_news_lookahead.py`、
`test_ohlcv_latest_bar.py`(AA)、`test_reddit_fallback.py`、`test_signal_processing.py`、
`test_social_lookahead.py`(AA)、`test_symbol_normalization_paths.py`

理由：这批测的是**上游引擎模块**，v0.6.0 的测试是配套新结构的。我方自有测试在 `prediction_research/tests/`（6 个）与根 `tests/` 的非冲突文件里，**不在本批中**。
⚠️ 例外待查：`test_signal_processing.py`（我方 54/3 改过，上游 24/36）—— 我方改的可能是自己的 `graph/signal_processing.py`。

### C 档｜🔴 上游删除 / 本地仍在用 —— 6 个（`UD`）→ **必须业务决策**

| 被删模块（我方路径） | 我方改动 | 上游替代位置 | 我方引用点 |
|---|---|---|---|
| `agents/utils/memory.py` | 45/10 | **`tradingagents/memory/`** 新包（`__init__`/`log`/`reflection`/`settlement`） | `graph/trading_graph.py:33`、`tests/test_memory_log.py`、`tests/test_memory_pointintime.py` |
| `agents/utils/rating.py` | 48/13 | `agents/rating.py` | `graph/signal_processing.py:17`、`agents/utils/memory.py:6`、`tests/test_signal_processing.py:13` |
| `dataflows/reddit.py` | 72/9 | `dataflows/vendors/reddit.py` | `agents/analysts/sentiment_analyst.py:43` |
| `dataflows/stockstats_utils.py` | **80/6** | `dataflows/vendors/yahoo/ohlcv.py` | `dataflows/market_data_validator.py:18`、`tests/test_{date_boundaries,ohlcv_cache_freshness,yfinance_stale_ohlcv_guard}.py` |
| `dataflows/yfinance_news.py` | 4/28 | `dataflows/vendors/yahoo/news.py` | `tests/test_news_lookahead.py:13`、`tests/test_symbol_normalization_paths.py:11` |
| `graph/signal_processing.py` | 14/7 | 无对等文件（评分解析并入 `agents/rating.py`） | `scripts/smoke_structured_output.py:29`、`tests/test_signal_processing.py:20` |

⚠️ `stockstats_utils.py` 我方只改 80/6，但含**自建护栏**（`_assert_ohlcv_not_stale` 陈旧数据保护、`load_ohlcv` 日期解析）。上游 `vendors/yahoo/ohlcv.py` 是**另一套实现**，不能假设带同样护栏。

### D 档｜双方都改的引擎 / 配置 / CLI —— 20 个

| 文件 | LOCAL | UPSTREAM | 以我为主的处置 |
|---|---|---|---|
| `main.py` | **93/13** | 1/4 | **保我方**。上游此文件是 16 行 demo 脚本（`ta.propagate("NVDA",...)`），我方是自建 CLI（`ticker`/`--date`/`--reports-dir`/`--quiet`/`--screen-evidence`），是 `prediction_research/tradingagents_adapter.py:190` 的**唯一入口契约** |
| `agents/schemas.py` | **205/14** | 61/23 | **保我方** + 看上游新增字段是否要吸收。我方含 MEMORY §2 价格块 schema |
| `graph/trading_graph.py` | 185/57 | **316/369** | 双方都大改 → **逐块人工合并**，最耗时 |
| `cli/main.py` | 118/97 | **102/1259** | 上游 TUI 重写；我方 118 行改动需重贴 |
| `default_config.py` | 14/8 | **139/94** | 上游重构；我方 14 行配置需重贴 |
| `llm_clients/model_catalog.py` | 9/9 | 79/32 | 逐块合并（我方新模型条目须保留） |
| `agents/analysts/sentiment_analyst.py` | 27/2 | 51/62 | 上游版 + 重贴（且 reddit import 要跟着搬家改） |
| `agents/analysts/news_analyst.py` | 1/1 | 15/21 | 上游版 + 重贴 1 行 |
| `agents/managers/portfolio_manager.py` | 21/1 | 32/21 | 上游版 + 重贴 |
| `agents/managers/research_manager.py` | 10/1 | 11/7 | 上游版 + 重贴 |
| `agents/researchers/bear_researcher.py` | 4/1 | 10/6 | 上游版 + 重贴 |
| `agents/researchers/bull_researcher.py` | 4/1 | 10/6 | 上游版 + 重贴 |
| `agents/risk_mgmt/aggressive_debator.py` | 7/2 | 16/7 | 上游版 + 重贴 |
| `agents/risk_mgmt/conservative_debator.py` | 7/2 | 16/7 | 上游版 + 重贴 |
| `agents/risk_mgmt/neutral_debator.py` | 7/2 | 16/7 | 上游版 + 重贴 |
| `agents/trader/trader.py` | 39/7 | 46/15 | 逐块人工合并 |
| `dataflows/date_window.py` | 30/0 | 170/0 | `AA` 双方都新增 → 取上游（实现更完整） |
| `.env.example` | 2/0 | 39/29 | 上游为基底 + 追加我方自有变量 |
| `README.md` | 15/3 | 140/25 | 上游为基底 + 追加我方说明 |
| `pyproject.toml` | 5/3 | 22/23 | 上游为基底 + 追加我方依赖（且版本声明需与我方实装对齐） |

---

## 3. 上游搬家映射表（v0.6.0 包结构重构）

```
agents/utils/           →  整个目录消失
agents/utils/rating.py  →  agents/rating.py
agents/utils/memory.py  →  memory/{__init__,log,reflection,settlement}.py   （新包）

dataflows/reddit.py          →  dataflows/vendors/reddit.py
dataflows/fred.py            →  dataflows/vendors/fred.py
dataflows/stocktwits.py      →  dataflows/vendors/stocktwits.py
dataflows/polymarket.py      →  dataflows/vendors/polymarket.py
dataflows/stockstats_utils.py→  dataflows/vendors/yahoo/ohlcv.py
dataflows/yfinance_news.py   →  dataflows/vendors/yahoo/news.py

新增：graph/{analyst_execution,checkpointer,conditional_logic,propagation,setup}.py
       agents/{state,structured}.py、backtest.py、portfolio.py、reporting.py、report_html.py
```

---

## 4. 「以我为主」的执行方案

### 方案 1（最保守，纯以我为主）
```bash
cd /e/Trade/_ta_upstream_sync
git merge -X ours upstream/main          # 冲突块全取我方，非冲突自动吸收
# 不做任何 import 迁移，本地 6 个模块原样保留
```
- 结果：本地代码**行为零变化**；上游新增文件照单收下但不接管。
- 代价：上游对引擎的重写**全部放弃**；且出现「`agents/utils/memory.py` 与 `tradingagents/memory/` 并存」等冗余。
- 风险：**最低**。适合「先保住自己，上游改天再说」。

### 方案 2（推荐：以我为主 + 挑着吸收）
```bash
git merge -X ours upstream/main          # 第 1 步同上，拿到可跑的基线
```
然后在该基线上**只做三件事**：
1. **C 档 6 个模块**：把引用点的 import 切到上游新路径，删除旧文件（拿到上游对 memory/rating 的重构收益）；
   其中 `stockstats_utils.py` 的**自建护栏必须先比对**，护栏要移植进新实现，不能丢。
2. **D 档 4 个重写文件**（`trading_graph.py` / `cli/main.py` / `default_config.py` / `model_catalog.py`）：
   逐个开 diff，按块决定；**我方 185/57（trading_graph）与 118/97（cli/main）的改动不许丢**。
3. **测试**：先跑上游新测试 → 再跑 `prediction_research/tests/`（6 文件）→ 最后 `run_research.py cycle --top 1` 冒烟。
- 代价：一张整块时间。
- 收益：真正拿到 v0.6.0 的结构收益（backtest/portfolio/reporting/memory 包）。

### 两者的共同红线
- **绝不在 18:00–20:00 动 `main`**（今天是节后首个交易日，三条链要跑）。
- 全程在隔离 worktree / 隔离分支操作，全绿后才合回 `main`。

---

## 5. 回滚点

| 回滚到 | 命令 |
|---|---|
| 合并前的 main | `git switch main && git reset --hard backup/pre-upstream-merge-20261008` |
| 丢掉隔离分支 | `git branch -D upstream-sync-v0.6.0` |
| 丢掉隔离 worktree | `git worktree remove /e/Trade/_ta_upstream_sync --force` |
| 恢复未提交改动 | `git apply E:/Trade/_backup/uncommitted_before_upstream_merge_20261008.patch` |
| 恢复未跟踪文件 | `tar xzf E:/Trade/_backup/untracked_20261008.tgz -C /e/Trade/TradingAgents` |

---

## 6. 勘误与落地实况（2026-10-08 晚补，**本节优先于上文**）

> 上文为**合并前**的静态推演。实际执行后，有 4 处判断需要更正，另有 3 处**上文完全没预见到**的 CN 缺口。

### 6.1 三处事实更正

| # | 上文原文 | 实况 | 影响 |
|---|---|---|---|
| 1 | 基线「我方 HEAD `787bdfc`」 | 实际 **`0e54c05`**（`787bdfc` 之后又推进了几笔提交） | 无实质影响，仅对账用 |
| 2 | `cli/main.py`「上游 TUI 重写；我方 118 行改动**需重贴**」 | ❌ **不需要重贴**。我方那 118 行是 **checkpoint 恢复接线**（`begin_checkpoint` / `checkpoint_input` / `clear_checkpoint_on_success` / `end_checkpoint`）；上游 v0.6.0 已把同一套逻辑**重构进 `cli/run.py:225-358` + `graph/trading_graph.py:stream_run`**，只是 `trading_graph.py` 与 `cli/run.py` 有我们自己的实现。**取上游 CLI = 零功能损失** | 省掉一整块人工重贴 |
| 3 | 方案 2 第 1 条「C 档 6 个模块：把 import 切到新路径，**删除旧文件**」 | 实际**没有一个是"删旧文件"的简单情形**：上游把 `agents/utils/` **整目录删除并重组**，我方 6 个引擎文件 import 全部悬空 ⇒「保我方引擎层」**物理不可行**。最终统一改为 **「以上游 v0.6.0 为骨架 + 移植我方自建锚点」** | 工作性质从「挑着吸收」变为「骨架置换」 |

### 6.2 上文未预见的三处 CN 缺口（均会导致**整条国内链崩**）

| 缺口 | 症状 | 处置 |
|---|---|---|
| 🔴 `dataflows/router.py` 的 `VENDOR_METHODS` **硬编码**，只登记 `alpha_vantage`/`yfinance`/`sec_edgar`/`fred`/`polymarket`，**缺我方 3 个 CN vendor** | 我方配置 `data_vendors="akshare"` 时 `route_to_vendor` 过滤得空链 → `raise ValueError(...)` ⇒ **CN 数据链整条崩** | 加性并入 `akshare`/`eastmoney`/`akshare_macro` 三个 vendor 的 import 与注册项（保留上游 `sec_edgar` 与增强版 `route_to_vendor`） |
| 🔴 `memory/settlement.py` 的 `fetch_returns()` 走 `get_closes`（**yfinance**） | A股/港股 ETF 结算失败或限流 | 新增 `_closes()` 分派 + `_cn_closes()`（走我方 `load_ohlcv_akshare`，`_CN_SUFFIXES = (".SS",".SZ",".BJ",".HK")`） |
| 🔴 `dataflows/akshare_data.py` 引用 `dataflows/symbol_utils.py` 与 `dataflows/utils.py` —— **两模块上游已删**（并入 `symbols.py`） | `ModuleNotFoundError`，我方自建文件**直接 import 崩** | 两行合并为 `from .symbols import normalize_symbol, safe_ticker_component`（`symbols.py` 两函数签名兼容） |

### 6.3 C 档 6 项的实际裁决（回应「能否纯沿用新版本代码」）

| # | C 档模块 | 上游替代 | 能否纯沿用 | 实况 |
|---|---|---|---|---|
| 1 | `agents/utils/memory.py` | `tradingagents/memory/` 新包 | ✅ 零移植 | 已取上游 |
| 2 | `agents/utils/rating.py` | `agents/rating.py` | ✅ | 已取上游 |
| 3 | `dataflows/reddit.py` | `vendors/reddit.py` | ✅ | 已取上游 |
| 4 | `dataflows/yfinance_news.py` | `vendors/yahoo/news.py` | ✅ | 已取上游 |
| 5 | `graph/signal_processing.py` | 无对等（并入 `agents/rating.py`） | ✅ 整份丢 | 已丢 |
| 6 | `dataflows/stockstats_utils.py` | `vendors/yahoo/ohlcv.py` | ⚠️ **也不用搬** | 新 vendor 路由层让"改上游文件打补丁"变成"注册 vendor"；我方 `us_ohlcv_vendor=sina` 开关在 `daily_predict.py:47` 已成**惰性死键**，但美股行情/指标实际走 `get_akshare_data_online`→`_fetch_us_sina` / `get_akshare_indicators_window`，**不碰** `stockstats_utils.py`。（MEMORY §12「hold 到 12 月才删」⇒ 该键保留） |

### 6.4 顺手查出的清单外真缺口：Polymarket 代理回退链

- 我方原 `dataflows/polymarket.py` 独有 `_system_proxy`@35 / `_proxy_candidates`@63 + 成功代理缓存 + 502 失效重试；新树 **0 命中**。
- 上游 `vendors/polymarket.py:32` 是**裸 `requests.get`**，上游新增集中层 `dataflows/net.py` **也不带代理处理**。
- `retry_utils.setup_proxy_bypass()` 只设 `NO_PROXY` 直连白名单，**补不上**（Polymarket 境外必须走代理）。
- 影响：19:00 `daily_predict`（SPY/QQQ/GLD mandatory 快照）+ `morning_briefing.py:323`。**待拍板**（方案 A 中央层回退 / B 局部搬回 / C 接受单点）。

### 6.5 非冲突死引用修复（3 处，均不在冲突表内）

| 文件 | 原引用 | 改为 |
|---|---|---|
| `tradingagents/dataflows/daily_prediction_markets.py:14` | `dataflows.polymarket._request` | `dataflows.vendors.polymarket` |
| `morning_briefing.py:323` | `dataflows.polymarket.get_prediction_markets` | `dataflows.vendors.polymarket` |
| `daily_predict.py:115` | `dataflows.utils.safe_ticker_component` | `dataflows.symbols` |

### 6.6 根文件实际处置

| 文件 | 上文建议 | 实际 |
|---|---|---|
| `.env.example` | 上游为基底 + 追加我方变量 | ✅ 取上游（我方差异已被上游覆盖，无需补） |
| `README.md` | 上游为基底 + 追加我方说明 | ✅ 已补：A股 ETF 模块段 + CN 数据源说明；上游已含 v0.6.0 news 段、模型示例已更新到 GPT-6 ⇒ 我方旧 v0.4.0 行/gpt-5.6 行**不重贴** |
| `pyproject.toml` | 上游为基底 + 追加 `prediction_research` | ✅ 已补：包发现 / 入口 / testpaths / 版本对齐 |
