# ETF 预测流水线阻塞诊断报告

- **日期**：2026-09-22（周二）19:00
- **脱敏说明**：本文推送至公开仓库前，已将出口 IP 掩码为 `113.25.x.x` / `37.9.x.x`，出口归属地亦做模糊化处理；真实取值保留在本机 `.workbuddy/memory/`（未入库），需要完整证据时以本地记忆为准。

> **19:25 重验（⛔ 阻断面已扩大到资金流接口）**：见第七节。核心变化：此前短暂可用的 `push2his /api/qt/stock/fflow/daykline/get`（资金流接口）**现已被一并阻断**（`http=000`），且本机**不存在第二条出口**（直连无路由、备用代理端口不通）。故此前「用 fflow 接口抢救出真实五档资金流」的兜底路径也已关闭。
>
> **19:01 自动化重验补记（本轮 7100f2bb）**：19:01 再次以 `curl` 探测 + `fetch-etfs` 最小复现，结果与 18:33/18:57 完全一致——`push2 /api/qt/clist/get` 仍 `HTTP:000`（连接被重置），`/` 仍 404 可达，`datacenter-web`/`fundmobapi` 仍 200；`fetch-etfs` 仍报 `ETF quote page 1 failed: RemoteDisconnected`；出口 IP 仍为 `37.9.x.x`（数据中心段）。**东财行情 WAF 拉黑未解除，本轮照旧无法产出预测。**
- **范围**：`prediction_research` ETF 资金流筛选 → 预测流水线（`run_research.py cycle`）
- **结论（先看这条）**：今日**未产出预测**。根因不在项目代码，而是**东财 `push2*.eastmoney.com/api/qt/*` 行情接口对本机出口 IP 的路径级阻断**。经全量核验，**目前没有任何可达数据源能提供筛选硬性要求的 ETF 主力资金流**，故按现有方法论无法跑通。**今日不发布预测，也不以后备源拼凑替代结论。** ⚠️ **本节结论已被第九节推翻**：新浪双源回退上线后链路恢复，已产出 Top5 候选。

---

## 一、现象

| 项 | 值 |
|---|---|
| 运行清单 | `prediction_research/runs/cycle_20260922_183314_283267.json` |
| 运行结果 | `outcome = running`（中断） |
| 失败步骤 | `fetch_etf_snapshot` = **failed** |
| 连带阻断 | `screen_etfs` = **blocked_dependency**（reason `current_snapshot_fetch_failed`） |
| 复现错误 | `RuntimeError: ETF quote page 1 failed: RemoteDisconnected: Remote end closed connection without response` |
| 上一次成功快照 | `etf_snapshot_20260917_095821_085492.json`（1618 条，9/17 09:58 UTC） |

---

## 二、根因取证

### 2.1 阻断是「路径级」，不是「主机级」

同一批主机，根路径可达、行情 API 路径被重置：

| 主机 | 路径 | 结果 |
|---|---|---|
| push2.eastmoney.com | `/` | HTTP 404（**可达**） |
| push2.eastmoney.com | `/api/qt/clist/get` | **code=000 / curl(56) 连接被重置** |
| push2.eastmoney.com | `/api/qt/stock/get` | **code=000** |
| push2his.eastmoney.com | `/api/qt/stock/kline/get` | **code=000** |
| datacenter-web.eastmoney.com | `/api/data/v1/get` | HTTP 200，**返回真实 JSON** |
| fundmobapi.eastmoney.com | `/FundMNewApi/FundMNFInfo` | HTTP 200，**返回真实 ETF 净值/价格 JSON** |
| quote / www / wap .eastmoney.com | `/` | HTTP 200（**可达**） |

> 判定：东财 WAF 仅对 `push2*` 系的 `/api/qt/*` 前缀做拦截；其余东财服务（数据中心、天天基金移动端、页面站）均正常。**排除代码 bug、参数错误、UA/Referer/Cookie 缺失、TLS 指纹问题。**

### 2.2 直连与代理是同一个出口，换通道无解

| 路径 | 出口 IP |
|---|---|
| 经代理 `127.0.0.1:17891` | **37.9.x.x** |
| 直连（绕过代理） | **37.9.x.x** |

两者一致，且该 IP 属**数据中心/代理段**。东财行情 API 对数据中心段 IP 做反爬拉黑，这是与「静态页面可达、行情 API 被重置」完全吻合的解释。**因此换代理、加 NO_PROXY 白名单都无效。**

