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


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def _cell(value):
    return str(value).replace("|", "／").replace("\n", " ") if value is not None else "未记录"


def _amount(value):
    value = finite(value)
    return "数据不足" if value is None else f"{value / 10000:,.2f} 万元"


def _pct(value):
    return "数据不足" if finite(value) is None else f"{value:.2f}%"


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
             f"本轮：`{context.get('cycle_id', context.get('started_at_utc'))}`；结果：`{context.get('outcome', 'running')}`。",
             f"数据模式：`{context.get('data_mode', 'legacy_not_recorded')}`；决策时间：{context.get('decision_at_utc', '旧版本未记录')}。", "",
             "## 执行与等待", "", "| 步骤 | 状态 |", "|---|---|"]
    lines.extend(f"| {_cell(step['name'])} | {_cell(step['status'])} |" for step in context.get("steps", []))
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
    lines.extend(["", "## 本轮引用的冻结预测", "", "| ETF | 行情日 | 周期 | 上涨概率 | 质量状态 | 冻结时间 |", "|---|---|---:|---:|---|---|"])
    for row in predictions:
        lines.append(f"| {row['symbol']} | {row['feature_date']} | {row['horizon']}日 | {row['probability_up']:.2%} | {row['probability_status']} | {row['frozen_at_utc']} |")
    if not predictions:
        lines.extend(["", "本轮没有可引用的冻结预测；未展示其他运行的预测补位。"])
    lines.extend(["", "## 每只 ETF 预测内的资金流依据", ""])
    for row in predictions:
        flow = row.get("fund_flow", {})
        if not flow:
            old = next((e for e in row.get("evidence", []) if e.get("type") == "money_flow_coarse_screen"), {})
            flow = {**old, "status": "legacy_time_not_verified", "used_for_probability": False}
        lines.extend([f"### {row['symbol']} · {row['horizon']} 日 · {_cell(row.get('name', row['symbol']))}", "",
                      f"预测 ID：`{row['prediction_id']}`；决策时间：{row.get('decision_at_utc', '旧记录未提供')}。",
                      f"可用状态：`{flow.get('status', 'no_data')}`；净流入 {_amount(flow.get('main_net_inflow'))}；占比 {_pct(flow.get('main_net_inflow_pct'))}。",
                      f"观察时间：{flow.get('observed_at_utc', '未记录')}；首次可得：{flow.get('available_at_utc', '未记录')}。",
                      f"资金与价格方向：`{flow.get('price_flow_alignment', 'unknown')}`；资金流对粗筛总分贡献：{flow.get('screen_flow_contribution', '未记录')}。",
                      f"超大单/大单背离：`{flow.get('order_divergence', {}).get('signal', 'no_data')}`；因子 {flow.get('order_divergence', {}).get('factor', '数据不足')}；对粗筛总分贡献：{flow.get('screen_order_divergence_contribution', '数据不足')}。",
                      "参与概率计算：否。金额与占比不是上涨概率。", ""])
        for size in (5, 20):
            window = flow.get("history_windows", {}).get(str(size), {})
            lines.append(f"- {size} 个观察日累计：{_amount(window.get('net_inflow_sum'))}；已覆盖 {window.get('observed_days', 0)} 日。")
        lines.extend(["", "窗口按已观测日期计数，不保证交易日连续；不足窗口或有缺值时不补零。", ""])
    lines.extend(["## 本轮模型验证", "", "| 周期 | 样本 | Brier | 历史概率 Brier | 发布门 |", "|---|---:|---:|---:|---|"])
    gate = cfg["model"].get("validation_gate", {})
    for horizon in cfg["horizons"]:
        backtest = _read(artifacts.get(f"backtest_{horizon}d"))
        if not backtest:
            continue
        metric = backtest["metrics"]
        enough = metric.get("rows", 0) >= gate.get("minimum_rows", 0)
        better = not gate.get("require_brier_below_historical_rate", True) or metric.get("brier", 1) < metric.get("historical_rate_brier", 0)
        lines.append(f"| {horizon}日 | {metric.get('rows', 0)} | {metric.get('brier')} | {metric.get('historical_rate_brier')} | {'通过' if enough and better else '未通过'} |")
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
