# 上游 v0.6.0 合并：实测取证与冲突实测清单（2026-10-08）

> 本文取代 `conflict_map_20261008.md` 中「46 冲突分 ABCD 档」的**定性推测**部分。
> 全部数字来自 2026-10-08 上午在隔离 worktree 的**实测**，非推断。

---

## 0. 三条推翻前论的更正

### 更正 1：本仓库**是**真 fork，有真实合并基

| 项 | 值 |
|---|---|
| 真实 merge-base | `a33fd4c`（2026-07-18 `docs: streamline README header`） |
| 上游自 base 起的提交数 | **225** |
| 我方自 base 起的提交数 | **258** |

前一轮曾据 `255ecb6 Initial commit`（只含 LICENSE + README）判定「不是 fork、是全新 git init」。
**该结论作废**：`942bc0c` 的父提交即 `756cbf0`（与 `a33fd4c` 同消息同日期、不同哈希 = 上游历史被重写产生的重复对象），
`b21ad8e merge: 合并远程仓库` 把两条谱系接了回去。`git merge-base HEAD upstream/main` 正常返回 `a33fd4c`。

**含义：可以做正规三方合并，不需要「整体 ours + 手工重放」。**

### 更正 2：我方引擎 delta 是 **33 文件 / 2285+ 194−**，且真正的自建内核是「国内数据源」

`git diff a33fd4c HEAD -- tradingagents/`：

```
33 files changed, 2285 insertions(+), 194 deletions(-)
  28 M   5 A
```

5 个新增（纯自建，上游 0 命中）：

| 文件 | 说明 |
|---|---|
| `tradingagents/dataflows/akshare_data.py` | 新浪/腾讯 A股+港股数据源 |
| `tradingagents/dataflows/eastmoney_data.py` | 东方财富数据源 |
| `tradingagents/dataflows/daily_prediction_markets.py` | Polymarket 日度快照 |
| `tradingagents/evidence_context.py` | 证据链上下文 |
| `tradingagents/dataflows/date_window.py` | 窗口裁剪（**上游 v0.6.0 也独立加了同名文件**） |

**判据**：`git grep -l akshare upstream/main -- tradingagents/` → **0 命中**；我方 → **7 命中**。
⇒ 国内源替换才是真正不可替代的自建部分。

### 更正 3：`_assert_ohlcv_not_stale` **不是我写的**，上游本来就有

上游位置：`tradingagents/dataflows/vendors/yahoo/ohlcv.py:109`（含 `MAX_OHLCV_STALE_DAYS`、`NoMarketDataError` 抛法，
以及 `vendors/yahoo/market.py:49` 的调用点）。文字逐条一致。

前一轮把它列为「必须先移植的自建护栏」——**错误，无需移植**。
我方对 `stockstats_utils.py` 的 80+/6− 实际是**国内源路由**（akshare 优先、Yahoo 兜底、`us_ohlcv_vendor=sina` 开关）。

---

## 1. 关键：`-X ours` 为什么产出残废包

`git merge -X ours` 对**冲突块的每一块**静默取我方，只把 `modify/delete`（无法自动解算）留成冲突。

| 结果 | 模块数 | 导入 OK | 失败 |
|---|---|---|---|
| 纯上游 v0.6.0 | 82 | 82 | 0 |
| 我方生产 | 76 | 76 | 0 |
| `-X ours` 混血 | 92 | 34 | **58** ❌ |

失败根因：上游 v0.6.0 **删掉了整个 `tradingagents/agents/utils/` 包**，而 `-X ours` 把我方对这些路径的引用
全部保留 → `No module named 'tradingagents.agents.utils.core_stock_tools'`，连锁拖垮
`graph.*` / `memory.*` / `reporting` / `report_html`。

⇒ **禁止用 `-X ours` 或 `-s ours` 处理引擎包。** 逐块人工解算才行。

---

## 2. 正规三方合并的**实测**冲突清单

命令（隔离 worktree `/e/Trade/_ta_port`，分支 `upstream-sync-port` = `1394a3f`）：

```bash
git switch upstream-sync-port          # = upstream/main v0.6.0
git -c merge.renames=false merge main --no-commit --no-ff
```

> `merge.renames=false` 是必要的：开着重命名探测时，git 会把
> `agents/utils/agent_utils.py`(我方) ↔ `agents/context.py`(上游) 这类**不同文件**误配成同一路径的冲突，
> 把文件「吃掉」，产生假冲突。关掉后冲突语义才干净。