### 2.3 持续复现，非瞬时抖动

- 18:51–18:55，以 20 秒间隔轮询 `push2` / `82.push2` / `push2delay` 三个域名共 **12 次 → 12/12 EMPTY**。
- 枚举 `push2` 的 **6 个解析 IP → 全部 EMPTY**。
- 完整浏览器请求头（UA / Accept / Accept-Language / Cookie / Referer / sec-ch-ua）重试 → 仍 code=000。**证明不是请求头伪装问题。**

---

## 三、可达替代源能力核验

| 源 | 能力 | 能否满足筛选 |
|---|---|---|
| 新浪 `fund_etf_category_sina` | 全量 ETF 列表 1676 行（代码/名称/最新价/涨跌幅/成交量/成交额） | ✗ 缺规模、缺资金流 |
| 新浪 `fetch_daily` | 日线历史（510300 → 1023 根，末行 2026-09-22） | ○ 仅历史，可作日线刷新 |
| 新浪 `hq.sinajs.cn` | 实时/收盘行情（含港股） | ○ 仅行情 |
| 东财 `datacenter-web` | 数据中心报表（北向、估值等） | ✗ 无 ETF 全量行情/资金流 |
| 东财 `fundmobapi` | 基金净值、场内价格、涨跌幅 | ✗ 无成交额/规模/资金流 |
| westock-mcp（服务端取数） | `data_etf`：`etfSize`（规模）、`etfTurnover`（成交额）、`chgPct` | ✗ **无主力净流入**；且限频，批量 >1 只即报「服务限频」 |
| akshare `fund_etf_spot_em` | — | ✗ 同源阻断（走 push2） |

**关键缺口**：所有可达源都**没有 ETF 主力净流入（`main_net_inflow` / `main_net_inflow_pct`）**。

---

## 四、为什么不能「降级跑通」

1. **筛选器硬性依赖资金流**：`prediction_research/screening.py` 第 63–64 行对每行记录要求 `main_net_inflow_pct` 与 `main_net_inflow` 均非空，缺一项即 `continue` 跳过。用无资金流的替代源建快照 → 全部行被跳过 → 报 `no eligible ETF candidates with fresh, complete money-flow data`。
2. **旧快照也救不了**：`latest.json` 指向 9/17 快照，距今 **5 个日历日**，超过 `workflow.max_quote_age_days = 4`；即使走 `skip_fetch`，`screen_etfs` 的 `fresh` 集合也会为空而报错。
3. 因此**唯一可用的资金流源就是被拉黑的 push2**，绕行路径不存在。

---

## 四点五、已执行的部分补偿动作及其残留降级

后台执行 `run_research.py fetch-screened --top 20`（新浪回退刷新候选日线）：

| 项 | 结果 |
|---|---|
| 退出码 | 0，`failures = {}` |
| 覆盖 | 20 个冻结筛选候选，全部刷新至 **2026-09-22**（1023 行，起始 2022-07-08） |
| provider | `Sina K-line`，`fallback_from = eastmoney_push2his`（再次印证 push2his 同被阻断） |
| **残留降级** | `amount_status = unavailable` —— 新浪 K 线**不含成交额**，日线特征链的成交额因子会缺失 |

> 即「日线新鲜度」这一项已补上，但**快照与资金流仍缺**，且历史数据的成交额字段是降级的。

---

## 五、今日可确认事实（补充参考，**非流水线输出**）

数据源：新浪（时间戳为交易所收盘后，15:00–16:30）。

| 代码 | 名称 | 昨收 | 今收 | 涨跌幅 | 成交额(元) |
|---|---|---|---|---|---|
| sh512400 | 有色金属ETF南方 | 1.729 | 1.733 | +0.23% | 625,209,659 |
| sz159611 | 电力ETF | 1.039 | 1.034 | −0.48% | 225,793,575 |
| sh510160 | 产业升级ETF南方 | 0.898 | 0.900 | +0.22% | 14,637 |
| sh589720 | 科创创新药ETF国泰 | 0.923 | 0.926 | +0.33% | 703,381,484 |
| sz159748 | AH创新药 | 0.870 | 0.868 | −0.23% | 57,769,009 |
| sh516090 | 新能源ETF易方达 | 0.465 | 0.464 | −0.22% | 26,494,087 |
| sh512170 | 医疗ETF华宝 | 0.348 | 0.348 | 0.00% | 492,627,744 |
| hk03032 | 恒生科技ETF | 4.420 | 4.436 | +0.36% | — |

