# S5—S7 文件输入

从本目录运行：

```powershell
.venv/Scripts/python.exe scripts/import_strategy_inputs.py --schema
.venv/Scripts/python.exe scripts/import_strategy_inputs.py --kind s5 --input E:/data/earnings.json
.venv/Scripts/python.exe scripts/import_strategy_inputs.py --kind s6 --input E:/data/borrow.json
.venv/Scripts/python.exe scripts/import_strategy_inputs.py --kind s7 --input E:/data/options.json
```

文件为 UTF-8 JSON，顶层严格包含 `evidence_kind`、`source_id`、`records`。`evidence_kind` 为 `real_data` 或 `synthetic_fixture`，`source_id` 为实际提供方/导出批次标识，`records` 必须非空。完整字段由 `--schema` 输出，数值使用 JSON 数字，布尔值使用 JSON true/false，所有时点须含时区。不要将本次下载时间写成历史 `available_at`。

| 类型 | 每条记录 | 执行的校验 | 仍未完成 |
| --- | --- | --- | --- |
| S5 | event、history、decision | EPS口径、发布前60分钟预期可得、之前8季度至少6个有效误差、SUE、交易日入退场时间 | 全市场事件百分位、历史股票池、持仓成交及公司行动集成 |
| S6 | evidence、at、requested_shares | 可借数量、观察/可得/失效时间、费率、召回 | 券商真实性核验、融资、分红支付与成交账本集成 |
| S7 | legs、quotes、at、nav、cash、underlying_shares、underlying_price、variant | 已有合约字段、报价年龄与价差、多腿同步、到期、保护预算、覆盖股数与delta | 证券主表绑定、真实指派、连续滚仓/退出状态机 |

每次调用写入独立 `outputs/strategy_import_*/report.json`，含输入SHA-256、逐条通过/失败原因及文件清单。失败不会静默丢行；不修改原文件，也不下单。

返回码：0表示声明为真实数据的全部记录通过现有校验；2表示文件/结构错误、空数据或任一记录失败；3表示合成算例验证。即使返回0，来源真实性仍是用户声明，`strategy_ready` 始终为 false，不能据此宣称完整策略已完成。没有合格真实文件时不制造成功导入记录。