**冲突总计 46 = 28 UU + 11 DU + 7 AA**

### 2.1 `UU` — 双方都改同一路径，逐块人工解算（28 个，**核心工作量**）

引擎 15 个：

```
tradingagents/agents/schemas.py                              ← 我方 205+/14−，含「可执行价格块」
tradingagents/graph/trading_graph.py                         ← 我方 185+/57−
tradingagents/agents/trader/trader.py                        ← 39+/7−
tradingagents/agents/analysts/sentiment_analyst.py           ← 27+/2−
tradingagents/agents/managers/portfolio_manager.py           ← 21+/1−
tradingagents/default_config.py                              ← 14+/8−
tradingagents/agents/managers/research_manager.py            ← 10+/1−
tradingagents/llm_clients/model_catalog.py                   ← 9+/9−
tradingagents/agents/risk_mgmt/{aggressive,conservative,neutral}_debator.py ← 各 7+/2−
tradingagents/agents/researchers/{bear,bull}_researcher.py   ← 各 4+/1−
tradingagents/agents/analysts/news_analyst.py                ← 1+/1−
```

包外 10 个：`cli/main.py`(118+/97−)、`main.py`(93+/13−)、`tests/*`(9 个)、`.env.example`、`README.md`、`pyproject.toml`

测试 9 个：`test_capabilities` / `test_dataflows_config` / `test_env_overrides` / `test_fred` /
`test_memory_log` / `test_news_lookahead` / `test_reddit_fallback` / `test_signal_processing` / `test_symbol_normalization_paths`

### 2.2 `DU` — 上游删掉整个路径、我方有改动，**必须决定搬家或丢弃**（11 个）

上游 v0.6.0 已删除 `tradingagents/agents/utils/` 整个包。

| 我方旧路径 | 我方改动 | 上游新家 | 处置 |
|---|---|---|---|
| `agents/utils/memory.py` | 45+/10− | `tradingagents/memory/`（包） | **搬家** |
| `agents/utils/rating.py` | 48+/13− | `tradingagents/agents/rating.py` | **搬家** |
| `agents/utils/agent_utils.py` | 24+/0− | 无对等 → `agents/tools.py` / `agents/context.py` | **搬家** |
| `dataflows/stockstats_utils.py` | 80+/6− | `dataflows/vendors/yahoo/ohlcv.py` + `market.py` | **搬家** |
| `dataflows/reddit.py` | 72+/9− | `dataflows/vendors/reddit.py` | **搬家** |
| `dataflows/fred.py` | 45+/43− | `dataflows/vendors/fred.py` | **搬家** |
| `dataflows/stocktwits.py` | 45+/− | `dataflows/vendors/stocktwits.py` | **搬家** |
| `dataflows/polymarket.py` | 80+/− | `dataflows/vendors/polymarket.py` | **搬家** |
| `dataflows/yfinance_news.py` | 4+/28− | `dataflows/vendors/yahoo/news.py` | **搬家** |
| `dataflows/interface.py` | +22 | `dataflows/router.py` / `config.py` | **搬家** |
| `graph/signal_processing.py` | 14+/7− | **无对等文件** | 待定（见 §3） |

### 2.3 `AA` — 双方各自新增同一路径（7 个）

`tests/test_{checkpoint_lifecycle,debate_opening,llm_max_tokens,memory_pointintime,ohlcv_latest_bar,social_lookahead}.py`
+ `tradingagents/dataflows/date_window.py`

**默认处置：取上游版本。** 我方这些文件测的是 v0.6.0 已不存在的旧 API；
且它们的 yfinance/旧工具引用在新架构下必然失效。
`date_window.py` 例外：上游版含 `in_window`/`is_historical`/`coverage_gap`/`withhold_live_profile`（我方只有 `in_window`）
⇒ **直接取上游版，我方调用点无需改**（`in_window` 签名一致）。

---

## 2.4 ⭐ 决定性判据：符号级溯源，把「我方改动」再砍一刀

**判据**：某符号在上游 v0.6.0 是否已存在。
已存在 ⇒ 我方那份只是继承来的上游码（我方谱系里混着 235 个上游提交），**解算时向上游靠拢，不必保留**；
不存在 ⇒ 真自建，必须搬进新路径。

