# 东财接口连通性诊断报告

生成时间：2026-10-05
诊断对象：`*.push2*.eastmoney.com` 全线不可达问题
结论性质：**服务端限制，本地不可修复；已用多源降级方案解决**

---

## 一、根因结论

**不是代理问题。** 在同一代理出口（`http://127.0.0.1:11625`）下：

| 目标站点 | 结果 |
|---|---|
| `www.baidu.com` | HTTP 200 |
| `finance.sina.com.cn` | HTTP 200 |
| `q.stock.sohu.com` | HTTP 200 |
| `web.ifzq.gtimg.cn`（腾讯行情） | **HTTP 200，数据完整** |
| `money.finance.sina.com.cn`（新浪行情） | **HTTP 200，数据完整** |
| `push2his.eastmoney.com` | **被 RST** |
| `82.push2.eastmoney.com` | **被 RST** |
| `push2.eastmoney.com` | **被 RST** |
| `push2delay.eastmoney.com` | **被 RST** |
| `48.push2 / 1.push2.eastmoney.com` | **被 RST** |

代理是通的，被拒的只有东财。这是东财侧对本网络出口的单方面限制，与本机配置无关。

### 排除的三个错误方向

1. **不是代理配置问题** —— 同一代理下腾讯、新浪、百度、搜狐全部正常。
2. **不是 TLS 指纹 / 反爬问题** —— 用 `curl_cffi` 伪装 `chrome` / `chrome124` / `edge` / `safari` 真实浏览器指纹，全部仍返回 `curl: (56) Connection closed abruptly`。
3. **不是 DNS 或 IP 问题** —— `nslookup` 正常解析出多条 A 记录，用 `--resolve` 强制指定不同 IP 均被拒。

失败位置精确定位在 **TLS 重协商之后**（curl -v 显示 `renegotiating SSL/TLS connection` 反复后断开），属于传输层被主动切断，而非应用层返回错误。这条链路不要再投入时间。

---

## 二、解决方案：多源自动降级

已在项目中落地 `prediction_research/etf_data_source.py`：

```
降级链：eastmoney → sina → tencent
```

- 东财保留在链首，**网络环境一旦恢复（换网 / 换出口 IP）会自动切回，无需改代码**
- 每源失败后 0.8 秒内快速失败（实测），不会拖慢流程
- 输出统一为 `date, open, high, low, close, volume, amount`，与现有 `datasets/market/cache_*.csv` **格式完全一致**

### 实测结果（21 只全池）

```
成功 21/21   全部经 sina 源
```

| ETF | 行数 | 历史区间 |
|---|---:|---|
| 510050 | 5253 | 2005-02-23 ~ 2026-09-30 |
| 159915 | 3596 | 2011-12-09 ~ 2026-09-30 |
| 510300 | 3488 | 2012-05-28 ~ 2026-09-30 |
| 162411 | 3329 | 2012-06-08 ~ 2026-09-30 |
| 518880 | 3205 | 2013-07-29 ~ 2026-09-30 |
| …其余 16 只 | 622 ~ 3291 | 均覆盖至 2026-09-30 |

### 口径一致性校验（关键）

换数据源最大的风险是复权口径漂移导致历史回测结果失真。三线交叉验证：

| 日期 | 原 cache（东财） | 腾讯 | 新浪 |
|---|---:|---:|---:|
| 2022-07-15 | 3.6220 | 3.6220 | 3.6220 |
| 2024-01-02 | 4.6660 | 4.6660 | 4.6660 |
| 2026-09-29 | 8.5190 | 8.5190 | 8.5190 |
| 2026-09-30 | 8.6330 | 8.6330 | 8.6330 |

偏差全部 **0.0000**，OHLC 四值逐一对齐。数据源可以安全切换。

---

## 三、踩到的一个隐蔽陷阱（已修复并注释）

腾讯 `web.ifzq.gtimg.cn` 的 K 线返回字段顺序是：

```
date, open, close, high, low, volume
```

**`close` 在第 3 位，不是第 5 位**。这与常见 OHLC 顺序不同。若按 OHLC 解析，会把 `low` 当成 `close`，实测偏差达 **0.07 元**（2026-09-30：8.562 vs 正确值 8.633）。

该偏差只在跨源交叉校验时才会暴露——单看一个源完全发现不了。已在代码中固化注释。

腾讯源另有一个限制：返回的是"截至 end 的最近 N 条"，单次上限约 640-1150 行，必须**向前推进 end** 分段拉取，已实现（修复后 518880 取到完整 3205 行）。

---

## 四、意外收获（需要你决策）

新浪源返回的是**全历史**，比现有 cache 深得多：

| | 现有 cache | 新浪源 |
|---|---:|---:|
| 518880 行数 | 1023 | **3205** |
| 起始日期 | 2022-07-15 | **2013-07-29** |
| 510050 行数 | ~1023 | **5253（2005 年起）** |

即历史样本量可扩大 **3-5 倍**。

**但这带来一个需要你决定的取舍**：

- 回测结论目前建立在 1023 行样本上（`commodity h20` 的 Brier skill +0.00528 为正）
- 样本扩到 3200+ 行后，**该结论可能变化**——样本量增加会让置信区间收窄，但如果早期年份的行情特征不同，也可能稀释近期规律
- 现有 cache 是历史回测的基准，**不建议未经确认就覆盖**

建议做法：先用新数据跑一版对照回测，看 `commodity h20` 的结论是否稳定。若结论不变，说明它更可靠；若翻转，说明原结论是样本期的产物。

---

## 五、使用方式

```python
from prediction_research.etf_data_source import fetch_etf_daily, refresh_pool, probe_sources

df = fetch_etf_daily("518880")                    # 自动降级
df = fetch_etf_daily("518880", source="sina")     # 指定源
probe_sources()                                    # 诊断当前网络哪些源可用
report = refresh_pool(codes, "datasets/market")    # 批量刷新写盘
```

命令行：

```bash
python prediction_research/etf_data_source.py --probe          # 只探测
python prediction_research/etf_data_source.py --code 518880    # 取单只
python prediction_research/etf_data_source.py --pool           # 刷新全池
```

---

## 六、仍依赖东财的部分（不受影响 / 待观察）

- `daily_predict.py` 的 `tool_vendors` 中 `news_data` / `fundamental_data` 指向东财 —— 走的是 LLM 工具调用链路，是否受影响需另行验证
- `stock_news_aggregator/fetcher.py` 的东财新闻 URL —— 经模型联网抓取，非本机直连，暂不受本次问题影响