> 仅为行情事实记录。**未做任何点位推演、未输出买卖建议**，因为流水线的资金流证据链今日缺失。

---

## 六、建议动作

| 优先级 | 动作 | 说明 |
|---|---|---|
| P0 | 更换出口为**非数据中心 IP**（住宅/公司宽带直出）后重跑 `run_research.py cycle --top 5` | 最直接；当前 37.9.x.x 属代理段，被东财行情 WAF 拉黑 |
| P1 | 明日开盘前重跑，观察 WAF 是否自动解除 | 反爬拉黑有时限，成本最低 |
| P2 | 为 `eastmoney_etf.py` 增设多源回退 | **前置条件**：先确认存在能提供 ETF 主力净流入的可达源；当前未找到，故未实施 |
| P3 | 仅在确需降级时，显式修改 `screen` 规则放宽资金流依赖 | 属方法论变更，须先声明并留痕，本次**未执行** |

---

## 附：本次明确未执行项

- 未回填 2026-09-21 因 429 失败的那次运行（用户决定：只跑今天的）。
- 未产出任何预测、未调用 LLM（`skip_tradingagents = true`）。
- 未改写筛选规则；`screen_freeze_policy` 仍为 `first_daily_selection_wins`，今日无 screen 记录入库。
- 未改动 `prediction_research` 任何代码。

---

## 七、19:25 重验：阻断面扩大到资金流接口（本轮「再试一下」实测）

按用户 P0/P1/P2 三项逐一实测，结论如下表。**探测先于跑链路**（避免再次挂在重试上白等 7 分钟）。

| # | 探针 | 命令 / 目标 | 结果 | 判读 |
|---|---|---|---|---|
| 1 | 出口 IP（经 17891） | `curl -x 127.0.0.1:17891 api.ipify.org` | `37.9.x.x` | **与 18:33 完全相同** → P0 未落地，出口没换 |
| 2 | 出口 IP（直连） | `curl --noproxy '*' api.ipify.org` | 空，`rc=35`（SSL 连接失败） | 直连无路由，**没有第二条出口可用** |
| 3 | 备用代理端口 | `curl -x 127.0.0.1:2815` | 空（不通） | env 里的 `2815` 代理对本机外网不可用；`netstat` 仅见 2815 在听 |
| 4 | 行情 API | `push2 /api/qt/clist/get` | **`http=000`** | 阻断持续 |
| 5 | 资金流 API | `push2his /api/qt/stock/fflow/daykline/get` | **`http=000`** | ⚠️ **本轮新增：此前短暂可用的资金流接口也被封** |
| 6 | 同主机健康对照 | `datacenter-web /api/data/v1/get` | `http=200` | 主机可达 → 仍为**路径级** WAF 拉黑，非主机不可达 |
| 7 | ETF 主力净流入兜底源 | westock-mcp `data_fund_flow`（`sh512400`、`sz159611`，间隔 45s） | 两次均 `error_type=2 服务限频` | 回退源**不可用** |

**P0/P1/P2 结案**

| 项 | 要求 | 实测结论 |
|---|---|---|
| P0 | 切到非数据中心 IP 后重跑 `cycle --top 5` | ❌ **未完成**（本机无第二条出口；换出口属网络侧操作，非代理层可解）。出口仍 `37.9.x.x`，故未再跑链路 |
| P1 | 明日开盘前重跑，观察 WAF 是否自动解封 | ⏳ **待执行**（明日 09:15 前一条 curl 探针即可判定，见第五节策略） |
| P2 | 确认存在可达 ETF 主力净流入源后，给 `eastmoney_etf.py` 加多源回退 | ❌ **前置条件不成立**（push2 已封、fflow 已封、westock 限频、新浪无资金流、datacenter-web/fundmobapi 无 ETF 流向），**故未改代码** |

**本轮净增量**：唯一新增事实是**阻断面从「行情接口」扩大到「资金流接口」**，代价是原本可作临时抢救的 `fflow` 兜底路径关闭。项目代码零改动，无预测产出。

---

## 八、19:48–19:52 重诊断：推翻「数据中心 IP」结论 + 找到可用替代源

> 由用户提问「直接使用直连的方式获取数据不行吗，国内代理走的时候不用开不就行了吗」触发。
> 结论：**直连与代理对国内域名是同一回事，出口从来不是数据中心 IP；真实根因是东财对 `push2*` 家族 `/api/qt/*` 前缀按源 IP 的 WAF 拉黑。同时**首次确认存在可达的 ETF 主力净流入源**。