工具：`E:/Trade/_backup/du_symbols.py`（`python du_symbols.py <repo> <base> <head>`）

**11 个 `DU` 文件 + 5 个新增文件的符号溯源结果：**

| 文件 | 真自建 | 上游已有 | 处置 |
|---|---:|---:|---|
| `agents/utils/memory.py` | **0** | 13 | **整份丢弃，取上游** |
| `agents/utils/rating.py` | **0** | 4 | **整份丢弃，取上游** |
| `dataflows/fred.py` | **0** | 12 | **整份丢弃，取上游** |
| `dataflows/stocktwits.py` | **0** | 3 | **整份丢弃，取上游** |
| `dataflows/yfinance_news.py` | **0** | 3 | **整份丢弃，取上游** |
| `dataflows/date_window.py` | **0** | 2 | **整份丢弃，取上游** |
| `dataflows/interface.py` | 1 | 6 | 只搬 `VENDOR_LIST` |
| `dataflows/polymarket.py` | 2 | 7 | 只搬 `_proxy_candidates` / `_system_proxy` |
| `dataflows/reddit.py` | 2 | 10 | 只搬 `_fetch_subreddit` / `_fetch_subreddit_json` |
| `dataflows/stockstats_utils.py` | 3 | 12 | 只搬 `_needs_same_day_refresh` / `filter_financials_by_date` 等 |
| `agents/utils/agent_utils.py` | 2 | 6 | 复核后按需搬（`opponent_argument_or_opening` 上游 6 处已有 → 丢） |
| `graph/signal_processing.py` | 2 | 1 | `RATING_REVIEW`/`extract_rating`/`is_review` 上游已有 → **整份丢弃** |
| `dataflows/akshare_data.py` | 8 | 6 | **保留整个文件** |
| `dataflows/eastmoney_data.py` | 16 | 1 | **保留整个文件** |
| `dataflows/daily_prediction_markets.py` | 17 | 0 | **保留整个文件** |
| `evidence_context.py` | 1 | 0 | **保留整个文件** |
| **合计** | **54** | **86** | |

**交叉验证**（上游直接命中数）：

```
opponent_argument_or_opening   上游 6 个文件已有   -> 丢
RATING_REVIEW / extract_rating / is_review   上游已有 -> 丢
resolve_cn_instrument_identity   上游 0 命中       -> 我的，必须搬
akshare                          上游 0 命中       -> 我的，必须搬
```

⇒ **11 个 `DU` 里 6 个可直接丢弃**，剩 5 个各只需搬 1–3 个函数。
⇒ 真正的自建资产集中在 4 个新文件 + 少量函数（CN 身份解析 / 代理绕过 / 同日刷新护栏 / VENDOR_LIST）。

---

## 3. 必须在动手前拍板的两件事

1. ~~`graph/signal_processing.py` 我方 14+/7− 是什么~~ → **已取证，见 §2.4：整份丢弃。**
   我方那 21 行是 `RATING_REVIEW` / `extract_rating` / `is_review`（引用上游 issue `#1170`），
   上游 v0.6.0 已在别处实现 ⇒ 不是我的东西。

2. **9 个 `UU` 测试文件**：取上游版（推荐，省 1 天）还是保留我方断言？
   推荐取上游——测试是对新架构的规格，我方断言针对的是 v0.6.0 已删除的旧 API。
   若保留我方断言，须逐个改到新 API，收益低。

---

## 4. 排查纪律（本轮踩过）

- **先看作者，再判「我的改动」**：`git log $BASE..HEAD --format='%an' -- tradingagents/` 显示
  引擎包 173 个提交里 **只有 4 个是本人的**（Yijia-Xiao 130 / CadeYu 12 / luohy15 7 / Edward Sun 6）。
  我方侧 258 个提交中本人仅 **23** 个。**「2285 行引擎改动」不能整体当成自己的资产。**
- **判冲突归属用逐文件作者检查**：
  `git log --author=... $BASE..HEAD --oneline -- <file>`，为空 ⇒ 该路径的「我方版本」其实是继承来的上游码，
  解算应向**上游**靠，而不是向「我方」靠。
- **别用提交信息/行数推断归属**：`f397ffb` 的 `reddit.py` diff 里含上游 issue 号 `#1220` 的注释，
  说明我方谱系里混有上游内容，`+81 行` 不等于「我写了 81 行」。
