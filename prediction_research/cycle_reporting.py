"""Reports bound to one cycle and canonical frozen predictions."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import resolve_project_path
from .flow_context import FLOW_BASIS, finite
from .screening import _snapshot_path
from .store import connect, load_frozen_prediction
from .order_divergence import order_divergence
from .opportunity import ASSET_LABELS, CATEGORY_LABELS


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def _cell(value):
    return str(value).replace("|", "／").replace("\n", " ") if value is not None else "未记录"


def _amount(value):
    value = finite(value)
    return "数据不足" if value is None else f"{value / 10000:,.2f} 万元"


def _pct(value):
    return "数据不足" if finite(value) is None else f"{value:.2f}%"


def _observation_lines(observation: dict, selected: list[dict], prediction_symbols: set[str], heading: str) -> list[str]:
    coverage = observation["coverage"]
    lines = ["", f"## {heading}", "",
             f"观察时点：{observation['decision_at_utc']}；独立截面：`{observation['source_snapshot'].get('sha256')}`。",
             "以下是可复核的研究分类。单日资金不等于买卖信号，不证明连续流入、趋势或回调；风险榜不等于减持指令。",
             "分类规则尚未通过回测；数据时效、历史可得性和模型验证分别展示。仅有文件修改时间的历史缓存不能证明历史时点的原始可得性。",
             f"全量合格 {coverage['eligible']} 只；可做历史六类观察 {coverage['classified_with_history']} 只；数据不足 {coverage['insufficient_data']} 只；机会观察 {coverage['opportunity_watch']} 只；风险观察 {coverage['risk_watch']} 只（允许交叉）。",
             "", "| 历史状态 | 数量 |", "|---|---:|"]
    lines.extend(f"| {CATEGORY_LABELS.get(key, key)} | {value} |" for key, value in observation["category_counts"].items())
    lines.extend(["", "| 资产类别（名称推断） | 数量 |", "|---|---:|"])
    lines.extend(f"| {ASSET_LABELS.get(key, key)} | {value} |" for key, value in observation["asset_class_counts"].items())
    by_symbol = {row["symbol"]: row for row in observation["rows"]}
    if selected:
        lines.extend(["", f"### 全部冻结候选的观察覆盖（{len(selected)}只）", "",
                      "已生成本轮预测与仅粗筛观察分开标记；未预测不等于 HOLD。", "",
                      "| ETF | 名称 | 资产类别 | 单日观察 | 历史状态 | 历史数据质量 | 本轮预测 |",
                      "|---|---|---|---|---|---|---|"])
        for candidate in selected:
            row = by_symbol.get(candidate["symbol"])
            if row is None:
                lines.append(f"| {candidate['symbol']} | {_cell(candidate['name'])} | 未记录 | 本截面未覆盖 | 数据不足 | 不回填 | {'已生成' if candidate['symbol'] in prediction_symbols else '仅粗筛观察'} |")
                continue
            quality = row["quality"]
            lines.append(f"| {row['symbol']} | {_cell(row['name'])} | {row['asset_class_label']} | {row['snapshot_label']} | {row['category_label']} | `{quality['history_status']}` / `{quality['history_availability']}` | {'已生成' if row['symbol'] in prediction_symbols else '仅粗筛观察'} |")
    for key, label in (("opportunity_watch", "机会观察榜"), ("risk_watch", "弱势与资金流出风险观察榜")):
        lines.extend(["", f"### {label}（最多{observation['watchlist_top_n']}只）", "",
                      "| ETF | 名称 | 资产类别 | 单日观察 | 历史状态 | 净流入占比 | 是否冻结候选 |",
                      "|---|---|---|---|---|---:|---|"])
        for symbol in observation[key]:
            row = by_symbol[symbol]
            lines.append(f"| {row['symbol']} | {_cell(row['name'])} | {row['asset_class_label']} | {row['snapshot_label']} | {row['category_label']} | {_pct(row['evidence']['main_net_inflow_pct'])} | {'是' if row['in_frozen_selection'] else '否'} |")
        if not observation[key]:
            lines.append("| — | 当前没有满足观察规则的标的 | — | — | — | — | — |")
    lines.extend(["", "### 各类后续复核条件", "", "| 类别 | 继续观察的条件 | 重新分类条件 |", "|---|---|---|"])
    seen = set()
    for row in observation["rows"]:
        if row["category"] not in seen:
            lines.append(f"| {row['category_label']} | {_cell(row['followup_condition'])} | {_cell(row['invalidation_condition'])} |")
            seen.add(row["category"])
    lines.extend(["", f"规则版本：`{observation['rule_version']}`；完整阈值、全量合格行、原因码和来源哈希已写入本轮筛选 JSON。", observation["caveat"]])
    return lines


def _discrimination_label(row: dict) -> str:
    status = row.get("discrimination_status", row.get("model_diagnostics", {}).get("discrimination_status", "legacy_not_recorded"))
    labels = {"baseline_fallback": "历史基准回退", "base_rate_only": "历史基准回退",
              "baseline_only": "历史基准回退", "model_differentiated": "有区分（有效性另看验证）",
              "calibration_collapsed_to_base_rate": "历史基准回退",
              "uncalibrated": "未校准", "uncalibrated_insufficient_data": "未校准",
              "discriminating": "有区分（有效性另看验证）", "model_discriminates": "有区分（有效性另看验证）"}
    return labels.get(status, str(status))


def build_research_report(cfg: dict, context: dict | None = None) -> Path:
    runs = resolve_project_path(cfg, cfg["runs_dir"])
    runs.mkdir(parents=True, exist_ok=True)
    if context is None:
        paths = sorted(runs.glob("cycle_*.json"), reverse=True)
        if not paths:
            raise ValueError("no cycle context; run cycle first")
        context = _read(paths[0])
    artifacts = dict(context.get("artifacts", {}))
    for step in context.get("steps", []):
        value = step.get("result")
        if step["name"] == "screen_etfs" and isinstance(value, dict):
            artifacts.setdefault("screen", value.get("path"))
        elif step["name"].startswith(("predict_", "backtest_")) and isinstance(value, str):
            artifacts.setdefault(step["name"], value)
        elif step["name"] == "etf_evidence_integration" and isinstance(value, dict):
            artifacts.setdefault("integration", value.get("summary_path"))
    screen = _read(artifacts.get("screen"))
    predictions = []
    with connect(resolve_project_path(cfg, cfg["state_db"])) as connection:
        ids = set()
        for name, path in artifacts.items():
            if name.startswith("predict_"):
                for row in _read(path)["predictions"]:
                    if row["prediction_id"] not in ids:
                        predictions.append(load_frozen_prediction(connection, row["prediction_id"]))
                        ids.add(row["prediction_id"])
    lines = ["# ETF cycle 预测与资金流报告", "",
             f"生成：{datetime.now(timezone.utc).isoformat()}", "",
             f"本轮：`{context.get('cycle_id', context.get('started_at_utc'))}`；研究流程结果（归档前）：`{context.get('outcome', 'running')}`。",
             f"最终运行及备份状态以本轮 cycle JSON 的 outcome / archive 为准：`{context.get('result_path', '未记录')}`。",
             f"数据模式：`{context.get('data_mode', 'legacy_not_recorded')}`；决策时间：{context.get('decision_at_utc', '旧版本未记录')}。",
             f"深度预测范围：粗筛前 {context.get('requested_prediction_top_n', '旧版本未记录')} 名；本轮实际候选 {context.get('prediction_candidate_count', len(context.get('candidate_symbols', [])))} 只。", "",
             "## 执行与等待", "", "| 步骤 | 状态 |", "|---|---|"]
    lines.extend(f"| {_cell(step['name'])} | {_cell(step['status'])} |" for step in context.get("steps", []))
    if "prediction_eligible_symbols" in context:
        lines.extend(["", f"预测覆盖：申请候选 {context.get('prediction_candidate_count', 0)} 只；数据检查后可预测 {len(context['prediction_eligible_symbols'])} 只；本轮实际引用冻结预测 {len({row['symbol'] for row in predictions})} 只 / {len(predictions)} 条（标的×周期）。"])
    if context.get("prediction_exclusions"):
        reasons = {"missing_history": "缺少可用历史日线", "stale_or_future_history": "历史日线过期或含未来日期",
                   "insufficient_feature_history": "计算特征的历史长度不足", "candidate_history_dates_disagree": "历史行情日与本轮最新日期不同",
                   "pooled_model_requires_two_candidates": "联合模型至少需要两只可预测候选", "current_candidate_refresh_failed": "本轮历史刷新失败",
                   "readiness_check_failed": "数据就绪检查失败"}
        lines.extend(["", "### 预测排除与数据等待原因", "", "| ETF | 原因 | 最新行情日 |", "|---|---|---|"])
        for item in context["prediction_exclusions"]:
            lines.append(f"| {_cell(item.get('symbol', '本轮'))} | {_cell(reasons.get(item['reason'], item['reason']))} | {_cell(item.get('last_date'))} |")
    if context.get("data_readiness"):
        lines.extend(["", f"数据就绪：`{context['data_readiness']}`。"])
    lines.extend(["", "## 资金流口径", "", FLOW_BASIS + "。",
                  "主力资金流和超大单/大单背离用于候选粗筛；可用因子按配置权重归一化。当前上涨概率仍由量价模型产生，资金流不直接改变该概率。"])
    if screen:
        lines.extend(["", f"本轮引用截面抓取于：{screen['source_snapshot'].get('retrieved_at_utc')}；源记录 {screen['universe_records']} 条，合格 {screen['eligible_records']} 条，候选 {len(screen['selected'])} 条。",
                      f"筛选引用：`{artifacts['screen']}`；同日冻结复用：{screen.get('reused_frozen_screen', False)}。",
                      "", "## 本轮候选资金流", "", "| ETF | 名称 | 净流入金额 | 净流入占比 | 超大单/大单背离 | 背离因子 | 粗筛分 |", "|---|---|---:|---:|---|---:|---:|"])
        for row in screen["selected"]:
            divergence = row.get("order_divergence", {})
            lines.append(f"| {row['symbol']} | {_cell(row['name'])} | {_amount(row.get('main_net_inflow'))} | {_pct(row.get('main_net_inflow_pct'))} | {_cell(divergence.get('signal', 'no_data'))} | {_cell(divergence.get('factor'))} | {row['screen_score']:.4f} |")
        prediction_symbols = {row["symbol"] for row in predictions}
        if screen.get("opportunity_observation"):
            lines.extend(_observation_lines(screen["opportunity_observation"], screen["selected"], prediction_symbols,
                                            "冻结筛选时的分类与覆盖"))
        else:
            lines.extend(["", "旧冻结筛选未记录分类，不使用本轮数据补写其历史判断。"])
        if screen.get("current_opportunity_observation"):
            lines.extend(_observation_lines(screen["current_opportunity_observation"], screen["selected"], prediction_symbols,
                                            "本轮独立观察（不属于旧冻结决策依据）"))
        try:
            directory = resolve_project_path(cfg, cfg["etf_market"]["snapshot_dir"])
            raw = _read(_snapshot_path(directory, screen["source_snapshot"]))["records"]
            valid = [row for row in raw if finite(row.get("main_net_inflow")) is not None]
            lines.extend(["", "## 本轮来源截面的金额排行", "", "源截面全量排行，包含未通过粗筛的产品；这是观察数据，不等于冻结预测依据。",
                          "", "| 方向 | ETF | 名称 | 净流入金额 |", "|---|---|---|---:|"])
            for label, group in (("净流入", sorted([row for row in valid if row["main_net_inflow"] > 0], key=lambda row: row["main_net_inflow"], reverse=True)[:5]),
                                 ("净流出", sorted([row for row in valid if row["main_net_inflow"] < 0], key=lambda row: row["main_net_inflow"])[:5])):
                for row in group:
                    lines.append(f"| {label} | {row['symbol']} | {_cell(row['name'])} | {_amount(row['main_net_inflow'])} |")
            divergence_rows = [(row, order_divergence(row, cfg["etf_market"]["screen"])) for row in raw]
            divergence_rows = [(row, signal) for row, signal in divergence_rows
                               if signal["signal"] in {"bullish_divergence", "bearish_divergence"}]
            lines.extend(["", "## 本轮来源截面的背离信号", "",
                          "全源背离观察榜，不等于最终候选；按因子绝对值排序。", "",
                          "| 信号 | ETF | 名称 | 超大单占比 | 大单占比 | 差值 | 因子 |",
                          "|---|---|---|---:|---:|---:|---:|"])
            for row, signal in sorted(divergence_rows, key=lambda item: abs(item[1]["factor"]), reverse=True)[:10]:
                lines.append(f"| {signal['signal']} | {row['symbol']} | {_cell(row['name'])} | {_pct(signal['super_large_net_inflow_pct'])} | {_pct(signal['large_net_inflow_pct'])} | {signal['spread_pct_points']:.2f} | {signal['factor']:.4f} |")
            if not divergence_rows:
                lines.append("| no_data | — | 当前截面没有可确认的背离分项 | — | — | — | — |")
        except (OSError, ValueError, KeyError):
            lines.extend(["", "金额排行：原始截面不可读，未使用其他运行替代。"])
    lines.extend(["", "## 本轮引用的冻结预测", "", "| ETF | 行情日 | 周期 | 上涨概率 | 单日资金等级 | 5日资金等级 | 质量状态 | 冻结时间 |", "|---|---|---:|---:|---|---|---|---|"])
    for row in predictions:
        periods = row.get("fund_flow", {}).get("flow_periods", {})
        lines.append(f"| {row['symbol']} | {row['feature_date']} | {row['horizon']}日 | {row['probability_up']:.2%} | {_cell(periods.get('single_day', {}).get('grade', 'legacy_not_recorded'))} | {_cell(periods.get('five_observation_days', {}).get('grade', 'legacy_not_recorded'))} | {row['probability_status']} | {row['frozen_at_utc']} |")
    if not predictions:
        lines.extend(["", "本轮没有可引用的冻结预测；未展示其他运行的预测补位。"])
    else:
        lines.extend(["", "### 概率区分能力与验证状态", "",
                      "回退历史上涨比例时，不代表各 ETF 的机会恰好相同；原始概率存在差异也不证明有效。分类和概率均不自动转成买卖指令。", "",
                      "| ETF | 周期 | 原始上涨概率 | 最终上涨概率 | 区分状态 | 模型验证状态 |", "|---|---:|---:|---:|---|---|"])
        for row in predictions:
            raw = finite(row.get("raw_probability_up"))
            lines.append(f"| {row['symbol']} | {row['horizon']}日 | {f'{raw:.2%}' if raw is not None else '旧记录未提供'} | {row['probability_up']:.2%} | {_cell(_discrimination_label(row))} | {_cell(row['probability_status'])} |")
    lines.extend(["", "## 每只 ETF 预测内的资金流依据", ""])
    for row in predictions:
        flow = row.get("fund_flow", {})
        if not flow:
            old = next((e for e in row.get("evidence", []) if e.get("type") == "money_flow_coarse_screen"), {})
            flow = {**old, "status": "legacy_time_not_verified", "used_for_probability": False}
        periods = flow.get("flow_periods", {})
        single = periods.get("single_day", {})
        five = periods.get("five_observation_days", {})
        lines.extend([f"### {row['symbol']} · {row['horizon']} 日 · {_cell(row.get('name', row['symbol']))}", "",
                      f"预测 ID：`{row['prediction_id']}`；决策时间：{row.get('decision_at_utc', '旧记录未提供')}。",
                      f"可用状态：`{flow.get('status', 'no_data')}`；净流入 {_amount(flow.get('main_net_inflow'))}；占比 {_pct(flow.get('main_net_inflow_pct'))}。",
                      f"观察时间：{flow.get('observed_at_utc', '未记录')}；首次可得：{flow.get('available_at_utc', '未记录')}。",
                      f"资金与价格方向：`{flow.get('price_flow_alignment', 'unknown')}`；资金流对粗筛总分贡献：{flow.get('screen_flow_contribution', '未记录')}。",
                      f"超大单/大单背离：`{flow.get('order_divergence', {}).get('signal', 'no_data')}`；因子 {flow.get('order_divergence', {}).get('factor', '数据不足')}；对粗筛总分贡献：{flow.get('screen_order_divergence_contribution', '数据不足')}。",
                      f"单日资金：`{single.get('grade', '数据不足')}` / `{single.get('direction', 'unknown')}`；净额 {_amount(single.get('net_inflow'))}；净流入比 {_pct(single.get('net_inflow_ratio_pct'))}。",
                      f"5日资金：`{five.get('grade', '数据不足')}` / `{five.get('direction', 'unknown')}`；累计净额 {_amount(five.get('net_inflow_sum'))}；累计净流入比 {_pct(five.get('net_inflow_ratio_pct'))}；覆盖 {five.get('observed_days', 0)}/5 个观察日（流入 {five.get('inflow_days', 0)}、流出 {five.get('outflow_days', 0)}）。",
                      "参与概率计算：否。金额与占比不是上涨概率。", ""])
        window = flow.get("history_windows", {}).get("20", {})
        lines.append(f"- 20 个观察日参考：{_amount(window.get('net_inflow_sum'))}；已覆盖 {window.get('observed_days', 0)} 日。")
        lines.extend(["", "窗口按已观测日期计数，不保证交易日连续；不足窗口或有缺值时不补零。", ""])
    lines.extend(["## 本轮模型验证", "", "| 周期 | 样本 | Brier | 历史概率 Brier | 发布门 |", "|---|---:|---:|---:|---|"])
    from .pipeline import validation_metrics
    for horizon in cfg["horizons"]:
        backtest = _read(artifacts.get(f"backtest_{horizon}d"))
        if not backtest:
            continue
        metric = backtest["metrics"]
        validation = context.get("validation", {}).get(str(horizon)) or validation_metrics(cfg, metric)
        lines.append(f"| {horizon}日 | {metric.get('rows', 0)} | {metric.get('brier')} | {metric.get('historical_rate_brier')} | {'通过' if validation['passed'] else '未通过'} |")
        lines.append(f"\n{horizon}日核验：{validation['reason']}；覆盖 {metric.get('unique_dates', '旧版本未记录')} 个日期。通过这里只表示条件模型质量门，不能替代策略及前瞻验证。\n")
    if artifacts.get("integration"):
        integration = _read(artifacts["integration"])
        lines.extend(["", "## 证据与独立增益", "", f"本轮整合：`{integration['outcome']}`。明细：`{artifacts['integration']}`。"])
    if artifacts.get("flow_strategy"):
        strategy = _read(artifacts["flow_strategy"])
        lines.extend(["", "## 历史资金流筛选研究", "", f"状态：`{strategy['status']}`；观察 {strategy['observed_days']} 日，及时截面 {strategy['timely_screen_days']} 日，排除迟到截面 {strategy['late_snapshots_excluded']} 个。",
                      f"往返成本假设 {strategy['round_trip_cost_bps']} bp；缺历史标的 {len(strategy['missing_histories'])} 个。",
                      strategy["interpretation"], f"明细：`{artifacts['flow_strategy']}`。"])
        for horizon, metric in strategy.get("probability_metrics_after_historical_screen", {}).items():
            lines.append(f"历史筛选后 {horizon} 日样本外预测：{metric.get('rows', 0)} 条；Brier {metric.get('brier')}；历史概率基线 {metric.get('historical_rate_brier')}。")
    lines.extend(["", "## 历史候选复盘", ""])
    for step in context.get("steps", []):
        if step["name"].startswith("settle_") and isinstance(step.get("result"), dict):
            result = step["result"]
            waiting = result.get("waiting", [])
            lines.append(f"- {step['name']}：结算 {result.get('settled', 0)} 条，等待 {len(waiting)} 条，其中缺行情 {sum(r['status'] == 'waiting_for_bars' for r in waiting)} 条。")
    lines.extend(["", "A 股个股与 TradingAgents-Astock 延期；第三方真实 ETF 证据、申赎及账户身份数据仍按实际覆盖展示。"])
    path = runs / f"research_report_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