### 8.1 核心修正：出口 IP 判断错了

| 目标 | 直连（`--noproxy '*'`） | 经代理 `127.0.0.1:17891` |
|---|---|---|
| 国内 IP 回显 `myip.ipip.net` | `113.25.x.x`（中国电信 · 华北某地级市） | **`113.25.x.x`（完全相同）** |
| 国际 IP 回显 `api.ipify.org` | `rc=35`（无路由） | `37.9.x.x`（数据中心段） |

**代理客户端对国内域名走 China-direct 规则**，因此东财请求的实际出口**一直是家宽 `113.25.x.x`**，`37.9.x.x` 只是国际流量的出口。
⇒ 第二节/第七节中「出口 IP 为数据中心段 `37.9.x.x`」的表述**不准确**；P0「换非数据中心出口」的**前提本身是错的**。关掉代理与不关，对东财数据无任何区别。

### 8.2 排除法证据链（9 项变量全部排除，仅剩「源 IP + 路径」）

| # | 假设 | 测试 | 结果 | 判定 |
|---|---|---|---|---|
| 1 | 出口 IP 是问题 | 直连 vs 代理 | 出口同为 `113.25.x.x` | ✗ 与出口无关 |
| 2 | 客户端 TLS 指纹 | curl(schannel) vs Python(OpenSSL) vs httpx | 三者同报 `RemoteDisconnected` / `http=000` | ✗ 排除 |
| 3 | HTTP/2 差异 | Node 内置 `http2` 直连 | **连对照组 datacenter-web 都 h2 protocol error** → 该 API 本就 HTTP/1.1 | ✗ 排除 |
| 4 | TLS 中间盒 / MITM | `openssl s_client` 比对 4 台主机证书 | 全部 `*.eastmoney.com`，DigiCert/GeoTrust，SHA256 指纹完全一致 | ✗ 无 MITM |
| 5 | DNS 就近节点错误 | 系统 DNS=`8.8.8.8` vs 223.5.5.5/119.29.29.29/114DNS | 国内 DNS 返回 `61.129.129.196`，**四个边缘 IP 全部 000** | ✗ 排除 |
| 6 | 参数缺失（无 `ut`/`cb`/`_`） | 补全完整浏览器参数 | 仍 `000` | ✗ 排除 |
| 7 | 路径级拦截范围 | `/`、`/api/qt/clist/get`、`/api/qt/ulist.np/get`、`/api/qt/stock/get`、`/api/qt/stock/fflow/...` | `/` → **404（正常响应）**；其余 `/api/qt/*` → **全部 000** | ✓ **路径前缀级** |
| 8 | push2 镜像主机是否有活口 | `1/7/13/23/29/35/82/87/89/92/nufm/push2delay` 共 12 个 | **全部 000** | ✗ 无活口 |
| 9 | 是否东财全站不可用 | `quote`（200）、`push2ex`（200）、`datacenter-web`（200）、`fundmobapi`（200） | 这些主机**均正常** | ✗ 非全站 |

**真实根因**：东财对 `push2` / `push2his` / `push2delay` / `NN.push2` 家族的 **`/api/qt/*` 前缀**，针对源 IP `113.25.x.x` 实施了 WAF 级拉黑。TCP 可通、TLS 握手成功（服务端真实证书）、请求发出后由**服务端** RST，同主机 `/` 仍正常返回 404 —— 典型「路径 + 源 IP」黑名单特征。连续多日高频抓取是合理诱因。

### 8.3 新增发现：新浪 MoneyFlow 提供 ETF 主力净流入（P2 前置条件**已满足**）

此前判定「无任何可达的 ETF 主力净流入源」**有误**。实测 `MoneyFlow.ssl_qsfx_zjlrqs` 可用：

```
GET https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/
    MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=3&sort=opendate&asc=0&daima=sh510050
Referer: https://finance.sina.com.cn
```

返回字段（2026-09-22 实测，10/10 只测试 ETF 均有数据）：

| 新浪字段 | 含义 | 映射到 pipeline |
|---|---|---|
| `netamount` | 主力净流入额（元） | → **`main_net_inflow`** |
| `ratioamount` | 主力净流入率（占成交额，×100 即百分比） | → **`main_net_inflow_pct`** |
| `r0_net` | 超大单净额（元） | → 超大单分层（仅此一层） |
| `trade` / `changeratio` / `turnover` | 收盘价 / 涨跌幅 / 换手 | 交叉校验用 |

