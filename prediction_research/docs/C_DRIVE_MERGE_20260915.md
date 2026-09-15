# C 盘修改合并记录（2026-09-15）

## 合并方向

来源：`C:\Users\Administrator\TradingAgents`，HEAD `53d9e86`。
目标：`E:\Trade\TradingAgents`，原 HEAD `3620f2f`，包含本次对话尚未提交的因子和 agents 证据传递修改。

使用共同历史 `b21ad8e` 对比主项目；研究模块是两边分别引入的目录，使用 C 盘首次引入版本 `5a6bdb7` 作为该模块的比较基线。先生成三方合并预览，再逐项处理冲突；没有覆盖整个目录或重置 Git 历史。

合并前涉及文件备份：`reports/c_drive_merge_20260915/destination_before.zip`。三方原文和处理清单保存在同目录的 `ours`、`base`、`theirs`、`merged` 与 `manifest.json`；这些是合并过程材料，不是最终代码副本。

## 已合并的 C 盘功能

- 东方财富 ETF 基本面／资金流／新闻、akshare 宏观适配和国内行情路由。
- `run_research.py` 包装入口、脚本环境配置和代理旁路；每日默认标的中的煤炭 ETF 改动。
- 主力、超大单、大单、中单、小单的净额和净占比：采集、数据库兼容迁移、冻结候选、报告和 agents 证据全链路保留。
- 基金公司名称剥离、同指数／同赛道去重、取消粗筛数量上限。
- 来源中的历史记忆时点过滤、checkpoint 生命周期修复、辩论开场处理、数据日期窗口、模型输出上限等代码与相应测试。
- C 盘 `TEST_RESULTS.md` 已找到并读取；原文保存在 `reports/c_drive_merge_20260915/TEST_RESULTS.source.md`，根目录归纳更新为合并后口径。

## 冲突处理与当前配置

| 内容 | 合并后行为 |
|---|---|
| 粗筛 | `top_n=null`，全部合格候选按同指数／同赛道每组最多1只；显式 `--limit` 可限制 |
| 量化预测 | 保留 E 盘 `prediction_top_n=20`；`cycle --top` 可覆盖 |
| LLM预算 | 保留 `agent_top_n=5`、`skip_tradingagents=true` |
| 因子 | 保留 E 盘六因子及缺失值处理；中小单作为证据，未重复加权 |
| 评级 | 保留五档交易员枚举，合入技术报告价格依据和避免发言顺序偏差的提示词 |
| 报告 | 使用 E 盘单轮冻结上下文渲染器，移植 C 盘五档资金流表，缺失值不填零 |
| 存储 | 显式 SQL 列名与幂等迁移，保留冻结记录及校准修复 |
| 规则版本 | 算法版本升为5，区分新去重及不限数量规则 |

源项目的实际 `.env`、虚拟环境、日志、运行缓存、数据库及结果目录未复制；目标运行数据保持原状。`.env.example` 仅增加输出 token 上限的说明。模型名称保留来源的代码配置，未进行在线可用性确认。

## 验证

新增测试覆盖不限数量筛选与冻结、显式数量上限、历史回测兼容、同赛道择优、五档字段迁移及幂等入库。保留并运行两边的既有测试。

首轮完整测试中的真实 DeepSeek API 用例受环境网络限制失败；该用例不是离线验收依据。离线测试命令为：

```powershell
.venv/Scripts/python.exe -m pytest prediction_research/tests tests -m "not integration" -q --tb=short --disable-warnings
```

结果日志：`reports/c_drive_merge_20260915/pytest_offline.txt`。尚未刷新行情或进行在线 agents 重跑，不声称合并提高了收益或预测准确率。

最终离线结果：**851 passed，71 subtests passed，1 skipped，1 deselected，72.34秒**。跳过项为未安装可选依赖 `langchain_aws`；排除项为真实 DeepSeek API 联网测试。`git diff --check` 未发现空白错误。
