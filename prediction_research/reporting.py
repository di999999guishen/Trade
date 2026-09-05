from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import resolve_project_path
from .events import recent_events
from .screening import latest_screen
from .store import connect
from .workflow import workflow_status


def _latest(runs: Path, pattern: str) -> tuple[Path | None, dict | None]:
    paths = sorted(runs.glob(pattern), reverse=True)
    if not paths:
        return None, None
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))


def build_research_report(cfg: dict) -> Path:
    runs = resolve_project_path(cfg, cfg["runs_dir"])
    status = workflow_status(cfg)
    screen_path, screen = latest_screen(cfg)
    bt5_path, bt5 = _latest(runs, "backtest_screened_current_*_5d_*.json")
    bt20_path, bt20 = _latest(runs, "backtest_screened_current_*_20d_*.json")
    db_path = resolve_project_path(cfg, cfg["state_db"])
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT symbol,feature_date,horizon,probability_up,probability_status,model_version,evidence_json
            FROM predictions WHERE model_version='screened-flow-price-logistic-v1'
            ORDER BY created_at_utc DESC LIMIT 6"""
        ).fetchall()
    lines = [
        "# ETF 信息获取、粗筛与预测研究报告", "",
        f"生成时间（UTC）：{datetime.now(timezone.utc).isoformat()}", "",
        "## 当前流程位置", "",
        f"当前阶段：{status['current_stage']}；执行路径：`{status['active_path']}`。TradingAgents 按用户要求暂时跳过。", "",
        "## 数据与筛选口径", "",
        f"行情板块快照共 {screen['universe_records']} 条，其中 {screen['eligible_records']} 条通过风险资产、成交额、规模和价格门槛。",
        "资金流字段是成交单大小分类估计，不是 ETF 申购赎回、份额变化或机构账户披露。",
        f"当前已保存 {status['stages'][2]['historical_observation_days']} 个全市场资金流观察日；达到 60 日前，历史回测仅验证候选 ETF 上的量价模型，尚未验证完整的历史资金流轮动策略。", "",
        f"当前有 {status['stages'][2]['open_forward_selections']} 条粗筛前瞻记录等待 5/20 日结算。", "",
        f"粗筛报告：`{screen_path.resolve()}`", "",
        "## 冻结候选", "",
        "| 排名 | 代码 | 名称 | 分组 | 粗筛分 | 主力净流入占比 | 成交额 |", "|---:|---|---|---|---:|---:|---:|",
    ]
    for index, row in enumerate(screen["selected"][:20], 1):
        lines.append(f"| {index} | {row['symbol']} | {row['name']} | {row['screen_group']} | {row['screen_score']:.4f} | {row.get('main_net_inflow_pct', 0):.2f}% | {row.get('amount', 0):.0f} |")
    lines.extend(["", "## 条件量价模型回测", "", "| 周期 | 样本数 | Brier | 历史概率基线 | 准确率 | 发布门 |", "|---|---:|---:|---:|---:|---|"])
    for horizon, payload in ((5, bt5), (20, bt20)):
        if payload:
            metric = payload["metrics"]
            passed = metric["brier"] < metric["historical_rate_brier"]
            lines.append(f"| {horizon}日 | {metric['rows']} | {metric['brier']:.6f} | {metric['historical_rate_brier']:.6f} | {metric['accuracy']:.2%} | {'通过' if passed else '未通过'} |")
    lines.extend(["", "## 冻结预测", "", "| 代码 | 特征日 | 周期 | 上涨概率 | 状态 | 版本 |", "|---|---|---:|---:|---|---|"])
    for symbol, feature_date, horizon, probability, probability_status, model_version, _ in rows:
        lines.append(f"| {symbol} | {feature_date} | {horizon}日 | {probability:.2%} | `{probability_status}` | `{model_version}` |")
    candidate_exposures = {item["exposure"] for item in cfg["universes"].get("screened_current", [])}
    mapped_events = [event for event in recent_events(cfg, 100) if candidate_exposures.intersection(event["exposures"])]
    lines.extend(["", "## 与冻结候选映射的最新事件（只作证据，不改变本次概率）", ""])
    for event in mapped_events[:10]:
        lines.append(f"- {event['title']}｜证据分 {event['evidence_score']:.4f}｜敞口：{', '.join(event['exposures'])}")
    if not mapped_events:
        lines.append(f"- 当前事件库没有与 {', '.join(sorted(candidate_exposures))} 直接映射的合格事件；不使用无关商品新闻补位。")
    lines.extend(["", "## 下一检查点", "", "每日保存新的全市场 ETF 资金流截面；达到足够历史长度后，按当日可得信息重建候选池并验证完整轮动规则。当前预测到 5/20 个交易日后由 `settle` 自动结算。"])
    path = runs / f"research_report_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