实测样本：`510050 netamount=-2.0098亿 ratio=-11.146%`；`515220 netamount=+1.4187亿 ratio=+16.471%`；`510300 netamount=-2.2175亿 ratio=-7.423%`。
**交叉校验通过**：新浪 `trade=3.0150` 与当日东财快照 `3.015` 一致。

**ETF 全市场列表源已可用**（替代被拉黑的 `clist/get`）：

```
GET .../Market_Center.getHQNodeData?page=1&num=100&sort=symbol&asc=1&node=etf_hq_fund
```
返回 `symbol, code, name, trade, pricechange, changepercent, buy, sell, settlement, open, high, low, volume, amount, ticktime, mktcap, nmc, turnoverratio`；`getHQNodeStockCount` 报**全市场 1676 只 ETF**。

**已知降级点（必须显式声明）**：

1. **只有 1/5 层资金流**：新浪仅给 `r0_net`（超大单），**无大单/中单/小单**，故「超大单 vs 大单背离」这类判读**不可做**。
2. **资金流为逐只接口**：无批量端点（`ssl_bkzj_ssggzj` / `ssl_bkzj_bk` / `ssl_bkzj_zjlrqs` 实测均 `200 size=0`）。非资金流硬门槛（`min_amount=5e6`、`min_market_cap=5e7`）先过滤后仍约 600–700 只 → 需 **600–700 次逐只请求**，耗时约 3–8 分钟，且有触发新浪限频风险。
3. 新浪 K 线无成交额（`amount_status=unavailable`，既有事实）。
4. 逐只请求需并发节流 + 断点续传，属**新增工程改动**。

### 8.4 修正后的三档出路（按可执行性排序）

| 优先级 | 措施 | 可执行性 | 说明 |
|---|---|---|---|
| **1** | **改用新浪双源回退**（列表 + 逐只 MoneyFlow） | ✅ 立即可做，**前置条件已满足** | 零依赖东财即可恢复出预测；代价是资金流降为 1 层。需用户点头后改 `eastmoney_etf.py` |
| **2** | **换出口 IP**：手机热点 / 光猫重拨（PPPoE 换 IP） | ✅ 用户侧 5 分钟可验证 | 一条 curl 打 `clist/get` 即知是否解封。比「换宽带」轻得多 |
| **3** | 代理客户端加规则把 `eastmoney.com` 强制走远程出口 | ⚠️ 取决于客户端是否可改规则 | 当前 `17891` 对 CN 走 direct，需改规则才能换 IP；且 `37.9.x.x` 是否也被拉黑未验证 |
| **4** | 等待 WAF 自动解封（原 P1） | ⏳ 明日 09:15 前一条 curl 判定 | 不投入即无法产出预测 |

**明确不建议**：为绕行而放宽 `screening.py` 的资金流硬约束（属方法论变更，等于无证据出结论）。

**本轮净增量**：① 推翻「数据中心 IP」错误结论（影响 P0 前提）；② 完成 9 项排除法定位真实根因；③ **首次确认可达的 ETF 主力净流入源（新浪 MoneyFlow）**，P2 前置条件由「不成立」转为「**已满足**」；④ 项目代码仍零改动，无预测产出。

---

## 九、19:54–20:1x 落地：直连问题结案 + 新浪回退上线（链路恢复）

### 9.1 直连 vs 代理同端点对照（回答用户「不开代理直连不行吗」）

测试方式：`urllib` + `ProxyHandler({})`（**完全绕开 env 代理**）对比 `ProxyHandler({http/https: 127.0.0.1:17891})`，同一批端点逐一比对。

| 端点 | 直连（无代理） | 经代理 17891 | 差异 |
|---|---|---|---|
| 国内出口 IP `myip.ipip.net` | `113.25.x.x`（中国电信·华北某地级市） | `113.25.x.x` | **无** |
| 东财 `push2 /api/qt/clist/get` | `RemoteDisconnected` | `RemoteDisconnected` | **无** |
| 东财 `push2his /api/qt/stock/fflow/daykline/get` | `RemoteDisconnected` | `RemoteDisconnected` | **无** |
| 东财 `datacenter-web /api/data/v1/get`（健康对照） | `200` | `200` | **无** |
| 新浪 `MoneyFlow.ssl_qsfx_zjlrqs` | `200`（532 B 真实数据） | `200`（532 B） | **无** |

