# 每日 ETF 预测记录、归档与恢复指南

更新日期：2026-09-15。适用范围：`prediction_research` 的每日 `cycle`；历史大模型输出保留原文件存储方式。

## 1. 现有记录已经在哪里

用户记得“之前的 Agent 已经用文件存储”是准确的。本次沿用现有文件和 SQLite，增加每轮一致归档，无需迁移为其他数据库。

| 存储位置 | 已有内容和写入方式 | 主要限制 |
|---|---|---|
| `results/daily/` | `daily_predict.py` 写入 `ticker_YYYYMMDD_HHMM.md` 和同名 JSON；异常写 `error_YYYYMMDD_HHMM.log` | JSON 主要是标的、运行时间、分析日期、最终评级；Markdown 截取 `str(result)` 前 5000 个字符；分钟级文件名同分钟重跑可能覆盖 |
| `reports/` | `main.py` 通过 `graph.save_reports` 写入 `ticker_分析日期` 目录中的完整 Markdown 报告树 | 标的和日期重复时使用同一目录；不能直接把目录当作每次独立运行的不可变备份 |
| `prediction_research/runs/` | 粗筛 JSON、5/20 日预测 JSON、回测 JSON、资金流策略 JSON、综合 Markdown、每阶段 integration 报告、cycle JSON | 各文件分散保存；部分输入引用是绝对路径；复制单个报告不能恢复整个研究现场 |
| `prediction_research/state/research.db` | 冻结预测与完整 payload、实际结算、粗筛规则版本、选择记录、运行记录、标准证据、新闻和旧 Agent 分析映射 | 活动数据库会继续写入；直接复制正在使用的主 `.db` 可能漏掉 WAL 中已提交内容 |
| `prediction_research/datasets/market/` | `cache_代码.csv` 及行情采集 manifest | 最新缓存可能在下次采集被更新，旧报告中的 SHA 与当前缓存可能不同 |
| `prediction_research/datasets/etf_market/` | 带时间戳的 ETF 全市场快照及 `latest.json` | `latest.json` 指针会变化，备份必须找当轮实际引用的快照 |

2026-09-15 盘点时：`results/daily` 有 12 个文件（6 对 JSON/Markdown）；`runs` 有 307 个文件、约 69.2 MiB；状态库约 2.82 MiB。数量会随运行增长。这是盘点数据，不是保留配额。

**预测冻结与备份是两个层次。** 同标的、同特征日、同周期、同模型版本，原系统保留首次冻结的预测，随后结算更新实际结果；本次归档保存每个 cycle 当时看到的文件与数据库状态。重跑不能把修改后的模型冒充原预测。

## 2. 本次新增的归档结构

```text
prediction_research/archives/
  YYYY-MM-DD/                    # decision_at_utc 按配置 timezone 换算的日期
    <cycle_id>/                  # 每轮 UUID；同日多轮互不覆盖
      cycle.json                 # 归档前冻结的运行状态，避免自引用
      config.redacted.json       # 当轮有效配置，递归脱敏
      artifacts/                 # 当轮显式引用的 JSON、Markdown；a00002.json 等短文件名
      inputs/
        i00006.json              # 粗筛所引用的原始 ETF 快照
        i00007.csv               # 分类/预测/回测/资金流回测引用的行情 CSV
      state/research.db          # SQLite 一致性备份；需要时脱敏 runs.config_json
      manifest.json              # 文件清单、来源、哈希、大小、遗漏与转换说明
      manifest.sha256            # manifest 本身的 SHA256
```

临时写入目录为 `.p-<12位随机值>.partial`。全部文件写入、数据库校验、清单校验完成后，才在同一父目录内重命名为正式目录。失败时保留明确标记的临时目录，尽力写入 `FAILURE.json`；调用方得到异常，不能把临时目录当作成功备份。

归档文件采用短名，以减少 Windows 旧式 260 字符路径上限造成的失败。manifest 中的 `original_filename`、`source_path`、`recorded_source_path`、`archive_path` 提供完整映射。Markdown 内容保留原样，其原有绝对/相对链接不会自动改写，阅读或恢复时按清单定位文件。若工作区本身已接近路径上限，应将 `root_dir` 配置为更短的明确目录。

目录日期采用**决策日期**；行情日期仍以预测的 `feature_date` 和粗筛快照时间为准。周末运行、跨午夜运行可能与行情日期不同。时间戳必须包含时区；Windows 运行环境需要有效的 `tzdata`，项目 `.venv` 已具备。

### 默认配置

在 `config/research.json` 中：

```json
"archives": {
  "enabled": true,
  "root_dir": "archives",
  "include_inputs": true,
  "include_database": true
}
```

相对路径按 `prediction_research` 项目目录解析，`root_dir` 也可配置为明确的绝对路径。不要把密钥写进 JSON 配置；需要认证的配置仍应通过现有环境机制提供。

默认每轮 cycle 自动归档。单独运行 `predict` 仍按原逻辑写预测文件和冻结数据库；需要完整归档时使用 `cycle`。本次没有创建或修改操作系统计划任务，没有自动删除旧记录，也没有改动旧 `daily_predict.py` 的执行入口。

## 3. 完成状态怎样读

| 状态 | 含义 | 处理 |
|---|---|---|
| `complete` | 请求范围内的输出、引用输入和数据库均已归档，并通过文件校验 | 保存归档路径，加入每日核对 |
| `incomplete` | 已保存可用材料，但存在输入缺失、输入 SHA 与原记录不符、无状态库，或主动关闭输入/数据库备份 | 查看 manifest 的 `missing`；不得宣称可完整复现 |
| `disabled` | 配置关闭归档 | 属于显式关闭，不能计为备份成功 |
| `failed` | 磁盘、复制、数据库损坏、超时、ID 冲突或其他异常，未发布正式目录 | 保留原 runs 产物，检查 `.partial/FAILURE.json` 和 cycle 中的归档错误，修复后重试 |

`verify_archive` 的 `ok=true` 表示**归档中声明的文件字节完整**；只有 `complete=true` 才同时表示没有缺失来源。`incomplete` 目录可以具备完整的哈希校验，这是“完整保存了现有材料”，不等于“找到了所有原始输入”。CLI 输出中要同时检查这两个字段。

研究失败和备份失败也要分开：某轮预测因数据陈旧而未执行，仍可以完整保存“为什么未执行”的诊断档案；归档成功不代表预测成功，更不代表模型通过验证。

## 4. 每日按以下顺序操作

1. **确认当天运行入口使用研究 cycle。** 沿用已有调度方式调用 `python -m prediction_research.cli cycle`。本次默认候选规模及分类调整详见整改主文档。
2. **等待整轮结束。** cycle 按步骤持续写 JSON；最终写入报告、结算状态、数据库运行记录后归档。归档前异常仍在已有步骤记录中可诊断。
3. **读最新 cycle。** 核对 `cycle_id`、`decision_at_utc`、`data_mode`、`candidate_symbols`、5/20 日步骤状态、验证状态及 `archive`。
4. **核对当天归档目录。** 同一天可以有多个 UUID；保留全部。不要用同日最后一轮覆盖早先决策。
5. **校验归档。** 从仓库根目录执行：

   ```powershell
   & ./.venv/Scripts/python.exe -m prediction_research.cli verify-archive 'prediction_research/archives/YYYY-MM-DD/<cycle_id>'
   ```

6. **处理异常。** `missing` 中的 `source_hash_mismatch` 表示原报告记录的数据与当前可读取的文件不同；归档保留当前文件及双方哈希，不能把当前缓存当作原输入。`missing_or_disallowed_source` 表示路径不存在或超出白名单，不会追踪到 `.env` 或任意外部目录。
7. **登记异地备份结果。** 当前 `archives` 若仍位于 E 盘，只能防止误覆盖和便于审计，不能防止该硬盘损坏。后续可由现有备份软件复制完整日期目录到第二块磁盘或受控远端；复制后再次运行相同校验。目的地、加密方式和保留时间应另行确定。
8. **到期结算。** 后续交易日继续按现有 settle 逻辑写入实际结果，当天新归档保存最新状态；历史归档保持冻结。不要为补充收益字段修改早先归档文件。

## 5. 重试与历史补档

```powershell
& ./.venv/Scripts/python.exe -m prediction_research.cli archive-cycle 'prediction_research/runs/cycle_具体时间戳.json'
```

- 同一 cycle 的已完成归档先校验再复用；不会根据今天的缓存改写旧归档。
- API 对同一 ID、不同运行内容或配置的请求拒绝覆盖。自动归档记录的运行前 outcome 可通过 `archive.cycle_outcome_before_archive` 还原，避免归档不完整导致 cycle 标记变化破坏幂等性。
- 已有归档若损坏，重试也不会覆盖它。保留损坏样本，用独立新运行 ID 重新生成可诊断的材料；历史事实无法通过重跑恢复时必须标记缺失。
- 旧 Windows 绝对路径搬家后，输入只在当前配置的行情/ETF 快照目录查找同名文件，并核对原始 SHA；多个配置目录存在同名缓存时优先使用哈希匹配项。
- 分类所用历史 CSV 在粗筛当时冻结到 `datasets/market/snapshots/<SHA>/cache_代码.csv`，报告通过 `history_snapshot` 引用该副本；归档也收集未进入最终预测名单的分类输入。后续采集更新最新缓存不会改写分类依据。旧版仅有 `history_path/history_sha256` 的记录仍被检查，缺失或改写时明确 incomplete。
- 历史补档不等于历史重演。旧缓存已被覆盖、旧模型代码未保存、当轮有效配置没有留存时，当前配置和数据只能用于补档，不能证明当时使用的全部输入。不要为了得到 `complete` 删除 SHA 约束。
- 现有 `results/daily` 六组旧 Agent 文件仍保留原处；本次没有扫描复制整个 reports、news、外部 Agent 工作区。需要迁移旧系统时，先设计独立的 legacy 索引并保留原文件名、原字节及来源。

## 6. 恢复演练步骤