**结论：逐字节相同。** 代理客户端对国内域名走 China-direct 规则，东财出口**一直是家宽 IP**，与「开不开代理」无关。⇒ **关代理不能绕开阻断**；「换非数据中心 IP」这一 P0 措施的前提本身就是错的（见 8.1）。

### 9.2 已落地的代码改动（P2 条件式授权已满足）

| 文件 | 改动 |
|---|---|
| `prediction_research/adapters/sina_etf.py`（**新增**） | 东财不可达时的替代快照源：列表 `Market_Center.getHQNodeData` + 逐只 `MoneyFlow.ssl_qsfx_zjlrqs`，4 线程并发，产出与东财**同 `schema_version: 2`** 的快照，并写入**机器可读 `fallback` 块**声明降级 |
| `prediction_research/adapters/eastmoney_etf.py` | `fetch_etf_snapshot` → 改为「先东财 → 失败**自动**回退新浪」，东财原逻辑移入 `_fetch_eastmoney_snapshot`；回退时向 `stderr` 打印降级警告 |
| `prediction_research/screening.py` | **零改动**（未放宽 `main_net_inflow` 硬约束） |
| `prediction_research/config/research.json` | **零改动** |

**单位校准（必须记住，否则数字错 1e4）**：新浪 `mktcap`/`nmc` 是**万元**（×1e4 → 元）；`volume` 是**股**（÷100 → 手）；`amount`/`trade` 已是元；`changepercent` 已是百分比；`ratioamount`/`r0_ratio` 是**比值**（×100 → 百分比）。

**降级声明（`fallback.degraded_fields` 已机器可读记录）**：
`volume_ratio`、`large_net_inflow(_pct)`、`medium_net_inflow(_pct)`、`small_net_inflow(_pct)` —— 新浪只有主力聚合与超大单（`r0`）两层，**大/中/小单缺失，一律置 `null`，严禁推断**。后果：`order_divergence`（超大单 vs 大单背离）本日全部为 `missing_data`，该项权重从加权分中剔除（`screening.py` 的既有设计）。

### 9.3 回退链路实测

| 指标 | 结果 |
|---|---|
| 全市场 ETF 列表 | **1676** 只（17 页 × 100） |
| 取得主力净流入 | **1676 / 1676（100%）** |
| 快照新鲜记录 | 1676（`quote_epoch` = 2026-09-22 15:00 Asia/Shanghai） |
| 过非资金流门槛 | 682（`min_amount=5e6`、`min_market_cap=5e7`、equity/commodity/multi_asset） |
| 快照耗时 | **123 秒**（4 线程，0.178 s/只） |
| 落库 | `ingest_etf_snapshot` 正常 |

**交叉校验**：新浪 `netamount / amount` 与 `ratioamount` 自洽（`510050`：`-2.0098亿 / 18.03亿 = -11.15%`，与 `ratioamount=-0.11146` 一致）；`trade=3.0150` 与当日东财快照 `3.015` 一致。

### 9.4 本次粗筛结果（682 合格 → Top 5）

| 排名 | 代码 | 名称 | 得分 | 主力净流入 | 主力净流入率 | 涨跌 | 成交额 |
|---|---|---|---|---|---|---|---|
| 1 | 589380 | 科创人工智能ETF富国 | 0.6828 | +982.9 万 | +58.35% | +3.55% | 0.17 亿 |
| 2 | 510330 | 沪深300ETF华夏 | 0.6554 | +4.58 亿 | +49.38% | +0.21% | 9.27 亿 |
| 3 | 589170 | 科创芯片设计ETF鹏华 | 0.6316 | +830.4 万 | +43.16% | +2.30% | 0.19 亿 |
| 4 | 588530 | 科创创业人工智能ETF中银证券 | 0.6237 | +556.0 万 | +44.25% | +2.41% | 0.13 亿 |
| 5 | 562570 | 信创ETF华夏 | 0.6185 | +651.5 万 | +30.18% | +1.93% | 0.22 亿 |

> 运行清单：`prediction_research/runs/cycle_20260922_195830_777755.json`；粗筛报告：`prediction_research/runs/screen_etf_flow_20260922_200053_071344.json`。
> **口径提醒**：主力净流入率 = 新浪 `netamount / amount`；此为**订单成交口径**，非 ETF 申赎、非披露持仓。第 1/3/4/5 名的成交额仅 0.13–0.22 亿，流动性因子得分低，其高排名主要由主力流入率与同伴相对强度贡献 —— **需在最终建议中按流动性折价**。