1. 选择一轮 `complete` 归档，先执行 `verify-archive`，保存结果。
2. 将该 UUID 目录整体复制到一个**新的空临时目录**。不要直接恢复覆盖活动状态库。
3. 对复制后的目录再次执行 `verify-archive`。要求 `ok=true`、`complete=true`，文件数量和清单一致。
4. 用只读 SQLite 连接打开恢复目录的 `state/research.db`，执行 `PRAGMA integrity_check`，结果必须是 `ok`。
5. 查询 `predictions` 与 `prediction_payloads`，核对归档预测 JSON 中的 `prediction_id`、标的、日期、周期、概率、模型版本、冻结时间；查询实际收益字段确认恢复的是归档时状态。
6. 核对输入 CSV 和 ETF 快照的 SHA 与 manifest 相同，并检查是否存在 `expected_source_sha256` 不一致条目。
7. 如需要重跑研究，在另一份测试配置中将 market、runs、state 等路径映射到恢复目录及新的临时输出目录。按 manifest 的 `original_filename` 在测试数据目录重新建立 `cache_代码.csv` 等原命名副本；先检查恢复目标仍处于指定目录，再复制，避免直接执行源路径。配置快照和报告保留原路径用于审计，不会自动重写所有内部引用。
8. 记录演练日期、所用归档、校验结果、数据库查询结果和任何不能复现的条件。建议每月一次，并在修改归档格式、数据库结构或备份目的地后补做。

本次自动化测试已执行第 1–6 步的核心恢复校验；没有把生产数据库覆盖为历史状态。

## 7. 测试验收表

运行命令：

```powershell
& ./.venv/Scripts/python.exe -m unittest tests.test_archives -v
```

| 范围 | 验收重点 |
|---|---|
| 正常归档/恢复 | 文件和 SQLite 独立恢复；删除或更新源文件后恢复包仍可查询和校验 |
| SQLite 一致性 | 已提交但仍在 WAL 的预测进入副本；副本采用 DELETE journal 模式，自包含 |
| 配置保护 | 嵌套 api_key/token/password 脱敏；URL 移除认证、query、fragment；原活动库不改 |
| 重复运行 | 相同请求复用，改内容/配置拒绝同 ID 覆盖；归档 outcome 回填后仍幂等 |
| 缺失/变更 | 源 CSV 缺失、原 SHA 不匹配、数据库缺失都输出 incomplete；不能假报 complete |
| 损坏 | 归档文件改动、丢失、manifest 改动均被检测；坏 SQLite 不发布 |
| 运行失败 | 失败 cycle 的诊断被保留；模拟复制 I/O 错误只留下 partial 和失败标记 |
| 路径 | 非法 ID、越界 artifact、manifest `../` 路径被拒绝；旧 Windows 缓存路径可安全迁移；140 字符工作区加长原文件名仍可完成短路径归档 |
| 关闭配置 | 归档关闭返回 disabled；关闭输入或数据库快照明确标为 incomplete |

自动化测试使用合成数据和临时目录，不调用行情网络、不运行 LLM、不写生产 state。实际磁盘断电、目标磁盘满、远程备份中断和多进程并发运行属于后续运维演练，不能把模拟复制失败等同于已经验证所有硬件故障。

## 8. 后续建议，按优先级执行

1. **持续检查每日归档完整性。** 报告审核把数据完整性、模型验证和归档状态分别统计；分类数量或 BUY 数量不能替代这些检查。
2. **在现有调度器中加单实例约束和退出码检测。** 归档目录不会覆盖，但全研究流程的 latest 指针、CSV 采集和 state 更新目前不等于支持任意并发 cycle。
3. **建立异地副本与恢复记录。** 先明确备份目标及容量，再配置复制。归档不自动上传任何材料。
4. **保留对应代码。** cycle已记录Python、平台和研究模块源码SHA，预测已保存模型参数；仍建议保留对应Git修订与未提交补丁。源码哈希不等于源码备份，仅有配置和输入不能保证跨版本数值复现。
5. **升级旧 daily 脚本。** 将分钟级文件名升级为秒/微秒加 run ID，保存完整结构化结果并使日志目录在 FileHandler 初始化前建立；接入统一运行索引。此项属于旧单标的大模型流程，当前补丁未修改。
6. **再确定保留策略。** 建议保留全部原始预测与对应实际结算，较大的重复输入可后续改为按 SHA 去重；实施去重前必须支持缺块检测和恢复测试。当前版本不自动清理。

### 安全与可复现边界

SHA256 清单用于检测意外损坏；如果攻击者同时重写数据、manifest 和其哈希，则需要外部签名或独立可信索引才能识别。当前没有提供该保证。

数据库副本通过 SQLite 的 online backup API 保证一致读，之后仅在发现需要脱敏的 `runs.config_json` 时转换副本，并记录转换表与行数。manifest 的 SHA 针对**归档副本**，不宣称源数据库与副本字节相同。

完整性只覆盖 manifest 声明的研究材料：当轮显式输出、引用市场输入、脱敏配置和状态库。原始新闻网页、任意外部 Agent 目录、Python 环境、整个 Git 仓库不在递归复制范围内；证据库已存结构化证据及来源引用，完整第三方证据链的原始附件归档需要后续明确白名单实现。
